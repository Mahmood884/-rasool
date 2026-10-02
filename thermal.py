"""Thermal monitor. Reads coretemp from /sys/class/thermal, tracks
max and average across cores, and exposes a mode state machine."""
import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CPU_ZONE_HINTS = ("x86_pkg_temp", "coretemp", "cpu-thermal")


class ThermalMode(str, Enum):
    NORMAL = "normal"
    WARN = "warn"
    THROTTLE = "throttle"
    SUSPEND = "suspend"
    EMERGENCY = "emergency"


@dataclass
class ThermalReading:
    max_c: float
    per_core: dict[str, float]
    source: str


class ThermalMonitor:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.warn_below_throttle = float(cfg.get("warn_below_throttle", 70))
        self.throttle_at = float(cfg.get("throttle_at", 78))
        self.suspend_at = float(cfg.get("suspend_at", 85))
        self.emergency_at = float(cfg.get("emergency_at", 92))
        self.recover_below = float(cfg.get("recover_below", 60))
        self.poll_interval_s = float(cfg.get("poll_interval_s", 5))
        self._mode = ThermalMode.NORMAL
        self._last: ThermalReading | None = None
        self._cpu_zones: list[tuple[str, Path]] = []
        self._discover_zones()

    def _discover_zones(self) -> None:
        base = Path("/sys/class/thermal")
        if not base.is_dir():
            logger.warning("thermal: %s not found", base)
            return
        for zone in sorted(base.glob("thermal_zone*")):
            try:
                ztype = (zone / "type").read_text().strip()
            except OSError:
                continue
            if any(hint in ztype for hint in _CPU_ZONE_HINTS):
                self._cpu_zones.append((ztype, zone / "temp"))
        if not self._cpu_zones:
            logger.warning("thermal: no CPU zones found")

    def read(self) -> ThermalReading | None:
        if not self._cpu_zones:
            return None
        per_core: dict[str, float] = {}
        for ztype, temp_path in self._cpu_zones:
            try:
                raw = int(temp_path.read_text().strip())
            except (OSError, ValueError):
                continue
            per_core[ztype] = raw / 1000.0
        if not per_core:
            return None
        self._last = ThermalReading(
            max_c=max(per_core.values()),
            per_core=per_core,
            source="coretemp",
        )
        return self._last

    def _compute_mode(self, temp_c: float) -> ThermalMode:
        if self._mode is ThermalMode.EMERGENCY:
            if temp_c < self.recover_below:
                return ThermalMode.SUSPEND
            return ThermalMode.EMERGENCY
        if temp_c >= self.emergency_at:
            return ThermalMode.EMERGENCY
        if temp_c >= self.suspend_at:
            return ThermalMode.SUSPEND
        if temp_c >= self.throttle_at:
            return ThermalMode.THROTTLE
        if temp_c >= self.warn_below_throttle:
            return ThermalMode.WARN
        return ThermalMode.NORMAL

    @property
    def mode(self) -> ThermalMode:
        return self._mode

    @property
    def last(self) -> ThermalReading | None:
        return self._last

    async def run(self, on_change: Any) -> None:
        while True:
            reading = self.read()
            if reading is not None:
                new_mode = self._compute_mode(reading.max_c)
                if new_mode is not self._mode:
                    old = self._mode
                    self._mode = new_mode
                    try:
                        await on_change(old, new_mode, reading)
                    except Exception:
                        logger.exception("thermal: on_change failed")
            await asyncio.sleep(self.poll_interval_s)
