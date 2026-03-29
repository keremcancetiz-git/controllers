"""Configuration loading and typed dataclasses for the sensor gateway."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

logger = logging.getLogger("sensor.config")

VALID_FORMATS = {"INT16_DIV10", "INT16_DIV100"}


@dataclass(frozen=True)
class SerialConfig:
    """Serial port connection parameters.

    :param port: Device path (e.g. ``/dev/sensor``).
    :param baudrate: Baud rate for RS-485 communication.
    """

    port: str = "/dev/sensor"
    baudrate: int = 9600


@dataclass(frozen=True)
class MqttConfig:
    """MQTT broker connection parameters.

    :param broker: Broker hostname or IP.
    :param port: Broker TCP port.
    :param base_topic: Root prefix for all topics (e.g. ``testbench/lowpower``).
    """

    broker: str = "localhost"
    port: int = 1883
    base_topic: str = "testbench/lowpower"


@dataclass(frozen=True)
class TimingConfig:
    """Timing parameters for polling and watchdog.

    :param watchdog_timeout: Seconds before watchdog triggers.
    :param sensor_timeout: Max wait for a single sensor response.
    :param inter_sensor_delay: Pause between polling consecutive sensors.
    :param byte_timeout: Timeout for individual byte reads.
    :param maintenance_settle: Delay after entering maintenance mode.
    :param address_change_stabilize: Delay after writing new address.
    """

    watchdog_timeout: float = 60.0
    sensor_timeout: float = 1.0
    inter_sensor_delay: float = 0.1
    byte_timeout: float = 0.05
    maintenance_settle: float = 2.0
    address_change_stabilize: float = 3.0


@dataclass(frozen=True)
class TopicConfig:
    """MQTT topic path fragments appended to base_topic.

    :param address_change_request: Sub-path for address change commands.
    :param address_change_status: Sub-path for address change results.
    :param status: Sub-path for gateway online/offline status (LWT).
    """

    address_change_request: str = "address/change"
    address_change_status: str = "address/status"
    status: str = "gateway/status"


@dataclass(frozen=True)
class SensorConfig:
    """Individual sensor definition.

    :param address: Modbus slave address.
    :param name: Human-readable sensor name.
    :param location: Physical location descriptor.
    :param format: Data format (``INT16_DIV10`` 0.1 res or ``INT16_DIV100`` 0.01 res).
    :param type: Sensor type for MQTT topic (``temperature``, ``humidity``).
    """

    address: int
    name: str
    location: str
    format: str
    type: str


@dataclass(frozen=True)
class GatewayConfig:
    """Top-level configuration container.

    :param serial: Serial port settings.
    :param mqtt: MQTT broker settings.
    :param timing: Timing and timeout settings.
    :param topics: MQTT topic path fragments.
    :param sensors: Mapping of Modbus address to sensor config.
    """

    serial: SerialConfig = field(default_factory=SerialConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    topics: TopicConfig = field(default_factory=TopicConfig)
    sensors: Dict[int, SensorConfig] = field(default_factory=dict)


def load_config(path: Path = Path("config.json")) -> GatewayConfig:
    """Load gateway configuration from a JSON file.

    :param path: Path to the configuration file.
    :returns: Fully populated GatewayConfig instance.
    :raises FileNotFoundError: If config file does not exist.
    :raises ValueError: If config file contains invalid data.
    """
    logger.info("Loading configuration from %s", path)

    with open(path, "r") as f:
        raw = json.load(f)

    serial = SerialConfig(**raw.get("serial", {}))
    mqtt = MqttConfig(**raw.get("mqtt", {}))
    timing = TimingConfig(**raw.get("timing", {}))
    topics = TopicConfig(**raw.get("topics", {}))

    sensors: Dict[int, SensorConfig] = {}
    for addr_str, sensor_data in raw.get("sensors", {}).items():
        try:
            addr = int(addr_str)
        except ValueError:
            raise ValueError("Sensor address must be an integer, got: %s" % addr_str)

        fmt = sensor_data.get("format", "")
        if fmt not in VALID_FORMATS:
            raise ValueError(
                "Sensor %s has invalid format '%s', must be one of %s"
                % (addr_str, fmt, VALID_FORMATS)
            )

        sensors[addr] = SensorConfig(address=addr, **sensor_data)

    config = GatewayConfig(
        serial=serial,
        mqtt=mqtt,
        timing=timing,
        topics=topics,
        sensors=sensors,
    )

    logger.info(
        "Configuration loaded: %s sensors, broker=%s, base_topic=%s",
        len(sensors),
        mqtt.broker,
        mqtt.base_topic,
    )

    return config
