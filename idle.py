"""Idle detector using systemd-logind IdleHint via loginctl."""
import asyncio
import logging
import os
import subprocess
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class IdleDetector:
    def __init__(
        self,
        on_change: Callable[[bool], Awaitable[None]],
        poll_interval_s: float = 30.0,
    ) -> None:
        self._on_change = on_change
        self._poll_interval_s = poll_interval_s
        self._is_idle: bool | None = None
        self._uid = os.getuid()

    def _query_loginctl(self) -> bool | None:
        try:
            result = subprocess.run(
                ["loginctl", "show-user", str(self._uid), "-p", "IdleHint"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        line = result.stdout.strip()
        if line == "IdleHint=yes":
            return True
        if line == "IdleHint=no":
            return False
        return None

    async def run(self) -> None:
        while True:
            state = await asyncio.to_thread(self._query_loginctl)
            if state is not None and state != self._is_idle:
                self._is_idle = state
                try:
                    await self._on_change(state)
                except Exception:
                    logger.exception("idle: on_change failed")
            await asyncio.sleep(self._poll_interval_s)
