"""Modbus RTU communication layer using *modbus-tk*.

Provides :class:`SerialConnection` — a thin wrapper around an RTU master that
handles connection lifecycle, exponential-backoff reconnection, and HG803
register parsing.

All public methods are **synchronous** (the gateway manager calls them via
``asyncio.to_thread``).
"""

import logging
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import modbus_tk.defines as cst
import modbus_tk.modbus_rtu as modbus_rtu
import serial
from modbus_tk.exceptions import ModbusInvalidResponseError

from src.config import SensorType, SerialConfig, TimingConfig

logger = logging.getLogger("sensor.device")

# ---------------------------------------------------------------------------
# HG803 register map (temperature / humidity / dew-point)
# ---------------------------------------------------------------------------
# 0.1 resolution block (÷ 10)
REG_TEMP_DIV10 = 0x0004
REG_HUM_DIV10 = 0x0005
REG_DEW_DIV10 = 0x0006
REG_STATUS_DIV10 = 0x0007

# Configuration registers (hold registers — function code 0x06)
REG_DEVICE_ADDRESS = 0x0100
REG_BAUD_RATE = 0x0101

# ---------------------------------------------------------------------------
# QDW90A-G register map (pressure transmitter)
# ---------------------------------------------------------------------------
# Two holding registers encoding pressure as an IEEE-754 big-endian float32.
REG_PRESSURE_FLOAT32 = 0x0016

# Backoff parameters
_BACKOFF_BASE = 1.0
_BACKOFF_MULTIPLIER = 2.0
_BACKOFF_CAP = 60.0


@dataclass
class SensorReading:
    """Parsed HG803 measurement.

    :param temperature: Temperature in °C.
    :param humidity: Relative humidity in %RH.
    :param dew_point: Dew-point temperature in °C.
    :param status: Raw status register (0 = no fault).
    """

    temperature: float
    humidity: float
    dew_point: float
    status: int


@dataclass
class PressureReading:
    """Parsed QDW90A-G pressure measurement.

    :param pressure: Pressure in bar.
    """

    pressure: float


