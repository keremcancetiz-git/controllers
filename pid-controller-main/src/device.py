import logging
import threading
from dataclasses import dataclass, field

from pymodbus.client import ModbusSerialClient

from .config import AppConfig, RegisterMap

logger = logging.getLogger("pid.device")


@dataclass
class DeviceState:
    """Current state of a single PID controller."""

    device_id: int
    modbus_address: int
    pv: float | None = None
    sv: float | None = None
    online: bool = False
    consecutive_errors: int = 0


class PidDevice:
    """Communicates with a single Kane TCN6-D PID controller over Modbus RTU.

    :param modbus_address: The Modbus slave address of the device.
    :param device_id: The logical device ID (modbus_address - offset).
    :param client: Shared ModbusSerialClient instance.
    :param registers: Register address map.
    :param value_scale: Scale factor for temperature values.
    :param max_consecutive_errors: Errors before marking offline.
    :param serial_lock: Shared lock for serial bus access.
    """

    def __init__(
        self,
        modbus_address: int,
        device_id: int,
        client: ModbusSerialClient,
        registers: RegisterMap,
        value_scale: int,
        max_consecutive_errors: int,
        serial_lock: threading.Lock,
    ) -> None:
        self._modbus_address = modbus_address
        self._device_id = device_id
        self._client = client
        self._registers = registers
        self._value_scale = value_scale
        self._max_errors = max_consecutive_errors
        self._serial_lock = serial_lock
        self.state = DeviceState(
            device_id=device_id, modbus_address=modbus_address
        )

    @property
    def device_id(self) -> int:
        """Return the logical device ID."""
        return self._device_id

    def read_pv(self) -> float | None:
        """Read the process value (current temperature).

        :returns: Temperature in degrees, or None on failure.
        """
        with self._serial_lock:
            result = self._client.read_holding_registers(
                address=self._registers.pv,
                count=1,
                device_id=self._modbus_address,
            )
        if result.isError():
            logger.warning(
                "Device %s: failed to read PV: %s", self._device_id, result
            )
            return None
        return result.registers[0] / self._value_scale

    def read_sv(self) -> float | None:
        """Read the current setpoint value.

        :returns: Setpoint temperature in degrees, or None on failure.
        """
        with self._serial_lock:
            result = self._client.read_holding_registers(
                address=self._registers.sv,
                count=1,
                device_id=self._modbus_address,
            )
        if result.isError():
            logger.warning(
                "Device %s: failed to read SV: %s", self._device_id, result
            )
            return None
        return result.registers[0] / self._value_scale

    def write_sv(self, value: float) -> bool:
        """Write a new setpoint value to the device.

        :param value: Target temperature in degrees (e.g. 25.0).
        :returns: True if the write succeeded.
        """
        raw = int(round(value * self._value_scale))
        with self._serial_lock:
            result = self._client.write_register(
                address=self._registers.sv,
                value=raw,
                device_id=self._modbus_address,
            )
        if result.isError():
            logger.error(
                "Device %s: failed to write SV=%s: %s",
                self._device_id,
                value,
                result,
            )
            return False
        logger.info("Device %s: SV set to %.1f", self._device_id, value)
        return True

    def poll(self) -> DeviceState:
        """Read PV and SV, update and return the device state.

        :returns: Updated device state.
        """
        pv = self.read_pv()
        sv = self.read_sv()

        if pv is None or sv is None:
            self.state.consecutive_errors += 1
            if (
                self.state.consecutive_errors >= self._max_errors
                and self.state.online
            ):
                logger.warning(
                    "Device %s: marked offline after %s consecutive errors",
                    self._device_id,
                    self.state.consecutive_errors,
                )
                self.state.online = False
        else:
            self.state.pv = pv
            self.state.sv = sv
            if self.state.consecutive_errors > 0:
                logger.info(
                    "Device %s: recovered after %s errors",
                    self._device_id,
                    self.state.consecutive_errors,
                )
            self.state.consecutive_errors = 0
            if not self.state.online:
                logger.info("Device %s: online", self._device_id)
            self.state.online = True

        return self.state
