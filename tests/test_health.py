"""Collector health: connection events from the paho callbacks, and the
diagnostic entities that show them."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.core import HomeAssistant

from custom_components.navimow.binary_sensor import NavimowCloudConnected
from custom_components.navimow.health import CollectorHealth, instrument_mqtt
from custom_components.navimow.sensor import NavimowLastMessageSensor

DEVICE = SimpleNamespace(id="dev-1", name="Mower", model="X430",
                         firmware_version="1.0", serial_number="SN1")


class FakeSdkMqtt:
    """The parts of the SDK's NavimowMQTT that instrument_mqtt touches."""

    def __init__(self, connected=False):
        self.calls = []
        self.client = SimpleNamespace(_client_id=b"web_user_1")
        self.connected = connected

    @property
    def is_connected(self):
        return self.connected

    def _on_connect(self, client, userdata, flags, rc):
        self.calls.append(("connect", rc))

    def _on_disconnect(self, client, userdata, rc):
        self.calls.append(("disconnect", rc))

    def _build_new_client(self):
        client = SimpleNamespace(_client_id=b"web_user_2")
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client


class InstrumentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.health = CollectorHealth()
        self.mqtt = FakeSdkMqtt()
        instrument_mqtt(self.mqtt, asyncio.get_running_loop(), self.health)

    async def settle(self):
        await asyncio.sleep(0)

    async def test_connect_and_disconnect_are_reported_and_forwarded(self):
        client = self.mqtt.client
        client.on_connect(client, None, {}, 0)
        await self.settle()
        self.assertTrue(self.health.connected)
        self.assertEqual(self.health.client_id, "web_user_1")
        self.assertIsNotNone(self.health.connected_at)
        client.on_disconnect(client, None, 7)
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.disconnect_reason, "lost (rc=7)")
        self.assertEqual(self.mqtt.calls, [("connect", 0), ("disconnect", 7)])

    async def test_refused_connect_is_a_failure(self):
        self.mqtt.client.on_connect(self.mqtt.client, None, {}, 5)
        await self.settle()
        self.assertFalse(self.health.connected)
        self.assertEqual(self.health.connect_fail_reason, "refused: not authorised")
        self.assertEqual(self.mqtt.calls, [("connect", 5)])  # the SDK still sees it

    async def test_failure_before_connack_is_reported(self):
        self.mqtt.client.on_connect_fail(self.mqtt.client, None)
        await self.settle()
        self.assertEqual(self.health.connect_fail_reason, "connection failed before CONNACK")

    async def test_rebuilt_clients_are_instrumented_too(self):
        client = self.mqtt._build_new_client()
        self.assertTrue(callable(client.on_connect_fail))
        client.on_connect(client, None, {}, 0)
        await self.settle()
        self.assertEqual(self.health.client_id, "web_user_2")
        self.assertEqual(self.mqtt.calls, [("connect", 0)])

    async def test_already_connected_client_counts_as_connected(self):
        health = CollectorHealth()
        instrument_mqtt(FakeSdkMqtt(connected=True), asyncio.get_running_loop(), health)
        self.assertTrue(health.connected)

    async def test_listeners_hear_every_change_until_removed(self):
        heard = []
        remove = self.health.async_add_listener(lambda: heard.append(self.health.connected))
        self.health.note_connected("c")
        self.health.note_disconnected("lost")
        remove()
        self.health.note_connected("c")
        self.assertEqual(heard, [True, False])


class CloudConnectedSensorTest(unittest.IsolatedAsyncioTestCase):
    def test_state_and_attributes_follow_the_health(self):
        health = CollectorHealth()
        sensor = NavimowCloudConnected(health, DEVICE)
        self.assertEqual(sensor.unique_id, "navimow_dev-1_cloud_connected")
        self.assertFalse(sensor.is_on)
        health.note_connected("web_user_1")
        self.assertTrue(sensor.is_on)
        health.note_disconnected("lost (rc=7)")
        attrs = sensor.extra_state_attributes
        self.assertFalse(sensor.is_on)
        self.assertEqual(attrs["client_id"], "web_user_1")
        self.assertEqual(attrs["disconnect_reason"], "lost (rc=7)")
        self.assertEqual(sensor.entity_category, "diagnostic")
        self.assertEqual(sensor.device_class, "connectivity")

    async def test_sensor_writes_its_state_on_every_change(self):
        health = CollectorHealth()
        sensor = NavimowCloudConnected(health, DEVICE)
        sensor.async_write_ha_state = Mock()
        sensor.async_on_remove = Mock()
        await sensor.async_added_to_hass()
        health.note_connected("c")
        health.note_disconnected("lost")
        self.assertEqual(sensor.async_write_ha_state.call_count, 2)
        sensor.async_on_remove.assert_called_once()


class LastMessageSensorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.health = CollectorHealth()
        self.sensor = NavimowLastMessageSensor(self.health, DEVICE)
        self.sensor.hass = self.hass
        self.sensor.async_write_ha_state = Mock()
        self.removers = []
        self.sensor.async_on_remove = self.removers.append
        self.now = 1000.0
        self.sensor._clock = lambda: self.now
        await self.sensor.async_added_to_hass()

    def writes(self):
        return self.sensor.async_write_ha_state.call_count

    async def test_first_message_is_written_at_once(self):
        self.assertIsNone(self.sensor.native_value)
        self.health.note_message("dev-1")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.sensor.native_value, self.health.last_message_at["dev-1"])
        self.assertEqual(self.sensor.unique_id, "navimow_dev-1_last_message")
        self.assertEqual(self.sensor.device_class, "timestamp")

    async def test_other_mowers_messages_are_ignored(self):
        self.health.note_message("dev-2")
        self.assertEqual(self.writes(), 0)

    async def test_burst_is_throttled_and_its_last_message_flushed(self):
        self.health.note_message("dev-1")
        first = self.sensor.native_value
        for step in (2, 4, 6):
            self.now = 1000.0 + step
            self.health.note_message("dev-1")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.sensor.native_value, first)
        self.assertIsNotNone(self.sensor._cancel_flush)
        self.sensor._flush(None)  # the scheduled write at the end of the interval
        self.assertEqual(self.writes(), 2)
        self.assertEqual(self.sensor.native_value, self.health.last_message_at["dev-1"])

    async def test_message_after_the_interval_is_written_at_once(self):
        self.health.note_message("dev-1")
        self.now += 30
        self.health.note_message("dev-1")
        self.assertEqual(self.writes(), 2)

    async def test_removal_cancels_a_pending_write(self):
        self.health.note_message("dev-1")
        self.now += 1
        self.health.note_message("dev-1")
        for remove in self.removers:
            remove()
        self.assertIsNone(self.sensor._cancel_flush)
        self.health.note_message("dev-1")
        self.assertEqual(self.writes(), 1)
