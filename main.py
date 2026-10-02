"""Rasool entrypoint."""
import argparse
import asyncio
import logging
import logging.handlers
import sys
import tomllib
from pathlib import Path

from supervisor import Supervisor


def _setup_logging(level: str, logfile: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(prog="rasool")
    parser.add_argument(
        "--config", type=Path,
        default=Path(__file__).with_name("config.toml"),
    )
    parser.add_argument(
        "--log-level", default="info",
        choices=["debug", "info", "warning", "error"],
    )
    parser.add_argument(
        "--log-file", type=Path,
        default=Path.home() / ".cache" / "rasool" / "rasool.log",
    )
    args = parser.parse_args()
    _setup_logging(args.log_level, args.log_file)
    config = _load_config(args.config)
    supervisor = Supervisor(config)
    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
