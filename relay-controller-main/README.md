# Relay Controller

Waveshare Modbus RTU 16CH relay controller with MQTT integration. Supports three daisy-chained modules (48 relays total) over RS-485, with configurable relay blocking and periodic state synchronisation.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) package manager
- Waveshare Modbus RTU 16CH relay modules connected via USB-serial RS-485 converter
- MQTT broker (e.g. Mosquitto)

## Configuration

Edit `config.json` to match your setup:

- **mqtt** — MQTT broker address, topic prefix, QoS level
- **modbus** — serial port, baud rate, timeout, retries
- **relay_modules** — Modbus addresses and channel counts for each module
- **blocked_relays** — relay numbers that cannot be controlled via MQTT
- **status_poll_interval** — seconds between periodic state readbacks
- **max_consecutive_errors** — errors before marking a module offline

### Relay numbering

Relays are numbered sequentially across modules: `(module_position - 1) * 16 + channel`.

| Module | Modbus address | Relay numbers |
|--------|---------------|---------------|
| 1      | 2             | 1–16          |
| 2      | 3             | 17–32         |
| 3      | 4             | 33–48         |

## Development Setup

```bash
git clone <repo-url>
cd relay-controller
uv sync
```

### Run locally

```bash
uv run main.py
```

### Custom config path

```bash
uv run main.py -c /path/to/config.json
```

### Build documentation

```bash
uv run sphinx-build -b html docs docs/_build/html
```

## Raspberry Pi Deployment

### 1. Clone and install

```bash
git clone <repo-url> /home/innoflex/relay-controller
cd /home/innoflex/relay-controller
uv sync --no-dev
```

### 2. Configure

Edit `config.json` with the correct MQTT broker IP, serial port, and module addresses for this machine.

### 3. Install systemd service

```bash
sudo ln -s /home/innoflex/relay-controller/relay-controller.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable relay-controller
sudo systemctl start relay-controller
```

### 4. Manage the service

```bash
# Check status
sudo systemctl status relay-controller

# View logs
journalctl -u relay-controller -f

# Restart after config changes
sudo systemctl restart relay-controller
```

## MQTT Topics

All topics are prefixed with the configured `base_topic` (e.g. `testbench/lowpower/relay`).

| Topic | Direction | Description |
|-------|-----------|-------------|
| `{base}/{relay}/state` | Publish | Current relay state (`0` or `1`, retained) |
| `{base}/{relay}/error` | Publish | Error messages for a relay |
| `{base}/status` | Publish | Controller online/offline (LWT) |
| `{base}/{relay}/set_binary` | Subscribe | Set relay state (`0` = OFF, `1` = ON) |
