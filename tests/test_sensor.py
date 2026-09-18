"""Sensor attributes are grouped by the message type that produces them."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import State
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.navimow.location import parse_location_payload
from custom_components.navimow.sensor import (
    RESTORING_KEYS,
    SENSOR_DESCRIPTIONS,
    NavimowLocationSensor,
    NavimowSensor,
)

from tests.test_location import FULL_TASK, POSE, RECEIVED

DESCRIPTIONS = {d.key: d for d in SENSOR_DESCRIPTIONS}


class FakeCoordinator:
    def __init__(self):
        self.device = SimpleNamespace(
            id="dev-1", name="Mower", model="X430", firmware_version="1.0",
            serial_number="SN1",
        )
        self.location = None

    def get_device_location(self):
        return self.location

    def get_device_state(self):
        return None

    def get_dock_position(self):
        return None

    def get_data_source(self):
        return "mqtt_push"

    def get_source_details(self):
        return {"mqtt_state": "mowing", "rest_status": None}


class SensorAttributesTest(unittest.TestCase):
    def setUp(self):
        self.coordinator = FakeCoordinator()
        self.cache = {}

    def sensor(self, key):
        return NavimowSensor(
            coordinator=self.coordinator, entity_description=DESCRIPTIONS[key]
        )

    def feed(self, *entries):
        self.coordinator.location = parse_location_payload(
            self.cache, "dev-1", list(entries), received_at=RECEIVED
        )

    def test_mowing_zone_exposes_the_task_group(self):
        self.feed(FULL_TASK)
        sensor = self.sensor("mowing_zone")
        self.assertEqual(sensor.native_value, 2)
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["route_progress"], 5000)
        self.assertEqual(attrs["area_m2"], 100.0)
        self.assertEqual(attrs["week_area_m2"], 250.0)
        self.assertEqual(attrs["task_time_ms"], 1700000000032)
        self.assertIsNone(attrs["sub_action"])

    def test_mowing_zone_has_no_attributes_before_a_task_entry(self):
        self.feed(POSE)
        self.assertIsNone(self.sensor("mowing_zone").extra_state_attributes)

    def test_zone_no_longer_carries_task_fields(self):
        self.feed(
            FULL_TASK,
            {"type": 3, "partitionIds": [2], "time": 1700000000010},
            {"type": 4, "taskDelay": False},
        )
        attrs = self.sensor("zone").extra_state_attributes
        self.assertEqual(attrs["partition_ids"], [2])
        self.assertIs(attrs["task_delay"], False)
        self.assertNotIn("mow_boundary", attrs)
        self.assertNotIn("mow_progress", attrs)

    def test_other_sensors_have_no_attributes(self):
        self.feed(FULL_TASK, POSE)
        for key in ("position_y", "heading"):
            self.assertIsNone(self.sensor(key).extra_state_attributes, key)

    def test_position_x_exposes_the_complete_pose(self):
        self.feed(POSE)
        sensor = self.sensor("position_x")
        self.assertEqual(sensor.native_value, 1.5)
        self.assertEqual(sensor.extra_state_attributes, {
            "y": 0.25, "theta_rad": 0.1, "vehicle_state": 4,
            "pose_time_ms": 1700000000000, "received_at": RECEIVED,
            "source": "mqtt_location", "is_restored": False,
        })

    def test_position_x_has_no_attributes_before_a_pose(self):
        self.feed(FULL_TASK)
        self.assertIsNone(self.sensor("position_x").extra_state_attributes)

    def test_same_x_different_y_changes_the_attributes(self):
        self.feed(POSE)
        first = self.sensor("position_x").extra_state_attributes
        self.feed({**POSE, "postureY": "0.500"})
        second = self.sensor("position_x").extra_state_attributes
        self.assertNotEqual(first, second)
        self.assertEqual(second["y"], 0.5)

    def test_zone_attributes_do_not_change_on_a_pose(self):
        self.feed(
            {"type": 3, "partitionIds": [2], "time": 1700000000010},
            {"type": 4, "taskDelay": False},
        )
        before = self.sensor("zone").extra_state_attributes
        self.feed(POSE)
        after = self.sensor("zone").extra_state_attributes
        self.assertEqual(before, after)
        self.assertEqual(set(after), {"partition_ids", "task_delay", "is_restored"})

    def test_progress_is_unknown_until_a_task_report(self):
        self.feed(POSE)
        sensor = self.sensor("mow_progress")
        self.assertIsNone(sensor.native_value)
        self.assertEqual(
            sensor.extra_state_attributes, {"progress_source": "none", "is_restored": False}
        )

    def test_progress_names_its_source(self):
        self.feed({"type": 2, "mowingPercentage": 12})
        sensor = self.sensor("mow_progress")
        self.assertEqual(sensor.native_value, 12.0)
        self.assertEqual(sensor.extra_state_attributes["progress_source"], "percentage")
        self.feed(FULL_TASK)
        self.assertEqual(sensor.native_value, 50.0)
        self.assertEqual(sensor.extra_state_attributes["progress_source"], "route")

    def test_data_source_sensor_reads_the_coordinator(self):
        sensor = self.sensor("data_source")
        self.assertEqual(sensor.native_value, "mqtt_push")
        self.assertEqual(
            sensor.extra_state_attributes, {"mqtt_state": "mowing", "rest_status": None}
        )
        self.assertEqual(sensor.entity_description.entity_category, "diagnostic")

    def test_coordinates_and_heading_produce_no_statistics(self):
        # A mean position or a mean of a 0-360 heading is meaningless, so
        # these sensors carry no state_class and HA computes no statistics.
        for key in ("position_x", "position_y", "heading"):
            self.assertIsNone(DESCRIPTIONS[key].state_class, key)
        self.assertEqual(DESCRIPTIONS["mow_progress"].state_class, "measurement")

    def test_restored_marker_follows_each_group(self):
        self.cache["dev-1"] = {
            "device_id": "dev-1", "x": 2.0, "y": 0.3, "theta": None,
            "vehicle_state": 1, "pose_time": 1, "received_at": RECEIVED,
            "pose_restored": True,
            "mow_boundary": 2, "task": {"route_progress": 10000, "mowing_percentage": 100.0},
            "mow_progress": 10000, "task_restored": True, "progress_restored": True,
            "partition_ids": None, "partition": None, "target_restored": True,
            "task_delay": False, "delay_restored": True,
        }
        self.coordinator.location = self.cache["dev-1"]
        self.assertTrue(self.sensor("position_x").extra_state_attributes["is_restored"])
        self.assertTrue(self.sensor("mowing_zone").extra_state_attributes["is_restored"])
        self.assertEqual(self.sensor("mowing_zone").native_value, 2)
        progress = self.sensor("mow_progress")
        self.assertEqual(progress.native_value, 100.0)
        self.assertTrue(progress.extra_state_attributes["is_restored"])
        self.assertTrue(self.sensor("zone").extra_state_attributes["is_restored"])
        self.feed(POSE)  # live pose: only the pose group becomes live
        self.assertFalse(self.sensor("position_x").extra_state_attributes["is_restored"])
        self.assertEqual(self.sensor("position_x").native_value, 1.5)
        self.assertTrue(self.sensor("mowing_zone").extra_state_attributes["is_restored"])
        self.assertTrue(self.sensor("zone").extra_state_attributes["is_restored"])

    def test_live_data_is_not_marked_restored(self):
        self.feed(FULL_TASK, POSE, {"type": 3, "partitionIds": [2], "time": 1})
        for key in RESTORING_KEYS:
            self.assertFalse(self.sensor(key).extra_state_attributes["is_restored"], key)

    def test_no_location_means_no_attributes(self):
        for key in ("zone", "mowing_zone", "mow_progress", "position_x"):
            self.assertIsNone(self.sensor(key).extra_state_attributes, key)


class RestoreOnStartupTest(unittest.IsolatedAsyncioTestCase):
    def make_sensor(self, key):
        coordinator = FakeCoordinator()
        coordinator.restore_location = Mock()
        coordinator.async_add_listener = Mock(return_value=lambda: None)
        sensor = NavimowLocationSensor(
            coordinator=coordinator, entity_description=DESCRIPTIONS[key]
        )
        return sensor, coordinator

    async def added(self, sensor, last):
        with (
            patch.object(RestoreEntity, "async_added_to_hass", AsyncMock()),
            patch.object(RestoreEntity, "async_get_last_state", AsyncMock(return_value=last)),
        ):
            await sensor.async_added_to_hass()

    async def test_position_sensor_seeds_the_coordinator(self):
        sensor, coordinator = self.make_sensor("position_x")
        last = State("sensor.position_x", "2.068", {
            "y": 0.308, "theta_rad": 0.356, "vehicle_state": 1,
            "pose_time_ms": 5, "received_at": RECEIVED, "source": "mqtt_location",
        })
        await self.added(sensor, last)
        coordinator.restore_location.assert_called_once_with("pose", {
            "x": 2.068, "y": 0.308, "theta": 0.356, "vehicle_state": 1,
            "pose_time": 5, "received_at": RECEIVED,
        })

    async def test_zone_sensor_seeds_target_and_delay(self):
        sensor, coordinator = self.make_sensor("zone")
        await self.added(sensor, State("sensor.zone", "unknown", {"partition_ids": None, "task_delay": False}))
        self.assertEqual(coordinator.restore_location.call_count, 2)

    async def test_nothing_recorded_means_nothing_restored(self):
        sensor, coordinator = self.make_sensor("mowing_zone")
        await self.added(sensor, None)
        coordinator.restore_location.assert_not_called()
        sensor, coordinator = self.make_sensor("mowing_zone")
        await self.added(sensor, State("sensor.mowing_zone", "unknown", {}))
        coordinator.restore_location.assert_not_called()
