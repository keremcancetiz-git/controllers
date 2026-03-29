"""Entry point for the relay controller.

Loads configuration, creates the :class:`~src.manager.RelayManager`, and
runs until interrupted by ``SIGINT`` or ``SIGTERM``.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from src.config import load_config
from src.manager import RelayManager

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"

logger = logging.getLogger("relay.main")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    :returns: Parsed arguments with ``config`` attribute.
    :rtype: argparse.Namespace
    """
    parser = argparse.ArgumentParser(
        description="Modbus RTU relay controller with MQTT interface",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="path to config.json (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> None:
    """Load configuration and run the relay manager."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    manager = RelayManager(config)

    def _shutdown_handler(signum, frame) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — shutting down", sig_name)
        manager.stop()

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        manager.start()
    except RuntimeError as exc:
        logger.error("Startup failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
