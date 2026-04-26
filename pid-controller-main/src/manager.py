import logging
import os
import threading
import time

from pymodbus.client import ModbusSerialClient

from .config import AppConfig
from .device import PidDevice
from .mqtt_handler import MqttHandler

logger = logging.getLogger("pid.manager")


class DeviceManager:
    """Orchestrates PID device polling and MQTT communication.

    :param config: Application configuration.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._serial_lock = threading.Lock()
        self._running = False
        self._stop_event = threading.Event()
        self._devices: dict[int, PidDevice] = {}
        self._previous_online: dict[int, bool] = {}

        self._modbus_client = ModbusSerialClient(
            port=config.serial.port,
            baudrate=config.serial.baudrate,
            bytesize=config.serial.bytesize,
            parity=config.serial.parity,
            stopbits=config.serial.stopbits,
            timeout=config.serial.timeout,
        )

        device_ids = []
        for entry in config.devices:
            device_id = entry.modbus_address - config.modbus_address_offset
            device = PidDevice(
                modbus_address=entry.modbus_address,
                device_id=device_id,
                client=self._modbus_client,
                registers=config.registers,
                value_scale=config.value_scale,
                max_consecutive_errors=config.max_consecutive_errors,
                serial_lock=self._serial_lock,
            )
            self._devices[device_id] = device
            self._previous_online[device_id] = False
            device_ids.append(device_id)

        self._mqtt = MqttHandler(
            config=config.mqtt,
            device_ids=device_ids,
            on_set_setpoint=self._handle_set_setpoint,
        )

    def _check_serial_port(self) -> bool:
        """Verify the serial port exists before connecting.

        :returns: True if the port device file exists.
        """
        if not os.path.exists(self._config.serial.port):
            logger.error(
                "Serial port %s does not exist", self._config.serial.port
            )
            return False
        return True

    def _connect_serial(self) -> bool:
        """Connect to the Modbus serial bus with exponential backoff.

        :returns: True once connected successfully.
        """
        cooldown = self._config.reconnect_base_cooldown
        while not self._stop_event.is_set():
            if not self._check_serial_port():
                logger.info("Retrying serial connection in %.0fs", cooldown)
                self._stop_event.wait(cooldown)
                cooldown = min(cooldown * 2, self._config.reconnect_max_cooldown)
                continue

            if self._modbus_client.connect():
                logger.info(
                    "Connected to Modbus on %s", self._config.serial.port
                )
                return True

            logger.warning(
                "Failed to connect to %s, retrying in %.0fs",
                self._config.serial.port,
                cooldown,
            )
            self._stop_event.wait(cooldown)
            cooldown = min(cooldown * 2, self._config.reconnect_max_cooldown)

        return False

    def _connect_mqtt(self) -> bool:
        """Connect to the MQTT broker with exponential backoff.

        :returns: True once connected successfully.
        """
        cooldown = self._config.reconnect_base_cooldown
        while not self._stop_event.is_set():
            try:
                self._mqtt.connect()
                return True
            except (ConnectionRefusedError, OSError) as e:
                logger.warning(
                    "MQTT connection failed: %s, retrying in %.0fs", e, cooldown
                )
                self._stop_event.wait(cooldown)
                cooldown = min(
                    cooldown * 2, self._config.reconnect_max_cooldown
                )
        return False

    def start(self) -> None:
        """Start the manager: connect serial + MQTT, then enter the poll loop."""
        logger.info("Starting device manager")
        self._running = True

        if not self._connect_serial():
            return
        if not self._connect_mqtt():
            self._modbus_client.close()
            return

        try:
            self._poll_loop()
        finally:
            self._cleanup()

    def stop(self) -> None:
        """Signal the poll loop to stop. Cleanup happens in start()."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        logger.info("Stop requested")

    def _cleanup(self) -> None:
        """Disconnect MQTT and serial after the poll loop exits."""
        logger.info("Cleaning up connections")
        self._mqtt.disconnect()
        self._modbus_client.close()
        logger.info("Device manager stopped")

    def _poll_loop(self) -> None:
        """Continuously poll all devices and publish their state."""
        logger.info(
            "Polling %s devices every %.1fs",
            len(self._devices),
            self._config.polling_interval,
        )
        while not self._stop_event.is_set():
            for device in self._devices.values():
                if self._stop_event.is_set():
                    break
                state = device.poll()

                # Publish status change
                if state.online != self._previous_online[state.device_id]:
                    self._mqtt.publish_status(state.device_id, state.online)
                    self._previous_online[state.device_id] = state.online

                # Publish readings when online
                if state.online and state.pv is not None and state.sv is not None:
                    self._mqtt.publish_measurement(state.device_id, state.pv)
                    self._mqtt.publish_setpoint(state.device_id, state.sv)

            self._stop_event.wait(self._config.polling_interval)

    def _handle_set_setpoint(self, device_id: int, value: float) -> None:
        """Handle an incoming MQTT set_setpoint command.

        :param device_id: Target device ID.
        :param value: Desired setpoint temperature.
        """
        device = self._devices.get(device_id)
        if device is None:
            logger.warning("set_setpoint for unknown device %s", device_id)
            return

        if not device.state.online:
            logger.warning(
                "Device %s is offline, ignoring set_setpoint", device_id
            )
            return

        if device.write_sv(value):
            self._mqtt.publish_setpoint(device_id, value)
