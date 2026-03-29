# Sensor Controller

Async Modbus RTU sensor gateway over RS-485 with MQTT publishing. Reads industrial sensors (temperature, humidity, pressure) via Modbus and publishes measurements to an MQTT broker following a hierarchical topic convention.

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/) package manager
- RS-485 serial adapter (e.g. `/dev/sensor`)
- MQTT broker (e.g. Mosquitto)

## Quick Start

```bash
uv sync
uv run main.py
```

## Configuration

All settings live in `config.json`:

| Section   | Key                        | Description                              |
|-----------|----------------------------|------------------------------------------|
| serial    | port, baudrate             | RS-485 serial connection                 |
| mqtt      | broker, port, base_topic   | MQTT broker and topic prefix             |
| timing    | watchdog_timeout, ...      | Polling and timeout intervals            |
| topics    | address_change_request, ...| MQTT sub-topic paths                     |
| sensors   | keyed by Modbus address    | Sensor name, location, format, type      |

### Sensor Formats

- **INT16_DIV10** - Temperature/humidity sensors. Publishes to both `temperature` and `humidity` topics.
- **FLOAT32** - Pressure sensors. Publishes to the sensor's configured `type` topic.

## MQTT Topics

Pattern: `{base_topic}/{sensor_type}/{sensor_id}/{command}`

Examples with `base_topic = "testbench/lowpower"`:

```
testbench/lowpower/temperature/1/measurement
testbench/lowpower/humidity/1/measurement
testbench/lowpower/pressure/3/measurement
testbench/lowpower/gateway/status              # LWT: "online" / "offline"
testbench/lowpower/address/change              # Subscribe: address change requests
testbench/lowpower/address/status              # Publish: address change results
```

### Address Change

Publish to the address change request topic:

```json
{"old_addr": 1, "new_addr": 5}
```

The gateway enters maintenance mode (pauses polling), writes the new address, saves to flash, and publishes the result to the status topic.

## Project Structure

```
main.py              # Entry point: logging, config loading, asyncio.run()
config.json          # All configuration
src/
  config.py          # Frozen dataclasses and config loader
  device.py          # Serial connection (pymodbus) and data parsers
  mqtt_handler.py    # MQTT client (aiomqtt) with LWT and topic management
  manager.py         # Gateway orchestrator: poll loop, watchdog, maintenance
```

## Deployment

### systemd

```bash
sudo ln -s $(pwd)/sensor-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sensor-controller
```

Ensure the service user has access to the serial device (e.g. add to `dialout` group).

### Logs

```bash
journalctl -u sensor-controller -f
```

Output is JSON-structured: `{"ts": "...", "lvl": "INFO", "msg": "...", "mod": "sensor.device"}`

## Documentation

```bash
uv sync --group dev
uv run sphinx-build -b html docs docs/_build/html
```