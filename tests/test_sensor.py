"""Sensor attributes are grouped by the message type that produces them."""
import unittest
from types import SimpleNamespace

from custom_components.navimow.location import parse_location_payload
from custom_components.navimow.sensor import SENSOR_DESCRIPTIONS, NavimowSensor

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
            "source": "mqtt_location",
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
        self.assertEqual(set(after), {"partition_ids", "task_delay"})

    def test_progress_is_unknown_until_a_task_report(self):
        self.feed(POSE)
        sensor = self.sensor("mow_progress")
        self.assertIsNone(sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes, {"progress_source": "none"})

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

    def test_no_location_means_no_attributes(self):
        for key in ("zone", "mowing_zone", "mow_progress", "position_x"):
            self.assertIsNone(self.sensor(key).extra_state_attributes, key)
