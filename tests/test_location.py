"""Location helpers around the SDK's record: the restore of the sensors'
recorded states. The SDK decodes the channel and infers the target zone."""
import unittest
from datetime import datetime

from mower_sdk.location import LocationDecoder, target_zone
from mower_sdk.models import DeviceLocation

from custom_components.navimow.location import (
    TASK_ATTRIBUTES,
    restore_location_groups,
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


class DockRestoreTest(unittest.TestCase):
    """Each dock sensor seeds its coordinate of the SDK's estimate, with the
    sample count, so the decoder goes on averaging from the restored dock."""

    def test_each_coordinate_restores_with_the_samples(self):
        self.assertEqual(
            restore_location_groups("dock_x", "2.07", {"samples": 12, "source": "live"}),
            [("dock", {"dock_x": 2.07, "dock_samples": 12})],
        )
        self.assertEqual(
            restore_location_groups("dock_y", "-0.5", {"samples": 12, "source": "live"}),
            [("dock", {"dock_y": -0.5, "dock_samples": 12})],
        )

    def test_a_value_without_samples_counts_as_one(self):
        # The sensors of earlier releases recorded samples 0 after a restart.
        self.assertEqual(
            restore_location_groups("dock_x", "2.07", {"samples": 0, "source": "restored"}),
            [("dock", {"dock_x": 2.07, "dock_samples": 1})],
        )
        self.assertEqual(restore_location_groups("dock_y", "1", {}), [("dock", {"dock_y": 1.0, "dock_samples": 1})])

    def test_unknown_restores_nothing(self):
        self.assertEqual(restore_location_groups("dock_x", "unknown", {"samples": 0}), [])
        self.assertEqual(restore_location_groups("dock_y", None, {}), [])

    def test_the_two_assemble_one_estimate_the_decoder_continues(self):
        fields = {}
        for key, state in (("dock_x", "2.0"), ("dock_y", "1.0")):
            for _, group in restore_location_groups(key, state, {"samples": 3}):
                fields.update(group)
        location = DeviceLocation.from_dict({**fields, "device_id": "dev-1"})
        decoder = LocationDecoder()
        decoder.restore("dev-1", location)
        docked = {**POSE, "postureX": "2.4", "postureY": "1.0", "vehicleState": 1}
        record = decode(docked, decoder=decoder)[-1].location
        self.assertEqual(record.dock_samples, 4)
        self.assertAlmostEqual(record.dock_x, 2.1)  # the mean of three at 2.0 and one at 2.4

