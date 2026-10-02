import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import stat
import sys
import time
import tomllib
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from rasool.eventbus import EventBus
from rasool.safety import Safety
from rasool.service import Service

logger = logging.getLogger(__name__)

_MAC_RE = re.compile(
    r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"
)
_MAC_LIKE_RE = re.compile(r"(?i)\b[0-9a-f:-]{11,20}\b")
_UPTIME_RE = re.compile(
    r"(?i)(\d+)\s*days?\s*,?\s*(\d{1,2}):(\d{2}):(\d{2})"
)
_UPTIME_SECONDS_RE = re.compile(r"(?i)uptime\D{0,12}(\d+)\s*(?:s|sec|seconds)?")
_REJECTED_MARKERS = (
    "incorrect username",
    "incorrect password",
    "invalid password",
    "login failed",
    "authentication failed",
    "用户名或密码错误",
    "密码错误",
)
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[list[str]]] = []
        self.current_row: list[list[str]] | None = None
        self.current_cell: list[str] | None = None
        self.in_table = 0
        self.text_parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "table":
            self.in_table += 1
        elif tag == "tr" and self.in_table:
            self.current_row = []
        elif tag in {"td", "th"} and self.in_table and self.current_row is not None:
            self.current_cell = []
        elif tag == "input" and self.current_cell is not None:
            value = dict(attrs).get("value")
            if value:
                self.current_cell.append(value)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "input" and self.current_cell is not None:
            value = dict(attrs).get("value")
            if value:
                self.current_cell.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.current_cell is not None:
            text = re.sub(r"\s+", " ", " ".join(self.current_cell)).strip()
            if self.current_row is not None:
                self.current_row.append(text)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "table" and self.in_table:
            self.in_table -= 1

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.text_parts.append(text)
            if self.current_cell is not None:
                self.current_cell.append(text)


