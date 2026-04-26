# Sensor Controller

Async Modbus RTU gateway for Waveshare HG803 temperature/humidity/dew-point sensors and QDW90A-G pressure transmitters. Polls a multi-drop RS-485 bus, publishes readings over MQTT, and accepts runtime commands to add, remove, or reconfigure sensors — including bus address — without restarting the service.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager
- Supported sensors on an RS-485 bus — Waveshare HG803 (temperature/humidity/dew-point) and/or QDW90A-G pressure transmitters (Waveshare FT4232HL USB adapter recommended)
- MQTT broker (e.g. Mosquitto)

## Configuration

Edit `config.json` to match your setup:

- **buses** — one entry per RS-485 bus; each has a stable `id` (referenced by add-sensor commands), a `serial` block (port path and baud rate), and its own `sensors` map keyed by Modbus address. Each sensor entry has a `name`, `location`, and `type` (`temperature_humidity` for HG803 or `pressure` for QDW90A-G). Sensor addresses must be globally unique across all buses.
- **mqtt** — broker address, port, and `base_topic` prefix
- **timing** — poll cadence, watchdog timeout, serial timeouts, and address-change settle delays
- **topics** — MQTT topic suffixes for commands and status

## Development Setup

```bash
git clone <repo-url>
cd sensor-controller
uv sync
```

### Run locally

```bash
uv run main.py
```

### Build documentation

```bash
uv run sphinx-build -b html docs docs/_build/html
```

### Scan the bus

When commissioning hardware, list responding sensor addresses:

```bash
uv run tools/find_address.py /dev/ttyUSB0
uv run tools/find_address.py /dev/ttyUSB0 --start 1 --end 20
```

## Raspberry Pi Deployment

### 1. Clone and install

```bash
git clone <repo-url> /home/innoflex/sensor-controller
cd /home/innoflex/sensor-controller
uv sync --no-dev
```

### 2. Pin the USB-RS485 adapter to a stable device name

Install the udev rules so the FT4232HL adapter is always available at `/dev/SENSORS0`–`/dev/SENSORS3` regardless of USB enumeration order:

```bash
sudo ln -s /home/innoflex/sensor-controller/99-usb-serial.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The rules match the adapter's FTDI serial string (`SENSORS1`) and create one symlink per interface. If your adapter reports a different serial, either reprogram its EEPROM with `ftdi_eeprom` or edit `99-usb-serial.rules` to match.

### 3. Configure

Edit `config.json` with the correct serial port, MQTT broker, and the Modbus address of every connected sensor. Use `tools/find_address.py` if you don't yet know the addresses.

### 4. Install systemd service

```bash
sudo ln -s /home/innoflex/sensor-controller/sensor-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sensor-controller
sudo systemctl start sensor-controller
```

### 5. Manage the service

```bash
# Check status
sudo systemctl status sensor-controller

# View logs
journalctl -u sensor-controller -f

# Restart after config changes
sudo systemctl restart sensor-controller
```

## MQTT Topics

All topics are prefixed with the configured `base_topic` (e.g. `testbench/lowpower`).

| Topic | Direction | Description |
|-------|-----------|-------------|
| `{base}/temperature/{address}/measurement` | Publish | Temperature reading (°C) |
| `{base}/humidity/{address}/measurement` | Publish | Humidity reading (%RH) |
| `{base}/dew_point/{address}/measurement` | Publish | Dew-point reading (°C) |
| `{base}/pressure/{address}/measurement` | Publish | Pressure reading (bar) |
| `{base}/gateway/status` | Publish | Gateway online/offline (LWT, retained) |
| `{base}/sensor/status` | Publish | Result of the most recent sensor command |
| `{base}/sensor/update` | Subscribe | Update an existing sensor (address, name, location, or type) |
| `{base}/sensor/add` | Subscribe | Add a new sensor to the gateway |
| `{base}/sensor/remove` | Subscribe | Remove a sensor from the gateway |

### Command payloads

`sensor/update` — change Modbus address (triggers a write to the sensor):

```json
{ "address": 13, "new_address": 21 }
```

`sensor/update` — metadata only, no bus write:

```json
{ "address": 11, "name": "Kiln_intake", "location": "Second_floor" }
```

`sensor/add` — all fields required; `bus_id` picks which bus the new sensor lives on. `type` is either `temperature_humidity` (HG803) or `pressure` (QDW90A-G):

```json
{ "bus_id": "bus0", "address": 22, "name": "Line_B", "location": "Third_floor", "type": "temperature_humidity" }
```

`sensor/remove`:

```json
{ "address": 15 }
```
