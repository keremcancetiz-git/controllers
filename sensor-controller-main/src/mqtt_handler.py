"""MQTT client wrapper using aiomqtt for native async operation."""

import asyncio
import json
import logging
import socket
from typing import Any, Awaitable, Callable, Dict, Optional

import aiomqtt

from src.config import MqttConfig, TopicConfig

logger = logging.getLogger("sensor.mqtt")


class MqttHandler:
    """Async MQTT client with topic convention enforcement and LWT.

    Topic pattern: ``{base_topic}/{sensor_type}/{sensor_id}/{command}``

    :param mqtt_config: MQTT broker settings.
    :param topic_config: Topic path fragments.
    """

    def __init__(self, mqtt_config: MqttConfig, topic_config: TopicConfig) -> None:
        self._config = mqtt_config
        self._topics = topic_config
        self._client: Optional[aiomqtt.Client] = None
        self._subscriptions: Dict[str, Callable[[str, dict], Awaitable[None]]] = {}

    def build_topic(self, type_or_category: str, device_id: str, command: str) -> str:
        """Construct a full MQTT topic from components.

        :param type_or_category: Sensor type or category (e.g. ``temperature``).
        :param device_id: Sensor or device identifier (e.g. ``1``).
        :param command: Command suffix (e.g. ``measurement``).
        :returns: Full topic string like ``testbench/lowpower/temperature/1/measurement``.
        """
        return "%s/%s/%s/%s" % (self._config.base_topic, type_or_category, device_id, command)

    def _status_topic(self) -> str:
        """Build the gateway status topic for LWT.

        :returns: Full status topic string.
        """
        return "%s/%s" % (self._config.base_topic, self._topics.status)

    def _address_change_request_topic(self) -> str:
        """Build the address change request subscription topic (wildcard).

        :returns: Topic with wildcard for all address change requests.
        """
        return "%s/%s" % (self._config.base_topic, self._topics.address_change_request)

    def _address_change_status_topic(self) -> str:
        """Build the address change status publish topic.

        :returns: Full address change status topic.
        """
        return "%s/%s" % (self._config.base_topic, self._topics.address_change_status)

    def _scan_request_topic(self) -> str:
        """Build the scan request subscription topic.

        :returns: Full scan request topic.
        """
        return "%s/scan/request" % self._config.base_topic

    def _scan_results_topic(self) -> str:
        """Build the scan results publish topic.

        :returns: Full scan results topic.
        """
        return "%s/scan/results" % self._config.base_topic

    def _config_update_topic(self) -> str:
        """Build the config update subscription topic.

        :returns: Full config update topic.
        """
        return "%s/config/update" % self._config.base_topic

    @staticmethod
    def probe_broker(host: str, port: int, timeout: float = 5.0) -> bool:
        """Check broker availability via TCP socket probe.

        :param host: Broker hostname.
        :param port: Broker TCP port.
        :param timeout: Socket timeout in seconds.
        :returns: True if broker is reachable.
        """
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def register_subscription(
        self, topic: str, handler: Callable[[str, dict], Awaitable[None]]
    ) -> None:
        """Register an async handler for a topic pattern.

        Must be called before :meth:`run`.

        :param topic: MQTT topic pattern to subscribe to.
        :param handler: Async callable receiving (topic_str, payload_dict).
        """
        self._subscriptions[topic] = handler
        logger.info("Registered handler for topic %s", topic)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Connect to broker and process messages until stopped.

        Probes broker availability first, then connects with LWT.
        Subscribes to all registered topics and dispatches messages.
        Reconnects automatically on disconnection via aiomqtt.

        :param stop_event: Event that signals shutdown.
        """
        status_topic = self._status_topic()

        while not stop_event.is_set():
            if not self.probe_broker(self._config.broker, self._config.port):
                logger.warning("MQTT broker %s:%s unreachable, retrying in 5s",
                               self._config.broker, self._config.port)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                    return
                except asyncio.TimeoutError:
                    continue

            try:
                async with aiomqtt.Client(
                    hostname=self._config.broker,
                    port=self._config.port,
                    will=aiomqtt.Will(
                        topic=status_topic,
                        payload="offline",
                        qos=1,
                        retain=True,
                    ),
                ) as client:
                    self._client = client
                    logger.info("MQTT connected to %s:%s",
                                self._config.broker, self._config.port)

                    # Publish online status
                    await client.publish(status_topic, payload="online", qos=1, retain=True)

                    # Subscribe to all registered topics
                    for topic in self._subscriptions:
                        await client.subscribe(topic)
                        logger.info("Subscribed to %s", topic)

                    # Process messages until stop
                    async for message in client.messages:
                        if stop_event.is_set():
                            break
                        await self._dispatch_message(
                            str(message.topic), message.payload
                        )

            except aiomqtt.MqttError:
                logger.exception("MQTT connection lost, reconnecting in 5s")
                self._client = None
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                    return
                except asyncio.TimeoutError:
                    continue

        # Publish offline on clean shutdown
        if self._client is not None:
            try:
                await self._client.publish(
                    status_topic, payload="offline", qos=1, retain=True
                )
            except aiomqtt.MqttError:
                pass
            self._client = None

    async def publish(self, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> None:
        """Publish a message to an MQTT topic.

        :param topic: Full MQTT topic string.
        :param payload: Payload (will be JSON-encoded if dict/list).
        :param qos: MQTT QoS level.
        :param retain: Whether to retain the message.
        """
        if self._client is None:
            logger.warning("Cannot publish to %s: MQTT not connected", topic)
            return

        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)

        try:
            await self._client.publish(topic, payload=payload, qos=qos, retain=retain)
        except aiomqtt.MqttError:
            logger.exception("Failed to publish to %s", topic)

    async def publish_measurement(
        self, sensor_type: str, sensor_id: str, payload: dict
    ) -> None:
        """Publish a sensor measurement to the standard topic.

        Publishes to ``{base_topic}/{sensor_type}/{sensor_id}/measurement``.

        :param sensor_type: Sensor type string (e.g. ``temperature``).
        :param sensor_id: Sensor identifier string.
        :param payload: Measurement data dictionary.
        """
        topic = self.build_topic(sensor_type, sensor_id, "measurement")
        await self.publish(topic, payload)

    async def publish_address_change_result(self, result: dict) -> None:
        """Publish address change outcome.

        :param result: Result dictionary with success/error details.
        """
        topic = self._address_change_status_topic()
        await self.publish(topic, result)

    async def _dispatch_message(self, topic: str, raw_payload: bytes) -> None:
        """Validate and route an incoming MQTT message.

        :param topic: The topic the message was received on.
        :param raw_payload: Raw message payload bytes.
        """
        try:
            payload = json.loads(raw_payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Malformed MQTT payload on %s, dropping", topic)
            return

        if not isinstance(payload, dict):
            logger.warning("MQTT payload on %s is not a JSON object, dropping", topic)
            return

        for pattern, handler in self._subscriptions.items():
            if self._topic_matches(pattern, topic):
                try:
                    await handler(topic, payload)
                except Exception:
                    logger.exception("Error in handler for %s", topic)
                return

        logger.debug("No handler matched topic %s", topic)

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """Check if a topic matches a subscription pattern.

        Supports ``+`` (single level) and ``#`` (multi level) wildcards.

        :param pattern: Subscription pattern.
        :param topic: Actual topic string.
        :returns: True if topic matches pattern.
        """
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")

        for i, part in enumerate(pattern_parts):
            if part == "#":
                return True
            if i >= len(topic_parts):
                return False
            if part != "+" and part != topic_parts[i]:
                return False

        return len(pattern_parts) == len(topic_parts)
