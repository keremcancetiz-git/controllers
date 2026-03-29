"""MQTT client wrapper for MFC Controller.

Encapsulates connection management (with broker-availability probing),
Last-Will-and-Testament setup, topic construction, and inbound message
routing.
"""

import json
import logging
import socket
import threading
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from src.config import SystemConfig

logger = logging.getLogger("mfc.mqtt")


class MQTTHandler:
    """Manages the MQTT connection for the MFC controller.

    :param system_config: System configuration (broker, port, topics).
    :param on_setpoint: Callback ``(device_name, value)`` for setpoint
        commands.
    :param on_gas_change: Callback ``(device_name, gas_name)`` for gas
        selection commands.
    :param on_reset: Callback ``(device_name)`` for device reset
        commands.  Pass ``"all"`` to reset every device.
    """

    def __init__(
        self,
        system_config: SystemConfig,
        on_setpoint: Callable[[str, float], None] | None = None,
        on_gas_change: Callable[[str, str], None] | None = None,
        on_reset: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._cfg = system_config
        self._on_setpoint = on_setpoint
        self._on_gas_change = on_gas_change
        self._on_reset = on_reset
        self._stop_event = stop_event or threading.Event()

        self._client: mqtt.Client | None = None

    # -- public API ----------------------------------------------------------

    def connect(self) -> None:
        """Block until the broker is reachable, then connect.

        Sets up LWT, subscribes to command topics, and starts the
        network loop in a background thread.

        :raises SystemExit: If the MQTT connection fails after the
            broker becomes reachable.
        """
        if not self._wait_for_broker():
            return

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"mfc_controller_{socket.gethostname()}",
            protocol=mqtt.MQTTv311,
        )

        base = self._cfg.base_topic

        self._client.will_set(
            f"{base}/status",
            json.dumps({"status": "offline", "timestamp": int(time.time() * 1000)}),
            qos=1,
            retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.connect(self._cfg.mqtt_broker, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()
        logger.info(
            "MQTT connecting to %s:%d", self._cfg.mqtt_broker, self._cfg.mqtt_port,
        )

    def disconnect(self) -> None:
        """Publish offline status, stop loop, and disconnect."""
        if not self._client:
            return

        try:
            base = self._cfg.base_topic
            self._client.publish(
                f"{base}/status",
                json.dumps({"status": "offline", "timestamp": int(time.time() * 1000)}),
                qos=1,
                retain=True,
            )
            time.sleep(0.5)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the MQTT client is currently connected."""
        return self._client is not None and self._client.is_connected()

    def publish(self, suffix: str, payload: str, **kwargs) -> None:
        """Publish a message under the configured base topic.

        :param suffix: Topic suffix appended to ``base_topic/``.
        :param payload: JSON string payload.
        :param kwargs: Extra arguments forwarded to
            ``paho.mqtt.Client.publish`` (e.g. *retain*, *qos*).
        """
        if not self.is_connected:
            return
        topic = f"{self._cfg.base_topic}/{suffix}"
        self._client.publish(topic, payload, **kwargs)

    def publish_raw(self, topic: str, payload: str, **kwargs) -> None:
        """Publish to an arbitrary topic.

        :param topic: Full MQTT topic string.
        :param payload: JSON string payload.
        :param kwargs: Extra arguments for ``paho.mqtt.Client.publish``.
        """
        if not self.is_connected:
            return
        self._client.publish(topic, payload, **kwargs)

    # -- internal callbacks --------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        """Handle successful MQTT connection."""
        if not reason_code.is_failure:
            base = self._cfg.base_topic
            logger.info("MQTT connected")
            client.subscribe(f"{base}/+/setpoint/set", qos=1)
            client.subscribe(f"{base}/+/gas/set", qos=1)
            client.subscribe(f"{base}/+/reset", qos=1)
            client.publish(
                f"{base}/status",
                json.dumps({"status": "online", "timestamp": int(time.time() * 1000)}),
                qos=1,
                retain=True,
            )
        else:
            logger.error("MQTT connection failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """Log unexpected disconnections."""
        if reason_code.is_failure:
            logger.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        """Route inbound messages to the appropriate callback."""
        try:
            parts = msg.topic.split("/")
            base_depth = len(self._cfg.base_topic.split("/"))

            # Expect: {base_topic}/{device_name}/{command...}
            if len(parts) < base_depth + 2:
                return

            device_name = parts[base_depth]
            command_parts = parts[base_depth + 1:]
            command = "/".join(command_parts)
            payload = msg.payload.decode()

            if command == "setpoint/set" and self._on_setpoint:
                try:
                    value = float(payload)
                except ValueError:
                    try:
                        data = json.loads(payload)
                        value = float(data.get("value", data))
                    except Exception:
                        logger.error("Invalid setpoint payload: %s", payload)
                        return
                self._on_setpoint(device_name, value)

            elif command == "gas/set" and self._on_gas_change:
                self._on_gas_change(device_name, payload)

            elif command == "reset" and self._on_reset:
                self._on_reset(device_name)

        except Exception as exc:
            logger.error("Error handling MQTT message: %s", exc)

    # -- broker probing ------------------------------------------------------

    def _wait_for_broker(self, timeout: int = 5) -> bool:
        """Block until the MQTT broker is reachable via TCP or stop is requested.

        :param timeout: Socket connect timeout in seconds.
        :returns: ``True`` if broker was reached, ``False`` if stop was signalled.
        """
        host = self._cfg.mqtt_broker
        port = self._cfg.mqtt_port
        logger.info("Waiting for MQTT broker at %s:%d ...", host, port)

        while not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((host, port))
                sock.close()
                if result == 0:
                    logger.info("Broker reachable at %s:%d", host, port)
                    return True
                logger.warning(
                    "Broker unreachable (code %d). Retrying in 5 s ...", result,
                )
            except Exception as exc:
                logger.warning("Broker check error: %s. Retrying in 5 s ...", exc)
            self._stop_event.wait(5)
        return False
