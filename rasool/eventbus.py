import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable

Handler = Callable[[str, Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    async def publish(self, topic: str, payload: Any) -> None:
        for handler in self._subs.get(topic, []):
            asyncio.create_task(handler(topic, payload))

    async def publish_sync(self, topic: str, payload: Any) -> None:
        """Await all handlers when ordering matters."""
        await asyncio.gather(
            *(handler(topic, payload) for handler in self._subs.get(topic, []))
        )
