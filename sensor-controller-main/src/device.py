"""Sensor device communication over Modbus RTU / RS-485 using pymodbus."""

import asyncio
import logging
import struct
from pathlib import Path
from typing import List, Optional

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException

from src.config import SerialConfig, TimingConfig

logger = logging.getLogger("sensor.device")

# HG803 register map — 0.1 resolution (function code 0x03)
# 0x0004: temperature (signed int16, /10), 0x0005: humidity (unsigned int16, /10)
# 0x0007: sensor status
REG_INT16_DIV10_START = 0x0004
REG_INT16_DIV10_COUNT = 4

# HG803 register map — 0.01 resolution (function code 0x03)
# 0x0000: temperature (signed int16, /100), 0x0001: humidity (unsigned int16, /100)
# 0x0003: sensor status
REG_INT16_DIV100_START = 0x0000
REG_INT16_DIV100_COUNT = 4

# HG803 hold registers (function code 0x06)
# Address change: write to 0x0100 using broadcast address 0x00
# Baud rate: write to 0x0101
REG_ADDRESS = 0x0100
REG_BAUD_RATE = 0x0101
# Broadcast address used when changing device address
BROADCAST_ADDRESS = 0x00


class SerialConnection:
    """Manages async Modbus RTU serial connection with exponential backoff.

    :param serial_config: Serial port configuration.
    :param timing_config: Timing configuration for timeouts.
    """

    def __init__(self, serial_config: SerialConfig, timing_config: TimingConfig) -> None:
        self._serial_config = serial_config
        self._timing_config = timing_config
        self._client: Optional[AsyncModbusSerialClient] = None
        self._backoff: float = 1.0
        self._max_backoff: float = 60.0

    @property
    def connected(self) -> bool:
        """Whether the serial client is connected."""
        return self._client is not None and self._client.connected

    async def connect(self, stop_event: asyncio.Event) -> bool:
        """Attempt serial connection with port pre-check and exponential backoff.

        :param stop_event: Event checked during backoff for clean shutdown.
        :returns: True if connection succeeded, False if interrupted.
        """
        port_path = Path(self._serial_config.port)
        if not port_path.exists():
            logger.warning("Serial port %s does not exist, backing off %.1fs",
                           self._serial_config.port, self._backoff)
            interrupted = await self._interruptible_sleep(self._backoff, stop_event)
            if interrupted:
                return False
            self._backoff = min(self._backoff * 2, self._max_backoff)
            return False

        try:
            self._client = AsyncModbusSerialClient(
                port=self._serial_config.port,
                baudrate=self._serial_config.baudrate,
                timeout=self._timing_config.sensor_timeout,
                parity="N",
                stopbits=1,
                bytesize=8,
            )
            connected = await self._client.connect()
            if connected:
                logger.info("Serial port %s connected", self._serial_config.port)
                self._backoff = 1.0
                return True
            else:
                logger.warning("Serial connection to %s failed", self._serial_config.port)
                self._client = None
                return False
        except Exception:
            logger.exception("Serial connection error on %s", self._serial_config.port)
            self._client = None
            interrupted = await self._interruptible_sleep(self._backoff, stop_event)
            if interrupted:
                return False
            self._backoff = min(self._backoff * 2, self._max_backoff)
            return False

    async def disconnect(self) -> None:
        """Close the serial connection gracefully."""
        if self._client is not None:
            self._client.close()
            self._client = None
            logger.info("Serial port disconnected")

    async def read_registers(
        self, address: int, register_start: int, count: int
    ) -> Optional[List[int]]:
        """Read holding registers from a Modbus slave.

        :param address: Modbus slave address.
        :param register_start: Starting register number.
        :param count: Number of registers to read.
        :returns: List of register values, or None on error.
        """
        if not self.connected:
            return None

        try:
            result = await self._client.read_holding_registers(
                address=register_start,
                count=count,
                device_id=address,
            )
            if result.isError():
                logger.debug("Modbus read error from slave %s: %s", address, result)
                return None
            return result.registers
        except ModbusException:
            logger.exception("Modbus exception reading slave %s", address)
            return None
        except Exception:
            logger.exception("Unexpected error reading slave %s", address)
            await self.disconnect()
            return None

    async def write_register(
        self, address: int, register: int, value: int
    ) -> bool:
        """Write a single holding register on a Modbus slave.

        :param address: Modbus slave address.
        :param register: Target register number.
        :param value: Value to write.
        :returns: True if write succeeded.
        """
        if not self.connected:
            return False

        try:
            result = await self._client.write_register(
                address=register,
                value=value,
                device_id=address,
            )
            if result.isError():
                logger.warning("Modbus write error on slave %s register 0x%04X: %s",
                               address, register, result)
                return False
            return True
        except ModbusException:
            logger.exception("Modbus exception writing slave %s", address)
            return False
        except Exception:
            logger.exception("Unexpected error writing slave %s", address)
            await self.disconnect()
            return False

    @staticmethod
    async def _interruptible_sleep(
        seconds: float, stop_event: asyncio.Event
    ) -> bool:
        """Sleep that can be interrupted by the stop event.

        :param seconds: Duration to sleep.
        :param stop_event: Event that interrupts the sleep.
        :returns: True if interrupted (stop requested), False if completed.
        """
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False


def parse_int16_div10(registers: List[int]) -> tuple[float, float]:
    """Parse HG803 0.1-resolution register values into temperature and humidity.

    Registers at 0x0004 (temp) and 0x0005 (humidity) are magnified 10x.
    Temperature is signed int16, humidity is unsigned int16.

    :param registers: List of at least 2 register values.
    :returns: Tuple of (temperature in Celsius, humidity in %RH).
    :raises ValueError: If fewer than 2 registers provided.
    """
    if len(registers) < 2:
        raise ValueError("INT16_DIV10 requires at least 2 registers, got %d" % len(registers))

    raw_bytes = struct.pack(">H", registers[0])
    temp_raw = struct.unpack(">h", raw_bytes)[0]
    hum_raw = registers[1]
    return temp_raw / 10.0, hum_raw / 10.0


def parse_int16_div100(registers: List[int]) -> tuple[float, float]:
    """Parse HG803 0.01-resolution register values into temperature and humidity.

    Registers at 0x0000 (temp) and 0x0001 (humidity) are magnified 100x.
    Temperature is signed int16, humidity is unsigned int16.

    :param registers: List of at least 2 register values.
    :returns: Tuple of (temperature in Celsius, humidity in %RH).
    :raises ValueError: If fewer than 2 registers provided.
    """
    if len(registers) < 2:
        raise ValueError("INT16_DIV100 requires at least 2 registers, got %d" % len(registers))

    raw_bytes = struct.pack(">H", registers[0])
    temp_raw = struct.unpack(">h", raw_bytes)[0]
    hum_raw = registers[1]
    return temp_raw / 100.0, hum_raw / 100.0
