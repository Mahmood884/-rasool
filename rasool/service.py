import asyncio
from abc import ABC, abstractmethod
from typing import Any

from rasool.eventbus import EventBus


class Service(ABC):
    name: str = "unnamed"

    def __init__(
        self, bus: EventBus, config: dict[str, Any] | None = None
    ) -> None:
        self.bus = bus
        self.config = config or {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    async def health(self) -> dict[str, Any]:
        return {"name": self.name, "running": self._running}

    async def _supervise(self) -> None:
        self._running = True
        await self.bus.publish(
            "agent.health", {"name": self.name, "state": "up"}
        )
        try:
            while self._running:
                await asyncio.sleep(3600)
        finally:
            self._running = False
            await self.bus.publish(
                "agent.health", {"name": self.name, "state": "down"}
            )
