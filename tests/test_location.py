"""Location helpers around the SDK's record: the restore of the sensors'
recorded states, and the target-zone state. The SDK decodes the channel."""
import unittest
from datetime import datetime

from mower_sdk.location import LocationDecoder
from mower_sdk.models import DeviceLocation

from custom_components.navimow.location import (
    TASK_ATTRIBUTES,
    restore_location_groups,
    target_zone,
)

# Shapes from the X430 capture (values invented, consistent with each other).
FULL_TASK = {
    "action": 1, "currentMowBoundary": 2, "currentMowProgress": 5000,
    "mapWorkPosition": "00000001000000000000000200000001", "mowStartType": 1,
    "mowingPercentage": 50, "mowingWeekArea": "250.00", "subtotalArea": "100.00",
    "time": 1700000000032, "type": 2,
}
POSE = {
    "postureTheta": "0.100", "postureX": "1.500", "postureY": "0.250",
    "time": 1700000000000, "type": 1, "vehicleState": 4,
}
RECEIVED = "2026-09-17T20:00:00+00:00"
RECEIVED_AT = datetime.fromisoformat(RECEIVED)


def decode(*entries, decoder=None):
    """Every message the SDK's decoder produces for one location message."""
    decoder = decoder or LocationDecoder()
    return decoder.decode("dev-1", list(entries), RECEIVED_AT).messages


def record(*entries):
    """The SDK's record after one location message, or None if nothing applied."""
    messages = decode(*entries)
    return messages[-1].location if messages else None


def restored(key, state, attributes):
    """What a sensor's recorded state restores, as the record it would give."""
    fields = {}
    for _, group_fields in restore_location_groups(key, state, attributes):
        fields.update(group_fields)
    return DeviceLocation.from_dict({**fields, "device_id": "dev-1"})


class RestoreTest(unittest.TestCase):
    def test_position_x_restores_the_whole_pose(self):
        groups = restore_location_groups("position_x", "2.068", {
            "y": 0.308, "theta_rad": 0.356, "vehicle_state": 1,
            "pose_time_ms": 1789692272968, "received_at": RECEIVED,
            "source": "mqtt_location", "is_restored": False,
        })
        self.assertEqual(groups, [("pose", {
            "x": 2.068, "y": 0.308, "theta": 0.356, "vehicle_state": 1,
            "pose_at": 1789692272968, "pose_received_at": RECEIVED,
        })])
        location = restored("position_x", "2.068", {"y": 0.308, "pose_time_ms": 5, "received_at": RECEIVED})
        self.assertEqual((location.x, location.y, location.pose_at), (2.068, 0.308, 5))
        self.assertEqual(location.pose_received_at, RECEIVED_AT)

    def test_position_without_a_usable_pose_restores_nothing(self):
        self.assertEqual(restore_location_groups("position_x", "unknown", {}), [])
        self.assertEqual(restore_location_groups("position_x", "1.0", {}), [])

    def test_mowing_zone_restores_zone_and_task(self):
        attrs = {
            "route_progress": 5000, "mowing_percentage": 50.0, "area_m2": 100.0,
            "week_area_m2": 250.0, "action": 1, "sub_action": None,
            "mow_start_type": 1, "map_work_position": "00", "task_time_ms": 1,
            "is_restored": False,
        }
        self.assertEqual(restore_location_groups("mowing_zone", "2", attrs), [("task", {
            "current_zone": 2, "mowing_percentage": 50.0, "area_m2": 100.0,
            "week_area_m2": 250.0, "action": 1, "sub_action": None,
            "mow_start_type": 1, "map_work_position": "00", "task_at": 1,
        })])
        # The route reading is the progress sensor's to restore.
        self.assertIsNone(restored("mowing_zone", "2", attrs).route_progress)
        self.assertEqual(restore_location_groups("mowing_zone", "unknown", {}), [])

    def test_every_mowing_zone_attribute_is_a_record_field(self):
        names = set(DeviceLocation.__dataclass_fields__)
        self.assertLessEqual(set(TASK_ATTRIBUTES.values()), names)

    def test_progress_restores_route_progress_only(self):
        self.assertEqual(
            restore_location_groups("mow_progress", "25.0", {"progress_source": "route"}),
            [("progress", {"route_progress": 2500})],
        )
        self.assertEqual(
            restore_location_groups("mow_progress", "12.0", {"progress_source": "percentage"}),
            [],  # comes back with the task group
        )
        self.assertEqual(
            restore_location_groups("mow_progress", "unknown", {"progress_source": "none"}),
            [],
        )


