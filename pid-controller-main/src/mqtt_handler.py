import json
import logging
import socket
from collections.abc import Callable

import paho.mqtt.client as mqtt

from .config import MqttConfig

logger = logging.getLogger("pid.mqtt")


class MqttHandler:
    """Manages the MQTT connection, subscriptions, and publishing.

    :param config: MQTT configuration.
    :param device_ids: List of logical device IDs to subscribe for.
    :param on_set_setpoint: Callback(device_id, value) when a set_setpoint message arrives.
    """

    def __init__(
        self,
        config: MqttConfig,
        device_ids: list[int],
        on_set_setpoint: Callable[[int, float], None],
    ) -> None:
        self._config = config
        self._device_ids = device_ids
        self._on_set_setpoint = on_set_setpoint
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Last Will: mark service offline on ungraceful disconnect
        self._client.will_set(
            topic=f"{self._config.base_topic}/status",
            payload="offline",
            qos=1,
            retain=True,
        )

    def probe_broker(self) -> bool:
        """Check if the MQTT broker is reachable via TCP.

        :returns: True if the broker accepts a connection.
        """
        try:
            with socket.create_connection(
                (self._config.broker, self._config.port), timeout=5
            ):
                return True
        except OSError as e:
            logger.warning(
                "MQTT broker not reachable at %s:%s: %s",
                self._config.broker,
                self._config.port,
                e,
            )
            return False

    def connect(self) -> None:
        """Connect to the MQTT broker and start the network loop.

        :raises ConnectionRefusedError: If the broker is not reachable.
        """
        if not self.probe_broker():
            raise ConnectionRefusedError(
                f"MQTT broker at {self._config.broker}:{self._config.port} is not reachable"
            )
        self._client.connect(
            self._config.broker,
            self._config.port,
            keepalive=self._config.keepalive,
        )
        self._client.loop_start()

    def disconnect(self) -> None:
        """Publish offline status and disconnect gracefully."""
        self._client.publish(
            f"{self._config.base_topic}/status",
            payload="offline",
            qos=1,
            retain=True,
        )
        for device_id in self._device_ids:
            self.publish_status(device_id, False)
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT disconnected")

    def publish_measurement(self, device_id: int, value: float) -> None:
        """Publish the current process value (PV) for a device.

        :param device_id: Logical device ID.
        :param value: Temperature reading.
        """
        topic = f"{self._config.base_topic}/{device_id}/measurement"
        self._client.publish(topic, payload=str(value), qos=1, retain=True)

    def publish_setpoint(self, device_id: int, value: float) -> None:
        """Publish the current setpoint value (SV) for a device.

        :param device_id: Logical device ID.
        :param value: Setpoint temperature.
        """
        topic = f"{self._config.base_topic}/{device_id}/setpoint"
        self._client.publish(topic, payload=str(value), qos=1, retain=True)

    def publish_status(self, device_id: int, online: bool) -> None:
        """Publish the online/offline status for a device.

        :param device_id: Logical device ID.
        :param online: True if device is reachable.
        """
        topic = f"{self._config.base_topic}/{device_id}/status"
        payload = "online" if online else "offline"
        self._client.publish(topic, payload=payload, qos=1, retain=True)

    def _on_connect(
        self, client: mqtt.Client, userdata, connect_flags, reason_code, properties
    ) -> None:
        """Handle MQTT connection established."""
        if reason_code == 0:
            logger.info("Connected to MQTT broker")
            # Publish service online
            client.publish(
                f"{self._config.base_topic}/status",
                payload="online",
                qos=1,
                retain=True,
            )
            # Subscribe to set_setpoint for all devices
            topic = f"{self._config.base_topic}/+/set_setpoint"
            client.subscribe(topic, qos=1)
            logger.info("Subscribed to %s", topic)
        else:
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_disconnect(
        self, client: mqtt.Client, userdata, disconnect_flags, reason_code, properties
    ) -> None:
        """Handle MQTT disconnection."""
        logger.warning("Disconnected from MQTT broker: %s", reason_code)

    def _on_message(
        self, client: mqtt.Client, userdata, message: mqtt.MQTTMessage
    ) -> None:
        """Handle incoming MQTT messages (set_setpoint commands)."""
        try:
            parts = message.topic.split("/")
            # Expected: {base_topic_parts...}/{device_id}/set_setpoint
            device_id = int(parts[-2])
            value = float(message.payload.decode())
        except (IndexError, ValueError) as e:
            logger.warning(
                "Invalid set_setpoint message on %s: %s", message.topic, e
            )
            return

        if device_id not in self._device_ids:
            logger.warning(
                "Received set_setpoint for unknown device %s", device_id
            )
            return

        logger.info(
            "MQTT command: set device %s setpoint to %.1f", device_id, value
        )
        self._on_set_setpoint(device_id, value)
