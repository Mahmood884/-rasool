import asyncio
import errno
import logging
import socket
import sys
import tomllib
from pathlib import Path
from typing import Any

from rasool.eventbus import EventBus
from rasool.safety import Safety
from rasool.service import Service

logger = logging.getLogger(__name__)


class SniffAgent(Service):
    name = "sniff_agent"
    priority = "critical"

    def __init__(
        self,
        bus: EventBus,
        config: dict[str, Any] | None = None,
        safety: Safety | None = None,
    ) -> None:
        resolved_config = config if config is not None else self._load_config()
        super().__init__(bus, resolved_config)
        self.iface = self.config.get("iface", "enp0s31f6")
        self.bpf = self.config.get("bpf", "arp or udp port 53")
        self.publish_arp = self.config.get("publish_arp", True)
        self.publish_dns = self.config.get("publish_dns", True)
        if not isinstance(self.iface, str) or not self.iface:
            raise ValueError("config 'iface' must be a non-empty string")
        if not isinstance(self.bpf, str) or not self.bpf:
            raise ValueError("config 'bpf' must be a non-empty string")
        if not isinstance(self.publish_arp, bool):
            raise ValueError("config 'publish_arp' must be a boolean")
        if not isinstance(self.publish_dns, bool):
            raise ValueError("config 'publish_dns' must be a boolean")
        self.safety = safety or Safety()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._sniffer: Any | None = None
        self._arp_layer: Any | None = None
        self._dns_layer: Any | None = None
        self._ip_layer: Any | None = None
        self._ipv6_layer: Any | None = None

    @staticmethod
    def _load_config() -> dict[str, Any]:
        config_path = Path(__file__).with_name("config.toml")
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
        if not isinstance(config, dict):
            raise ValueError(f"Agent config must be a TOML table: {config_path}")
        return config

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._health_task = asyncio.create_task(self._supervise())
        self._task = asyncio.create_task(self._capture_loop())
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._running = False
        capture_task = self._task
        if capture_task is not None and not capture_task.done():
            capture_task.cancel()
            try:
                await capture_task
            except asyncio.CancelledError:
                pass
        self._task = None
        health_task = self._health_task
        if health_task is not None and not health_task.done():
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass
        self._health_task = None

    async def _capture_loop(self) -> None:
        try:
            try:
                from scapy.all import ARP, DNS, IP, IPv6, AsyncSniffer
            except ImportError as exc:
                await self._emit_error(exc, "scapy_import")
                return

            self._arp_layer = ARP
            self._dns_layer = DNS
            self._ip_layer = IP
            self._ipv6_layer = IPv6

            while self._running:
                sniffer: Any | None = None
                try:
                    try:
                        socket.if_nametoindex(self.iface)
                    except OSError as exc:
                        await self._emit_error(exc, "interface")
                        await asyncio.sleep(30)
                        continue

                    sniffer = AsyncSniffer(
                        iface=self.iface,
                        filter=self.bpf,
                        prn=self._handle_packet,
                        store=False,
                    )
                    self._sniffer = sniffer
                    await asyncio.to_thread(sniffer.start)

                    while self._running and getattr(sniffer, "running", True):
                        await asyncio.sleep(0.5)

                    if self._running:
                        await self._emit_error(
                            RuntimeError("Scapy sniffer stopped unexpectedly"),
                            "capture",
                        )
                        await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._emit_error(exc, "capture")
                    await asyncio.sleep(
                        60 if self._is_permission_error(exc) else 30
                    )
                finally:
                    if sniffer is not None:
                        await self._stop_sniffer(sniffer)
                    self._sniffer = None
        finally:
            self._running = False
            health_task = self._health_task
            if (
                health_task is not None
                and health_task is not asyncio.current_task()
                and not health_task.done()
            ):
                health_task.cancel()
                try:
                    await health_task
                except asyncio.CancelledError:
                    pass

    async def _stop_sniffer(self, sniffer: Any) -> None:
        if not getattr(sniffer, "running", False):
            return
        try:
            await asyncio.to_thread(sniffer.stop)
        except Exception as exc:
            await self._emit_error(exc, "capture_stop")

    @staticmethod
    def _is_permission_error(exc: Exception) -> bool:
        if isinstance(exc, PermissionError):
            return True
        if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
            return True
        message = str(exc).lower()
        return "permission denied" in message or "operation not permitted" in message

    def _handle_packet(self, packet: Any) -> None:
        try:
            if self.publish_arp and self._arp_layer is not None and packet.haslayer(
                self._arp_layer
            ):
                arp = packet[self._arp_layer]
                payload = {
                    "op": int(arp.op),
                    "psrc": str(arp.psrc),
                    "pdst": str(arp.pdst),
                    "hwsrc": str(arp.hwsrc),
                    "hwdst": str(arp.hwdst),
                }
                self._schedule_publish("net.arp", payload)

            if self.publish_dns and self._dns_layer is not None and packet.haslayer(
                self._dns_layer
            ):
                dns = packet[self._dns_layer]
                question = dns.qd
                raw_name = getattr(question, "qname", None) if question else None
                if isinstance(raw_name, bytes):
                    qname = raw_name.decode("utf-8", errors="replace")
                elif raw_name is None:
                    qname = None
                else:
                    qname = str(raw_name)
                qtype_value = getattr(question, "qtype", None) if question else None
                qtype = int(qtype_value) if qtype_value is not None else None

                source: str | None = None
                if self._ip_layer is not None and packet.haslayer(self._ip_layer):
                    source = str(packet[self._ip_layer].src)
                elif self._ipv6_layer is not None and packet.haslayer(
                    self._ipv6_layer
                ):
                    source = str(packet[self._ipv6_layer].src)
                if source is not None:
                    self._schedule_publish(
                        "net.dns",
                        {"qname": qname, "qtype": qtype, "src": source},
                    )
        except Exception as exc:
            self._schedule_error(exc, "packet")

    def _schedule_publish(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            if not self.safety.check(topic, payload):
                return
            loop = self._loop
            if loop is None or loop.is_closed():
                raise RuntimeError("event loop is not available")
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._publish(topic, payload))
            )
        except Exception as exc:
            self._schedule_error(exc, "event_publish")

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            await self.bus.publish(topic, payload)
        except Exception as exc:
            await self._emit_error(exc, "event_publish")

    def _schedule_error(self, exc: Exception, phase: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.error(
                "%s: %s: %s", self.name, type(exc).__name__, exc, exc_info=True
            )
            print(f"{self.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        try:
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._emit_error(exc, phase))
            )
        except RuntimeError:
            logger.exception("Could not schedule %s error event", self.name)

    async def _emit_error(self, exc: Exception, phase: str) -> None:
        message = f"{self.name} [{phase}]: {type(exc).__name__}: {exc}"
        logger.error(message)
        print(message, file=sys.stderr)
        try:
            await self.bus.publish_sync(
                "agent.error",
                {
                    "name": self.name,
                    "phase": phase,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
            )
        except Exception:
            logger.exception("Could not publish %s error event", self.name)
