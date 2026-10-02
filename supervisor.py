"""Supervisor: launches agents in order, staggers starts, reacts to
idle + thermal state."""
import asyncio
import importlib
import logging
from typing import Any

from rasool.eventbus import EventBus
from rasool.intel_feed import IntelFeedSink
from rasool.registry import Registry
from rasool.safety import Safety
from rasool.service import Service

from idle import IdleDetector
from thermal import ThermalMode, ThermalMonitor

logger = logging.getLogger("supervisor")


AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "sniff_agent": ("agents.sniff_agent.agent", "SniffAgent"),
    "github_agent": ("agents.github_agent.agent", "GitHubAgent"),
    "router_agent": ("agents.router_agent.agent", "RouterAgent"),
}


class Supervisor:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.bus = EventBus()
        self.safety = Safety(
            set(config.get("safety", {}).get("allowlist", []) or [])
        )
        self.registry = Registry()
        self.intel_feed_sink = IntelFeedSink(
            self.bus, config.get("intel_feed", {})
        )
        self.registry.register(self.intel_feed_sink)

        runtime = config.get("runtime", {})
        self.enabled: list[str] = list(runtime.get("enabled_agents", []))
        self.start_stagger_s = float(runtime.get("start_stagger_s", 10))
        self.idle_stagger_s = float(runtime.get("idle_stagger_s", 30))
        self.stop_stagger_s = float(runtime.get("stop_stagger_s", 2))

        self.thermal = ThermalMonitor(config.get("thermal", {}))
        self.idle = IdleDetector(self._on_idle_change)

        self._agents: dict[str, Service] = {}
        self._started: set[str] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self._thermal_mode: ThermalMode = ThermalMode.NORMAL
        self._is_idle: bool | None = None
        self._thermal_recovery_task: asyncio.Task[None] | None = None

    def _instantiate(self, name: str) -> Service:
        if name in self._agents:
            return self._agents[name]
        if name not in AGENT_REGISTRY:
            raise KeyError(f"Unknown agent: {name}")
        mod_path, cls_name = AGENT_REGISTRY[name]
        module = importlib.import_module(mod_path)
        cls = getattr(module, cls_name)
        agent = cls(self.bus, safety=self.safety)
        self._agents[name] = agent
        self.registry.register(agent)
        return agent

    async def _start_one(self, name: str) -> bool:
        if name in self._started:
            return True
        try:
            agent = self._instantiate(name)
            await agent.start()
            if self._is_idle is not None:
                await self.bus.publish_sync(
                    "system.idle", {"idle": self._is_idle}
                )
            self._started.add(name)
            logger.info("started %s (priority=%s)", name, agent.priority)
            return True
        except Exception:
            logger.exception("failed to start %s", name)
            return False

    async def _stop_one(self, name: str) -> None:
        if name not in self._started:
            return
        try:
            agent = self._agents[name]
            await agent.stop()
            self._started.discard(name)
            logger.info("stopped %s", name)
        except Exception:
            logger.exception("failed to stop %s", name)

    async def _start_all_sequential(self, stagger_s: float) -> None:
        for name in self.enabled:
            if name in self._started:
                continue
            await self._start_one(name)
            await asyncio.sleep(stagger_s)

    async def _stop_by_priority(self, levels: set[str]) -> None:
        for name in list(self._started):
            agent = self._agents.get(name)
            if agent is None:
                continue
            if agent.priority in levels:
                await self._stop_one(name)
                await asyncio.sleep(self.stop_stagger_s)

    async def _on_thermal_change(
        self, old: ThermalMode, new: ThermalMode, reading: Any
    ) -> None:
        logger.warning(
            "thermal: %s -> %s (max=%.1fC)",
            old.value, new.value, reading.max_c,
        )
        self._thermal_mode = new
        if new is not ThermalMode.NORMAL:
            recovery_task = self._thermal_recovery_task
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
            self._thermal_recovery_task = None
        if new is ThermalMode.WARN:
            pass
        elif new is ThermalMode.THROTTLE:
            await self._stop_by_priority({"low"})
        elif new is ThermalMode.SUSPEND:
            await self._stop_by_priority({"low", "normal"})
        elif new is ThermalMode.EMERGENCY:
            await self._stop_by_priority({"low", "normal", "critical"})
        elif new is ThermalMode.NORMAL:
            self._thermal_recovery_task = asyncio.create_task(
                self._restart_low_after_stable_normal()
            )

    async def _on_idle_change(self, is_idle: bool) -> None:
        self._is_idle = is_idle
        logger.info("idle: %s", "yes" if is_idle else "no")
        await self.bus.publish_sync("system.idle", {"idle": is_idle})
        if is_idle:
            if self._thermal_mode in (ThermalMode.NORMAL, ThermalMode.WARN):
                await self._start_all_sequential(self.idle_stagger_s)
        else:
            await self._stop_by_priority({"normal"})

    async def _restart_low_after_stable_normal(self) -> None:
        try:
            await asyncio.sleep(60)
            if self._thermal_mode is not ThermalMode.NORMAL:
                return
            for name in self.enabled:
                agent = self._agents.get(name)
                if agent is None:
                    try:
                        agent = self._instantiate(name)
                    except Exception:
                        logger.exception("failed to load %s for recovery", name)
                        continue
                if agent.priority == "low":
                    await self._start_one(name)
                    await asyncio.sleep(self.idle_stagger_s)
        except asyncio.CancelledError:
            raise
        finally:
            if self._thermal_recovery_task is asyncio.current_task():
                self._thermal_recovery_task = None

    async def run(self) -> None:
        await self.intel_feed_sink.start()
        self._tasks = [
            asyncio.create_task(self.thermal.run(self._on_thermal_change)),
            asyncio.create_task(self.idle.run()),
        ]
        try:
            await self._start_all_sequential(self.start_stagger_s)
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("supervisor stopping")
            recovery_task = self._thermal_recovery_task
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
                await asyncio.gather(recovery_task, return_exceptions=True)
            for name in list(self._started):
                await self._stop_one(name)
            raise
        finally:
            await self.intel_feed_sink.stop()
