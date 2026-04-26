"""Sensor gateway entry point.

Configures JSON structured logging, loads ``config.json``, and hands control
to :class:`~src.manager.GatewayManager`.
"""

import asyncio
import json
import logging
import sys

from src.config import load_config
from src.manager import GatewayManager


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        """Format *record* as JSON.

        :param record: The log record to format.
        :returns: A JSON string with ``timestamp``, ``level``, ``logger``,
            and ``message`` keys.
        :rtype: str
        """
        entry: dict[str, str] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging() -> None:
    """Wire every logger to emit JSON on *stdout*.

    Third-party loggers (``modbus_tk``, ``aiomqtt``) are quieted to WARNING
    so that only application messages appear at INFO level.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    for noisy in ("modbus_tk", "aiomqtt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> None:
    """Load configuration and run the gateway until terminated."""
    setup_logging()
    logger = logging.getLogger("sensor.main")

    try:
        config = load_config()
    except (FileNotFoundError, KeyError, ValueError):
        logger.exception("Failed to load configuration")
        sys.exit(1)

    logger.info("Starting sensor gateway")
    manager = GatewayManager(config)
    await manager.run()


if __name__ == "__main__":
    asyncio.run(main())
