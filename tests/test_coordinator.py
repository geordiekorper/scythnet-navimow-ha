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
from custom_components.navimow.location import parse_location_payload

from tests.test_location import POSE, RECEIVED

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

    async def test_restore_seeds_the_cache_and_live_data_wins(self):
        cache = {}
        self.coordinator.location_cache = cache
        pose = {"x": 2.0, "y": 0.3, "theta": 0.35, "vehicle_state": 1,
                "pose_time": 1, "received_at": "2026-09-17T20:00:00+00:00"}
        self.coordinator.restore_location("pose", pose)
        loc = self.coordinator.get_device_location()
        self.assertEqual(loc["x"], 2.0)
        self.assertTrue(loc["pose_restored"])
        self.assertEqual(cache["dev-1"]["x"], 2.0)
        # a group that is already present is not overwritten
        self.coordinator.restore_location("pose", {"x": 9.0, "y": 9.0})
        self.assertEqual(self.coordinator.get_device_location()["x"], 2.0)
        # a live pose merges over it and drops the marker
        live = parse_location_payload(cache, "dev-1", [POSE], received_at=RECEIVED)
        self.coordinator.ingest_location(live)
        loc = self.coordinator.get_device_location()
        self.assertEqual(loc["x"], 1.5)
        self.assertNotIn("pose_restored", loc)

    async def test_restore_without_a_cache_or_fields_is_a_no_op(self):
        self.coordinator.restore_location("pose", {"x": 1.0, "y": 1.0})
        self.assertIsNone(self.coordinator.get_device_location())
        self.coordinator.location_cache = {}
        self.coordinator.restore_location("pose", {})
        self.assertIsNone(self.coordinator.get_device_location())

    async def test_restored_pose_does_not_train_the_dock(self):
        cache = {}
        self.coordinator.location_cache = cache
        self.coordinator._last_state = mqtt_message(state="docked", raw="isDocked")
        self.coordinator.restore_location("pose", {"x": 2.0, "y": 0.3})
        self.coordinator.ingest_location(cache["dev-1"])
        self.assertIsNone(self.coordinator.get_dock_position())
        live = parse_location_payload(cache, "dev-1", [POSE], received_at=RECEIVED)
        self.coordinator.ingest_location(live)
        self.assertEqual(self.coordinator.get_dock_position()["n"], 1)
