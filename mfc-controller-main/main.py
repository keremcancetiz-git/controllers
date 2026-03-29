#!/usr/bin/env python3
"""MFC Controller — entry point.

Loads configuration, sets up logging, installs signal handlers, and
runs the :class:`~src.manager.MFCManager` loop until interrupted.
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

from src.config import load_config
from src.manager import MFCManager

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.json"
logger = logging.getLogger("mfc")

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    :returns: Parsed arguments with ``config`` attribute.
    """
    parser = argparse.ArgumentParser(description="Bronkhorst MFC Controller")
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="path to JSON configuration file (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> None:
    """Application entry point.

    1. Parse CLI arguments (``--config``).
    2. Load configuration from the specified JSON file.
    3. Configure logging at the level specified in config.
    4. Create :class:`MFCManager` and wire SIGTERM / SIGINT.
    5. Block in :meth:`MFCManager.run_forever`.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    args = parse_args()
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    logger.info("MFC Controller starting")

    manager = MFCManager(config)

    def _signal_handler(signum, frame):
        logger.info("Signal %d received, shutting down ...", signum)
        manager.stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    manager.run_forever()


if __name__ == "__main__":
    main()
