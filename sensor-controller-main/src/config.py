"""Configuration loading and validation.

Loads gateway configuration from a JSON file into typed, frozen dataclasses.
All domain constants live in ``config.json`` — nothing is hardcoded.
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, asdict

logger = logging.getLogger("sensor.config")


class SensorType(Enum):
    """Sensor category.

    Drives two pieces of internal routing:

    * Which Modbus register block to read and how to decode it
      (see :mod:`~src.device`).
    * Which MQTT measurement topics the gateway publishes for this
      sensor (``temperature`` / ``humidity`` / ``dew_point`` for
      temperature-humidity sensors, ``pressure`` for pressure
      transmitters).

    Supported values:

    * ``TEMPERATURE_HUMIDITY`` — Waveshare HG803 (registers
      0x0004-0x0007, INT16 ÷ 10).
    * ``PRESSURE`` — QDW90A-G pressure transmitter (registers
      0x0016-0x0017, IEEE-754 big-endian float32 in bar).
    """

    TEMPERATURE_HUMIDITY = "temperature_humidity"
    PRESSURE = "pressure"


@dataclass(frozen=True)
class SerialConfig:
    """RS-485 serial port settings.

    :param port: Device path (e.g. ``/dev/ttyUSB0``).
    :param baudrate: Baud rate — must match the sensor configuration.
    """

    port: str = "/dev/SENSORS0"
    baudrate: int = 9600


@dataclass(frozen=True)
class MqttConfig:
    """MQTT broker connection settings.

    :param broker: Broker hostname or IP.
    :param port: Broker TCP port.
    :param base_topic: Prefix for all published topics.
    """

    broker: str
    port: int = 1883
    base_topic: str = "testbench/lowpower"


@dataclass(frozen=True)
class TimingConfig:
    """Timing parameters that govern gateway behaviour.

    :param watchdog_timeout: Seconds without a successful read before forced restart.
    :param sensor_timeout: Modbus response timeout per request.
    :param inter_sensor_delay: Pause between consecutive sensor polls.
    :param byte_timeout: Inter-byte timeout on the serial line.
    :param maintenance_settle: Pause after entering maintenance mode.
    :param address_change_stabilize: Pause after writing a new sensor address.
    """

    watchdog_timeout: float = 60.0
    sensor_timeout: float = 1.0
    inter_sensor_delay: float = 0.1
    byte_timeout: float = 0.05
    maintenance_settle: float = 2.0
    address_change_stabilize: float = 3.0


@dataclass(frozen=True)
class TopicConfig:
    """MQTT topic suffixes appended to ``base_topic``.

    :param sensor_update_request: Topic the gateway subscribes to for update-sensor commands (address, name, etc.).
    :param sensor_add_request: Topic the gateway subscribes to for add-sensor commands.
    :param sensor_remove_request: Topic the gateway subscribes to for remove-sensor commands.
    :param sensor_command_status: Topic where add/remove/update results are published.
    :param status: Topic for gateway online/offline status (LWT).
    """

    sensor_update_request: str = "sensor/update"
    sensor_add_request: str = "sensor/add"
    sensor_remove_request: str = "sensor/remove"
    sensor_command_status: str = "sensor/status"
    status: str = "gateway/status"


@dataclass(frozen=True)
class SensorConfig:
    """Descriptor for one physical sensor on the bus.

    :param address: Modbus slave address (1-247), globally unique across all buses.
    :param name: Human-readable name used in MQTT payloads.
    :param location: Physical location description.
    :param type: Sensor category — determines which register block to read
        and which MQTT measurement topics are published.
    """

    address: int
    name: str
    location: str
    type: SensorType


@dataclass(frozen=True)
class BusConfig:
    """One RS-485 bus and the sensors attached to it.

    :param id: Stable identifier used by MQTT add-sensor commands to pick a bus.
    :param serial: Serial port parameters for this bus.
    :param sensors: Sensor descriptors on this bus, sorted by address.
    """

    id: str
    serial: SerialConfig
    sensors: list[SensorConfig]


@dataclass(frozen=True)
class GatewayConfig:
    """Top-level container holding the complete gateway configuration.

    :param buses: RS-485 buses, each with its own serial port and sensors.
    :param mqtt: MQTT broker settings.
    :param timing: Timing parameters.
    :param topics: MQTT topic suffixes.
    """

    buses: list[BusConfig]
    mqtt: MqttConfig
    timing: TimingConfig
    topics: TopicConfig

    def save(self, path: str = "config.json") -> None:
        """Serialize configuration back to JSON."""
        config_path = Path(path)
        data = {
            "buses": [
                {
                    "id": bus.id,
                    "serial": asdict(bus.serial),
                    "sensors": {
                        str(s.address): {
                            "name": s.name,
                            "location": s.location,
                            "type": s.type.value,
                        }
                        for s in bus.sensors
                    },
                }
                for bus in self.buses
            ],
            "mqtt": asdict(self.mqtt),
            "timing": asdict(self.timing),
            "topics": asdict(self.topics),
        }
        with open(config_path, "w") as fh:
            json.dump(data, fh, indent=2)
        logger.info("Configuration saved to %s", path)


def load_config(path: str = "config.json") -> GatewayConfig:
    """Load and validate gateway configuration from a JSON file.

    :param path: Path to the configuration file.
    :returns: Fully populated :class:`GatewayConfig`.
    :raises FileNotFoundError: If *path* does not exist.
    :raises KeyError: If a required field is missing.
    :raises ValueError: If a sensor type string is invalid.
    :rtype: GatewayConfig
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError("Configuration file not found: %s" % path)

    with open(config_path) as fh:
        raw = json.load(fh)

    buses: list[BusConfig] = []
    seen_addresses: set[int] = set()
    for bus_data in raw.get("buses", []):
        sensors: list[SensorConfig] = []
        for addr_str, sensor_data in bus_data.get("sensors", {}).items():
            address = int(addr_str)
            if address in seen_addresses:
                raise ValueError(
                    "Sensor address %s appears on multiple buses" % address
                )
            seen_addresses.add(address)
            sensors.append(
                SensorConfig(
                    address=address,
                    name=sensor_data["name"],
                    location=sensor_data["location"],
                    type=SensorType(sensor_data["type"]),
                )
            )
        buses.append(
            BusConfig(
                id=bus_data["id"],
                serial=SerialConfig(**bus_data.get("serial", {})),
                sensors=sorted(sensors, key=lambda s: s.address),
            )
        )

    if len({bus.id for bus in buses}) != len(buses):
        raise ValueError("Duplicate bus id in configuration")

    config = GatewayConfig(
        buses=buses,
        mqtt=MqttConfig(**raw.get("mqtt", {})),
        timing=TimingConfig(**raw.get("timing", {})),
        topics=TopicConfig(**raw.get("topics", {})),
    )

    logger.info(
        "Loaded configuration with %s bus(es), %s sensor(s) from %s",
        len(config.buses),
        len(seen_addresses),
        path,
    )
    return config
