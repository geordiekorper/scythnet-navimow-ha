"""The coordinator, fed by the client: the latest state, the two sources kept
apart, every REST reply, the location record and what was restored."""
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant
from mower_sdk.location import LocationDecoder
from mower_sdk.models import (
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceStatus,
    RejectedMessage,
)

from custom_components.navimow.coordinator import NavimowCoordinator

from tests.fakes import REST_PAYLOAD, FakeClient, device, mqtt_state, rest_state
from tests.test_location import POSE, RECEIVED, RECEIVED_AT, decode

DETAIL_KEYS = {
    "mqtt_state", "mqtt_raw_state", "mqtt_battery", "mqtt_timestamp",
    "mqtt_received_at", "rest_status", "rest_vehicle_state", "rest_battery",
    "rest_battery_level", "rest_timestamp", "rest_polled_at",
}
POLLED_AT = datetime(2026, 9, 17, 20, 2, tzinfo=timezone.utc)


class CoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.client = FakeClient()
        self.coordinator = NavimowCoordinator(
            hass=self.hass, client=self.client, device=device(), config_entry=None
        )
        self.coordinator.async_register()
        self.published = []
        self.coordinator.async_add_listener(lambda: self.published.append(self.coordinator.data))

    def feed(self, *entries, decoder=None):
        """One location message through the SDK's decoder; each applied entry
        reaches the coordinator as a state carrying the record after it."""
        for message in decode(*entries, decoder=decoder):
            self.coordinator.ingest_state(mqtt_state(location=message.location))

    # ---- registration and the two sources --------------------------------------

    def test_registers_for_its_own_device_only(self):
        for channel in ("state", "event", "attributes", "rejected"):
            ((_, device_id),) = self.client.callbacks[channel]
            self.assertEqual(device_id, "dev-1", channel)

    def test_before_any_state_there_is_nothing(self):
        self.assertIsNone(self.coordinator.get_device_state())
        self.assertEqual(self.coordinator.get_data_source(), "none")
        self.assertEqual(self.coordinator.get_rest_details(), (None, None))
        self.assertEqual(self.coordinator.get_rejected(), (0, None))
        self.assertIsNone(self.coordinator.get_dock_position())

    def test_an_mqtt_state_labels_the_source_and_fills_the_snapshot(self):
        state = mqtt_state()
        self.client.deliver("state", state)
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        self.assertIs(self.coordinator.get_device_state(), state)
        details = self.coordinator.get_source_details()
        self.assertEqual(details["mqtt_state"], "mowing")
        self.assertEqual(details["mqtt_raw_state"], "isRunning")
        self.assertEqual(details["mqtt_battery"], 80)
        self.assertEqual(details["mqtt_timestamp"], 1700000000)
        self.assertEqual(details["mqtt_received_at"], RECEIVED)
        for key in (k for k in DETAIL_KEYS if k.startswith("rest_")):
            self.assertIsNone(details[key], key)
        self.assertEqual(len(self.published), 1)

    def test_a_rest_state_labels_the_fallback(self):
        state = rest_state()
        self.coordinator.ingest_state(state)
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.assertIs(self.coordinator.get_device_state(), state)
        self.assertEqual(state.status.value, "docked")
        details = self.coordinator.get_source_details()
        self.assertIsNone(details["mqtt_state"])
        self.assertIsNone(details["rest_status"])  # the reply itself comes through the poll

    def test_every_poll_reply_is_kept_whether_shown_or_not(self):
        shown = mqtt_state()
        self.coordinator.ingest_state(shown)
        status = DeviceStatus.from_dict({**REST_PAYLOAD, "vehicleState": "isIdel"})
        self.coordinator.note_rest_status(status, POLLED_AT)
        self.assertIs(self.coordinator.get_device_state(), shown)  # MQTT is what is shown
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        details = self.coordinator.get_source_details()
        self.assertEqual(details["mqtt_state"], "mowing")
        self.assertEqual(details["rest_status"], "idle")
        self.assertEqual(details["rest_vehicle_state"], "isIdel")
        self.assertEqual(details["rest_battery"], 100)
        self.assertEqual(details["rest_battery_level"], "FULL")
        self.assertIsNone(details["rest_timestamp"])  # the API sends none
        self.assertEqual(details["rest_polled_at"], POLLED_AT.isoformat())
        self.assertEqual(len(self.published), 2)  # a row per poll

    def test_a_later_mqtt_state_keeps_the_rest_side(self):
        self.coordinator.note_rest_status(DeviceStatus.from_dict(dict(REST_PAYLOAD)), POLLED_AT)
        self.coordinator.ingest_state(rest_state())
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.coordinator.ingest_state(mqtt_state(state="docked", raw="isDocked"))
        details = self.coordinator.get_source_details()
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        self.assertEqual(details["mqtt_state"], "docked")
        self.assertEqual(details["rest_status"], "docked")
        self.assertEqual(details["rest_polled_at"], POLLED_AT.isoformat())

    def test_details_contain_no_secrets_and_only_the_known_keys(self):
        self.coordinator.ingest_state(mqtt_state())
        self.coordinator.note_rest_status(DeviceStatus.from_dict(dict(REST_PAYLOAD)), POLLED_AT)
        details = self.coordinator.get_source_details()
        self.assertEqual(set(details), DETAIL_KEYS)
        text = json.dumps(details).lower()
        for word in ("token", "authorization", "bearer", "password", "pwd", "secret"):
            self.assertNotIn(word, text)

    def test_rest_details_show_the_reply_as_sent(self):
        self.coordinator.note_rest_status(
            DeviceStatus.from_dict({**REST_PAYLOAD, "vehicleState": "isIdel", "foo": -61}),
            POLLED_AT,
        )
        state, attrs = self.coordinator.get_rest_details()
        self.assertEqual(state, "isIdel")  # not the SDK's "idle"
        self.assertEqual(attrs, {
            "battery": 100, "battery_level": "FULL",
            "polled_at": POLLED_AT.isoformat(),
            "unknown_fields": {"foo": -61},
        })

    def test_rest_details_of_a_reply_with_only_known_fields(self):
        self.coordinator.note_rest_status(DeviceStatus.from_dict(dict(REST_PAYLOAD)), POLLED_AT)
        self.assertIsNone(self.coordinator.get_rest_details()[1]["unknown_fields"])

    # ---- the location record ---------------------------------------------------------

    def test_each_entry_of_a_message_is_published_with_its_record(self):
        poses = [
            {**POSE, "postureX": f"{i}.000", "time": 1700000000000 + 2000 * i}
            for i in range(1, 5)
        ]
        self.feed(*poses)
        self.assertEqual([data["location"].x for data in self.published], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(self.coordinator.get_device_location().x, 4.0)

    def test_a_state_without_a_record_leaves_the_last_one(self):
        self.feed(POSE)
        self.coordinator.ingest_state(mqtt_state(state="docked"))
        self.assertEqual(self.coordinator.get_device_location().x, 1.5)

    def test_a_task_entry_counts_as_a_task_report_timed_or_not(self):
        self.assertFalse(self.coordinator.has_task_report())
        self.feed(POSE)
        self.assertFalse(self.coordinator.has_task_report())
        self.feed({"type": 2, "currentMowProgress": 5000})
        self.assertTrue(self.coordinator.has_task_report())

    def test_the_delay_time_is_first_seen(self):
        later = datetime(2026, 9, 17, 20, 2, tzinfo=timezone.utc)
        decoder = LocationDecoder()
        (first,) = decoder.decode("dev-1", [{"type": 4, "taskDelay": False}], RECEIVED_AT).messages
        self.coordinator.ingest_state(mqtt_state(location=first.location))
        self.assertEqual(self.coordinator.get_delay_since(), RECEIVED_AT)
        (repeat,) = decoder.decode("dev-1", [{"type": 4, "taskDelay": False}], later).messages
        self.coordinator.ingest_state(mqtt_state(location=repeat.location))
        self.assertEqual(self.coordinator.get_device_location().delay_received_at, later)
        self.assertEqual(self.coordinator.get_delay_since(), RECEIVED_AT)  # a repeat keeps it
        (change,) = decoder.decode("dev-1", [{"type": 4, "taskDelay": True}], later).messages
        self.coordinator.ingest_state(mqtt_state(location=change.location))
        self.assertEqual(self.coordinator.get_delay_since(), later)

    def test_the_dock_is_read_from_the_record(self):
        self.coordinator.ingest_state(mqtt_state(location=DeviceLocation(
            device_id="dev-1", x=1.0, y=2.0, dock_x=0.5, dock_y=0.25, dock_samples=7,
        )))
        self.assertEqual(self.coordinator.get_dock_position(), {"x": 0.5, "y": 0.25, "n": 7})
        self.coordinator.ingest_state(mqtt_state(location=DeviceLocation(device_id="dev-1", x=1.0)))
        self.assertIsNone(self.coordinator.get_dock_position())

    # ---- the restore after a restart ---------------------------------------------

    def test_restore_assembles_one_record_and_hands_it_to_the_client_once(self):
        self.coordinator.restore_location("pose", {
            "x": 2.0, "y": 0.3, "theta": 0.35, "vehicle_state": 1,
            "pose_at": 1700000000000, "pose_received_at": RECEIVED,
        })
        self.coordinator.restore_location("task", {"current_zone": 2, "task_at": 1700000000032})
        self.coordinator.restore_location("target", {"partition_ids": [3]})
        # the entities show what was restored so far, at once
        self.assertEqual([data["location"].x for data in self.published], [2.0, 2.0, 2.0])
        location = self.coordinator.get_device_location()
        self.assertEqual((location.x, location.current_zone, location.partition_ids), (2.0, 2, (3,)))
        self.assertEqual(location.pose_received_at, RECEIVED_AT)
        for group in ("pose", "task", "target"):
            self.assertTrue(self.coordinator.is_group_restored(group), group)
        self.assertFalse(self.coordinator.is_group_restored("delay"))
        self.assertTrue(self.coordinator.has_task_report())
        self.client.restore_location.assert_not_called()
        self.coordinator.async_finish_restore()
        self.client.restore_location.assert_called_once_with("dev-1", location)

    def test_nothing_restored_hands_nothing_to_the_client(self):
        self.coordinator.restore_location("pose", {})
        self.assertIsNone(self.coordinator.get_device_location())
        self.coordinator.async_finish_restore()
        self.client.restore_location.assert_not_called()

    def test_the_startup_state_carries_the_restored_record_and_keeps_it_restored(self):
        self.coordinator.restore_location("pose", {"x": 2.0, "y": 0.3, "pose_at": 1700000000000})
        restored = self.coordinator.get_device_location()
        # the connect's poll: a state whose record is the installed one, which
        # the decoder marked with the record's own times
        installed = replace(restored, marks={1: 1700000000000})
        self.coordinator.ingest_state(rest_state(location=installed))
        self.assertTrue(self.coordinator.is_group_restored("pose"))
        self.assertEqual(self.coordinator.get_device_location().x, 2.0)

    def test_a_live_entry_ends_the_restore_of_its_group_and_live_data_wins(self):
        self.coordinator.restore_location("pose", {"x": 2.0, "y": 0.3})
        decoder = LocationDecoder()
        decoder.restore("dev-1", self.coordinator.get_device_location())
        self.feed(POSE, decoder=decoder)
        self.assertEqual(self.coordinator.get_device_location().x, 1.5)
        self.assertFalse(self.coordinator.is_group_restored("pose"))
        # a sensor restoring after live data arrived changes nothing
        self.coordinator.restore_location("task", {"current_zone": 9})
        self.assertIsNone(self.coordinator.get_device_location().current_zone)

    def test_a_restored_delay_time_survives_a_live_repeat(self):
        self.coordinator.restore_location(
            "delay", {"task_delay": False, "delay_received_at": RECEIVED}
        )
        self.assertEqual(self.coordinator.get_delay_since(), RECEIVED_AT)
        decoder = LocationDecoder()
        decoder.restore("dev-1", self.coordinator.get_device_location())
        later = datetime(2026, 9, 17, 20, 2, tzinfo=timezone.utc)
        (repeat,) = decoder.decode("dev-1", [{"type": 4, "taskDelay": False}], later).messages
        self.coordinator.ingest_state(mqtt_state(location=repeat.location))
        self.assertEqual(self.coordinator.get_device_location().delay_received_at, later)
        self.assertEqual(self.coordinator.get_delay_since(), RECEIVED_AT)
        self.assertFalse(self.coordinator.is_group_restored("delay"))

    # ---- the other channels ----------------------------------------------------------

    def test_rejected_input_counts_and_publishes_each_item(self):
        self.coordinator.record_rejected("location", "/t", "stale", "[1]")
        self.coordinator.record_rejected("state", "/s", "unknown_field", {"x": 1})
        self.assertEqual([data for data in self.published].__len__(), 2)
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 2)
        self.assertEqual(latest["channel"], "state")
        self.assertEqual(latest["reason"], "unknown_field")
        self.assertIsNotNone(latest["received_at"])

    def test_a_rejected_message_is_recorded_as_the_mower_sent_it(self):
        self.client.deliver("rejected", RejectedMessage(
            channel="state", topic="/downlink/vehicle/dev-1/realtimeDate/state", device_id="dev-1",
            reason="stale", reasons=("stale", "unknown_field"),
            payload=b'{"state":"isRunning","device_id":"dev-1"}', received_at=RECEIVED_AT,
            original=b'{"state":"isRunning"}',
        ))
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 1)
        self.assertEqual((latest["channel"], latest["reason"]), ("state", "stale"))
        self.assertEqual(latest["reasons"], ["stale", "unknown_field"])
        self.assertEqual(json.loads(latest["payload"]), {"state": "isRunning"})

    def test_a_rejected_message_without_the_original_uses_the_payload(self):
        self.client.deliver("rejected", RejectedMessage(
            channel="location", topic="/t", device_id="dev-1", reason="unparsable",
            reasons=("unparsable",), payload=b"not json", received_at=RECEIVED_AT,
        ))
        self.assertEqual(self.coordinator.get_rejected()[1]["payload"], "not json")

    def test_an_event_message_is_recorded_whole_and_changes_no_state(self):
        event = DeviceEventMessage.from_dict({"device_id": "dev-1", "event": "bladeBlocked", "timestamp": 1})
        self.client.deliver("event", event)
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 1)
        self.assertEqual((latest["channel"], latest["topic"], latest["reason"]), ("event", None, "unknown_channel"))
        self.assertEqual(json.loads(latest["payload"]), {"device_id": "dev-1", "event": "bladeBlocked", "timestamp": 1})
        self.assertIsNone(self.coordinator.get_device_state())
        self.client.deliver("event", DeviceEventMessage.from_dict({"device_id": "dev-2", "event": "x"}))
        self.assertEqual(self.coordinator.get_rejected()[0], 1)  # another mower's

    def test_an_attributes_message_is_recorded_whole_and_applied(self):
        attrs = DeviceAttributesMessage.from_dict({"device_id": "dev-1", "attributes": {"firmware": "1.2"}})
        self.client.deliver("attributes", attrs)
        self.assertEqual(self.coordinator.get_rejected()[1]["channel"], "attributes")
        self.assertIs(self.coordinator.get_device_attributes(), attrs)

    async def test_a_refresh_changes_nothing(self):
        state = mqtt_state()
        self.coordinator.ingest_state(state)
        await self.coordinator.async_refresh()
        self.assertIs(self.coordinator.get_device_state(), state)
        self.assertTrue(self.coordinator.last_update_success)
