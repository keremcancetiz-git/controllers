"""Gateway manager orchestrating sensor polling, watchdog, and maintenance."""

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from src.config import GatewayConfig, SensorConfig
from src.device import (
    SerialConnection,
    parse_int16_div10,
    parse_int16_div100,
    BROADCAST_ADDRESS,
    REG_ADDRESS,
    REG_INT16_DIV10_COUNT,
    REG_INT16_DIV10_START,
    REG_INT16_DIV100_COUNT,
    REG_INT16_DIV100_START,
)
from src.mqtt_handler import MqttHandler

logger = logging.getLogger("sensor.manager")


@dataclass
class SensorState:
    """Mutable runtime state for a sensor.

    :param config: Static sensor configuration.
    :param consecutive_errors: Count of consecutive read failures.
    :param online: Whether the sensor is considered reachable.
    """

    config: SensorConfig
    consecutive_errors: int = 0
    online: bool = True


class GatewayManager:
    """Main gateway orchestrator managing poll loop, watchdog, and maintenance.

    :param config: Full gateway configuration.
    """

    MAX_CONSECUTIVE_ERRORS = 3

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._maintenance_mode = False
        self._last_healthy_time = time.time()

        self._sensors: Dict[int, SensorState] = {
            addr: SensorState(config=cfg)
            for addr, cfg in config.sensors.items()
        }

        self._serial = SerialConnection(config.serial, config.timing)
        self._mqtt = MqttHandler(config.mqtt, config.topics)

    async def run(self) -> None:
        """Entry point: set up signal handlers, MQTT subscriptions, start tasks.

        Runs poll_loop, watchdog, and MQTT listener concurrently.
        Shuts down gracefully on SIGTERM/SIGINT.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop_event.set)

        # Register MQTT handlers
        addr_change_topic = self._mqtt._address_change_request_topic()
        self._mqtt.register_subscription(addr_change_topic, self._handle_address_change)

        # Register Scan and Config handlers
        self._mqtt.register_subscription(self._mqtt._scan_request_topic(), self._handle_scan_request)
        self._mqtt.register_subscription(self._mqtt._config_update_topic(), self._handle_config_update)

        logger.info("Starting gateway with %s sensors", len(self._sensors))

        tasks = [
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._watchdog()),
            asyncio.create_task(self._mqtt.run(self._stop_event)),
        ]

        try:
            # Wait until shutdown is requested
            await self._stop_event.wait()
        finally:
            # Cancel all tasks (mqtt message loop won't exit on its own)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._shutdown()

    async def _poll_loop(self) -> None:
        """Main sensor polling loop.

        Ensures serial connection, skips cycles during maintenance,
        polls each sensor sequentially, and publishes measurements.
        """
        while not self._stop_event.is_set():
            # Ensure serial connection
            if not self._serial.connected:
                connected = await self._serial.connect(self._stop_event)
                if not connected:
                    continue

            # Skip cycle during maintenance
            if self._maintenance_mode:
                if await self._interruptible_sleep(0.5):
                    return
                continue

            # Poll all sensors
            if self._sensors:
                for addr, state in list(self._sensors.items()):
                    if self._stop_event.is_set() or self._maintenance_mode:
                        break
                    await self._poll_sensor(addr, state)
                    await asyncio.sleep(self._config.timing.inter_sensor_delay)

                if not self._maintenance_mode:
                    self._last_healthy_time = time.time()
            else:
                self._last_healthy_time = time.time()

            if await self._interruptible_sleep(0.5):
                return

    async def _poll_sensor(self, addr: int, state: SensorState) -> None:
        """Poll a single sensor and publish results.

        Builds the Modbus read command based on sensor format,
        parses the response, and publishes to MQTT.

        :param addr: Modbus slave address.
        :param state: Mutable sensor state.
        """
        cfg = state.config

        if cfg.format == "INT16_DIV10":
            reg_start = REG_INT16_DIV10_START
            reg_count = REG_INT16_DIV10_COUNT
        else:
            reg_start = REG_INT16_DIV100_START
            reg_count = REG_INT16_DIV100_COUNT

        async with self._lock:
            registers = await self._serial.read_registers(addr, reg_start, reg_count)

        if registers is None:
            state.consecutive_errors += 1
            if state.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS and state.online:
                state.online = False
                logger.warning("Sensor %s (%s) marked offline after %s errors",
                               addr, cfg.name, state.consecutive_errors)
            return

        # Successful read — reset error count
        state.consecutive_errors = 0
        if not state.online:
            state.online = True
            logger.info("Sensor %s (%s) back online", addr, cfg.name)

        timestamp = time.time() * 1000
        sensor_id = str(addr)

        try:
            if cfg.format == "INT16_DIV10":
                temp, humidity = parse_int16_div10(registers)
            else:
                temp, humidity = parse_int16_div100(registers)

            logger.info("Sensor %s (%s): temp=%.1f°C, humidity=%.1f%%RH",
                        addr, cfg.name, temp, humidity)

            await self._mqtt.publish_measurement(
                "temperature", sensor_id,
                {"name": cfg.name, "location": cfg.location,
                 "measurement": temp, "unit": "°C", "timestamp": timestamp},
            )
            await self._mqtt.publish_measurement(
                "humidity", sensor_id,
                {"name": cfg.name, "location": cfg.location,
                 "measurement": humidity, "unit": "%RH", "timestamp": timestamp},
            )
        except ValueError:
            logger.exception("Failed to parse data from sensor %s (%s)", addr, cfg.name)

    async def _watchdog(self) -> None:
        """Watchdog task: triggers shutdown if no healthy activity within timeout.

        Extends timeout by 10s during maintenance mode.
        Checks every 5 seconds.
        """
        while not self._stop_event.is_set():
            if await self._interruptible_sleep(5.0):
                return

            limit = self._config.timing.watchdog_timeout
            if self._maintenance_mode:
                limit += 10.0

            elapsed = time.time() - self._last_healthy_time
            if elapsed > limit:
                logger.critical("Watchdog triggered: no healthy activity for %.1fs", elapsed)
                self._stop_event.set()

    async def _handle_address_change(self, topic: str, payload: dict) -> None:
        """Handle MQTT address change request.

        Validates payload, enters maintenance mode, executes two-step
        address change sequence, and publishes result.

        :param topic: The MQTT topic that triggered this handler.
        :param payload: Dict expected to contain ``old_addr`` and ``new_addr``.
        """
        # Validate payload
        try:
            old_addr = int(payload["old_addr"])
            new_addr = int(payload["new_addr"])
        except (KeyError, ValueError, TypeError):
            logger.warning("Invalid address change payload: %s", payload)
            await self._mqtt.publish_address_change_result(
                {"success": False, "error": "payload must contain integer old_addr and new_addr"}
            )
            return

        if not (1 <= old_addr <= 247 and 1 <= new_addr <= 247):
            logger.warning("Address out of range: old=%s new=%s", old_addr, new_addr)
            await self._mqtt.publish_address_change_result(
                {"success": False, "error": "addresses must be in range 1-247"}
            )
            return

        logger.info("Entering maintenance mode for address change %s -> %s",
                     old_addr, new_addr)
        self._maintenance_mode = True

        # Wait for any active poll to finish and bus to settle
        await asyncio.sleep(self._config.timing.maintenance_settle)

        async with self._lock:
            try:
                if not self._serial.connected:
                    logger.error("Serial disconnected during address change")
                    await self._mqtt.publish_address_change_result(
                        {"success": False, "error": "serial disconnected"}
                    )
                    return

                # HG803: write new address to register 0x0100 using broadcast
                # address 0x00 (per datasheet p.35, the address code of the
                # downlink packet is fixed at 0x00 when setting address)
                logger.info("Writing new address %s via broadcast to register 0x%04X",
                            new_addr, REG_ADDRESS)
                success = await self._serial.write_register(
                    BROADCAST_ADDRESS, REG_ADDRESS, new_addr
                )

                if success:
                    logger.info("Address change to %s complete (takes effect immediately)",
                                new_addr)
                    await self._mqtt.publish_address_change_result(
                        {"success": True, "old": old_addr, "new": new_addr}
                    )
                else:
                    logger.warning("Address change to %s may have failed", new_addr)
                    await self._mqtt.publish_address_change_result(
                        {"success": False, "old": old_addr, "new": new_addr,
                         "error": "write register returned error"}
                    )

                # Wait for sensor stabilization after address change
                logger.info("Waiting %.1fs for sensor stabilization",
                            self._config.timing.address_change_stabilize)
                await asyncio.sleep(self._config.timing.address_change_stabilize)

                self._last_healthy_time = time.time()

            except Exception:
                logger.exception("Address change failed")
                await self._mqtt.publish_address_change_result(
                    {"success": False, "error": "unexpected error during address change"}
                )

            finally:
                logger.info("Exiting maintenance mode")
                self._maintenance_mode = False

    async def _handle_scan_request(self, topic: str, payload: dict) -> None:
        """Scan Modbus addresses 1-247 for responding sensors."""
        logger.info("Starting Modbus address scan (1-247)...")
        self._maintenance_mode = True
        await asyncio.sleep(self._config.timing.maintenance_settle)

        found_addresses = []
        
        async with self._lock:
            for addr in range(1, 248):
                if self._stop_event.is_set():
                    break
                
                # Use a single register read as a probe (0x0001 is common for HG803)
                # We'll use the same register as in poll_sensor but with single count
                reg_start = REG_INT16_DIV10_START
                registers = await self._serial.read_registers(addr, reg_start, 1)
                
                if registers is not None:
                    logger.info("  -> Found sensor at address %s", addr)
                    found_addresses.append(addr)
                
                # Minimal delay between scan steps
                await asyncio.sleep(0.01)

        logger.info("Scan complete. Found %s sensors: %s", len(found_addresses), found_addresses)
        await self._mqtt.publish(
            self._mqtt._scan_results_topic(),
            {"success": True, "addresses": found_addresses, "timestamp": time.time() * 1000}
        )
        
        self._maintenance_mode = False

    async def _handle_config_update(self, topic: str, payload: dict) -> None:
        """Update sensor configuration and persist to config.json."""
        # payload expected: { "address": 1, "name": "...", "location": "...", "format": "...", "type": "..." }
        try:
            addr = int(payload["address"])
            name = payload.get("name", f"Sensor {addr}")
            location = payload.get("location", "unknown")
            fmt = payload.get("format", "INT16_DIV10")
            stype = payload.get("type", "temperature")
            
            logger.info("Updating config for sensor %s: %s at %s", addr, name, location)
            
            # 1. Update persistent config.json
            config_path = Path("config.json")
            with open(config_path, "r") as f:
                raw_config = json.load(f)
            
            if "sensors" not in raw_config:
                raw_config["sensors"] = {}
                
            raw_config["sensors"][str(addr)] = {
                "name": name,
                "location": location,
                "format": fmt,
                "type": stype
            }
            
            with open(config_path, "w") as f:
                json.dump(raw_config, f, indent=2)
            
            # 2. Update internal runtime state
            # Since SensorConfig is frozen, we create a new one
            new_sensor_config = SensorConfig(
                address=addr,
                name=name,
                location=location,
                format=fmt,
                type=stype
            )
            
            if addr in self._sensors:
                self._sensors[addr].config = new_sensor_config
            else:
                self._sensors[addr] = SensorState(config=new_sensor_config)
                
            await self._mqtt.publish(topic + "/status", {"success": True, "address": addr})
            logger.info("Successfully updated and persisted sensor %s", addr)

        except Exception as e:
            logger.error("Failed to update config: %s", e)
            await self._mqtt.publish(topic + "/status", {"success": False, "error": str(e)})

    async def _shutdown(self) -> None:
        """Graceful shutdown: disconnect serial and log."""
        logger.info("Shutting down gateway")
        await self._serial.disconnect()

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep that can be interrupted by the stop event.

        :param seconds: Duration to sleep.
        :returns: True if interrupted (stop requested), False if completed.
        """
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False
