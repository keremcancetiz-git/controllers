"""Bronkhorst MFC device driver.

Handles serial communication with a single Bronkhorst Mass Flow
Controller via the *propar* library.  Provides connection management,
parameter reading (fast / slow), setpoint writing with gas-correction,
and setpoint ramping.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import propar
import serial

from src.config import AppConfig

logger = logging.getLogger("mfc.device")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTER_READ_DELAY: float = 0.015
"""Seconds between sequential register reads."""

RETRY_DELAY: float = 0.2
"""Seconds between retry attempts."""

DEFAULT_DEVICE_ADDRESS: int = 128
"""Bronkhorst default RS-232 device address."""

MAX_CONSECUTIVE_ERRORS: int = 3
"""Consecutive errors before marking a device offline."""

MAX_RETRIES: int = 3
"""Maximum retries per connection / config-read attempt."""


class DDE(IntEnum):
    """Bronkhorst Direct Digital Exchange parameter addresses."""

    CONTROL_MODE = 12
    CAPACITY_FULL = 21
    FLUID_NAME = 25
    VALVE_OUTPUT = 55
    SERIAL_NUMBER = 92
    USER_TAG = 115
    CAPACITY_UNIT = 129
    TEMPERATURE = 142
    FMEASURE = 205
    FSETPOINT = 206


class ControlMode(IntEnum):
    """Known control-mode values."""

    RS232_ONLY = 18


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class MFCData:
    """Run-time state for one MFC device.

    :param name: Human-readable device name (e.g. ``MFC1``).
    :param port: Serial-port path (may be a symlink).
    :param online: ``True`` while the device responds to reads.
    :param raw_fmeasure: Last raw measured-flow percentage from DDE 205.
    :param raw_fsetpoint: Last raw setpoint percentage from DDE 206.
    :param valve_output: Valve output value (DDE 55).
    :param temperature: Device temperature (DDE 142).
    :param control_mode: Active control mode (DDE 12).
    :param native_fluid: Fluid name reported by the device.
    :param native_unit: Capacity-unit string reported by the device.
    :param native_factor: Gas correction factor for the native fluid.
    :param unit_factor: Multiplier to convert native unit to base
        (ml/min).
    :param capacity_full_native: 100 % capacity in native units.
    :param target_gas: Currently selected target gas name.
    :param target_factor: Gas correction factor for the target gas.
    :param target_setpoint: Desired setpoint (after gas correction,
        in base units).
    :param current_setpoint: Ramped setpoint being written to HW.
    :param last_ramp_time: Timestamp of last ramp step.
    :param consecutive_errors: Errors in a row (reset on success).
    :param total_errors: Lifetime error counter.
    :param successful_reads: Lifetime success counter.
    :param last_read_time: Epoch of last successful read.
    :param last_error_time: Epoch of last error.
    :param last_error: Human-readable description of the last error.
    """

    name: str = ""
    port: str = ""
    online: bool = False
    raw_fmeasure: float = 0.0
    raw_fsetpoint: float = 0.0
    valve_output: int | None = None
    temperature: float | None = None
    control_mode: int = 0
    native_fluid: str = "N2"
    native_unit: str = ""
    native_factor: float = 1.0
    unit_factor: float = 1.0
    capacity_full_native: float = 100.0
    target_gas: str = "Native"
    target_factor: float = 1.0
    target_setpoint: float = 0.0
    current_setpoint: float = 0.0
    last_ramp_time: float = field(default_factory=time.time)
    consecutive_errors: int = 0
    total_errors: int = 0
    successful_reads: int = 0
    last_read_time: float = 0.0
    last_error_time: float = 0.0
    last_error: str = ""
    _pressure_units: list[str] = field(default_factory=list, repr=False)

    @property
    def is_pressure_device(self) -> bool:
        """Return ``True`` if native unit indicates a pressure device.

        :returns: Whether gas correction should be skipped.
        """
        clean = self.native_unit.lower().replace("(a)", "a").replace("(g)", "g")
        return any(u in clean for u in self._pressure_units)

    @property
    def correction_ratio(self) -> float:
        """Gas correction ratio (target / native).

        :returns: 1.0 for pressure devices or when native_factor is 0.
        """
        if self.is_pressure_device or self.native_factor == 0:
            return 1.0
        return self.target_factor / self.native_factor

    @property
    def fmeasure_corrected(self) -> float:
        """Measured flow in base units, gas-corrected.

        :returns: Flow in ml/min (or equivalent base unit).
        """
        return self.raw_fmeasure * self.unit_factor * self.correction_ratio

    @property
    def capacity_corrected(self) -> float:
        """Full-scale capacity in base units, gas-corrected.

        :returns: Capacity in ml/min (or equivalent base unit).
        """
        return self.capacity_full_native * self.unit_factor * self.correction_ratio

    def to_mqtt_dict(self) -> dict[str, Any]:
        """Serialize state for MQTT publishing.

        :returns: Dictionary suitable for ``json.dumps``.
        """
        display_unit = self.native_unit
        if not self.is_pressure_device:
            display_unit = "ml/min"

        return {
            "online": self.online,
            "timestamp": int(time.time() * 1000),
            "fmeasure": round(self.fmeasure_corrected, 3),
            "fsetpoint": round(self.current_setpoint, 3),
            "capacity_full": round(self.capacity_corrected, 3),
            "valve_output": self.valve_output,
            "temperature": self.temperature,
            "control_mode": self.control_mode,
            "units": display_unit,
            "fluid_name": (
                self.target_gas if self.target_gas != "Native"
                else self.native_fluid
            ),
            "target_setpoint": self.target_setpoint,
            "native_fluid": self.native_fluid,
            "successful_reads": self.successful_reads,
            "total_errors": self.total_errors,
            "last_error": self.last_error if not self.online else "",
        }


# ---------------------------------------------------------------------------
# Device driver
# ---------------------------------------------------------------------------

class MFCDevice:
    """Driver for a single Bronkhorst MFC.

    :param name: Logical device name (e.g. ``MFC1``).
    :param port: Serial-port path (may be a udev symlink).
    :param config: Application configuration (provides gas factors,
        unit multipliers, pressure-unit list, and timing constants).
    """

    def __init__(self, name: str, port: str, config: AppConfig) -> None:
        self.name = name
        self.port = port
        self._config = config
        self.instrument: propar.instrument | None = None
        self.data = MFCData(
            name=name,
            port=port,
            _pressure_units=config.pressure_units,
        )
        self.data_lock = threading.Lock()
        self._connection_attempts = 0

    # -- helpers -------------------------------------------------------------

    def _cleanup(self) -> None:
        """Close the underlying serial connection if open."""
        if self.instrument:
            try:
                if hasattr(self.instrument, "master") and self.instrument.master:
                    self.instrument.master.close()
                elif hasattr(self.instrument, "serial") and self.instrument.serial:
                    self.instrument.serial.close()
            except Exception:
                pass
            self.instrument = None

    @staticmethod
    def _test_serial_port(port: str, baudrate: int) -> tuple[bool, str]:
        """Verify that the OS can open *port*.

        :param port: Path to the serial device.
        :param baudrate: Baud rate for the test open.
        :returns: ``(ok, message)`` tuple.
        """
        try:
            real = os.path.realpath(port)
            if not os.path.exists(real):
                return False, f"Port {real} does not exist"
            s = serial.Serial(real, baudrate, timeout=0.5)
            s.close()
            return True, "OK"
        except serial.SerialException as exc:
            return False, f"Serial error: {exc}"
        except Exception as exc:
            return False, f"Error: {exc}"

    def _flush_port(self) -> None:
        """Drain any stale bytes from the serial buffer."""
        try:
            real = os.path.realpath(self.port)
            s = serial.Serial(
                real, self._config.system.serial_baudrate, timeout=0.1,
            )
            s.reset_input_buffer()
            s.reset_output_buffer()
            s.close()
            time.sleep(0.05)
        except Exception:
            pass

    # -- discovery -----------------------------------------------------------

    @staticmethod
    def scan_port(port: str, baudrate: int) -> str | None:
        """Connect to *port*, read the user tag, and disconnect.

        Used during auto-discovery to identify which MFC is on which
        serial port.  The connection is torn down after reading so
        that the normal :meth:`connect` flow can re-establish it.

        :param port: Serial port path (e.g. ``/dev/ttyUSB0``).
        :param baudrate: Baud rate for the connection.
        :returns: User tag string, or ``None`` if no MFC was found.
        """
        try:
            ok, msg = MFCDevice._test_serial_port(port, baudrate)
            if not ok:
                return None

            inst = propar.instrument(
                port, address=DEFAULT_DEVICE_ADDRESS, baudrate=baudrate,
            )
            tag = None
            for _ in range(MAX_RETRIES):
                tag = inst.readParameter(DDE.USER_TAG)
                if tag is not None:
                    break
                time.sleep(RETRY_DELAY)

            # Clean up the temporary connection
            try:
                if hasattr(inst, "master") and inst.master:
                    inst.master.close()
                elif hasattr(inst, "serial") and inst.serial:
                    inst.serial.close()
            except Exception:
                pass

            if tag:
                return str(tag).strip()
            return None
        except Exception:
            return None

    # -- connection ----------------------------------------------------------

    def connect(self) -> bool:
        """Open a connection to the physical MFC.

        Steps: OS-level serial test, flush, create propar instrument,
        test-read with retries, read device config,
        and set RS-232 control mode.

        :returns: ``True`` on success.
        """
        self._connection_attempts += 1
        baudrate = self._config.system.serial_baudrate

        try:
            self._cleanup()

            if not os.path.exists(self.port):
                raise ConnectionError(f"Port {self.port} does not exist")

            real_port = os.path.realpath(self.port)
            logger.debug("%s: Connecting via %s -> %s", self.name, self.port, real_port)

            ok, msg = self._test_serial_port(self.port, baudrate)
            if not ok:
                raise ConnectionError(f"Serial port test failed: {msg}")

            self._flush_port()
            time.sleep(0.1)

            self.instrument = propar.instrument(
                self.port, address=DEFAULT_DEVICE_ADDRESS, baudrate=baudrate,
            )

            # Test read with retries
            test_val = None
            for attempt in range(MAX_RETRIES):
                test_val = self.instrument.readParameter(DDE.FMEASURE)
                if test_val is not None:
                    break
                logger.debug(
                    "%s: Test read attempt %d returned None",
                    self.name, attempt + 1,
                )
                time.sleep(RETRY_DELAY)

            if test_val is None:
                raise ConnectionError(
                    f"Test read returned None after {MAX_RETRIES} attempts"
                )

            # Set RS-232 control
            self.instrument.writeParameter(DDE.CONTROL_MODE, ControlMode.RS232_ONLY)

            # Read device configuration (fluid, unit, capacity)
            if not self._read_device_config():
                raise ConnectionError("Failed to read device configuration")

            self.data.online = True
            self.data.consecutive_errors = 0
            self.data.last_error = ""
            self._connection_attempts = 0
            logger.info("%s: Connected (fmeasure=%.2f)", self.name, test_val)
            return True

        except Exception as exc:
            self._cleanup()
            self.data.online = False
            self.data.last_error = str(exc)
            self.data.last_error_time = time.time()
            self.data.total_errors += 1

            if self._connection_attempts >= MAX_CONSECUTIVE_ERRORS:
                logger.error(
                    "%s: Connection failed (attempt %d): %s",
                    self.name, self._connection_attempts, exc,
                )
            else:
                logger.warning("%s: Connection failed: %s", self.name, exc)

            return False

    def _read_device_config(self) -> bool:
        """Read native fluid, unit, and capacity from the device.

        Retries up to 3 times on transient failures.

        :returns: ``True`` if all parameters were read successfully.
        """
        for _ in range(MAX_RETRIES):
            try:
                p_fluid = self.instrument.readParameter(DDE.FLUID_NAME)
                p_unit = self.instrument.readParameter(DDE.CAPACITY_UNIT)
                p_cap = self.instrument.readParameter(DDE.CAPACITY_FULL)
                if p_fluid is None:
                    time.sleep(RETRY_DELAY)
                    continue

                fluid = str(p_fluid).strip()
                unit = str(p_unit).strip()
                cap = float(p_cap)

                self.data.native_fluid = fluid
                self.data.native_unit = unit
                self.data.capacity_full_native = cap
                self.data.native_factor = self._config.gases.get(
                    fluid.upper(), 1.0,
                )
                self.data.unit_factor = self._config.unit_multipliers.get(
                    unit.lower(), 1.0,
                )

                if self.data.target_gas == "Native":
                    self.data.target_factor = self.data.native_factor

                logger.info(
                    "%s: fluid=%s, unit=%s, capacity=%.1f",
                    self.name, fluid, unit, cap,
                )
                return True
            except Exception:
                time.sleep(RETRY_DELAY)
        return False

    def disconnect(self) -> None:
        """Cleanly close the connection and mark the device offline."""
        self._cleanup()
        self.data.online = False
        self.data.control_mode = 0

    def should_retry_connection(self) -> bool:
        """Check whether enough time has elapsed for a reconnect.

        Uses exponential back-off: *cooldown*, 2x, 4x, capped at 60 s.

        :returns: ``True`` if a reconnect attempt should be made.
        """
        if self.data.online:
            return False
        cooldown = self._config.system.reconnect_cooldown
        backoff = min(cooldown * (2 ** min(self._connection_attempts, 2)), 60)
        return time.time() - self.data.last_error_time >= backoff

    # -- reads ---------------------------------------------------------------

    def read_fast(self) -> bool:
        """Read high-frequency parameters (flow + setpoint).

        :returns: ``True`` on success.
        """
        if not self.instrument:
            return False

        try:
            fmeasure = self.instrument.readParameter(DDE.FMEASURE)
            time.sleep(INTER_READ_DELAY)
            fsetpoint = self.instrument.readParameter(DDE.FSETPOINT)

            if fmeasure is None or fsetpoint is None:
                raise ValueError(
                    f"Read returned None (fmeasure={fmeasure}, fsetpoint={fsetpoint})"
                )

            mode = self.instrument.readParameter(DDE.CONTROL_MODE)
            time.sleep(INTER_READ_DELAY)
            temperature = self.instrument.readParameter(DDE.TEMPERATURE)

            with self.data_lock:
                self.data.raw_fmeasure = float(fmeasure)
                self.data.raw_fsetpoint = float(fsetpoint)
                if mode is not None:
                    self.data.control_mode = int(mode)
                if temperature is not None:
                    self.data.temperature = float(temperature)
                self.data.last_read_time = time.time()
                self.data.consecutive_errors = 0
                self.data.successful_reads += 1
            return True

        except Exception as exc:
            with self.data_lock:
                self.data.consecutive_errors += 1
                self.data.total_errors += 1
                self.data.last_error = str(exc)

                if self.data.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.warning(
                        "%s: Offline after %d consecutive errors: %s",
                        self.name, self.data.consecutive_errors, exc,
                    )
                    self.data.online = False
                    self.data.last_error_time = time.time()
                    self._cleanup()
                else:
                    logger.debug(
                        "%s: Read error (%d/%d): %s",
                        self.name, self.data.consecutive_errors,
                        MAX_CONSECUTIVE_ERRORS, exc,
                    )
            return False

    def read_slow(self) -> bool:
        """Read low-frequency parameters (valve, temperature).

        Called once after connect; not repeated every cycle.

        :returns: ``True`` on success.
        """
        if not self.instrument:
            return False

        try:
            params = [
                ("valve_output", DDE.VALVE_OUTPUT),
                ("temperature", DDE.TEMPERATURE),
            ]
            for attr, dde in params:
                try:
                    value = self.instrument.readParameter(dde)
                    setattr(self.data, attr, value)
                    time.sleep(INTER_READ_DELAY)
                except Exception:
                    pass

            logger.info("%s: Slow parameters read", self.name)
            return True
        except Exception as exc:
            logger.warning("%s: Slow read failed: %s", self.name, exc)
            return False

    # -- writes / ramping ----------------------------------------------------

    def apply_ramp(self) -> None:
        """Advance the ramped setpoint toward *target_setpoint*.

        Called once per poll cycle.  The step size is limited by
        ``config.system.ramp_rate * dt``.
        """
        now = time.time()
        dt = now - self.data.last_ramp_time
        self.data.last_ramp_time = now
        max_step = self._config.system.ramp_rate * dt

        error = self.data.target_setpoint - self.data.current_setpoint
        if abs(error) < 0.001:
            return

        if abs(error) <= max_step:
            self.data.current_setpoint = self.data.target_setpoint
        else:
            self.data.current_setpoint += max_step * (1.0 if error > 0 else -1.0)

        self._write_hardware(self.data.current_setpoint)

    def _write_hardware(self, value: float) -> None:
        """Convert *value* from target-gas base units to native and write.

        :param value: Setpoint in base units (ml/min) for the target gas.
        """
        if not self.instrument or not self.data.online:
            return

        if self.data.is_pressure_device:
            cmd = value / self.data.unit_factor if self.data.unit_factor else value
        else:
            if self.data.target_factor == 0:
                return
            ratio = self.data.native_factor / self.data.target_factor
            cmd = (value * ratio) / self.data.unit_factor if self.data.unit_factor else value

        cmd = max(0.0, min(cmd, self.data.capacity_full_native))
        try:
            self.instrument.writeParameter(DDE.FSETPOINT, cmd)
        except Exception as exc:
            self.data.consecutive_errors += 1
            self.data.total_errors += 1
            self.data.last_error = str(exc)

            if self.data.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.warning(
                    "%s: Offline after %d consecutive write errors: %s",
                    self.name, self.data.consecutive_errors, exc,
                )
                self.data.online = False
                self.data.last_error_time = time.time()
                self._cleanup()
            else:
                logger.error("%s: Setpoint write failed: %s", self.name, exc)

    def set_target_setpoint(self, value: float) -> bool:
        """Set a new target setpoint (in corrected base units).

        :param value: Desired setpoint (>= 0).
        :returns: ``True`` if accepted.
        """
        if not self.data.online:
            logger.error("%s: Cannot set setpoint — not connected", self.name)
            return False

        self.data.target_setpoint = max(0.0, value)
        logger.info("%s: Target setpoint set to %.3f", self.name, self.data.target_setpoint)
        return True

    def set_target_gas(self, gas_name: str) -> bool:
        """Switch the target gas for flow correction.

        :param gas_name: Gas name (looked up in config gases).
        :returns: ``True`` if the gas was found in config.
        """
        key = gas_name.strip().upper()
        factor = self._config.gases.get(key)
        if factor is None:
            logger.warning("%s: Unknown gas '%s'", self.name, gas_name)
            return False

        self.data.target_gas = gas_name.strip()
        self.data.target_factor = factor
        logger.info("%s: Gas set to %s (factor=%.2f)", self.name, gas_name.strip(), factor)
        return True
