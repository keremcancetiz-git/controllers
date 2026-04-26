# PID Controller

Kane TCN6-D PID temperature controller manager with MQTT integration. Supports multiple Modbus RTU devices over RS-485, automatic polling, and setpoint control via MQTT.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager
- Kane TCN6-D controllers connected via WAVESHARE USB-to-RS485 converter
- MQTT broker (e.g. Mosquitto)

## Configuration

Edit `config.json` to match your setup:

- **serial** — serial port, baud rate, timeout
- **mqtt** — broker address, port, topic prefix, keepalive
- **devices** — list of Modbus addresses (5, 6, 7 by default)
- **registers** — Modbus register addresses for PV (measured temp) and SV (setpoint)
- **polling_interval** — seconds between measurement reads (default 1.0)
- **reconnect_base_cooldown / reconnect_max_cooldown** — exponential backoff for reconnection
- **max_consecutive_errors** — errors before marking a device offline

## Development Setup

```bash
git clone <repo-url>
cd pid-controller
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

## Raspberry Pi Deployment

### 1. Clone and install

```bash
git clone <repo-url> /home/innoflex/pid-controller
cd /home/innoflex/pid-controller
uv sync --no-dev
```

### 2. Configure

Edit `config.json` with the correct MQTT broker IP, topic prefix, and Modbus addresses for this machine.

### 3. Install systemd service

```bash
sudo ln -s /home/innoflex/pid-controller/pid-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pid-controller
sudo systemctl start pid-controller
```

### 4. Manage the service

```bash
# Check status
sudo systemctl status pid-controller

# View logs
journalctl -u pid-controller -f

# Restart after config changes
sudo systemctl restart pid-controller
```

## MQTT Topics

All topics are prefixed with the configured `base_topic` (e.g. `testbench/highpower/pid`). Device IDs are derived from Modbus addresses (address - 4), so addresses 5, 6, 7 map to device IDs 1, 2, 3.

| Topic | Direction | Description |
|-------|-----------|-------------|
| `{base}/{device}/measurement` | Publish | Current temperature (PV) |
| `{base}/{device}/setpoint` | Publish | Current setpoint (SV) |
| `{base}/{device}/status` | Publish | Device `online`/`offline` |
| `{base}/status` | Publish | Service `online`/`offline` (LWT) |
| `{base}/{device}/set_setpoint` | Subscribe | Set new target temperature |
