"""Sensor gateway entry point."""

import asyncio
import json
import logging
import sys

from src.config import load_config
from src.manager import GatewayManager


class JsonFormatter(logging.Formatter):
    """JSON log formatter producing single-line structured records.

    Output format: ``{"ts": "...", "lvl": "INFO", "msg": "...", "mod": "sensor.device"}``
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON string.

        :param record: The log record to format.
        :returns: JSON-encoded log line.
        """
        log_data = {
            "ts": self.formatTime(record),
            "lvl": record.levelname,
            "msg": record.getMessage(),
            "mod": record.name,
        }
        if record.exc_info and record.exc_info[1] is not None:
            log_data["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


def setup_logging() -> None:
    """Configure root logger with JSON formatter on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> None:
    """Load configuration and start the gateway manager."""
    setup_logging()
    logger = logging.getLogger("sensor.main")

    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as exc:
        logger.critical("Failed to load configuration: %s", exc)
        sys.exit(1)

    logger.info("Starting sensor gateway with %s sensors", len(config.sensors))

    manager = GatewayManager(config)

    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        pass

    logger.info("Gateway shutdown complete")


if __name__ == "__main__":
    main()