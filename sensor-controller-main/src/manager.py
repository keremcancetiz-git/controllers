"""Gateway orchestrator — poll loops, watchdog, and sensor-management commands.

:class:`GatewayManager` ties together the Modbus device layer and the MQTT
handler, runs them as concurrent ``asyncio`` tasks (one poll task per RS-485
bus plus MQTT and watchdog), and monitors health via a watchdog timer.

Sensor addresses are assumed to be globally unique across all buses.
"""

import asyncio
import json
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

import serial

from src.config import BusConfig, GatewayConfig, SensorConfig, SensorType
from src.device import PressureReading, SerialConnection, SensorReading
from src.mqtt_handler import MqttHandler

logger = logging.getLogger("sensor.manager")

_MAX_CONSECUTIVE_ERRORS = 3


@dataclass
class SensorState:
    """Mutable runtime state for one sensor.

    :param online: Whether the sensor is considered reachable.
    :param consecutive_errors: Number of consecutive read failures.
    """

    online: bool = True
    consecutive_errors: int = 0


class GatewayManager:
    """Top-level coordinator.

    Responsibilities:

    * Continuously poll every configured sensor across every configured
      RS-485 bus, running one poll task per bus in parallel.
    * Publish temperature, humidity, and dew-point readings to MQTT.
    * Reconnect each serial port independently, with exponential backoff.
    * Mark sensors offline after ``_MAX_CONSECUTIVE_ERRORS`` failures.
    * Provide a watchdog that forces shutdown when no successful I/O
      has happened on any bus for ``watchdog_timeout`` seconds.
    * Accept sensor add / remove / update commands from MQTT and enter
      per-bus maintenance mode while performing them.

    :param config: Fully-loaded :class:`GatewayConfig`.
    """

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._mqtt = MqttHandler(config.mqtt, config.topics)
        self._devices: dict[str, SerialConnection] = {
            bus.id: SerialConnection(bus.serial, config.timing)
            for bus in config.buses
        }
        self._sensor_states: dict[int, SensorState] = {}
        self._address_to_bus: dict[int, str] = {}
        for bus in config.buses:
            for sensor in bus.sensors:
                self._sensor_states[sensor.address] = SensorState()
                self._address_to_bus[sensor.address] = bus.id
        self._running = True
        self._maintenance: dict[str, bool] = {
            bus.id: False for bus in config.buses
        }
        self._last_healthy = time.monotonic()
        self._tasks: list[asyncio.Task] = []

    # -- public entry point --------------------------------------------------

    async def run(self) -> None:
        """Start all gateway tasks and block until shutdown.

        Installs POSIX signal handlers for ``SIGTERM`` / ``SIGINT`` so that
        ``systemctl stop`` triggers a clean exit.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_shutdown)

        self._mqtt.on_message(self._handle_mqtt_message)

        self._tasks = [
            asyncio.create_task(self._mqtt.run(), name="mqtt"),
            asyncio.create_task(self._watchdog(), name="watchdog"),
        ]
        for bus in self._config.buses:
            self._tasks.append(
                asyncio.create_task(
                    self._poll_loop(bus.id),
                    name="poll-%s" % bus.id,
                )
            )

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for device in self._devices.values():
                await asyncio.to_thread(device.disconnect)
            logger.info("Gateway stopped")

    def request_shutdown(self) -> None:
        """Signal all tasks to stop.  Safe to call from a signal handler."""
        logger.info("Shutdown requested")
        self._running = False
        for task in self._tasks:
            task.cancel()

    # -- poll loop -----------------------------------------------------------

    async def _poll_loop(self, bus_id: str) -> None:
        """Continuously poll sensors on one bus and publish readings."""
        device = self._devices[bus_id]
        while self._running:
            if self._maintenance[bus_id]:
                await asyncio.sleep(0.5)
                continue

            if not device.connected:
                if not await self._try_connect(bus_id):
                    continue

            await self._poll_bus(bus_id)

    async def _try_connect(self, bus_id: str) -> bool:
        """Attempt to (re)connect the serial port for one bus.

        :param bus_id: Bus identifier.
        :returns: ``True`` on success.
        :rtype: bool
        """
        device = self._devices[bus_id]
        try:
            if device.connected:
                await asyncio.to_thread(device.reconnect)
            else:
                await asyncio.to_thread(device.connect)
            return True
        except (ConnectionError, serial.SerialException, OSError) as exc:
            logger.error("Serial connection failed on %s: %s", bus_id, exc)
            await asyncio.sleep(1)
            return False

    async def _poll_bus(self, bus_id: str) -> None:
        """Run one polling cycle across every sensor on *bus_id*."""
        bus = self._get_bus(bus_id)
        if bus is None or not bus.sensors:
            # Avoid tight-spinning when the bus has no sensors configured;
            # an empty for-loop below would return without awaiting.
            await asyncio.sleep(self._config.timing.inter_sensor_delay)
            return

        device = self._devices[bus_id]
        for sensor in bus.sensors:
            if not self._running or self._maintenance[bus_id]:
                break
            try:
                await self._poll_sensor(bus_id, sensor)
            except (serial.SerialException, OSError) as exc:
                logger.error(
                    "Serial error during poll on %s: %s", bus_id, exc,
                )
                await asyncio.to_thread(device.disconnect)
                break
            await asyncio.sleep(self._config.timing.inter_sensor_delay)

    async def _poll_sensor(
        self,
        bus_id: str,
        sensor: SensorConfig,
    ) -> None:
        """Read one sensor and publish its data.

        Modbus protocol errors are counted against the sensor.  Serial-level
        errors (port gone) are re-raised so the bus's poll loop can
        reconnect.
        """
        state = self._sensor_states.get(sensor.address)
        if state is None:
            # Sensor was removed between the poll-loop snapshot and now.
            return

        device = self._devices[bus_id]
        try:
            reading: SensorReading | PressureReading = await asyncio.to_thread(
                device.read_sensor,
                sensor.address,
                sensor.type,
            )
        except (serial.SerialException, OSError):
            raise
        except Exception as exc:
            state.consecutive_errors += 1
            if (
                state.consecutive_errors >= _MAX_CONSECUTIVE_ERRORS
                and state.online
            ):
                state.online = False
                logger.error(
                    "Sensor %s (%s) offline after %s consecutive errors: %s",
                    sensor.address,
                    sensor.name,
                    state.consecutive_errors,
                    exc,
                )
            elif state.consecutive_errors < _MAX_CONSECUTIVE_ERRORS:
                logger.warning(
                    "Sensor %s (%s) error %s/%s: %s",
                    sensor.address,
                    sensor.name,
                    state.consecutive_errors,
                    _MAX_CONSECUTIVE_ERRORS,
                    exc,
                )
            return

        state.consecutive_errors = 0
        if not state.online:
            state.online = True
            logger.info(
                "Sensor %s (%s) back online",
                sensor.address,
                sensor.name,
            )

        self._last_healthy = time.monotonic()
        await self._publish_reading(sensor, reading)

    async def _publish_reading(
        self,
        sensor: SensorConfig,
        reading: SensorReading | PressureReading,
    ) -> None:
        """Fan-out a reading into one MQTT measurement topic per value."""
        common = dict(sensor_name=sensor.name, location=sensor.location)

        if isinstance(reading, PressureReading):
            await self._mqtt.publish_measurement(
                "pressure",
                sensor.address,
                reading.pressure,
                "bar",
                **common,
            )
            return

        await self._mqtt.publish_measurement(
            "temperature",
            sensor.address,
            reading.temperature,
            "°C",
            **common,
        )
        await self._mqtt.publish_measurement(
            "humidity",
            sensor.address,
            reading.humidity,
            "%RH",
            **common,
        )
        await self._mqtt.publish_measurement(
            "dew_point",
            sensor.address,
            reading.dew_point,
            "°C",
            **common,
        )

    # -- watchdog ------------------------------------------------------------

    async def _watchdog(self) -> None:
        """Force shutdown when no healthy I/O occurs within the timeout."""
        timeout = self._config.timing.watchdog_timeout

        while self._running:
            await asyncio.sleep(5)
            elapsed = time.monotonic() - self._last_healthy
            in_maintenance = any(self._maintenance.values())
            effective_timeout = timeout + 10 if in_maintenance else timeout

            if elapsed > effective_timeout:
                logger.critical(
                    "Watchdog timeout — no healthy activity for %.0fs",
                    elapsed,
                )
                self.request_shutdown()
                return

    # -- sensor commands (via MQTT) ------------------------------------------

    def _get_bus(self, bus_id: str) -> BusConfig | None:
        """Look up a bus in the current config by id."""
        for bus in self._config.buses:
            if bus.id == bus_id:
                return bus
        return None

    def _replace_bus_sensors(
        self,
        bus_id: str,
        new_sensors: list[SensorConfig],
    ) -> None:
        """Swap the sensor list for one bus, producing a new config."""
        new_buses = [
            replace(bus, sensors=new_sensors) if bus.id == bus_id else bus
            for bus in self._config.buses
        ]
        self._config = replace(self._config, buses=new_buses)

    async def _handle_mqtt_message(self, message) -> None:
        """Dispatch incoming MQTT messages by topic."""
        topic = str(message.topic)
        handlers: dict[str, Callable[[bytes], Awaitable[None]]] = {
            self._mqtt.build_topic(
                self._config.topics.sensor_update_request,
            ): self._handle_update_sensor,
            self._mqtt.build_topic(
                self._config.topics.sensor_add_request,
            ): self._handle_add_sensor,
            self._mqtt.build_topic(
                self._config.topics.sensor_remove_request,
            ): self._handle_remove_sensor,
        }
        handler = handlers.get(topic)
        if handler is not None:
            await handler(message.payload)

    async def _handle_update_sensor(self, raw_payload: bytes) -> None:
        """Process an update-sensor command.

        Expected JSON payload::

            {
                "address": 13,
                "new_address": 20,
                "name": "New name",
                "location": "New location",
                "type": "temperature_humidity"
            }

        Only ``address`` is required — it identifies the sensor to
        update.  Any combination of the other fields may be supplied.
        When ``new_address`` differs from ``address`` the gateway writes
        the new address to the sensor over Modbus (entering maintenance
        mode on that bus for the duration).  Metadata-only updates skip
        the hardware write.  Sensors stay on the same bus — moving a
        sensor between buses requires a remove + add.
        """
        try:
            payload = json.loads(raw_payload)
            address = int(payload["address"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error("Invalid update-sensor payload: %s", exc)
            await self._mqtt.publish_sensor_command_result(
                False, "update", 0, "Invalid payload: %s" % exc,
            )
            return

        if address not in self._sensor_states:
            msg = "No sensor configured at address %s" % address
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "update", address, msg,
            )
            return

        bus_id = self._address_to_bus[address]

        if self._maintenance[bus_id]:
            msg = "Another command is already in progress on %s" % bus_id
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "update", address, msg,
            )
            return

        new_address = address
        try:
            updates: dict[str, object] = {}
            if "new_address" in payload:
                new_addr = int(payload["new_address"])
                if not 1 <= new_addr <= 247:
                    raise ValueError("new_address must be 1-247")
                if new_addr != address and new_addr in self._sensor_states:
                    raise ValueError(
                        "new_address %s is already in use" % new_addr
                    )
                if new_addr != address:
                    updates["address"] = new_addr
                    new_address = new_addr
            if "name" in payload:
                updates["name"] = str(payload["name"])
            if "location" in payload:
                updates["location"] = str(payload["location"])
            if "type" in payload:
                updates["type"] = SensorType(payload["type"])
        except (ValueError, TypeError) as exc:
            msg = "Invalid update fields: %s" % exc
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "update", address, msg,
            )
            return

        if not updates:
            msg = "No updateable fields supplied"
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "update", address, msg,
            )
            return

        change_address = new_address != address

        logger.info(
            "Update sensor %s on %s: %s",
            address,
            bus_id,
            sorted(updates.keys()),
        )
        self._maintenance[bus_id] = True

        try:
            if change_address:
                await asyncio.sleep(self._config.timing.maintenance_settle)
                device = self._devices[bus_id]
                await asyncio.to_thread(
                    device.write_address,
                    address,
                    new_address,
                )
                await asyncio.sleep(
                    self._config.timing.address_change_stabilize,
                )

            bus = self._get_bus(bus_id)
            assert bus is not None
            new_sensors = sorted(
                [
                    replace(s, **updates) if s.address == address else s
                    for s in bus.sensors
                ],
                key=lambda s: s.address,
            )
            self._replace_bus_sensors(bus_id, new_sensors)
            self._config.save("config.json")

            if change_address:
                self._sensor_states[new_address] = self._sensor_states.pop(
                    address,
                )
                self._address_to_bus[new_address] = self._address_to_bus.pop(
                    address,
                )

            self._last_healthy = time.monotonic()

            await self._mqtt.publish_sensor_command_result(
                True, "update", new_address, "Sensor updated",
            )
            logger.info(
                "Sensor %s updated (address now %s)",
                address,
                new_address,
            )

        except Exception as exc:
            logger.error("Update sensor failed: %s", exc)
            await self._mqtt.publish_sensor_command_result(
                False, "update", new_address, "Failed: %s" % exc,
            )
        finally:
            self._maintenance[bus_id] = False

    async def _handle_add_sensor(self, raw_payload: bytes) -> None:
        """Process an add-sensor command.

        Expected JSON payload::

            {
                "bus_id": "bus0",
                "address": 21,
                "name": "Floor_3_probe",
                "location": "Third_floor",
                "type": "temperature_humidity"
            }

        All fields are required.  The new sensor is appended to the
        specified bus in config, persisted to disk, and picked up on
        that bus's next poll cycle.  No hardware interaction — the
        physical sensor must already be on the bus at *address*.
        """
        try:
            payload = json.loads(raw_payload)
            bus_id = str(payload["bus_id"])
            address = int(payload["address"])
            name = str(payload["name"])
            location = str(payload["location"])
            sensor_type = SensorType(payload["type"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error("Invalid add-sensor payload: %s", exc)
            await self._mqtt.publish_sensor_command_result(
                False, "add", 0, "Invalid payload: %s" % exc,
            )
            return

        if bus_id not in self._devices:
            msg = "Unknown bus_id: %s" % bus_id
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "add", address, msg,
            )
            return

        if not 1 <= address <= 247:
            msg = "Address must be 1-247, got %s" % address
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "add", address, msg,
            )
            return

        if address in self._sensor_states:
            msg = "Address %s is already configured" % address
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "add", address, msg,
            )
            return

        if self._maintenance[bus_id]:
            msg = "Another command is already in progress on %s" % bus_id
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "add", address, msg,
            )
            return

        logger.info("Adding sensor %s on %s", address, bus_id)
        self._maintenance[bus_id] = True

        try:
            new_sensor = SensorConfig(
                address=address,
                name=name,
                location=location,
                type=sensor_type,
            )
            bus = self._get_bus(bus_id)
            assert bus is not None
            new_sensors = sorted(
                [*bus.sensors, new_sensor],
                key=lambda s: s.address,
            )
            self._replace_bus_sensors(bus_id, new_sensors)
            self._config.save("config.json")
            self._sensor_states[address] = SensorState()
            self._address_to_bus[address] = bus_id
            self._last_healthy = time.monotonic()

            await self._mqtt.publish_sensor_command_result(
                True, "add", address, "Sensor added",
            )
            logger.info("Sensor %s added on %s", address, bus_id)

        except Exception as exc:
            logger.error("Add sensor failed: %s", exc)
            await self._mqtt.publish_sensor_command_result(
                False, "add", address, "Failed: %s" % exc,
            )
        finally:
            self._maintenance[bus_id] = False

    async def _handle_remove_sensor(self, raw_payload: bytes) -> None:
        """Process a remove-sensor command.

        Expected JSON payload::

            {"address": 21}

        The sensor is removed from the config and runtime state.  The
        physical sensor on the bus is unaffected — detach it manually
        if desired.
        """
        try:
            payload = json.loads(raw_payload)
            address = int(payload["address"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.error("Invalid remove-sensor payload: %s", exc)
            await self._mqtt.publish_sensor_command_result(
                False, "remove", 0, "Invalid payload: %s" % exc,
            )
            return

        if address not in self._sensor_states:
            msg = "No sensor configured at address %s" % address
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "remove", address, msg,
            )
            return

        bus_id = self._address_to_bus[address]

        if self._maintenance[bus_id]:
            msg = "Another command is already in progress on %s" % bus_id
            logger.error(msg)
            await self._mqtt.publish_sensor_command_result(
                False, "remove", address, msg,
            )
            return

        logger.info("Removing sensor %s from %s", address, bus_id)
        self._maintenance[bus_id] = True

        try:
            bus = self._get_bus(bus_id)
            assert bus is not None
            new_sensors = [
                s for s in bus.sensors if s.address != address
            ]
            self._replace_bus_sensors(bus_id, new_sensors)
            self._config.save("config.json")
            self._sensor_states.pop(address, None)
            self._address_to_bus.pop(address, None)
            self._last_healthy = time.monotonic()

            await self._mqtt.publish_sensor_command_result(
                True, "remove", address, "Sensor removed",
            )
            logger.info("Sensor %s removed from %s", address, bus_id)

        except Exception as exc:
            logger.error("Remove sensor failed: %s", exc)
            await self._mqtt.publish_sensor_command_result(
                False, "remove", address, "Failed: %s" % exc,
            )
        finally:
            self._maintenance[bus_id] = False
