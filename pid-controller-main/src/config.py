import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pid.config")


@dataclass
class SerialConfig:
    """Serial port configuration for the Modbus RTU connection."""

    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: int
    timeout: float


@dataclass
class MqttConfig:
    """MQTT broker connection settings."""

    broker: str
    port: int
    base_topic: str
    keepalive: int


@dataclass
class DeviceEntry:
    """Single PID controller device definition."""

    modbus_address: int


@dataclass
class RegisterMap:
    """Modbus register addresses for the PID controller."""

    pv: int
    sv: int


@dataclass
class AppConfig:
    """Top-level application configuration.

    :param serial: Serial port settings.
    :param mqtt: MQTT broker settings.
    :param devices: List of PID controller devices.
    :param registers: Modbus register map.
    :param polling_interval: Seconds between poll cycles.
    :param modbus_address_offset: Subtracted from modbus_address to get device_id.
    :param value_scale: Divisor/multiplier for raw register values (e.g. 10 means 250 = 25.0).
    :param reconnect_base_cooldown: Initial reconnect delay in seconds.
    :param reconnect_max_cooldown: Maximum reconnect delay in seconds.
    :param max_consecutive_errors: Errors before marking a device offline.
    """

    serial: SerialConfig
    mqtt: MqttConfig
    devices: list[DeviceEntry]
    registers: RegisterMap
    polling_interval: float
    modbus_address_offset: int
    value_scale: int
    reconnect_base_cooldown: float
    reconnect_max_cooldown: float
    max_consecutive_errors: int


def load_config(path: str) -> AppConfig:
    """Load and parse the JSON configuration file.

    :param path: Path to the config.json file.
    :returns: Parsed application configuration.
    :raises FileNotFoundError: If the config file does not exist.
    :raises json.JSONDecodeError: If the config file is not valid JSON.
    """
    config_path = Path(path)
    logger.info("Loading configuration from %s", config_path)
    with config_path.open() as f:
        data = json.load(f)

    return AppConfig(
        serial=SerialConfig(**data["serial"]),
        mqtt=MqttConfig(**data["mqtt"]),
        devices=[DeviceEntry(**d) for d in data["devices"]],
        registers=RegisterMap(**data["registers"]),
        polling_interval=data["polling_interval"],
        modbus_address_offset=data["modbus_address_offset"],
        value_scale=data["value_scale"],
        reconnect_base_cooldown=data["reconnect_base_cooldown"],
        reconnect_max_cooldown=data["reconnect_max_cooldown"],
        max_consecutive_errors=data["max_consecutive_errors"],
    )