class TargetRestoreTest(unittest.TestCase):
    """A target group is restored only on positive evidence that one was
    reported: the zone sensor's partition_ids attribute is None both before
    any report and for a report with no active target."""

    def target(self, state, attrs):
        groups = dict(restore_location_groups("zone", state, attrs))
        return groups.get("target")

    def test_a_recorded_unknown_restores_no_target(self):
        self.assertIsNone(self.target("unknown", {"partition_ids": None, "task_delay": False}))
        location = restored("zone", "unknown", {"partition_ids": None})
        self.assertIsNone(target_zone(location, "mowing"))  # the zone stays unknown

    def test_unavailable_without_attributes_restores_no_target(self):
        self.assertEqual(restore_location_groups("zone", "unavailable", {}), [])

    def test_an_id_with_missing_attributes_restores_no_target(self):
        self.assertIsNone(self.target("2", {"task_delay": False}))
        self.assertIsNone(self.target("2", {"partition_ids": None}))
        self.assertIsNone(self.target("2", {"partition_ids": []}))

    def test_a_recorded_all_with_no_list_is_an_empty_report(self):
        self.assertEqual(self.target("all", {"partition_ids": None}), {
            "partition_ids": [], "target_at": None, "target_last_at": None,
        })
        location = restored("zone", "all", {"partition_ids": None})
        self.assertEqual(location.partition_ids, ())
        self.assertEqual(target_zone(location, "mowing"), "all")
        self.assertEqual(target_zone(location, "docked"), "none")

    def test_a_populated_list_is_restored(self):
        self.assertEqual(self.target("2", {
            "partition_ids": [2, 3], "target_time_ms": 1700000000010,
            "target_last_time_ms": 1700000060010,
        }), {"partition_ids": [2, 3], "target_at": 1700000000010, "target_last_at": 1700000060010})
        self.assertEqual(target_zone(restored("zone", "2", {"partition_ids": [2, 3]}), "docked"), 2)

    def test_a_recorded_none_with_an_empty_list_is_an_empty_report(self):
        self.assertEqual(self.target("none", {"partition_ids": []})["partition_ids"], [])

    def test_delay_is_restored_only_as_a_bool(self):
        groups = dict(restore_location_groups("zone", "unknown", {"partition_ids": None, "task_delay": None}))
        self.assertNotIn("delay", groups)
        groups = dict(restore_location_groups(
            "zone", "unknown", {"partition_ids": None, "task_delay": False, "delay_received_at": RECEIVED}
        ))
        self.assertEqual(groups["delay"], {"task_delay": False, "delay_received_at": RECEIVED})
        self.assertIs(restored("zone", "unknown", {"task_delay": False}).task_delay, False)


class TargetZoneTest(unittest.TestCase):
    NO_TARGET = {"time": 1700000242000, "type": 3}
    ZONE_2 = {"partitionIds": [2], "time": 1700000000010, "type": 3}

    def test_unknown_until_a_target_report_arrives(self):
        self.assertIsNone(target_zone(None, "mowing"))
        self.assertIsNone(target_zone(record(POSE), "mowing"))

    def test_named_target_is_the_first_id(self):
        location = record({"partitionIds": [7, 19], "time": 1700000000010, "type": 3})
        self.assertEqual(target_zone(location, "mowing"), 7)
        self.assertEqual(target_zone(location, "docked"), 7)

    def test_empty_target_while_mowing_or_paused_is_all(self):
        location = record(self.NO_TARGET)
        self.assertEqual(target_zone(location, "mowing"), "all")
        self.assertEqual(target_zone(location, "paused"), "all")
        self.assertEqual(target_zone(location, "Mowing"), "all")

    def test_empty_target_otherwise_is_none(self):
        location = record(self.NO_TARGET)
        for activity in ("docked", "charging", "idle", "returning", "error", "", None):
            self.assertEqual(target_zone(location, activity), "none", activity)

    def test_empty_list_counts_as_no_target(self):
        location = record({"partitionIds": [], "time": 1700000000010, "type": 3})
        self.assertEqual(target_zone(location, "docked"), "none")

    def test_dock_command_clears_a_named_target(self):
        decoder = LocationDecoder()
        decode(self.ZONE_2, decoder=decoder)
        location = decode(self.NO_TARGET, decoder=decoder)[-1].location
        self.assertEqual(target_zone(location, "returning"), "none")