class SerialConnection:
    """Modbus RTU master with automatic reconnection.

    :param serial_config: Serial port parameters.
    :param timing_config: Timeout / backoff values.
    """

    def __init__(
        self,
        serial_config: SerialConfig,
        timing_config: TimingConfig,
    ) -> None:
        self._config = serial_config
        self._timing = timing_config
        self._master: modbus_rtu.RtuMaster | None = None
        self._lock = threading.Lock()
        self._backoff = _BACKOFF_BASE

    # -- properties ----------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Return ``True`` when the underlying serial port is open."""
        return self._master is not None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and create an RTU master.

        :raises ConnectionError: If the serial device node does not exist.
        :raises serial.SerialException: If the port cannot be opened.
        """
        port_path = Path(self._config.port)
        if not port_path.exists():
            raise ConnectionError("Serial port not found: %s" % self._config.port)

        ser = serial.Serial(
            port=self._config.port,
            baudrate=self._config.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self._timing.sensor_timeout,
            inter_byte_timeout=self._timing.byte_timeout,
        )
        self._master = modbus_rtu.RtuMaster(ser)
        self._master.set_timeout(self._timing.sensor_timeout)
        self._backoff = _BACKOFF_BASE
        logger.info(
            "Connected to %s at %s baud",
            self._config.port,
            self._config.baudrate,
        )

    def disconnect(self) -> None:
        """Close the serial port gracefully."""
        if self._master is not None:
            try:
                self._master.close()
            except Exception:
                pass
            self._master = None
            logger.info("Serial port disconnected")

    def reconnect(self) -> None:
        """Disconnect, wait with exponential backoff, then reconnect.

        The backoff doubles on every call (1 s → 2 s → … → 60 s cap) and
        resets to 1 s on a successful :meth:`connect`.
        """
        self.disconnect()
        logger.warning("Reconnecting in %.1fs …", self._backoff)
        time.sleep(self._backoff)
        self._backoff = min(self._backoff * _BACKOFF_MULTIPLIER, _BACKOFF_CAP)
        self.connect()

    # -- sensor I/O ----------------------------------------------------------

    def read_sensor(
        self, address: int, sensor_type: SensorType,
    ) -> SensorReading | PressureReading:
        """Read a measurement from one sensor.

        :param address: Modbus slave address (1-247).
        :param sensor_type: Sensor category — selects the register block
            and decode path.
        :returns: :class:`SensorReading` for temperature/humidity sensors,
            :class:`PressureReading` for pressure transmitters.
        :raises ConnectionError: If not connected.
        :raises ValueError: If *sensor_type* is unsupported.
        :raises modbus_tk.exceptions.ModbusError: On protocol-level failure.
        :rtype: SensorReading | PressureReading
        """
        if self._master is None:
            raise ConnectionError("Not connected to serial port")

        with self._lock:
            if sensor_type is SensorType.TEMPERATURE_HUMIDITY:
                regs = self._master.execute(
                    address,
                    cst.READ_HOLDING_REGISTERS,
                    REG_TEMP_DIV10,
                    4,
                )
                return _parse_div10(regs)
            if sensor_type is SensorType.PRESSURE:
                regs = self._master.execute(
                    address,
                    cst.READ_HOLDING_REGISTERS,
                    REG_PRESSURE_FLOAT32,
                    2,
                )
                return _parse_float32(regs)
            raise ValueError("Unsupported sensor type: %s" % sensor_type)

    def write_address(self, current_address: int, new_address: int) -> None:
        """Change one sensor's Modbus address.

        Writes :data:`REG_DEVICE_ADDRESS` on the sensor currently at
        *current_address*.  Other sensors on the bus are unaffected.

        :param current_address: The sensor's current Modbus address (1-247).
        :param new_address: Target address (1-247).
        :raises ConnectionError: If not connected.
        :raises ValueError: If either address is out of range.
        """
        if not 1 <= current_address <= 247:
            raise ValueError(
                "Current address must be 1-247, got %s" % current_address
            )
        if not 1 <= new_address <= 247:
            raise ValueError(
                "New address must be 1-247, got %s" % new_address
            )
        if self._master is None:
            raise ConnectionError("Not connected to serial port")

        with self._lock:
            try:
                self._master.execute(
                    current_address,
                    cst.WRITE_SINGLE_REGISTER,
                    REG_DEVICE_ADDRESS,
                    output_value=new_address,
                )
            except ModbusInvalidResponseError as exc:
                # HG803 replies from the NEW address after applying the
                # change, which modbus_tk reports as a request/response
                # slave mismatch.  That specific mismatch is the success
                # signal — any other invalid-response (timeout, CRC) is
                # a real failure and must propagate.
                if "different from request address" not in str(exc):
                    raise
        logger.info(
            "Wrote new address %s to sensor at %s",
            new_address,
            current_address,
        )


# ---------------------------------------------------------------------------
# Internal register parsing
# ---------------------------------------------------------------------------

def _to_signed16(raw: int) -> int:
    """Convert an unsigned 16-bit value to signed (two's complement)."""
    return raw - 0x10000 if raw > 0x7FFF else raw


def _parse_div10(regs: tuple) -> SensorReading:
    """Parse the 0.1-resolution register block (0x0004-0x0007).

    :param regs: Four-element tuple ``(temp, humidity, dew_point, status)``.
    :returns: :class:`SensorReading` with values divided by 10.
    :rtype: SensorReading
    """
    return SensorReading(
        temperature=_to_signed16(regs[0]) / 10.0,
        humidity=regs[1] / 10.0,
        dew_point=_to_signed16(regs[2]) / 10.0,
        status=regs[3],
    )


def _parse_float32(regs: tuple) -> PressureReading:
    """Parse a two-register IEEE-754 big-endian float32.

    :param regs: Two-element tuple as returned by
        :meth:`modbus_tk.modbus_rtu.RtuMaster.execute` — ``regs[0]`` holds
        the high word, ``regs[1]`` the low word.
    :returns: :class:`PressureReading` with pressure in bar.
    :rtype: PressureReading
    """
    raw = struct.pack(">HH", regs[0], regs[1])
    (pressure,) = struct.unpack(">f", raw)
    return PressureReading(pressure=pressure)
