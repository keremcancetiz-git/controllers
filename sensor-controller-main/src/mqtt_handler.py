"""Async MQTT client with Last Will, reconnection, and topic management.

Wraps :mod:`aiomqtt` and enforces the project's topic naming convention::

    {base_topic}/{sensor_type}/{sensor_id}/{command}
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiomqtt

from src.config import MqttConfig, TopicConfig

logger = logging.getLogger("sensor.mqtt")

MessageCallback = Callable[[aiomqtt.Message], Awaitable[None]]

_RECONNECT_BASE = 1.0
_RECONNECT_CAP = 60.0


class MqttHandler:
    """Manages the MQTT lifecycle: connect, subscribe, publish, reconnect.

    :param mqtt_config: Broker connection settings.
    :param topic_config: Topic suffix definitions.
    """

    def __init__(
        self,
        mqtt_config: MqttConfig,
        topic_config: TopicConfig,
    ) -> None:
        self._config = mqtt_config
        self._topics = topic_config
        self._client: aiomqtt.Client | None = None
        self._message_callback: MessageCallback | None = None
        self._connected = asyncio.Event()

    # -- public helpers ------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return ``True`` when the broker connection is alive."""
        return self._client is not None

    def build_topic(self, *parts: str) -> str:
        """Build a full topic string from path segments.

        :param parts: One or more path segments appended to ``base_topic``.
        :returns: Slash-joined topic string.
        :rtype: str
        """
        return "/".join((self._config.base_topic, *parts))

    def on_message(self, callback: MessageCallback) -> None:
        """Register a coroutine to handle every incoming MQTT message.

        :param callback: Async callable receiving an :class:`aiomqtt.Message`.
        """
        self._message_callback = callback

    async def wait_connected(self) -> None:
        """Block until the first successful broker connection."""
        await self._connected.wait()

    # -- connection loop -----------------------------------------------------

    async def run(self) -> None:
        """Connect to the broker and listen for messages, reconnecting on failure.

        This coroutine **never returns** under normal operation.  It keeps
        reconnecting with exponential backoff (1 s → 2 s → … → 60 s cap).
        """
        backoff = _RECONNECT_BASE
        while True:
            try:
                await self._session(backoff)
                backoff = _RECONNECT_BASE  # reset after clean session
            except aiomqtt.MqttError as exc:
                self._client = None
                self._connected.clear()
                logger.warning(
                    "MQTT connection lost: %s — reconnecting in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_CAP)
            except asyncio.CancelledError:
                await self._publish_offline_best_effort()
                raise

    # -- publishing ----------------------------------------------------------

    async def publish(
        self,
        topic: str,
        payload: str,
        *,
        retain: bool = False,
    ) -> None:
        """Publish a message.  Silently drops when disconnected.

        :param topic: Full MQTT topic.
        :param payload: Message payload (typically JSON).
        :param retain: Whether the broker should retain the message.
        """
        if self._client is None:
            return
        try:
            await self._client.publish(topic, payload, retain=retain)
        except aiomqtt.MqttError:
            logger.debug("Publish to %s dropped (disconnected)", topic)

    async def publish_measurement(
        self,
        sensor_type: str,
        sensor_id: int,
        value: float,
        unit: str,
        *,
        sensor_name: str,
        location: str,
    ) -> None:
        """Publish a single measurement value.

        :param sensor_type: E.g. ``"temperature"`` or ``"humidity"``.
        :param sensor_id: Modbus address of the sensor.
        :param value: Measured value.
        :param unit: Unit string (``"°C"``, ``"%RH"``).
        :param sensor_name: Human-readable sensor name.
        :param location: Physical location description.
        """
        topic = self.build_topic(sensor_type, str(sensor_id), "measurement")
        payload = json.dumps(
            {
                "value": value,
                "unit": unit,
                "name": sensor_name,
                "location": location,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await self.publish(topic, payload)

    async def publish_sensor_command_result(
        self,
        success: bool,
        command: str,
        address: int,
        detail: str,
    ) -> None:
        """Publish the outcome of an add/remove-sensor command.

        :param success: Whether the operation succeeded.
        :param command: ``"add"`` or ``"remove"``.
        :param address: The target sensor address.
        :param detail: Human-readable result message.
        """
        topic = self.build_topic(self._topics.sensor_command_status)
        payload = json.dumps(
            {
                "success": success,
                "command": command,
                "address": address,
                "detail": detail,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await self.publish(topic, payload)

    # -- internals -----------------------------------------------------------

    async def _session(self, backoff: float) -> None:
        """Run a single MQTT session (connect → subscribe → listen)."""
        status_topic = self.build_topic(self._topics.status)

        will = aiomqtt.Will(
            topic=status_topic,
            payload="offline",
            retain=True,
        )

        async with aiomqtt.Client(
            hostname=self._config.broker,
            port=self._config.port,
            will=will,
        ) as client:
            self._client = client

            await client.publish(status_topic, "online", retain=True)

            for suffix in (
                self._topics.sensor_update_request,
                self._topics.sensor_add_request,
                self._topics.sensor_remove_request,
            ):
                await client.subscribe(self.build_topic(suffix))

            logger.info(
                "MQTT connected to %s:%s",
                self._config.broker,
                self._config.port,
            )
            self._connected.set()

            async for message in client.messages:
                if self._message_callback is not None:
                    try:
                        await self._message_callback(message)
                    except Exception:
                        logger.exception("Error in MQTT message callback")

    async def _publish_offline_best_effort(self) -> None:
        """Try to publish an offline status on shutdown."""
        if self._client is None:
            return
        try:
            topic = self.build_topic(self._topics.status)
            await self._client.publish(topic, "offline", retain=True)
        except Exception:
            pass
