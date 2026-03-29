"""Relay manager — orchestration layer between MQTT and Modbus device.

Bridges :class:`~src.mqtt_handler.MqttHandler` and
:class:`~src.device.RelayDevice`, enforcing blocked-relay rules and
dispatching commands through a thread-safe queue.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from .config import AppConfig
from .device import RelayDevice
from .mqtt_handler import MqttHandler

logger = logging.getLogger("relay.manager")

_SENTINEL = object()
"""Poison pill to signal the command loop to exit."""


@dataclass
class RelayCommand:
    """A pending relay command received from MQTT.

    :param relay_number: Flat relay number (1-based).
    :param state: ``True`` for ON, ``False`` for OFF.
    """

    relay_number: int
    state: bool


class RelayManager:
    """Coordinates MQTT communication with Modbus relay hardware.

    Commands arrive from MQTT on a background thread and are placed into
    a :class:`queue.Queue`.  The main thread processes them sequentially,
    keeping Modbus serial access single-threaded.

    :param config: Application configuration.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._command_queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()

        self._device = RelayDevice(config)
        self._mqtt = MqttHandler(config.mqtt, on_command=self._enqueue_command)

    def start(self) -> None:
        """Connect to hardware and MQTT broker, then enter the main loop.

        This method blocks until :meth:`stop` is called.  It connects the
        Modbus device and MQTT client, publishes initial relay states, and
        then processes incoming commands.

        :raises RuntimeError: If the Modbus device cannot be connected.
        """
        if not self._device.connect():
            raise RuntimeError("Failed to connect to relay device")

        if not self._mqtt.connect():
            self._device.disconnect()
            raise RuntimeError("Failed to connect to MQTT broker")

        self._publish_all_states()

        # Periodic status polling in a background thread
        poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="status-poll"
        )
        poll_thread.start()

        logger.info("Relay manager started — processing commands")
        self._process_commands()

    def stop(self) -> None:
        """Signal shutdown and clean up resources.

        Safe to call from a signal handler on the main thread.
        """
        logger.info("Relay manager shutting down")
        self._shutdown.set()
        # Wake up the command loop
        self._command_queue.put(_SENTINEL)

    def _enqueue_command(self, relay_number: int, state: bool) -> None:
        """Callback wired to :class:`MqttHandler` — enqueues a command.

        Called from the paho-mqtt network thread; must not block.

        :param relay_number: Flat relay number (1-based).
        :param state: Desired relay state.
        """
        self._command_queue.put(RelayCommand(relay_number, state))

    def _process_commands(self) -> None:
        """Main loop: drain the command queue until shutdown.

        Each command is validated against the blocked-relay list,
        executed on the Modbus device, and the result is published
        back via MQTT.
        """
        while not self._shutdown.is_set():
            try:
                item = self._command_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is _SENTINEL:
                break

            cmd: RelayCommand = item
            self._handle_command(cmd)

        self._mqtt.disconnect()
        self._device.disconnect()
        logger.info("Relay manager stopped")

    def _handle_command(self, cmd: RelayCommand) -> None:
        """Execute a single relay command with safety checks.

        :param cmd: The relay command to execute.
        """
        relay = cmd.relay_number
        total = self._config.total_relays

        # Range check
        if relay < 1 or relay > total:
            logger.warning("Relay number %d out of range (1–%d)", relay, total)
            self._mqtt.publish_error(
                relay, "relay number out of range (1–%d)" % total
            )
            return

        # Blocked relay check
        if relay in self._config.blocked_relays:
            logger.warning("Relay %d is blocked by configuration", relay)
            self._mqtt.publish_error(
                relay, "relay %d is blocked by configuration" % relay
            )
            return

        # Execute on hardware
        success = self._device.set_relay(relay, cmd.state)
        if success:
            self._mqtt.publish_state(relay, cmd.state)
        else:
            self._mqtt.publish_error(
                relay, "failed to set relay %d" % relay
            )

    def _publish_all_states(self) -> None:
        """Read and publish the state of every relay.

        Used at startup and periodically to synchronise MQTT retained
        messages with actual hardware state.
        """
        states = self._device.get_all_relays()
        for relay_number, state in states.items():
            if state is not None:
                self._mqtt.publish_state(relay_number, state)

    def _poll_loop(self) -> None:
        """Periodically read all relay states and publish them.

        Runs in a daemon thread until shutdown is signalled.
        """
        interval = self._config.status_poll_interval
        while not self._shutdown.wait(timeout=interval):
            logger.debug("Polling relay states")
            self._publish_all_states()
