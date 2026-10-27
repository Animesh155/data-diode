"""Tests for the MQTT protocol driver.

Uses pytest-asyncio for async test support and unittest.mock to avoid
requiring a live MQTT broker.
"""

from __future__ import annotations

import asyncio
import json
import queue
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.core.tag_manager import TagValue
from gateway.drivers.base import DriverStatus
from gateway.drivers.mqtt_driver import MQTTDriver


# ======================================================================
# Fixtures
# ======================================================================

def _base_config(**overrides: Any) -> dict[str, Any]:
    """Return a minimal MQTT driver config with optional overrides."""
    cfg: dict[str, Any] = {
        "name": "test-mqtt",
        "type": "mqtt",
        "broker": {
            "host": "localhost",
            "port": 1883,
            "client_id": "test-client",
            "clean_session": True,
            "keepalive_seconds": 60,
        },
        "subscriptions": [
            {
                "topic": "factory/temperature/+",
                "qos": 1,
                "tag_prefix": "mqtt_temp",
                "payload_format": "json",
                "value_key": "value",
                "data_type": "float64",
                "unit": "°C",
            },
            {
                "topic": "factory/status/#",
                "qos": 0,
                "tag_prefix": "mqtt_status",
                "payload_format": "plain",
                "data_type": "string",
            },
        ],
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def driver() -> MQTTDriver:
    """Return an MQTTDriver with default test config."""
    return MQTTDriver(_base_config())


@pytest.fixture
def mqtt_message() -> MagicMock:
    """Factory for paho MQTTMessage-like objects."""

    def _make(topic: str, payload: str | bytes) -> MagicMock:
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        return msg

    return _make


# ======================================================================
# Tests
# ======================================================================


class TestDriverInitialisation:
    """Verify driver construction and config parsing."""

    def test_protocol_name(self, driver: MQTTDriver) -> None:
        assert driver.protocol_name == "mqtt"

    def test_driver_name(self, driver: MQTTDriver) -> None:
        assert driver.name == "test-mqtt"

    def test_initial_status(self, driver: MQTTDriver) -> None:
        assert driver.status == DriverStatus.DISCONNECTED

    def test_broker_config_defaults(self) -> None:
        d = MQTTDriver({"name": "minimal"})
        assert d._host == "localhost"
        assert d._port == 1883

    def test_subscriptions_parsed(self, driver: MQTTDriver) -> None:
        assert len(driver._subscriptions) == 2
        assert driver._subscriptions[0]["topic"] == "factory/temperature/+"

    def test_queue_is_empty_initially(self, driver: MQTTDriver) -> None:
        assert driver._msg_queue.empty()


class TestConnectToBroker:
    """Verify connection logic with a mocked paho client."""

    @pytest.mark.asyncio
    async def test_connect_creates_client(self, driver: MQTTDriver) -> None:
        with patch("gateway.drivers.mqtt_driver.mqtt.Client") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value = mock_instance

            await driver._connect()

            MockClient.assert_called_once()
            mock_instance.connect.assert_called_once_with("localhost", 1883, 60)
            mock_instance.loop_start.assert_called_once()
            assert driver._client is mock_instance

    @pytest.mark.asyncio
    async def test_connect_with_auth(self) -> None:
        cfg = _base_config()
        cfg["broker"]["username"] = "user"
        cfg["broker"]["password"] = "pass"
        d = MQTTDriver(cfg)

        with patch("gateway.drivers.mqtt_driver.mqtt.Client") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value = mock_instance

            await d._connect()

            mock_instance.username_pw_set.assert_called_once_with("user", "pass")

    @pytest.mark.asyncio
    async def test_connect_with_tls(self) -> None:
        cfg = _base_config()
        cfg["broker"]["tls_enabled"] = True
        d = MQTTDriver(cfg)

        with patch("gateway.drivers.mqtt_driver.mqtt.Client") as MockClient:
            mock_instance = MagicMock()
            MockClient.return_value = mock_instance

            await d._connect()

            mock_instance.tls_set.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect(self, driver: MQTTDriver) -> None:
        mock_client = MagicMock()
        driver._client = mock_client

        await driver._disconnect()

        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()
        assert driver._client is None


class TestJsonPayloadParsing:
    """Verify JSON message parsing and tag value creation."""

    def test_simple_json_value(self, driver: MQTTDriver, mqtt_message: Any) -> None:
        msg = mqtt_message("factory/temperature/zone1", '{"value": 23.5, "unit": "C"}')
        driver._on_message(None, None, msg)

        tag = driver._msg_queue.get_nowait()
        assert tag.name == "mqtt_temp.zone1"
        assert tag.value == 23.5
        assert tag.data_type == "float64"
        assert tag.unit == "°C"
        assert tag.quality == "Good"
        assert tag.source_driver == "test-mqtt"

    def test_nested_json_key_path(self, driver: MQTTDriver) -> None:
        """Dotted value_key navigates into nested JSON objects."""
        cfg = _base_config()
        cfg["subscriptions"] = [
            {
                "topic": "sensors/#",
                "tag_prefix": "sensor",
                "payload_format": "json",
                "value_key": "data.temperature",
                "data_type": "float64",
            }
        ]
        d = MQTTDriver(cfg)

        msg = MagicMock()
        msg.topic = "sensors/building1"
        msg.payload = json.dumps({"data": {"temperature": 42.0}}).encode()
        d._on_message(None, None, msg)

        tag = d._msg_queue.get_nowait()
        assert tag.value == 42.0

    def test_malformed_json_does_not_crash(
        self, driver: MQTTDriver, mqtt_message: Any
    ) -> None:
        msg = mqtt_message("factory/temperature/zone1", "NOT-JSON")
        driver._on_message(None, None, msg)
        # Should log an error and not enqueue anything
        assert driver._msg_queue.empty()


class TestPlainPayloadParsing:
    """Verify plain-text payload handling."""

    def test_plain_text_value(self, driver: MQTTDriver, mqtt_message: Any) -> None:
        msg = mqtt_message("factory/status/pump1", "RUNNING")
        driver._on_message(None, None, msg)

        tag = driver._msg_queue.get_nowait()
        assert tag.name == "mqtt_status.pump1"
        assert tag.value == "RUNNING"
        assert tag.data_type == "string"

    def test_plain_nested_topic(self, driver: MQTTDriver, mqtt_message: Any) -> None:
        msg = mqtt_message("factory/status/area1/pump2", "STOPPED")
        driver._on_message(None, None, msg)

        tag = driver._msg_queue.get_nowait()
        assert tag.name == "mqtt_status.area1.pump2"
        assert tag.value == "STOPPED"


class TestCsvPayloadParsing:
    """Verify CSV payload parsing."""

    def test_csv_extraction(self) -> None:
        cfg = _base_config()
        cfg["subscriptions"] = [
            {
                "topic": "data/csv/#",
                "tag_prefix": "csv",
                "payload_format": "csv",
                "csv_delimiter": ",",
                "csv_index": 1,
                "data_type": "float64",
            }
        ]
        d = MQTTDriver(cfg)

        msg = MagicMock()
        msg.topic = "data/csv/sensor1"
        msg.payload = b"timestamp,23.5,unit"
        d._on_message(None, None, msg)

        tag = d._msg_queue.get_nowait()
        assert tag.value == 23.5


class TestWildcardTopicSubscription:
    """Verify MQTT wildcard matching."""

    def test_plus_wildcard_matches(self, driver: MQTTDriver, mqtt_message: Any) -> None:
        """'+' wildcard matches exactly one level."""
        msg = mqtt_message("factory/temperature/zoneX", '{"value": 10.0}')
        driver._on_message(None, None, msg)
        assert not driver._msg_queue.empty()

    def test_hash_wildcard_matches(self, driver: MQTTDriver, mqtt_message: Any) -> None:
        """'#' wildcard matches one or more levels."""
        msg = mqtt_message("factory/status/area/unit/pump", "OK")
        driver._on_message(None, None, msg)
        assert not driver._msg_queue.empty()

    def test_unmatched_topic_ignored(
        self, driver: MQTTDriver, mqtt_message: Any
    ) -> None:
        msg = mqtt_message("other/topic/entirely", "data")
        driver._on_message(None, None, msg)
        assert driver._msg_queue.empty()


class TestTagNameMapping:
    """Verify topic → tag name resolution."""

    def test_prefix_based_mapping(self, driver: MQTTDriver, mqtt_message: Any) -> None:
        msg = mqtt_message("factory/temperature/zone3", '{"value": 1.0}')
        driver._on_message(None, None, msg)
        tag = driver._msg_queue.get_nowait()
        assert tag.name == "mqtt_temp.zone3"

    def test_explicit_topic_to_tag_mapping(self) -> None:
        cfg = _base_config()
        cfg["subscriptions"] = [
            {
                "topic": "factory/temperature/+",
                "tag_prefix": "temp",
                "payload_format": "json",
                "value_key": "value",
                "data_type": "float64",
                "topic_to_tag": {
                    "factory/temperature/zone1": "custom_tag_name",
                },
            }
        ]
        d = MQTTDriver(cfg)
        msg = MagicMock()
        msg.topic = "factory/temperature/zone1"
        msg.payload = b'{"value": 5.0}'
        d._on_message(None, None, msg)

        tag = d._msg_queue.get_nowait()
        assert tag.name == "custom_tag_name"


class TestReconnectOnDisconnect:
    """Verify the on_disconnect callback updates driver state."""

    def test_on_disconnect_sets_status(self, driver: MQTTDriver) -> None:
        flags = MagicMock()
        reason_code = MagicMock()
        reason_code.__str__ = lambda self: "0"

        driver._on_disconnect(None, None, flags, reason_code, None)

        assert driver.status == DriverStatus.DISCONNECTED

    def test_on_connect_success_sets_connected(self, driver: MQTTDriver) -> None:
        mock_client = MagicMock()
        flags = MagicMock()
        reason_code = MagicMock()
        reason_code.__eq__ = lambda self, other: other == 0

        driver._on_connect(mock_client, None, flags, reason_code, None)

        assert driver.status == DriverStatus.CONNECTED
        # Should have subscribed to all topics
        assert mock_client.subscribe.call_count == 2


class TestQueueDraining:
    """Verify _read_tags drains the internal queue."""

    @pytest.mark.asyncio
    async def test_drain_returns_all_queued(
        self, driver: MQTTDriver, mqtt_message: Any
    ) -> None:
        # Enqueue several messages
        for i in range(5):
            msg = mqtt_message(
                f"factory/temperature/zone{i}",
                json.dumps({"value": float(i)}),
            )
            driver._on_message(None, None, msg)

        tags = await driver._read_tags()
        assert len(tags) == 5
        assert all(isinstance(t, TagValue) for t in tags)

    @pytest.mark.asyncio
    async def test_drain_empty_returns_empty_list(
        self, driver: MQTTDriver
    ) -> None:
        tags = await driver._read_tags()
        assert tags == []

    @pytest.mark.asyncio
    async def test_drain_clears_queue(
        self, driver: MQTTDriver, mqtt_message: Any
    ) -> None:
        msg = mqtt_message("factory/temperature/zone1", '{"value": 1.0}')
        driver._on_message(None, None, msg)

        first_drain = await driver._read_tags()
        second_drain = await driver._read_tags()

        assert len(first_drain) == 1
        assert len(second_drain) == 0


class TestSubscriptionStats:
    """Verify per-subscription telemetry tracking."""

    def test_stats_updated_on_message(
        self, driver: MQTTDriver, mqtt_message: Any
    ) -> None:
        msg = mqtt_message("factory/temperature/zone1", '{"value": 1.0}')
        driver._on_message(None, None, msg)

        stats = driver.subscription_stats()
        assert "factory/temperature/+" in stats
        assert stats["factory/temperature/+"]["message_count"] == 1

    def test_stats_empty_when_no_messages(self, driver: MQTTDriver) -> None:
        stats = driver.subscription_stats()
        assert len(stats) == 0


class TestValueCoercion:
    """Verify _coerce_value handles various data types."""

    def test_float_coercion(self) -> None:
        assert MQTTDriver._coerce_value("23.5", "float64") == 23.5

    def test_int_coercion(self) -> None:
        assert MQTTDriver._coerce_value("42", "int16") == 42

    def test_bool_coercion_true(self) -> None:
        assert MQTTDriver._coerce_value("true", "bool") is True

    def test_bool_coercion_false(self) -> None:
        assert MQTTDriver._coerce_value("false", "bool") is False

    def test_string_fallback(self) -> None:
        assert MQTTDriver._coerce_value(123, "string") == "123"

    def test_invalid_coercion_returns_original(self) -> None:
        assert MQTTDriver._coerce_value("not_a_number", "float64") == "not_a_number"
