"""Collector health: the SDK client's counters and reasons, the flag and stamps
its connection events set, the poll's outcome, and the diagnostic entities
that show them."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.navimow.binary_sensor import NavimowCloudConnected
from custom_components.navimow.health import CollectorHealth
from custom_components.navimow.sensor import (
    NavimowCollectorStatusSensor,
    NavimowLastMessageSensor,
)
from tests.fakes import FakeClient, FakeMqtt, device, hass_for

DEVICE = device()


def event(kind, rebuilds=0):
    return SimpleNamespace(kind=kind, client_id="web_user_1", reason=None, at=None, rebuilds=rebuilds)


class HealthFromClientTest(unittest.TestCase):
    def setUp(self):
        self.mqtt = FakeMqtt()
        self.health = CollectorHealth(FakeClient(self.mqtt))

    def test_counters_reasons_and_client_id_are_the_clients(self):
        self.mqtt.connects, self.mqtt.disconnects, self.mqtt.connect_failures = 3, 2, 1
        self.mqtt.rebuilds, self.mqtt.last_rebuild_reason = 1, "watchdog: silence"
        self.mqtt.last_disconnect_reason = "Unspecified error"
        self.mqtt.last_connect_fail_reason = "connection failed before CONNACK"
        self.assertEqual(
            (self.health.connects, self.health.disconnects, self.health.connect_failures,
             self.health.rebuilds, self.health.last_rebuild_reason),
            (3, 2, 1, 1, "watchdog: silence"),
        )
        attrs = self.health.connection_attributes()
        self.assertEqual(attrs["client_id"], "web_user_1")
        self.assertEqual(attrs["disconnect_reason"], "Unspecified error")
        self.assertEqual(attrs["connect_fail_reason"], "connection failed before CONNACK")

    def test_before_the_connect_there_is_no_mqtt_client(self):
        health = CollectorHealth(FakeClient(None))
        self.assertEqual((health.connects, health.disconnects, health.connect_failures, health.rebuilds), (0, 0, 0, 0))
        self.assertIsNone(health.client_id)
        self.assertIsNone(health.last_message_at("dev-1"))
        self.assertEqual(health.status, "starting")
        health = CollectorHealth()  # the client is set after construction
        self.assertEqual(health.connects, 0)
        health.client = FakeClient(self.mqtt)
        self.mqtt.connects = 2
        self.assertEqual(health.connects, 2)

    def test_the_connection_events_set_the_flag_and_the_stamps(self):
        self.health.note_connection(event("connected"))
        self.assertTrue(self.health.connected)
        self.assertIsNotNone(self.health.connected_at)
        self.health.note_connection(event("disconnected"))
        self.assertFalse(self.health.connected)
        self.assertIsNotNone(self.health.disconnected_at)
        self.health.note_connection(event("connected"))
        self.health.note_connection(event("connect_failed"))
        self.assertFalse(self.health.connected)
        self.assertIsNotNone(self.health.connect_failed_at)

    def test_a_connect_after_failed_attempts_counts_one_recovery(self):
        self.health.note_connection(event("connected"))
        self.assertEqual(self.health.credential_refreshes, 0)
        self.health.note_connection(event("connect_failed"))
        self.health.note_connection(event("connect_failed"))
        self.assertEqual(self.health.credential_refreshes, 0)  # not recovered yet
        self.health.note_connection(event("connected"))
        self.assertEqual(self.health.credential_refreshes, 1)
        self.health.note_connection(event("disconnected"))
        self.health.note_connection(event("connected"))  # a plain reconnect
        self.assertEqual(self.health.credential_refreshes, 1)

    def test_message_times_are_the_clients(self):
        stamp = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        self.mqtt.message_at["dev-1"] = stamp
        self.assertEqual(self.health.last_message_at("dev-1"), stamp)
        self.assertIsNone(self.health.last_message_at("dev-2"))

    def test_a_rebuild_is_stamped_once_when_the_client_counted_it(self):
        heard = []
        self.health.async_add_listener(lambda: heard.append(self.health.last_rebuild_at))
        self.health.note_rebuild()  # nothing rebuilt: no stamp
        self.assertIsNone(self.health.last_rebuild_at)
        self.mqtt.rebuilds, self.mqtt.last_rebuild_reason = 1, "credentials updated while disconnected"
        self.health.note_rebuild()
        self.health.note_rebuild()  # the same rebuild
        self.assertIsNotNone(self.health.last_rebuild_at)
        self.assertEqual(len(heard), 1)
        self.assertEqual(self.health.status_attributes()["last_rebuild_reason"],
                         "credentials updated while disconnected")

    def test_the_connect_of_a_rebuilt_client_stamps_the_rebuild(self):
        self.mqtt.rebuilds, self.mqtt.last_rebuild_reason = 1, "requested"
        self.health.note_connection(event("connected", rebuilds=1))
        self.assertIsNotNone(self.health.last_rebuild_at)

    def test_listeners_hear_every_change_until_removed(self):
        heard = []
        remove = self.health.async_add_listener(lambda: heard.append(self.health.connected))
        self.health.note_connected()
        self.health.note_disconnected()
        remove()
        self.health.note_connected()
        self.assertEqual(heard, [True, False])

    def test_message_listeners_hear_the_device(self):
        heard = []
        remove = self.health.async_add_message_listener(heard.append)
        self.health.note_message("dev-1")
        remove()
        self.health.note_message("dev-1")
        self.assertEqual(heard, ["dev-1"])


class CloudConnectedSensorTest(unittest.IsolatedAsyncioTestCase):
    def test_state_and_attributes_follow_the_health(self):
        mqtt = FakeMqtt()
        health = CollectorHealth(FakeClient(mqtt))
        sensor = NavimowCloudConnected(health, DEVICE)
        self.assertEqual(sensor.unique_id, "navimow_dev-1_cloud_connected")
        self.assertFalse(sensor.is_on)
        health.note_connected()
        self.assertTrue(sensor.is_on)
        mqtt.last_disconnect_reason = "Unspecified error"
        health.note_disconnected()
        attrs = sensor.extra_state_attributes
        self.assertFalse(sensor.is_on)
        self.assertEqual(attrs["client_id"], "web_user_1")
        self.assertEqual(attrs["disconnect_reason"], "Unspecified error")
        self.assertEqual(sensor.entity_category, "diagnostic")
        self.assertEqual(sensor.device_class, "connectivity")

    async def test_sensor_writes_its_state_on_every_change(self):
        health = CollectorHealth(FakeClient(FakeMqtt()))
        sensor = NavimowCloudConnected(health, DEVICE)
        sensor.async_write_ha_state = Mock()
        sensor.async_on_remove = Mock()
        await sensor.async_added_to_hass()
        health.note_connected()
        health.note_disconnected()
        self.assertEqual(sensor.async_write_ha_state.call_count, 2)
        sensor.async_on_remove.assert_called_once()


class LastMessageSensorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = hass_for(self)
        self.mqtt = FakeMqtt()
        self.health = CollectorHealth(FakeClient(self.mqtt))
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

    def message(self, device_id):
        """A message arrives: the client times it, then the facade's
        on_message_seen hook notes it."""
        self.mqtt.message_at[device_id] = datetime.now(timezone.utc)
        self.health.note_message(device_id)

    async def test_first_message_is_written_at_once(self):
        self.assertIsNone(self.sensor.native_value)
        self.message("dev-1")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.sensor.native_value, self.mqtt.message_at["dev-1"])
        self.assertEqual(self.sensor.unique_id, "navimow_dev-1_last_message")
        self.assertEqual(self.sensor.device_class, "timestamp")

    async def test_message_before_the_entity_existed_is_shown(self):
        mqtt = FakeMqtt()
        mqtt.message_at["dev-1"] = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        sensor = NavimowLastMessageSensor(CollectorHealth(FakeClient(mqtt)), DEVICE)
        sensor.async_on_remove = Mock()
        await sensor.async_added_to_hass()
        self.assertEqual(sensor.native_value, mqtt.message_at["dev-1"])

    async def test_other_mowers_messages_are_ignored(self):
        self.message("dev-2")
        self.assertEqual(self.writes(), 0)

    async def test_burst_is_throttled_and_its_last_message_flushed(self):
        self.message("dev-1")
        first = self.sensor.native_value
        for step in (2, 4, 6):
            self.now = 1000.0 + step
            self.message("dev-1")
        self.assertEqual(self.writes(), 1)
        self.assertEqual(self.sensor.native_value, first)
        self.assertIsNotNone(self.sensor._cancel_flush)
        self.sensor._flush(None)  # the scheduled write at the end of the interval
        self.assertEqual(self.writes(), 2)
        self.assertEqual(self.sensor.native_value, self.mqtt.message_at["dev-1"])

    async def test_message_after_the_interval_is_written_at_once(self):
        self.message("dev-1")
        self.now += 30
        self.message("dev-1")
        self.assertEqual(self.writes(), 2)

    async def test_removal_cancels_a_pending_write(self):
        self.message("dev-1")
        self.now += 1
        self.message("dev-1")
        for remove in self.removers:
            remove()
        self.assertIsNone(self.sensor._cancel_flush)
        self.message("dev-1")
        self.assertEqual(self.writes(), 1)


class CollectorStatusTest(unittest.TestCase):
    def setUp(self):
        self.mqtt = FakeMqtt()
        self.health = CollectorHealth(FakeClient(self.mqtt))
        self.heard = []
        self.health.async_add_listener(lambda: self.heard.append(self.health.status))

    def test_status_follows_connection_and_poll(self):
        self.assertEqual(self.health.status, "starting")
        self.mqtt.connect_failures = 1
        self.health.note_connect_failed()
        self.assertEqual(self.health.status, "disconnected")
        self.mqtt.connects = 1
        self.health.note_connected()
        self.assertEqual(self.health.status, "ok")
        self.health.note_poll_error("HTTP 429")
        self.assertEqual(self.health.status, "poll_failing")
        self.health.note_disconnected()
        self.assertEqual(self.health.status, "disconnected")

    def test_counters_and_latest_events(self):
        self.mqtt.connects, self.mqtt.disconnects, self.mqtt.connect_failures = 2, 1, 1
        self.mqtt.rebuilds, self.mqtt.last_rebuild_reason = 1, "watchdog: silence"
        self.health.note_connect_failed()
        self.health.note_connected()
        self.health.note_rebuild()
        self.health.poll_interval = 300
        attrs = self.health.status_attributes()
        self.assertEqual(
            (attrs["connects"], attrs["disconnects"], attrs["connect_failures"],
             attrs["credential_refreshes"], attrs["rebuilds"]),
            (2, 1, 1, 1, 1),
        )
        self.assertEqual(attrs["last_rebuild_reason"], "watchdog: silence")
        self.assertIsNotNone(attrs["last_rebuild_at"])
        self.assertEqual(attrs["poll_interval"], 300)
        self.assertIsNone(attrs["last_poll_error"])

    def test_token_expiry_notifies_only_on_change(self):
        self.health.note_token(1700000000)
        self.health.note_token(1700000000.0)
        self.health.note_token(None)
        self.assertEqual(len(self.heard), 1)
        self.assertTrue(self.health.token_expires_at.startswith("2023-11-14T22:13:20"))

    def test_poll_outcome_announces_each_failure_and_the_recovery_once(self):
        self.health.note_poll_ok()  # healthy, as before: nothing to say
        self.assertEqual(self.heard, [])
        self.health.note_poll_error("HTTP 429")
        self.health.note_poll_error("HTTP 429")  # failed again: a row each
        self.health.note_poll_ok()  # recovered
        self.health.note_poll_ok()  # still healthy: nothing
        self.assertEqual(len(self.heard), 3)
        attrs = self.health.status_attributes()
        self.assertIsNone(attrs["last_poll_error"])
        self.assertIsNone(attrs["last_poll_error_at"])


class CollectorStatusSensorTest(unittest.IsolatedAsyncioTestCase):
    async def test_sensor_shows_status_and_writes_on_change(self):
        mqtt = FakeMqtt()
        health = CollectorHealth(FakeClient(mqtt))
        sensor = NavimowCollectorStatusSensor(health, DEVICE)
        sensor.async_write_ha_state = Mock()
        sensor.async_on_remove = Mock()
        await sensor.async_added_to_hass()
        self.assertEqual(sensor.unique_id, "navimow_dev-1_collector_status")
        self.assertEqual(sensor.native_value, "starting")
        mqtt.connects = 1
        health.note_connected()
        self.assertEqual(sensor.native_value, "ok")
        self.assertEqual(sensor.extra_state_attributes["connects"], 1)
        sensor.async_write_ha_state.assert_called_once()
