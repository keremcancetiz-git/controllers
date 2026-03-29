"""MQTT communication handler for the relay controller.

Wraps :mod:`paho.mqtt.client` with ``CallbackAPIVersion.VERSION2`` to provide
topic-aware publish/subscribe aligned with the project's MQTT naming convention::

    {base_topic}/{relay_id}/set_binary   ← incoming commands (subscribe)
    {base_topic}/{relay_id}/state        → relay state feedback (publish, retained)
    {base_topic}/{relay_id}/error        → error messages (publish)
    {base_topic}/status                  → controller online/offline (LWT, retained)
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable

from paho.mqtt.client import Client, CallbackAPIVersion, MQTTMessage

from .config import MqttConfig

logger = logging.getLogger("relay.mqtt")

CommandCallback = Callable[[int, bool], None]
"""Signature: ``(relay_number: int, state: bool) -> None``."""


class MqttHandler:
    """MQTT client for relay command reception and state publishing.

    :param config: MQTT configuration settings.
    :param on_command: Callback invoked when a valid ``set_binary`` message
        is received.  Called with ``(relay_number, state)``.
    """

    def __init__(self, config: MqttConfig, on_command: CommandCallback) -> None:
        self._config = config
        self._on_command = on_command

        self._client = Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def connect(self) -> bool:
        """Probe the broker, set LWT, authenticate, and connect.

        Performs a TCP socket probe to verify broker reachability before
        attempting the MQTT connection.

        :returns: ``True`` if the connection handshake was initiated.
        :rtype: bool
        """
        cfg = self._config

        if not self._probe_broker(cfg.broker_host, cfg.broker_port):
            return False

        # Last Will and Testament — broker publishes this if we drop
        status_topic = "%s/status" % cfg.base_topic
        self._client.will_set(
            topic=status_topic,
            payload="offline",
            qos=cfg.qos,
            retain=True,
        )

        if cfg.username:
            self._client.username_pw_set(cfg.username, cfg.password)

        try:
            self._client.connect(
                host=cfg.broker_host,
                port=cfg.broker_port,
                keepalive=cfg.keepalive,
            )
            self._client.loop_start()
            logger.info(
                "MQTT connecting to %s:%d", cfg.broker_host, cfg.broker_port
            )
            return True
        except OSError as exc:
            logger.error("MQTT connection failed: %s", exc)
            return False

    def disconnect(self) -> None:
        """Publish offline status and disconnect cleanly."""
        status_topic = "%s/status" % self._config.base_topic
        self._client.publish(
            topic=status_topic,
            payload="offline",
            qos=self._config.qos,
            retain=True,
        )
        self._client.disconnect()
        self._client.loop_stop()
        logger.info("MQTT disconnected")

    def publish_state(self, relay_number: int, state: bool) -> None:
        """Publish the current state of a relay (retained).

        :param relay_number: Flat relay number (1-based).
        :param state: ``True`` for ON, ``False`` for OFF.
        """
        topic = "%s/%d/state" % (self._config.base_topic, relay_number)
        payload = "1" if state else "0"
        self._client.publish(
            topic=topic,
            payload=payload,
            qos=self._config.qos,
            retain=True,
        )

    def publish_error(self, relay_number: int, message: str) -> None:
        """Publish an error message for a relay.

        :param relay_number: Flat relay number (1-based).
        :param message: Human-readable error description.
        """
        topic = "%s/%d/error" % (self._config.base_topic, relay_number)
        self._client.publish(
            topic=topic,
            payload=message,
            qos=self._config.qos,
            retain=False,
        )
        logger.warning("Published error for relay %d: %s", relay_number, message)

    # ------------------------------------------------------------------
    # Internal callbacks (paho-mqtt VERSION2 signatures)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties) -> None:
        """Handle broker connection (or reconnection).

        Subscribes to the ``set_binary`` command topic and publishes
        the controller's online status.

        :param client: The MQTT client instance.
        :param userdata: User data (unused).
        :param connect_flags: Connection acknowledgement flags.
        :param reason_code: MQTT reason code for the connection result.
        :param properties: MQTT v5.0 properties (``None`` for v3.1.1).
        """
        if reason_code.is_failure:
            logger.error("MQTT connection failed: %s", reason_code)
            return

        logger.info("MQTT connected: %s", reason_code)

        # Subscribe to relay commands: {base_topic}/+/set_binary
        command_topic = "%s/+/set_binary" % self._config.base_topic
        client.subscribe(command_topic, qos=self._config.qos)
        logger.info("Subscribed to %s", command_topic)

        # Announce online status
        status_topic = "%s/status" % self._config.base_topic
        client.publish(
            topic=status_topic,
            payload="online",
            qos=self._config.qos,
            retain=True,
        )

    def _on_message(self, client, userdata, message: MQTTMessage) -> None:
        """Parse incoming ``set_binary`` commands and dispatch them.

        Expected topic format: ``{base_topic}/{relay_number}/set_binary``
        Expected payload: ``0`` (OFF) or ``1`` (ON).

        :param client: The MQTT client instance.
        :param userdata: User data (unused).
        :param message: The received MQTT message.
        """
        try:
            parts = message.topic.split("/")
            # Topic structure: .../relay/{relay_number}/set_binary
            # The relay_number is the second-to-last segment
            relay_str = parts[-2]
            relay_number = int(relay_str)
        except (IndexError, ValueError):
            logger.warning("Malformed topic: %s", message.topic)
            return

        try:
            payload = message.payload.decode("utf-8").strip()
        except UnicodeDecodeError:
            logger.warning(
                "Non-UTF-8 payload on %s", message.topic
            )
            return

        if payload == "1":
            state = True
        elif payload == "0":
            state = False
        else:
            logger.warning(
                "Invalid set_binary payload '%s' for relay %d (expected 0 or 1)",
                payload,
                relay_number,
            )
            return

        logger.debug(
            "Command received: relay %d → %s", relay_number, "ON" if state else "OFF"
        )
        self._on_command(relay_number, state)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties) -> None:
        """Log broker disconnection events.

        :param client: The MQTT client instance.
        :param userdata: User data (unused).
        :param disconnect_flags: Disconnection flags.
        :param reason_code: MQTT reason code for the disconnection.
        :param properties: MQTT v5.0 properties (``None`` for v3.1.1).
        """
        if reason_code.is_failure:
            logger.warning("MQTT disconnected unexpectedly: %s", reason_code)
        else:
            logger.info("MQTT disconnected cleanly")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_broker(host: str, port: int, timeout: float = 5.0) -> bool:
        """Check broker reachability with a raw TCP socket probe.

        :param host: Broker hostname or IP.
        :param port: Broker TCP port.
        :param timeout: Connection timeout in seconds.
        :returns: ``True`` if a TCP connection could be established.
        :rtype: bool
        """
        try:
            with socket.create_connection((host, port), timeout=timeout):
                logger.debug("Broker probe OK: %s:%d", host, port)
                return True
        except OSError as exc:
            logger.error(
                "Broker unreachable at %s:%d: %s", host, port, exc
            )
            return False
