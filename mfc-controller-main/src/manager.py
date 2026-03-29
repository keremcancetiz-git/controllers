"""MFC Manager — orchestrates device polling, MQTT publishing, and commands.

Runs three logical threads:

* **Poll thread** — sequentially reads every device, applies setpoint
  ramping, and handles reconnection with exponential back-off.
* **Publish thread** — periodically serialises device state to MQTT.
* **Main thread** — signal handling and watchdog (via ``run_forever``).
"""

import json
import logging
import threading
import time

import serial.tools.list_ports

from src.config import AppConfig
from src.device import MFCDevice
from src.mqtt_handler import MQTTHandler

logger = logging.getLogger("mfc.manager")


class MFCManager:
    """Central controller that owns devices and coordinates I/O.

    :param config: Fully loaded application configuration.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._publish_thread: threading.Thread | None = None

        self._pending_setpoints: dict[str, float] = {}
        self._pending_gases: dict[str, str] = {}
        self._pending_resets: list[str] = []
        self._cmd_lock = threading.Lock()

        self.devices: dict[str, MFCDevice] = self._discover_devices()

        self._mqtt = MQTTHandler(
            system_config=config.system,
            on_setpoint=self._handle_setpoint,
            on_gas_change=self._handle_gas_change,
            on_reset=self._handle_reset,
            stop_event=self._stop_event,
        )

        logger.info("MFCManager initialised with %d devices", len(self.devices))

    # -- MQTT command handlers (called from MQTT thread) ---------------------

    def _handle_setpoint(self, device_name: str, value: float) -> None:
        """Queue a setpoint change (thread-safe).

        :param device_name: Target device name.
        :param value: Desired setpoint in corrected base units.
        """
        with self._cmd_lock:
            self._pending_setpoints[device_name] = value

    def _handle_gas_change(self, device_name: str, gas_name: str) -> None:
        """Queue a gas selection change (thread-safe).

        :param device_name: Target device name.
        :param gas_name: Gas name to look up in config.
        """
        with self._cmd_lock:
            self._pending_gases[device_name] = gas_name

    def _handle_reset(self, device_name: str) -> None:
        """Queue a device reset (thread-safe).

        :param device_name: Device to reset, or ``"all"``.
        """
        with self._cmd_lock:
            self._pending_resets.append(device_name)

    # -- discovery -----------------------------------------------------------

    def _discover_devices(self) -> dict[str, MFCDevice]:
        """Scan all serial ports and create devices identified by user tag.

        Uses ``serial.tools.list_ports`` to enumerate available ports,
        then calls :meth:`MFCDevice.scan_port` on each to read the
        Bronkhorst user tag (DDE 115).  The tag becomes the device name.

        :returns: Mapping of user-tag to :class:`MFCDevice`.
        """
        devices: dict[str, MFCDevice] = {}
        baudrate = self._config.system.serial_baudrate

        all_ports = serial.tools.list_ports.comports()
        usb_ports = [p for p in all_ports if p.vid is not None]
        logger.info(
            "Found %d serial port(s), %d are USB — scanning ...",
            len(all_ports), len(usb_ports),
        )

        for port_info in sorted(usb_ports, key=lambda p: p.device):
            port = port_info.device
            user_tag = MFCDevice.scan_port(port, baudrate)
            if user_tag:
                if user_tag in devices:
                    logger.warning(
                        "Duplicate user tag '%s' on %s, skipping",
                        user_tag, port,
                    )
                    continue
                devices[user_tag] = MFCDevice(user_tag, port, self._config)
                logger.info("Discovered %s on %s", user_tag, port)

        if not devices:
            logger.warning("No MFC devices found on any serial port")
        else:
            logger.info("Discovered %d MFC device(s)", len(devices))

        return devices

    # -- command processing (called from poll thread) ------------------------

    def _process_commands(self) -> None:
        """Apply all queued commands to devices.

        Acquires the command lock, takes a snapshot, then releases it
        before touching any device (keeps the lock hold time minimal).
        """
        with self._cmd_lock:
            setpoints = self._pending_setpoints.copy()
            self._pending_setpoints.clear()
            gases = self._pending_gases.copy()
            self._pending_gases.clear()
            resets = self._pending_resets.copy()
            self._pending_resets.clear()

        base = self._config.system.base_topic
        delay = self._config.system.inter_device_delay

        # Resets
        for name in resets:
            if name == "all":
                for dev in self.devices.values():
                    dev.disconnect()
                logger.info("All devices reset")
            elif name in self.devices:
                self.devices[name].disconnect()
                logger.info("%s reset", name)

        # Gas changes
        for name, gas in gases.items():
            dev = self.devices.get(name)
            if dev:
                dev.set_target_gas(gas)

        # Setpoints
        for name, value in setpoints.items():
            dev = self.devices.get(name)
            if dev:
                success = dev.set_target_setpoint(value)
                if self._mqtt.is_connected:
                    self._mqtt.publish(
                        f"{name}/setpoint/ack",
                        json.dumps({
                            "success": success,
                            "requested_value": value,
                            "timestamp": int(time.time() * 1000),
                        }),
                        qos=1,
                    )
                time.sleep(delay)

    # -- threads -------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Device polling loop (runs in its own thread).

        1. Initial connect + slow-param read.
        2. Main loop: process commands, read fast params, ramp
           setpoints, reconnect offline devices.
        """
        logger.info("Poll thread starting")
        cfg = self._config.system

        # Initial connection
        logger.info("Connecting to MFCs ...")
        for dev in self.devices.values():
            if self._stop_event.is_set():
                break
            dev.connect()
            if dev.data.online:
                dev.read_slow()
            time.sleep(0.15)

        online = [n for n, d in self.devices.items() if d.data.online]
        offline = [n for n, d in self.devices.items() if not d.data.online]
        logger.info("Initial: %d/%d online", len(online), len(self.devices))
        if offline:
            logger.warning("Offline: %s", ", ".join(offline))

        # Main loop
        cycle_count = 0
        while not self._stop_event.is_set():
            cycle_start = time.time()
            cycle_count += 1

            self._process_commands()

            for dev in self.devices.values():
                if self._stop_event.is_set():
                    break

                if not dev.data.online:
                    if dev.should_retry_connection():
                        dev.connect()
                        if dev.data.online:
                            dev.read_slow()
                    continue

                dev.read_fast()
                dev.apply_ramp()
                time.sleep(cfg.inter_device_delay)

            # Periodic status
            if cycle_count % 60 == 0:
                n_online = sum(1 for d in self.devices.values() if d.data.online)
                reads = sum(d.data.successful_reads for d in self.devices.values())
                errors = sum(d.data.total_errors for d in self.devices.values())
                logger.info(
                    "Status: %d/%d online, %d reads, %d errors",
                    n_online, len(self.devices), reads, errors,
                )

            elapsed = time.time() - cycle_start
            remaining = max(0.0, cfg.cycle_interval - elapsed)
            self._interruptible_sleep(remaining)

        # Cleanup
        for dev in self.devices.values():
            dev.disconnect()
        logger.info("Poll thread stopped")

    def _publish_loop(self) -> None:
        """MQTT publishing loop (runs in its own thread).

        Publishes per-device topics and a combined ``all`` payload at
        the configured interval.
        """
        logger.info("Publish thread starting")
        cfg = self._config.system

        while not self._stop_event.is_set():
            if self._mqtt.is_connected:
                try:
                    all_data = {}
                    ts = int(time.time() * 1000)
                    online_count = 0

                    for name, dev in self.devices.items():
                        with dev.data_lock:
                            data = dev.data.to_mqtt_dict()
                            is_online = dev.data.online
                        all_data[name] = data

                        if is_online:
                            online_count += 1

                        self._mqtt.publish(
                            f"{name}/fmeasure",
                            json.dumps({"value": data["fmeasure"], "timestamp": ts}),
                            retain=True,
                        )
                        self._mqtt.publish(
                            f"{name}/fsetpoint",
                            json.dumps({"value": data["fsetpoint"], "timestamp": ts}),
                            retain=True,
                        )
                        self._mqtt.publish(
                            f"{name}/temperature",
                            json.dumps({"value": data["temperature"], "timestamp": ts}),
                            retain=True,
                        )
                        self._mqtt.publish(
                            f"{name}/online",
                            json.dumps({"value": data["online"]}),
                            retain=True,
                        )
                        self._mqtt.publish(
                            f"{name}/data",
                            json.dumps(data),
                            retain=True,
                        )

                    self._mqtt.publish_raw(
                        f"{cfg.base_topic}/all",
                        json.dumps({
                            "timestamp": ts,
                            "online_count": online_count,
                            "total_count": len(self.devices),
                            "devices": all_data,
                        }),
                        retain=True,
                    )
                except Exception as exc:
                    logger.error("Publish error: %s", exc)

            self._interruptible_sleep(cfg.publish_interval)

        logger.info("Publish thread stopped")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Connect MQTT and launch poll / publish threads."""
        logger.info("Starting MFC Manager ...")

        self._mqtt.connect()
        time.sleep(1)

        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="MFC-Poller", daemon=True,
        )
        self._poll_thread.start()

        self._publish_thread = threading.Thread(
            target=self._publish_loop, name="MQTT-Publisher", daemon=True,
        )
        self._publish_thread.start()

        logger.info("MFC Manager started")

    def stop(self) -> None:
        """Signal threads to exit, join them, and disconnect MQTT."""
        logger.info("Stopping MFC Manager ...")
        self._stop_event.set()

        if self._poll_thread:
            self._poll_thread.join(timeout=10)
        if self._publish_thread:
            self._publish_thread.join(timeout=5)

        self._mqtt.disconnect()
        logger.info("MFC Manager stopped")

    def run_forever(self) -> None:
        """Start and block until interrupted or stopped.

        :raises KeyboardInterrupt: Caught and triggers clean shutdown.
        """
        self.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.stop()

    @property
    def stop_event(self) -> threading.Event:
        """Expose the stop event for external signal handlers."""
        return self._stop_event

    # -- helpers -------------------------------------------------------------

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in 100 ms increments, checking stop_event each step.

        :param seconds: Total sleep duration.
        """
        while seconds > 0 and not self._stop_event.is_set():
            time.sleep(min(0.1, seconds))
            seconds -= 0.1
