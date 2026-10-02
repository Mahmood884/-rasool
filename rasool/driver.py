from abc import ABC, abstractmethod
from typing import Any


class Driver(ABC):
    name: str = "unnamed"

    @abstractmethod
    async def open(self) -> None:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    @abstractmethod
    async def read(self) -> Any:
        ...
