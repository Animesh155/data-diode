"""MQTT protocol driver for the OSDD Protocol Gateway.

Subscribes to an MQTT broker, receives messages asynchronously via paho-mqtt
callbacks, and converts them into :class:`TagValue` instances.  Supports
wildcard topics, multiple payload formats (JSON / plain / CSV), and
thread-safe message queuing.

Requires **paho-mqtt ≥ 2.0** (CallbackAPIVersion.VERSION2).
"""

from __future__ import annotations

import asyncio
import json
import queue
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt
import structlog

from gateway.core.tag_manager import TagValue
from gateway.drivers.base import BaseDriver, DriverStatus


class MQTTDriver(BaseDriver):
    """MQTT driver that subscribes to broker topics and emits :class:`TagValue`s.

    Parameters
    ----------
    config:
        Driver configuration dictionary.  Expected shape::

            {
                "name": "mqtt-gateway",
                "type": "mqtt",
                "broker": { "host": "…", "port": 1883, … },
                "subscriptions": [ { "topic": "…", … }, … ],
            }
    """

    # ------------------------------------------------------------------
    # BaseDriver abstract property
    # ------------------------------------------------------------------

    @property
    def protocol_name(self) -> str:  # noqa: D401
        return "mqtt"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)

        broker_cfg = config.get("broker", {})
        self._host: str = broker_cfg.get("host", "localhost")
        self._port: int = int(broker_cfg.get("port", 1883))
        self._username: str | None = broker_cfg.get("username") or None
        self._password: str | None = broker_cfg.get("password") or None
        self._tls_enabled: bool = broker_cfg.get("tls_enabled", False)
        self._client_id: str = broker_cfg.get(
            "client_id", f"osdd-mqtt-{self.name}"
        )
        self._clean_session: bool = broker_cfg.get("clean_session", True)
        self._keepalive: int = int(broker_cfg.get("keepalive_seconds", 60))

        # Subscription descriptors
        self._subscriptions: list[dict[str, Any]] = config.get(
            "subscriptions", []
        )

        # Thread-safe internal message queue — paho callbacks run on a
        # separate network thread and push here; _read_tags drains it.
        self._msg_queue: queue.Queue[TagValue] = queue.Queue()

        # Per-subscription telemetry
        self._sub_stats: dict[str, _SubStats] = {}

        # paho client (created in _connect)
        self._client: mqtt.Client | None = None

        self.log = structlog.get_logger(
            driver=self.name,
            protocol=self.protocol_name,
        )

    # ------------------------------------------------------------------
    # BaseDriver abstract methods
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Create an MQTT client, configure it, and connect to the broker."""
        self.log.info(
            "mqtt.connecting",
            host=self._host,
            port=self._port,
            client_id=self._client_id,
        )

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            clean_session=self._clean_session,
        )

        # Optional authentication
        if self._username:
            client.username_pw_set(self._username, self._password)

        # Optional TLS
        if self._tls_enabled:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)

        # Register paho callbacks (VERSION2 signatures)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        # Enable automatic reconnection handled by paho-mqtt
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        # Blocking connect — run in the default executor so we don't
        # block the asyncio event loop.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: client.connect(self._host, self._port, self._keepalive),
        )

        # Start the network-loop thread (non-blocking)
        client.loop_start()

        self._client = client
        self.log.info("mqtt.connected")

    async def _disconnect(self) -> None:
        """Stop the network loop and disconnect from the broker."""
        if self._client is not None:
            self.log.info("mqtt.disconnecting")
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
            self.log.info("mqtt.disconnected")

    async def _read_tags(self) -> list[TagValue]:
        """Drain the internal queue and return all accumulated tag values.

        This is called periodically by the gateway's poll loop.  Because
        paho-mqtt delivers messages on its own network thread, we simply
        drain everything that has been queued since the last call.
        """
        tags: list[TagValue] = []
        while True:
            try:
                tags.append(self._msg_queue.get_nowait())
            except queue.Empty:
                break
        if tags:
            self.log.debug("mqtt.read_tags", count=len(tags))
        return tags

    # ------------------------------------------------------------------
    # paho-mqtt callbacks (run on the network thread)
    # ------------------------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        """Called when the client connects (or reconnects) to the broker."""
        if reason_code == 0:
            self.log.info(
                "mqtt.on_connect",
                reason=str(reason_code),
            )
            self._subscribe_all(client)
            self.status = DriverStatus.CONNECTED
        else:
            self.log.warning(
                "mqtt.on_connect_failed",
                reason=str(reason_code),
            )
            self.status = DriverStatus.ERROR

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        """Called when the client disconnects from the broker."""
        self.log.warning(
            "mqtt.on_disconnect",
            reason=str(reason_code),
        )
        self.status = DriverStatus.DISCONNECTED

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Called for every incoming message — parse and enqueue a TagValue."""
        topic: str = msg.topic
        payload_bytes: bytes = msg.payload

        try:
            sub_cfg = self._match_subscription(topic)
            if sub_cfg is None:
                self.log.debug("mqtt.unmatched_topic", topic=topic)
                return

            value = self._parse_payload(payload_bytes, sub_cfg)
            tag_name = self._resolve_tag_name(topic, sub_cfg)
            data_type = sub_cfg.get("data_type", "string")
            unit = sub_cfg.get("unit")

            tag = TagValue(
                name=tag_name,
                value=self._coerce_value(value, data_type),
                data_type=data_type,
                quality="Good",
                timestamp=datetime.now(tz=timezone.utc),
                unit=unit,
                source_driver=self.name,
                metadata={"mqtt_topic": topic},
            )

            self._msg_queue.put(tag)

            # Update per-subscription telemetry
            stats = self._sub_stats.setdefault(
                sub_cfg["topic"], _SubStats(sub_cfg["topic"])
            )
            stats.record()

        except Exception:
            self.log.exception(
                "mqtt.message_parse_error",
                topic=topic,
                payload=payload_bytes[:200],
            )

    # ------------------------------------------------------------------
    # Subscription helpers
    # ------------------------------------------------------------------

    def _subscribe_all(self, client: mqtt.Client) -> None:
        """Subscribe to every topic listed in the configuration."""
        for sub in self._subscriptions:
            topic = sub["topic"]
            qos = int(sub.get("qos", 0))
            client.subscribe(topic, qos)
            self._sub_stats.setdefault(topic, _SubStats(topic))
            self.log.info("mqtt.subscribed", topic=topic, qos=qos)

    def _match_subscription(self, topic: str) -> dict[str, Any] | None:
        """Find the subscription config whose topic pattern matches *topic*.

        Supports MQTT ``+`` and ``#`` wildcards via :func:`mqtt.topic_matches_sub`.
        """
        for sub in self._subscriptions:
            if mqtt.topic_matches_sub(sub["topic"], topic):
                return sub
        return None

    # ------------------------------------------------------------------
    # Payload parsing
    # ------------------------------------------------------------------

    def _parse_payload(
        self, raw: bytes, sub_cfg: dict[str, Any]
    ) -> Any:
        """Decode the raw MQTT payload according to *sub_cfg*."""
        fmt = sub_cfg.get("payload_format", "plain")
        text = raw.decode("utf-8", errors="replace")

        if fmt == "json":
            return self._extract_json_value(text, sub_cfg.get("value_key", "value"))
        elif fmt == "csv":
            return self._extract_csv_value(
                text,
                delimiter=sub_cfg.get("csv_delimiter", ","),
                index=int(sub_cfg.get("csv_index", 0)),
            )
        else:  # "plain" or unrecognised → raw string
            return text

    @staticmethod
    def _extract_json_value(text: str, key_path: str) -> Any:
        """Walk a dotted *key_path* (e.g. ``data.temperature``) into a JSON object."""
        obj = json.loads(text)
        for part in key_path.split("."):
            obj = obj[part]
        return obj

    @staticmethod
    def _extract_csv_value(text: str, delimiter: str, index: int) -> str:
        """Split CSV text and return the element at *index*."""
        parts = text.split(delimiter)
        return parts[index].strip()

    # ------------------------------------------------------------------
    # Tag name resolution
    # ------------------------------------------------------------------

    def _resolve_tag_name(self, topic: str, sub_cfg: dict[str, Any]) -> str:
        """Map an MQTT topic to a gateway tag name.

        Resolution order:
        1. If the subscription has an explicit ``topic_to_tag`` mapping dict
           and *topic* is a key, use that.
        2. Otherwise, combine ``tag_prefix`` with the topic suffix (the part
           after the subscription pattern's fixed prefix).
        """
        # Explicit mapping
        explicit = sub_cfg.get("topic_to_tag", {})
        if topic in explicit:
            return explicit[topic]

        # Prefix-based mapping
        prefix = sub_cfg.get("tag_prefix", "mqtt")
        pattern = sub_cfg["topic"]
        fixed_prefix = self._fixed_prefix(pattern)

        if topic.startswith(fixed_prefix):
            suffix = topic[len(fixed_prefix):]
        else:
            suffix = topic

        # Replace MQTT separators with dots for a clean tag name
        suffix_clean = suffix.strip("/").replace("/", ".")
        if suffix_clean:
            return f"{prefix}.{suffix_clean}"
        return prefix

    @staticmethod
    def _fixed_prefix(pattern: str) -> str:
        """Return the leading literal portion of an MQTT topic pattern.

        Everything up to (but not including) the first ``+`` or ``#``.
        """
        parts: list[str] = []
        for segment in pattern.split("/"):
            if segment in ("+", "#"):
                break
            parts.append(segment)
        if parts:
            return "/".join(parts) + "/"
        return ""

    # ------------------------------------------------------------------
    # Value coercion
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_value(value: Any, data_type: str) -> Any:
        """Best-effort coercion of *value* to the declared *data_type*."""
        try:
            if data_type in ("float32", "float64", "float"):
                return float(value)
            elif data_type in ("int16", "int32", "int64", "uint16", "uint32"):
                return int(float(value))
            elif data_type == "bool":
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "on", "yes")
                return bool(value)
            else:
                return str(value)
        except (ValueError, TypeError):
            return value

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def subscription_stats(self) -> dict[str, dict[str, Any]]:
        """Return per-subscription message-rate telemetry."""
        return {
            topic: stats.snapshot() for topic, stats in self._sub_stats.items()
        }


# ======================================================================
# Internal helper dataclass
# ======================================================================


class _SubStats:
    """Lightweight per-subscription message counter and rate tracker."""

    __slots__ = ("topic", "message_count", "first_message_at", "last_message_at")

    def __init__(self, topic: str) -> None:
        self.topic = topic
        self.message_count: int = 0
        self.first_message_at: float | None = None
        self.last_message_at: float | None = None

    def record(self) -> None:
        now = time.monotonic()
        self.message_count += 1
        if self.first_message_at is None:
            self.first_message_at = now
        self.last_message_at = now

    def snapshot(self) -> dict[str, Any]:
        rate: float | None = None
        if (
            self.first_message_at is not None
            and self.last_message_at is not None
            and self.last_message_at > self.first_message_at
        ):
            elapsed = self.last_message_at - self.first_message_at
            rate = round(self.message_count / elapsed, 2)

        return {
            "message_count": self.message_count,
            "messages_per_second": rate,
            "last_message_at": self.last_message_at,
        }
