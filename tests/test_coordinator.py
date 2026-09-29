"""Coordinator source tracking: MQTT and REST are kept apart, and the source
label changes only when a newer message or a REST result is adopted."""
import asyncio
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from mower_sdk.models import DeviceStateMessage, DeviceStatus

from custom_components.navimow.coordinator import NavimowCoordinator
from mower_sdk.location import LocationDecoder

from tests.test_location import POSE, RECEIVED, RECEIVED_AT, decode

# The full field set the REST status endpoint has been seen to return.
REST_PAYLOAD = {
    "id": "dev-1",
    "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 100}],
    "vehicleState": "isDocked",
    "descriptiveCapacityRemaining": "FULL",
}
DETAIL_KEYS = {
    "mqtt_state", "mqtt_raw_state", "mqtt_battery", "mqtt_timestamp",
    "mqtt_received_at", "rest_status", "rest_vehicle_state", "rest_battery",
    "rest_battery_level", "rest_timestamp", "rest_polled_at",
}


def mqtt_message(state="mowing", battery=80, timestamp=1700000000, raw="isRunning"):
    return DeviceStateMessage(
        device_id="dev-1", timestamp=timestamp, state=state, battery=battery,
        metrics={"raw_state": raw},
    )


class CoordinatorSourceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = HomeAssistant(self.temp.name)
        self.addAsyncCleanup(self.hass.async_stop, force=True)
        self.sdk = SimpleNamespace(
            on_state=Mock(), on_attributes=Mock(),
            get_cached_state=Mock(return_value=None),
            get_cached_attributes=Mock(return_value=None),
            restore_location=Mock(),
        )
        self.api = SimpleNamespace(
            async_get_device_status=AsyncMock(
                return_value=DeviceStatus.from_dict(dict(REST_PAYLOAD))
            ),
            set_token=Mock(), _token="secret-token",
        )
        device = SimpleNamespace(
            id="dev-1", name="Mower", model="X430", firmware_version="1.0",
            serial_number="SN1",
        )
        self.coordinator = NavimowCoordinator(
            hass=self.hass, sdk=self.sdk, api=self.api, device=device,
            config_entry=None,
        )
        await self.coordinator.async_setup()

    def mqtt_is_fresh(self):
        self.coordinator._last_mqtt_update = time.monotonic()

    async def test_mqtt_push_labels_the_source_and_fills_the_snapshot(self):
        msg = mqtt_message()
        self.coordinator._handle_state(msg)
        await asyncio.sleep(0)  # the callback hands the message to the loop
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        self.assertIs(self.coordinator.get_device_state(), msg)
        details = self.coordinator.get_source_details()
        self.assertEqual(details["mqtt_state"], "mowing")
        self.assertEqual(details["mqtt_raw_state"], "isRunning")
        self.assertEqual(details["mqtt_battery"], 80)
        self.assertEqual(details["mqtt_timestamp"], 1700000000)
        self.assertIsNotNone(details["mqtt_received_at"])
        for key in (k for k in DETAIL_KEYS if k.startswith("rest_")):
            self.assertIsNone(details[key], key)

    async def test_poll_that_finds_the_same_message_keeps_the_label(self):
        msg = mqtt_message()
        self.coordinator._update_from_state(msg, "2026-09-17T20:00:00+00:00")
        self.sdk.get_cached_state.return_value = msg
        self.mqtt_is_fresh()
        await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        self.assertEqual(
            self.coordinator.get_source_details()["mqtt_received_at"],
            "2026-09-17T20:00:00+00:00",
        )
        self.api.async_get_device_status.assert_not_awaited()

    async def test_cached_message_is_adopted_once(self):
        msg = mqtt_message()
        self.sdk.get_cached_state.return_value = msg
        self.mqtt_is_fresh()
        await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_cache")
        self.assertIs(self.coordinator.get_device_state(), msg)
        first = self.coordinator.get_source_details()["mqtt_received_at"]
        await self.coordinator._async_update_data()
        self.assertEqual(
            self.coordinator.get_source_details()["mqtt_received_at"], first
        )

    async def test_rest_fallback_labels_the_source_and_keeps_extra(self):
        await self.coordinator._async_update_data()  # no MQTT yet: stale, so REST
        self.api.async_get_device_status.assert_awaited_once_with("dev-1")
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.assertEqual(self.coordinator.get_device_state().state, "docked")
        details = self.coordinator.get_source_details()
        self.assertEqual(details["rest_status"], "docked")
        self.assertEqual(details["rest_vehicle_state"], "isDocked")
        self.assertEqual(details["rest_battery"], 100)
        self.assertEqual(details["rest_battery_level"], "FULL")
        self.assertIsNone(details["rest_timestamp"])  # the API sends none
        self.assertIsNotNone(details["rest_polled_at"])
        self.assertIsNone(details["mqtt_state"])

    async def test_snapshots_do_not_overwrite_each_other(self):
        self.coordinator._update_from_state(mqtt_message(), "2026-09-17T20:00:00+00:00")
        self.coordinator._last_mqtt_update = None  # MQTT went quiet
        await self.coordinator._async_update_data()
        details = self.coordinator.get_source_details()
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.assertEqual(details["mqtt_state"], "mowing")
        self.assertEqual(details["rest_status"], "docked")
        self.coordinator._update_from_state(
            mqtt_message(state="docked", raw="isDocked"), "2026-09-17T20:05:00+00:00"
        )
        details = self.coordinator.get_source_details()
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")
        self.assertEqual(details["mqtt_state"], "docked")
        self.assertEqual(details["mqtt_received_at"], "2026-09-17T20:05:00+00:00")
        self.assertEqual(details["rest_status"], "docked")
        self.assertIsNotNone(details["rest_polled_at"])

    async def test_poll_after_rest_does_not_revert_to_the_stale_mqtt_message(self):
        msg = mqtt_message()
        self.coordinator._update_from_state(msg, "2026-09-17T20:00:00+00:00")
        self.sdk.get_cached_state.return_value = msg
        self.coordinator._last_mqtt_update = None
        await self.coordinator._async_update_data()  # REST fallback
        await self.coordinator._async_update_data()  # next poll, inside the hourly limit
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.assertEqual(self.coordinator.get_device_state().state, "docked")
        self.api.async_get_device_status.assert_awaited_once()

    async def test_details_contain_no_secrets_and_only_the_known_keys(self):
        self.coordinator._update_from_state(mqtt_message(), "2026-09-17T20:00:00+00:00")
        self.coordinator._last_mqtt_update = None
        await self.coordinator._async_update_data()
        details = self.coordinator.get_source_details()
        self.assertEqual(set(details), DETAIL_KEYS)
        text = json.dumps(details).lower()
        for word in ("token", "authorization", "bearer", "password", "pwd", "secret"):
            self.assertNotIn(word, text)

    async def test_restore_assembles_one_record_and_hands_it_to_the_sdk_once(self):
        published = []
        self.coordinator.async_add_listener(
            lambda: published.append(self.coordinator.get_device_location())
        )
        self.coordinator.restore_location("pose", {
            "x": 2.0, "y": 0.3, "theta": 0.35, "vehicle_state": 1,
            "pose_at": 1700000000000, "pose_received_at": RECEIVED,
        })
        self.coordinator.restore_location("task", {"current_zone": 2, "task_at": 1700000000032})
        self.coordinator.restore_location("target", {"partition_ids": [3]})
        # the entities show what was restored so far, at once
        self.assertEqual([loc.x for loc in published], [2.0, 2.0, 2.0])
        location = self.coordinator.get_device_location()
        self.assertEqual((location.x, location.current_zone, location.partition_ids), (2.0, 2, (3,)))
        self.assertEqual(location.pose_received_at, RECEIVED_AT)
        for group in ("pose", "task", "target"):
            self.assertTrue(self.coordinator.is_group_restored(group), group)
        self.assertFalse(self.coordinator.is_group_restored("delay"))
        self.sdk.restore_location.assert_not_called()
        self.coordinator.async_finish_restore()
        self.sdk.restore_location.assert_called_once_with("dev-1", location)

    async def test_nothing_restored_hands_nothing_to_the_sdk(self):
        self.coordinator.restore_location("pose", {})
        self.assertIsNone(self.coordinator.get_device_location())
        self.coordinator.async_finish_restore()
        self.sdk.restore_location.assert_not_called()

    async def test_a_live_entry_ends_the_restore_of_its_group_and_live_data_wins(self):
        self.coordinator.restore_location("pose", {"x": 2.0, "y": 0.3})
        decoder = LocationDecoder()
        decoder.restore("dev-1", self.coordinator.get_device_location())
        for message in decode(POSE, decoder=decoder):
            self.coordinator.ingest_location(message)
        self.assertEqual(self.coordinator.get_device_location().x, 1.5)
        self.assertFalse(self.coordinator.is_group_restored("pose"))
        # a sensor restoring after live data arrived changes nothing
        self.coordinator.restore_location("task", {"current_zone": 9})
        self.assertIsNone(self.coordinator.get_device_location().current_zone)

    async def test_another_devices_entry_is_ignored(self):
        other = LocationDecoder().decode("dev-2", [POSE], RECEIVED_AT).messages
        self.coordinator.ingest_location(other[0])
        self.assertIsNone(self.coordinator.get_device_location())

    async def test_restored_pose_does_not_train_the_dock(self):
        self.coordinator._last_state = mqtt_message(state="docked", raw="isDocked")
        self.coordinator.restore_location("pose", {"x": 2.0, "y": 0.3})
        decoder = LocationDecoder()
        decoder.restore("dev-1", self.coordinator.get_device_location())
        # a delay entry: the record it carries still holds the restored pose
        (delay,) = decode({"type": 4, "taskDelay": False}, decoder=decoder)
        self.assertEqual((delay.location.x, delay.location.y), (2.0, 0.3))
        self.coordinator.ingest_location(delay)
        self.assertIsNone(self.coordinator.get_dock_position())
        for message in decode(POSE, decoder=decoder):
            self.coordinator.ingest_location(message)
        self.assertEqual(self.coordinator.get_dock_position()["n"], 1)

    async def test_each_entry_of_a_message_is_published(self):
        published = []
        self.coordinator.async_add_listener(
            lambda: published.append(self.coordinator.get_device_location().x)
        )
        poses = [
            {**POSE, "postureX": f"{i}.000", "time": 1700000000000 + 2000 * i}
            for i in range(1, 5)
        ]
        for message in decode(*poses):
            self.coordinator.ingest_location(message)
        self.assertEqual(published, [1.0, 2.0, 3.0, 4.0])

    async def test_rejected_input_counts_and_publishes_each_item(self):
        published = []
        self.coordinator.async_add_listener(
            lambda: published.append(self.coordinator.get_rejected())
        )
        self.assertEqual(self.coordinator.get_rejected(), (0, None))
        self.coordinator.record_rejected("location", "/t", "stale", "[1]")
        self.coordinator.record_rejected("state", "/s", "unknown_field", {"x": 1})
        self.assertEqual([count for count, _ in published], [1, 2])
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 2)
        self.assertEqual(latest["channel"], "state")
        self.assertEqual(latest["reason"], "unknown_field")
        self.assertIsNotNone(latest["received_at"])
        self.assertEqual(published[0][1]["channel"], "location")

    async def test_late_state_message_is_recorded_not_applied(self):
        newer = mqtt_message(state="docked", raw="isDocked", timestamp=1700000060)
        older = mqtt_message(state="mowing", raw="isRunning", timestamp=1700000000)
        self.coordinator._update_from_state(newer, "2026-09-17T20:01:00+00:00")
        self.coordinator._update_from_state(older, "2026-09-17T20:01:05+00:00")
        self.assertIs(self.coordinator.get_device_state(), newer)
        details = self.coordinator.get_source_details()
        self.assertEqual(details["mqtt_state"], "docked")
        self.assertEqual(details["mqtt_received_at"], "2026-09-17T20:01:00+00:00")
        count, latest = self.coordinator.get_rejected()
        self.assertEqual(count, 1)
        self.assertEqual(latest["reason"], "stale")
        self.assertEqual(latest["topic"], "/downlink/vehicle/dev-1/realtimeDate/state")
        self.assertEqual(json.loads(latest["payload"])["timestamp"], 1700000000)

    async def test_equal_and_untimed_state_messages_apply(self):
        first = mqtt_message(timestamp=1700000000)
        same_time = mqtt_message(battery=79, timestamp=1700000000)
        untimed = mqtt_message(battery=78, timestamp=None)
        for msg in (first, same_time, untimed):
            self.coordinator._update_from_state(msg)
            self.assertIs(self.coordinator.get_device_state(), msg)
        self.assertEqual(self.coordinator.get_rejected()[0], 0)

    async def test_milliseconds_and_seconds_compare_alike(self):
        self.coordinator._update_from_state(mqtt_message(timestamp=1700000060000))
        self.coordinator._update_from_state(mqtt_message(state="docked", timestamp=1700000000))
        self.assertEqual(self.coordinator.get_device_state().state, "mowing")
        self.assertEqual(self.coordinator.get_rejected()[1]["reason"], "stale")

    async def test_implausible_state_timestamp_is_rejected(self):
        self.coordinator._update_from_state(mqtt_message(timestamp=86400))  # 1970
        self.assertIsNone(self.coordinator.get_device_state())
        self.assertEqual(self.coordinator.get_rejected()[1]["reason"], "implausible_time")

    async def test_late_cached_message_is_not_adopted(self):
        newer = mqtt_message(state="docked", raw="isDocked", timestamp=1700000060)
        self.coordinator._update_from_state(newer)
        self.sdk.get_cached_state.return_value = mqtt_message(timestamp=1700000000)
        self.mqtt_is_fresh()
        await self.coordinator._async_update_data()
        self.assertIs(self.coordinator.get_device_state(), newer)
        self.assertEqual(self.coordinator.get_data_source(), "mqtt_push")

    async def test_rest_poll_reply_is_kept_and_shown_only_when_mqtt_is_stale(self):
        self.coordinator.apply_rest_status(dict(REST_PAYLOAD), "2026-09-17T20:00:00+00:00")
        self.assertEqual(self.coordinator.get_data_source(), "http_fallback")
        self.assertEqual(self.coordinator.get_device_state().state, "docked")
        msg = mqtt_message()
        self.coordinator._handle_state(msg)
        await asyncio.sleep(0)
        self.coordinator.apply_rest_status(
            {**REST_PAYLOAD, "vehicleState": "isIdel"}, "2026-09-17T20:02:00+00:00"
        )
        self.assertIs(self.coordinator.get_device_state(), msg)  # MQTT is fresh
        details = self.coordinator.get_source_details()
        self.assertEqual(details["rest_vehicle_state"], "isIdel")
        self.assertEqual(details["rest_polled_at"], "2026-09-17T20:02:00+00:00")

    async def test_rest_poll_satisfies_the_hourly_fallback(self):
        self.coordinator.apply_rest_status(dict(REST_PAYLOAD), "2026-09-17T20:00:00+00:00")
        await self.coordinator._async_update_data()
        self.api.async_get_device_status.assert_not_awaited()

    async def test_rest_details_show_the_reply_as_sent(self):
        self.assertEqual(self.coordinator.get_rest_details(), (None, None))
        self.coordinator.apply_rest_status(
            {**REST_PAYLOAD, "vehicleState": "isIdel", "signal": -61},
            "2026-09-17T20:00:00+00:00",
        )
        state, attrs = self.coordinator.get_rest_details()
        self.assertEqual(state, "isIdel")  # not the SDK's "idle"
        self.assertEqual(attrs, {
            "battery": 100, "battery_level": "FULL",
            "polled_at": "2026-09-17T20:00:00+00:00",
            "unknown_fields": {"signal": -61},
        })

    async def test_rest_details_of_a_reply_with_only_known_fields(self):
        self.coordinator.apply_rest_status(dict(REST_PAYLOAD), "2026-09-17T20:00:00+00:00")
        self.assertIsNone(self.coordinator.get_rest_details()[1]["unknown_fields"])

    async def test_valid_token_expiry_reaches_the_health(self):
        self.coordinator.oauth_session = SimpleNamespace(
            token={"access_token": "t", "expires_at": 1700000000},
            async_ensure_token_valid=AsyncMock(),
        )
        self.coordinator.health = SimpleNamespace(note_token=Mock())
        await self.coordinator._async_ensure_valid_token()
        self.coordinator.health.note_token.assert_called_once_with(1700000000)

    async def test_every_token_check_pushes_the_bearer_to_the_mqtt_session(self):
        self.coordinator.oauth_session = SimpleNamespace(
            token={"access_token": "t", "expires_at": 1700000000},
            async_ensure_token_valid=AsyncMock(),
        )
        self.coordinator.mqtt_session = SimpleNamespace(async_push_bearer=AsyncMock())
        self.assertEqual(await self.coordinator._async_ensure_valid_token(), "t")
        self.coordinator.mqtt_session.async_push_bearer.assert_awaited_once_with("t")

    async def test_watch_view_pairs_the_last_mqtt_report_with_rest(self):
        view = self.coordinator.get_watch_view(time.monotonic())
        self.assertEqual((view["mqtt_state"], view["rest_state"], view["mqtt_age"]), (None, None, None))
        self.coordinator._update_from_state(mqtt_message(), "2026-09-17T20:00:00+00:00")
        self.mqtt_is_fresh()  # so REST is kept but not shown
        self.coordinator.apply_rest_status(dict(REST_PAYLOAD), "2026-09-17T20:02:00+00:00")
        view = self.coordinator.get_watch_view(time.monotonic() + 150)
        self.assertEqual(view["mqtt_state"], "mowing")
        self.assertEqual(view["mqtt_key"], "2026-09-17T20:00:00+00:00")
        self.assertGreaterEqual(view["mqtt_age"], 150)
        self.assertEqual(view["rest_state"], "docked")
        self.assertEqual(view["rest_raw_state"], "isDocked")
        self.assertEqual(view["name"], "Mower")
        self.assertEqual(view["shown_state"], "mowing")
        self.assertFalse(view["has_pose"])
        self.coordinator.ingest_location(decode(POSE)[0])
        self.assertTrue(self.coordinator.get_watch_view(time.monotonic())["has_pose"])

    async def test_raw_state_check_catches_a_late_message_first(self):
        newer = {"state": "isDocked", "timestamp": 1700000060000, "battery": 90}
        older = {"state": "isRunning", "timestamp": 1700000000000, "battery": 91}
        self.assertIsNone(self.coordinator.check_raw_state(newer))
        # the older one arrives before the newer has reached the coordinator
        self.assertEqual(self.coordinator.check_raw_state(older), "stale")
        self.assertIsNone(self.coordinator.check_raw_state(dict(newer)))  # equal time
        self.assertEqual(self.coordinator.check_raw_state({"timestamp": 5}), "implausible_time")
        self.assertIsNone(self.coordinator.check_raw_state({"state": "isDocked"}))
        self.assertIsNone(self.coordinator.check_raw_state([1]))

    async def test_raw_check_and_the_backstop_agree_on_the_newer_message(self):
        self.assertIsNone(self.coordinator.check_raw_state({"timestamp": 1700000060000}))
        newer = mqtt_message(state="docked", timestamp=1700000060000)
        self.coordinator._update_from_state(newer)
        self.assertIs(self.coordinator.get_device_state(), newer)
        self.assertEqual(self.coordinator.get_rejected()[0], 0)
