from rasool.service import Service


class Registry:
    def __init__(self) -> None:
        self._services: dict[str, Service] = {}

    def register(self, svc: Service) -> None:
        self._services[svc.name] = svc

    def get(self, name: str) -> Service:
        return self._services[name]

    def all(self) -> list[Service]:
        return list(self._services.values())
