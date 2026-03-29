"""Modbus RTU relay device communication.

Wraps :class:`pymodbus.client.ModbusSerialClient` to provide a high-level
interface for controlling Waveshare Modbus RTU 16CH relay modules connected
via an RS-485 converter.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

from .config import AppConfig, ModbusConfig, RelayModuleConfig, relay_to_modbus

logger = logging.getLogger("relay.device")


@dataclass
class ModuleState:
    """Runtime state tracker for a single relay module.

    :param modbus_address: The Modbus device address.
    :param channel_count: Number of relay channels on this module.
    :param online: Whether the module is reachable.
    :param consecutive_errors: Running count of consecutive communication errors.
    :param relay_states: Last known state of each channel (``None`` if unknown).
    """

    modbus_address: int
    channel_count: int
    online: bool = True
    consecutive_errors: int = 0
    relay_states: dict[int, bool | None] = field(default_factory=dict)


class RelayDevice:
    """High-level interface to Modbus RTU relay modules on an RS-485 bus.

    All public methods are thread-safe — a :class:`threading.Lock` serialises
    access to the underlying serial connection.

    :param config: Application configuration.
    """

    def __init__(self, config: AppConfig) -> None:
        self._modbus_config: ModbusConfig = config.modbus
        self._modules_config: list[RelayModuleConfig] = config.relay_modules
        self._max_errors: int = config.max_consecutive_errors
        self._client: ModbusSerialClient | None = None
        self._lock = threading.Lock()

        self._module_states: dict[int, ModuleState] = {}
        for mod in self._modules_config:
            self._module_states[mod.modbus_address] = ModuleState(
                modbus_address=mod.modbus_address,
                channel_count=mod.channel_count,
            )

    def connect(self) -> bool:
        """Open the serial connection to the RS-485 bus.

        Checks that the serial port device exists before attempting
        to connect.

        :returns: ``True`` if the connection was established.
        :rtype: bool
        """
        port = self._modbus_config.port
        if not os.path.exists(port):
            logger.error("Serial port %s does not exist", port)
            return False

        cfg = self._modbus_config
        self._client = ModbusSerialClient(
            port=cfg.port,
            framer=FramerType.RTU,
            baudrate=cfg.baudrate,
            bytesize=cfg.bytesize,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            timeout=cfg.timeout,
            retries=cfg.retries,
        )

        if not self._client.connect():
            logger.error("Failed to connect to Modbus serial port %s", port)
            self._client = None
            return False

        logger.info("Connected to Modbus serial port %s", port)
        return True

    def disconnect(self) -> None:
        """Close the serial connection."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
                logger.info("Disconnected from Modbus serial port")

    def set_relay(self, relay_number: int, state: bool) -> bool:
        """Set a single relay ON or OFF.

        :param relay_number: Flat relay number (1-based).
        :param state: ``True`` for ON, ``False`` for OFF.
        :returns: ``True`` if the command succeeded.
        :rtype: bool
        :raises ValueError: If the relay number is out of range.
        """
        modbus_addr, coil_addr = relay_to_modbus(
            relay_number, self._modules_config
        )

        with self._lock:
            if self._client is None:
                logger.error("Cannot set relay %d: not connected", relay_number)
                return False

            module = self._module_states[modbus_addr]
            if not module.online:
                logger.warning(
                    "Module at address %d is offline, attempting anyway",
                    modbus_addr,
                )

            try:
                response = self._client.write_coil(
                    address=coil_addr,
                    value=state,
                    device_id=modbus_addr,
                )
                if response.isError():
                    self._record_error(module)
                    logger.error(
                        "Error setting relay %d (addr=%d, coil=%d): %s",
                        relay_number,
                        modbus_addr,
                        coil_addr,
                        response,
                    )
                    return False

                self._record_success(module)
                module.relay_states[relay_number] = state
                logger.info(
                    "Relay %d set to %s",
                    relay_number,
                    "ON" if state else "OFF",
                )
                return True

            except ModbusException as exc:
                self._record_error(module)
                logger.error(
                    "Modbus exception setting relay %d: %s", relay_number, exc
                )
                return False

    def get_relay(self, relay_number: int) -> bool | None:
        """Read the current state of a single relay.

        :param relay_number: Flat relay number (1-based).
        :returns: ``True`` if ON, ``False`` if OFF, or ``None`` on error.
        :rtype: bool | None
        :raises ValueError: If the relay number is out of range.
        """
        modbus_addr, coil_addr = relay_to_modbus(
            relay_number, self._modules_config
        )

        with self._lock:
            if self._client is None:
                logger.error(
                    "Cannot read relay %d: not connected", relay_number
                )
                return None

            module = self._module_states[modbus_addr]

            try:
                response = self._client.read_coils(
                    address=coil_addr,
                    count=1,
                    device_id=modbus_addr,
                )
                if response.isError():
                    self._record_error(module)
                    logger.error(
                        "Error reading relay %d: %s", relay_number, response
                    )
                    return None

                self._record_success(module)
                state = response.bits[0]
                module.relay_states[relay_number] = state
                return state

            except ModbusException as exc:
                self._record_error(module)
                logger.error(
                    "Modbus exception reading relay %d: %s", relay_number, exc
                )
                return None

    def get_all_relays(self) -> dict[int, bool | None]:
        """Read the state of every relay across all modules.

        Reads each module's full coil range in a single bulk request.

        :returns: Mapping of relay number to state (``None`` on read error).
        :rtype: dict[int, bool | None]
        """
        states: dict[int, bool | None] = {}
        relay_offset = 1

        with self._lock:
            if self._client is None:
                logger.error("Cannot read relays: not connected")
                total = sum(m.channel_count for m in self._modules_config)
                return {i: None for i in range(1, total + 1)}

            for mod_cfg in self._modules_config:
                module = self._module_states[mod_cfg.modbus_address]
                try:
                    response = self._client.read_coils(
                        address=0,
                        count=mod_cfg.channel_count,
                        device_id=mod_cfg.modbus_address,
                    )
                    if response.isError():
                        self._record_error(module)
                        logger.error(
                            "Error reading module at address %d: %s",
                            mod_cfg.modbus_address,
                            response,
                        )
                        for ch in range(mod_cfg.channel_count):
                            states[relay_offset + ch] = None
                    else:
                        self._record_success(module)
                        for ch in range(mod_cfg.channel_count):
                            state = response.bits[ch]
                            relay_num = relay_offset + ch
                            states[relay_num] = state
                            module.relay_states[relay_num] = state

                except ModbusException as exc:
                    self._record_error(module)
                    logger.error(
                        "Modbus exception reading module at address %d: %s",
                        mod_cfg.modbus_address,
                        exc,
                    )
                    for ch in range(mod_cfg.channel_count):
                        states[relay_offset + ch] = None

                relay_offset += mod_cfg.channel_count

        return states

    def is_module_online(self, modbus_address: int) -> bool:
        """Check whether a module is considered online.

        :param modbus_address: The Modbus device address of the module.
        :returns: ``True`` if the module has not exceeded the error threshold.
        :rtype: bool
        """
        module = self._module_states.get(modbus_address)
        return module.online if module else False

    def _record_error(self, module: ModuleState) -> None:
        """Increment the error counter and mark offline if threshold reached.

        :param module: The module state to update.
        """
        module.consecutive_errors += 1
        if module.consecutive_errors >= self._max_errors and module.online:
            module.online = False
            logger.error(
                "Module at address %d marked OFFLINE after %d consecutive errors",
                module.modbus_address,
                module.consecutive_errors,
            )

    def _record_success(self, module: ModuleState) -> None:
        """Reset the error counter and mark the module online.

        :param module: The module state to update.
        """
        if not module.online:
            logger.info(
                "Module at address %d is back ONLINE", module.modbus_address
            )
        module.consecutive_errors = 0
        module.online = True
