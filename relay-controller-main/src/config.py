"""Configuration loading and validation for the relay controller.

Provides typed dataclasses for all configuration sections and a loader
that reads ``config.json`` and returns a fully validated :class:`AppConfig`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("relay.config")


@dataclass(frozen=True)
class MqttConfig:
    """MQTT broker connection settings.

    :param broker_host: Hostname or IP of the MQTT broker.
    :param broker_port: TCP port of the MQTT broker.
    :param client_id: MQTT client identifier.
    :param username: Broker authentication username (empty to skip auth).
    :param password: Broker authentication password.
    :param keepalive: Maximum seconds between communications with broker.
    :param base_topic: Prefix for all relay MQTT topics.
    :param qos: Default quality-of-service level (0, 1, or 2).
    """

    broker_host: str
    broker_port: int
    client_id: str
    username: str
    password: str
    keepalive: int
    base_topic: str
    qos: int


@dataclass(frozen=True)
class ModbusConfig:
    """Modbus RTU serial connection settings.

    :param port: Serial port device path.
    :param baudrate: Baud rate for serial communication.
    :param bytesize: Number of data bits (7 or 8).
    :param parity: Parity checking: ``'N'``\\one, ``'E'``\\ven, ``'O'``\\dd.
    :param stopbits: Number of stop bits (1 or 2).
    :param timeout: Read timeout in seconds.
    :param retries: Maximum retries per Modbus request.
    """

    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: int
    timeout: float
    retries: int


@dataclass(frozen=True)
class RelayModuleConfig:
    """Physical relay module descriptor.

    :param modbus_address: Modbus device address on the RS-485 bus.
    :param channel_count: Number of relay channels on this module.
    """

    modbus_address: int
    channel_count: int


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration.

    :param mqtt: MQTT broker settings.
    :param modbus: Modbus serial settings.
    :param relay_modules: Ordered list of relay module descriptors.
    :param blocked_relays: Relay numbers that cannot be controlled via MQTT.
    :param status_poll_interval: Seconds between periodic relay state polls.
    :param max_consecutive_errors: Errors before marking a module offline.
    """

    mqtt: MqttConfig
    modbus: ModbusConfig
    relay_modules: list[RelayModuleConfig]
    blocked_relays: set[int] = field(default_factory=set)
    status_poll_interval: int = 30
    max_consecutive_errors: int = 3

    @property
    def total_relays(self) -> int:
        """Return the total number of relays across all modules.

        :returns: Sum of channel counts.
        :rtype: int
        """
        return sum(m.channel_count for m in self.relay_modules)


def load_config(path: str | Path) -> AppConfig:
    """Load and validate application configuration from a JSON file.

    :param path: Path to the JSON configuration file.
    :returns: Fully validated application configuration.
    :rtype: AppConfig
    :raises FileNotFoundError: If the configuration file does not exist.
    :raises ValueError: If the configuration is invalid.
    """
    path = Path(path)
    logger.info("Loading configuration from %s", path)

    with open(path) as fh:
        raw = json.load(fh)

    mqtt = MqttConfig(**raw["mqtt"])
    modbus = ModbusConfig(**raw["modbus"])
    modules = [RelayModuleConfig(**m) for m in raw["relay_modules"]]

    if not modules:
        raise ValueError("At least one relay module must be configured")

    blocked = set(raw.get("blocked_relays", []))
    total = sum(m.channel_count for m in modules)
    invalid = {r for r in blocked if r < 1 or r > total}
    if invalid:
        raise ValueError(
            "Blocked relay numbers out of range (1-%d): %s" % (total, invalid)
        )

    config = AppConfig(
        mqtt=mqtt,
        modbus=modbus,
        relay_modules=modules,
        blocked_relays=blocked,
        status_poll_interval=raw.get("status_poll_interval", 30),
        max_consecutive_errors=raw.get("max_consecutive_errors", 3),
    )

    logger.info(
        "Configuration loaded: %d modules, %d total relays, %d blocked",
        len(modules),
        total,
        len(blocked),
    )
    return config


def relay_to_modbus(
    relay_number: int, modules: list[RelayModuleConfig]
) -> tuple[int, int]:
    """Map a flat relay number to a (modbus_address, coil_address) pair.

    Relay numbering starts at 1 and spans all modules sequentially.
    For example, with three 16-channel modules at addresses 2, 3, 4:

    - Relay 1  → (modbus_address=2, coil=0)
    - Relay 16 → (modbus_address=2, coil=15)
    - Relay 17 → (modbus_address=3, coil=0)
    - Relay 34 → (modbus_address=4, coil=1)

    :param relay_number: Flat relay number (1-based).
    :param modules: Ordered list of relay module configurations.
    :returns: Tuple of (modbus_address, coil_address).
    :rtype: tuple[int, int]
    :raises ValueError: If the relay number is out of range.
    """
    remaining = relay_number - 1
    for module in modules:
        if remaining < module.channel_count:
            return module.modbus_address, remaining
        remaining -= module.channel_count

    total = sum(m.channel_count for m in modules)
    raise ValueError(
        "Relay number %d out of range (1–%d)" % (relay_number, total)
    )
