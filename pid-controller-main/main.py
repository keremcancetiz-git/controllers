import argparse
import logging
import signal
import sys

from src.config import load_config
from src.manager import DeviceManager


def main() -> None:
    """Entry point for the PID controller service."""
    parser = argparse.ArgumentParser(
        description="PID temperature controller MQTT interface"
    )
    parser.add_argument(
        "-c", "--config", default="config.json", help="Path to config file"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = load_config(args.config)
    manager = DeviceManager(config)

    def shutdown(signum, frame):
        logging.getLogger("pid.main").info("Received signal %s, shutting down", signum)
        manager.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    manager.start()


if __name__ == "__main__":
    main()