class RouterAgent(Service):
    name = "router_agent"
    priority = "normal"

    def __init__(
        self,
        bus: EventBus,
        config: dict[str, Any] | None = None,
        safety: Safety | None = None,
    ) -> None:
        self._config = config if config is not None else self._load_config()
        super().__init__(bus, self._config)
        self.safety = safety or Safety()
        self.router = self._config.get("router", {})
        if "password" in self.router:
            raise ValueError(
                "Inline router passwords are not supported; configure password_file"
            )
        password_file = Path(
            str(
                Path(
                    self.router.get(
                        "password_file",
                        "~/.config/rasool/router_agent/password",
                    )
                ).expanduser()
            )
        )
        self.router["password"] = self._read_password_file(password_file)
        self.endpoints = self._config.get("endpoints", {})
        self.schedule = self._config.get("schedule", {})
        self.known_seed = {
            str(mac).lower().replace("-", ":"): str(label)
            for mac, label in self._config.get("known", {}).items()
        }
        self.camera_ips = {
            str(ip) for ip in self._config.get("cameras", {}).get("ips", [])
        }
        self.cache_dir = Path(
            str(
                Path(
                    self._config.get(
                        "storage", {}
                    ).get(
                        "cache_dir", "~/.cache/rasool/router_agent"
                    )
                ).expanduser()
            )
        )
        self.devices_path = self.cache_dir / "known_devices.json"
        self._devices: dict[str, dict[str, Any]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._session_up: bool | None = None
        self._session_started: float | None = None
        self._authenticated = False
        self._device_lock = asyncio.Lock()

    @staticmethod
    def _load_config() -> dict[str, Any]:
        path = Path(__file__).with_name("config.toml")
        with path.open("rb") as config_file:
            return tomllib.load(config_file)

    @staticmethod
    def _read_password_file(path: Path) -> str:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"Could not read router password file {path}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Router password path is not a regular file: {path}")
        if metadata.st_uid != os.getuid():
            raise PermissionError(
                f"Router password file must be owned by the current user: {path}"
            )
        if metadata.st_mode & 0o077:
            raise PermissionError(
                f"Router password file must not be accessible by group or others: {path}"
            )
        try:
            password = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError(
                f"Could not read router password file {path}: {exc}"
            ) from exc
        if not password:
            raise ValueError(f"Router password file is empty: {path}")
        return password

    async def start(self) -> None:
        if self._running:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        await self._load_devices()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._supervise()),
            asyncio.create_task(self._poll_loop()),
        ]
        self._task = self._tasks[1]

    async def stop(self) -> None:
        self._running = False
        self._authenticated = False
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _poll_loop(self) -> None:
        try:
            import httpx
        except ImportError as exc:
            await self._emit_error(exc, "httpx_import")
            while self._running:
                await asyncio.sleep(60)
                try:
                    import httpx  # noqa: F401
                except ImportError as retry_exc:
                    await self._emit_error(retry_exc, "httpx_import")
                else:
                    break
        if not self._running:
            return

        timeout = float(self.router.get("timeout_s", 20))
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "RasoolRouterAgent/0.1.0"},
        ) as client:
            while self._running:
                try:
                    delay = await self._poll_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._emit_error(exc, "poll")
                    delay = 30 if self._is_connection_error(exc) else 60
                await asyncio.sleep(delay)

    async def _poll_once(self, client: Any) -> int:
        if not self._authenticated:
            if not await self._login(client, silent=False):
                await self._set_session(False)
                return 60

        clients_url = self._url(str(self.endpoints.get("clients", "")))
        clients_response = await client.get(clients_url)
        if self._is_login_response(clients_response):
            self._authenticated = False
            if not await self._login(client, silent=True):
                return 60
            clients_response = await client.get(clients_url)
            if self._is_login_response(clients_response):
                self._authenticated = False
                return 60
        clients_response.raise_for_status()

        status_url = self._url(str(self.endpoints.get("status", "")))
        status_response = await client.get(status_url)
        if self._is_login_response(status_response):
            self._authenticated = False
            if not await self._login(client, silent=True):
                return 60
            clients_response = await client.get(clients_url)
            if self._is_login_response(clients_response):
                self._authenticated = False
                return 60
            clients_response.raise_for_status()
            status_response = await client.get(status_url)
            if self._is_login_response(status_response):
                self._authenticated = False
                return 60
        status_response.raise_for_status()

        rows = self._parse_clients(clients_response.text)
        uptime_s = self._router_uptime(status_response.text)
        if uptime_s is None:
            uptime_s = (
                max(0, int(time.monotonic() - self._session_started))
                if self._session_started is not None
                else 0
            )
        await self._set_session(True, uptime_s)
        await self._publish_clients(rows)
        await self._persist_devices()
        return max(1, int(self.schedule.get("poll_interval_s", 300)))

    async def _login(self, client: Any, silent: bool) -> bool:
        base_url = str(self.router.get("base_url", "http://192.168.100.1"))
        login_path = str(self.endpoints.get("login", "/login.cgi"))
        username_field = str(
            self.endpoints.get("username_field", "UserName")
        )
        password_field = str(
            self.endpoints.get("password_field", "PassWord")
        )
        token_path = str(
            self.endpoints.get("random_count", "/asp/GetRandCount.asp")
        )
        token_response = await client.get(self._url(token_path))
        token_response.raise_for_status()
        token = token_response.text.strip()
        if not token:
            raise ValueError("Router returned an empty login token")
        data = {
            username_field: str(self.router.get("username", "")),
            password_field: base64.b64encode(
                str(self.router.get("password", "")).encode("utf-8")
            ).decode("ascii"),
        }
        extra_fields = self.router.get("extra_fields", {})
        if isinstance(extra_fields, dict):
            data.update(
                {str(key): str(value) for key, value in extra_fields.items()}
            )
        language = str(data.get("Language", "english"))
        client.cookies.set(
            "Cookie", f"body:Language:{language}:id=-1", path="/"
        )
        data[str(self.router.get("token_field", "x.X_HW_Token"))] = token
        response = await client.post(
            urljoin(base_url.rstrip("/") + "/", login_path.lstrip("/")),
            data=data,
        )
        if response.status_code in (401, 403) or self._login_rejected(response):
            self._authenticated = False
            if not silent:
                await self._emit_error(
                    RuntimeError(
                        f"Router login rejected (HTTP {response.status_code})"
                    ),
                    "login",
                )
            return False
        if response.is_error:
            response.raise_for_status()
        if self._contains_login_form(response) and response.status_code < 300:
            self._authenticated = False
            if not silent:
                await self._emit_error(
                    RuntimeError("Router login returned the login page"),
                    "login",
                )
            return False
        self._authenticated = True
        self._session_started = time.monotonic()
        return True

    async def _publish_clients(self, rows: list[dict[str, str]]) -> None:
        now = self._now()
        for row in rows:
            try:
                mac = self._normalize_mac(row["mac"])
                ip = row["ip"]
                hostname = row.get("hostname", "") or ""
                iface = row.get("iface", "") or "unknown"
                previous = self._devices.get(mac)
                seeded_label = self.known_seed.get(mac)
                camera = ip in self.camera_ips
                is_known = seeded_label is not None or previous is not None or camera
                first_seen = previous.get("first_seen") if previous else None
                label = seeded_label or (
                    str(previous.get("label", "")) if previous else ""
                )
                if camera and not label:
                    label = "hikvision-camera"
                if not label:
                    label = hostname or "unknown"
                record = {
                    "label": label,
                    "first_seen": first_seen or now,
                    "last_seen": now,
                }
                client_event = {
                    "mac": mac,
                    "ip": ip,
                    "hostname": hostname,
                    "iface": iface,
                    "first_seen": first_seen or now,
                    "last_seen": now,
                }
                if not await self._publish("router.client", client_event):
                    continue
                if not is_known:
                    if not await self._publish(
                        "router.unknown",
                        {"mac": mac, "ip": ip, "hostname": hostname},
                    ):
                        continue
                self._devices[mac] = record
            except (KeyError, ValueError) as exc:
                logger.exception("Skipping malformed router client row: %s", row)
                await self._emit_error(
                    exc, "client_row", row=row
                )

    async def _load_devices(self) -> None:
        async with self._device_lock:
            try:
                content = await asyncio.to_thread(
                    self.devices_path.read_text, encoding="utf-8"
                )
            except FileNotFoundError:
                self._devices = {}
            except OSError as exc:
                await self._emit_error(exc, "known_devices_read")
                self._devices = {}
            else:
                try:
                    payload = json.loads(content)
                    if not isinstance(payload, dict):
                        raise ValueError(
                            "known_devices.json must contain a JSON object"
                        )
                    self._devices = {
                        self._normalize_mac(str(mac)): value
                        for mac, value in payload.items()
                        if isinstance(value, dict)
                    }
                except (json.JSONDecodeError, ValueError) as exc:
                    await self._emit_error(exc, "known_devices_read")
                    self._devices = {}
            for mac, label in self.known_seed.items():
                self._devices.setdefault(
                    mac,
                    {"label": label, "first_seen": None, "last_seen": None},
                )

    async def _persist_devices(self) -> None:
        async with self._device_lock:
            serialized = json.dumps(
                self._devices, ensure_ascii=False, indent=2, sort_keys=True
            )
            await asyncio.to_thread(
                self._atomic_write, self.devices_path, serialized
            )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    async def _set_session(self, up: bool, uptime_s: int = 0) -> None:
        if self._session_up is up:
            return
        published = await self._publish(
            "router.session", {"up": up, "uptime_s": max(0, uptime_s)}
        )
        if published:
            self._session_up = up
            if up:
                self._session_started = time.monotonic()

    async def _publish(self, topic: str, payload: dict[str, Any]) -> bool:
        if not self.safety.check(topic, payload):
            return False
        await self.bus.publish_sync(topic, payload)
        return True

    async def _emit_error(
        self, exc: Exception, phase: str, **details: Any
    ) -> None:
        message = f"{self.name} [{phase}]: {type(exc).__name__}: {exc}"
        logger.error(message)
        print(message, file=sys.stderr)
        payload: dict[str, Any] = {
            "name": self.name,
            "phase": phase,
            "error": type(exc).__name__,
            "message": str(exc),
        }
        payload.update(details)
        try:
            if self.safety.check("agent.error", payload):
                await self.bus.publish_sync("agent.error", payload)
        except Exception:
            logger.exception("Could not publish %s error event", self.name)

    def _parse_clients(self, document: str) -> list[dict[str, str]]:
        parser = _TableParser()
        parser.feed(document)
        parser.close()
        client_rows: list[dict[str, str]] = []
        for cells in parser.rows:
            joined = " ".join(cells)
            mac_match = _MAC_RE.search(joined)
            if mac_match is None:
                malformed_mac = _MAC_LIKE_RE.search(joined)
                if malformed_mac:
                    logger.error(
                        "Skipping malformed router MAC address in row: %s",
                        joined[:240],
                    )
                continue
            try:
                mac = self._normalize_mac(mac_match.group(0))
            except ValueError as exc:
                logger.error(
                    "Skipping malformed router MAC address in row %s: %s",
                    joined[:240],
                    exc,
                )
                continue
            ip_value = ""
            ip_match_value = ""
            for cell in cells:
                candidate = re.sub(r"(?i)^(?:ip|ipv4)\s*[:=]\s*", "", cell.strip())
                candidate = candidate.strip("[]() ,;")
                try:
                    parsed = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                ip_value = candidate
                ip_match_value = str(parsed)
                break
            if not ip_value:
                logger.error(
                    "Skipping router client row without a valid IP: %s",
                    joined[:240],
                )
                continue
            ip_value = ip_match_value
            hostname = ""
            iface = ""
            for cell in cells:
                value = cell.strip()
                if not value or _MAC_RE.search(value):
                    continue
                try:
                    ipaddress.ip_address(value.strip("[]() ,;"))
                    continue
                except ValueError:
                    pass
                normalized = re.sub(r"[^a-z0-9]", "", value.lower())
                if any(
                    marker in normalized
                    for marker in ("wlan", "wifi", "wireless", "ethernet", "lan")
                ):
                    iface = value
                elif value.casefold() not in {
                    "mac", "mac address", "ip", "ip address",
                    "hostname", "host name", "interface", "type",
                    "connected", "online",
                }:
                    hostname = value
            client_rows.append(
                {"mac": mac, "ip": ip_value, "hostname": hostname, "iface": iface}
            )
        if not parser.rows:
            snippet = re.sub(r"\s+", " ", document)[:500]
            raise ValueError(
                f"Router response contained no HTML table; response snippet: {snippet}"
            )
        if not client_rows:
            snippet = re.sub(r"\s+", " ", document)[:500]
            raise ValueError(
                f"No valid client rows found in router table; response snippet: {snippet}"
            )
        return client_rows

    @staticmethod
    def _router_uptime(document: str) -> int | None:
        parser = _TableParser()
        parser.feed(document)
        text = " ".join(parser.text_parts)
        duration = _UPTIME_RE.search(text)
        if duration:
            days, hours, minutes, seconds = map(int, duration.groups())
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
        seconds = _UPTIME_SECONDS_RE.search(text)
        if seconds:
            return int(seconds.group(1))
        return None

    def _url(self, endpoint: str) -> str:
        return urljoin(
            str(self.router.get("base_url", "http://192.168.100.1")).rstrip("/")
            + "/",
            endpoint.lstrip("/"),
        )

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        normalized = mac.lower().replace("-", ":")
        if not _MAC_RE.fullmatch(normalized):
            raise ValueError(f"Invalid MAC address: {mac!r}")
        return normalized

    @staticmethod
    def _login_rejected(response: Any) -> bool:
        text = response.text.lower()
        return any(marker in text for marker in _REJECTED_MARKERS)

    @staticmethod
    def _is_login_response(response: Any) -> bool:
        if response.status_code in (401, 403):
            return True
        path = urlparse(str(response.url)).path.lower()
        if "login" in path:
            return True
        text = response.text.lower()
        return bool(
            re.search(r'<input[^>]+type=["\']password["\']', text)
            and ("login" in text or "username" in text or "password" in text)
        ) or (
            "<title>waiting...</title>" in text
            and "top.location.replace" in text
        )

    @staticmethod
    def _contains_login_form(response: Any) -> bool:
        text = response.text.lower()
        return bool(
            "txt_username" in text
            and "txt_password" in text
            and re.search(r'<input[^>]+type=["\']password["\']', text)
        )

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        return (
            isinstance(exc, (ConnectionError, TimeoutError, OSError))
            or "connecterror" in type(exc).__name__.lower()
            or "connecttimeout" in type(exc).__name__.lower()
            or "connection refused" in str(exc).lower()
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
