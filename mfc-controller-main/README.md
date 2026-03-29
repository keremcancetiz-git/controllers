# MFC Controller

Bronkhorst Mass Flow Controller manager with MQTT integration. Supports gas correction factors, setpoint ramping, and multi-device polling over RS-232.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager
- Bronkhorst MFCs connected via USB-serial adapters
- MQTT broker (e.g. Mosquitto)

## Configuration

Edit `config.json` to match your setup:

- **system** — MQTT broker address, topic prefix, timing, ramp rate
- **gases** — gas correction factors relative to N2
- **unit_multipliers** — native unit to ml/min conversion factors
- **pressure_units** — units that indicate pressure devices (gas correction skipped)

## Development Setup

```bash
git clone <repo-url>
cd mfc-controller
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
git clone <repo-url> /home/innoflex/mfc-controller
cd /home/innoflex/mfc-controller
uv sync --no-dev
```

### 2. Configure

Edit `config.json` with the correct MQTT broker IP and topic prefix for this machine. Devices are auto-discovered by reading the user tag (DDE 115) from each USB serial port.

### 3. Install systemd service

```bash
sudo ln -s /home/innoflex/mfc-controller/mfc-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mfc-controller
sudo systemctl start mfc-controller
```

### 4. Manage the service

```bash
# Check status
sudo systemctl status mfc-controller

# View logs
journalctl -u mfc-controller -f

# Restart after config changes
sudo systemctl restart mfc-controller
```

## MQTT Topics

All topics are prefixed with the configured `base_topic` (e.g. `testbench/lowpower/mfc`).

| Topic | Direction | Description |
|-------|-----------|-------------|
| `{base}/{device}/fmeasure` | Publish | Current flow measurement |
| `{base}/{device}/fsetpoint` | Publish | Current setpoint |
| `{base}/{device}/temperature` | Publish | Device temperature |
| `{base}/{device}/online` | Publish | Device online status |
| `{base}/{device}/data` | Publish | Full device state JSON |
| `{base}/all` | Publish | All devices combined |
| `{base}/status` | Publish | Controller online/offline |
| `{base}/{device}/setpoint/set` | Subscribe | Set new setpoint |
| `{base}/{device}/gas/set` | Subscribe | Change target gas |
| `{base}/{device}/reset` | Subscribe | Reset device connection |