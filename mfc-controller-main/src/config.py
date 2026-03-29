"""Configuration loader and validator for MFC Controller.

Loads settings from an external JSON file and exposes them as typed
dataclasses with sensible defaults.  Every domain-specific constant
(gas factors, unit multipliers, pressure-unit list) lives in the JSON
file so the Python code stays machine-agnostic.
"""

import json
import os
import sys
from dataclasses import dataclass, field


@dataclass
class SystemConfig:
    """Top-level system / network settings.

    :param mqtt_broker: Hostname or IP of the MQTT broker.
    :param mqtt_port: TCP port of the MQTT broker.
    :param base_topic: MQTT topic prefix
        (e.g. ``testbench/lowpower/mfc``).
    :param serial_baudrate: Baud rate for RS-232 connections.
    :param cycle_interval: Seconds between poll cycles.
    :param publish_interval: Seconds between MQTT publish cycles.
    :param reconnect_cooldown: Base seconds before first reconnect
        attempt (doubles up to 60 s).
    :param inter_device_delay: Seconds to wait between sequential
        device reads to reduce bus contention.
    :param ramp_rate: Maximum setpoint change per second
        (native units / s).
    :param log_level: Python logging level name.
    """

    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    base_topic: str = "mfc"
    serial_baudrate: int = 38400
    cycle_interval: float = 0.5
    publish_interval: float = 1.0
    reconnect_cooldown: float = 15.0
    inter_device_delay: float = 0.05
    ramp_rate: float = 50.0
    log_level: str = "INFO"


@dataclass
class AppConfig:
    """Complete application configuration.

    :param system: Network, timing, and logging settings.
    :param gases: Mapping of gas name (upper-case) to correction
        factor relative to N\u2082.
    :param unit_multipliers: Mapping of native unit string
        (lower-case) to multiplier that converts to ml/min
        (or equivalent base).
    :param pressure_units: List of unit strings that denote pressure
        devices (gas correction is skipped for these).
    """

    system: SystemConfig = field(default_factory=SystemConfig)
    gases: dict[str, float] = field(default_factory=dict)
    unit_multipliers: dict[str, float] = field(default_factory=dict)
    pressure_units: list[str] = field(default_factory=list)


def load_config(path: str | None = None) -> AppConfig:
    """Load and validate configuration from a JSON file.

    If *path* is ``None`` the loader looks for ``config.json`` next to
    the repository root (one level up from ``src/``).

    :param path: Absolute or relative path to the JSON config file.
    :returns: Fully populated :class:`AppConfig` instance.
    :raises SystemExit: If the file cannot be read or parsed.
    """

    if path is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.json",
        )

    try:
        with open(path, "r") as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"CRITICAL: Cannot load {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    system_raw = raw.get("system", {})
    known_keys = set(SystemConfig.__dataclass_fields__)
    unknown_keys = set(system_raw) - known_keys
    if unknown_keys:
        print(
            f"WARNING: Unknown system config keys (ignored): "
            f"{', '.join(sorted(unknown_keys))}",
            file=sys.stderr,
        )
    system = SystemConfig(**{
        k: v for k, v in system_raw.items()
        if k in known_keys
    })

    gases = {}
    for k, v in raw.get("gases", {}).items():
        try:
            gases[k.upper()] = float(v)
        except (TypeError, ValueError) as exc:
            print(f"WARNING: Skipping gas '{k}' — invalid value '{v}': {exc}",
                  file=sys.stderr)

    unit_multipliers = {}
    for k, v in raw.get("unit_multipliers", {}).items():
        try:
            unit_multipliers[k.lower()] = float(v)
        except (TypeError, ValueError) as exc:
            print(f"WARNING: Skipping unit '{k}' — invalid value '{v}': {exc}",
                  file=sys.stderr)

    pressure_units = [
        u.lower() for u in raw.get("pressure_units", [])
    ]

    return AppConfig(
        system=system,
        gases=gases,
        unit_multipliers=unit_multipliers,
        pressure_units=pressure_units,
    )
