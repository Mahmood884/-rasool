from typing import Any


class Safety:
    """User-controlled gate. Only explicitly allowlisted actions are permitted."""

    def __init__(self, allowlist: set[str] | None = None) -> None:
        self.allowlist = set(allowlist or ())

    def check(self, action: str, payload: Any) -> bool:
        return action in self.allowlist
