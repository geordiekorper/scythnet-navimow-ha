"""Location parser: each type-2 task entry is one complete observation."""
import unittest

from custom_components.navimow.location import (
    TASK_ATTRIBUTES,
    parse_location_payload,
    progress_percent,
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


class TaskGroupTest(unittest.TestCase):
    def setUp(self):
        self.cache = {}

    def parse(self, *entries):
        return parse_location_payload(self.cache, "dev-1", list(entries))

    def test_full_entry_converts_every_field(self):
        loc = self.parse(FULL_TASK)
        self.assertEqual(loc["task"], {
            "route_progress": 5000, "mowing_percentage": 50.0,
            "area_m2": 100.0, "week_area_m2": 250.0,
            "action": 1, "sub_action": None, "mow_start_type": 1,
            "map_work_position": "00000001000000000000000200000001",
            "task_time_ms": 1700000000032,
        })
        self.assertEqual(tuple(loc["task"]), TASK_ATTRIBUTES)
        # the merged keys the sensors already read are unchanged
        self.assertEqual(loc["mow_boundary"], 2)
        self.assertEqual(loc["mow_progress"], 5000)

    def test_zero_and_negative_values_are_kept(self):
        loc = self.parse({
            "type": 2, "currentMowProgress": 0, "subtotalArea": "0.00",
            "mowingPercentage": 0, "action": -1, "subAction": -1,
        })
        task = loc["task"]
        self.assertEqual(task["route_progress"], 0)
        self.assertEqual(task["area_m2"], 0.0)
        self.assertEqual(task["mowing_percentage"], 0.0)
        self.assertEqual(task["action"], -1)
        self.assertEqual(task["sub_action"], -1)

    def test_area_only_entry_exposes_area_without_progress(self):
        loc = self.parse({"type": 2, "subtotalArea": "12.50"})
        self.assertEqual(loc["task"]["area_m2"], 12.5)
        self.assertIsNone(loc["task"]["route_progress"])
        self.assertNotIn("mow_progress", loc)

    def test_partial_entry_replaces_the_whole_group(self):
        self.parse(FULL_TASK)
        loc = self.parse({"type": 2, "currentMowProgress": 5100, "time": 1700000180000})
        task = loc["task"]
        self.assertEqual(task["route_progress"], 5100)
        self.assertEqual(task["task_time_ms"], 1700000180000)
        self.assertIsNone(task["area_m2"])
        self.assertIsNone(task["action"])
        self.assertIsNone(task["map_work_position"])
        # merged keys keep their last value for the sensors
        self.assertEqual(loc["mow_boundary"], 2)
        self.assertEqual(loc["mow_progress"], 5100)

    def test_pose_leaves_the_task_group_unchanged(self):
        before = dict(self.parse(FULL_TASK)["task"])
        loc = self.parse(POSE)
        self.assertEqual(loc["task"], before)
        self.assertEqual(loc["x"], 1.5)

    def test_last_task_entry_in_a_batch_wins(self):
        loc = self.parse(
            {"type": 2, "subtotalArea": "1.00", "time": 1},
            {"type": 2, "subtotalArea": "2.00", "time": 2},
        )
        self.assertEqual(loc["task"]["area_m2"], 2.0)
        self.assertEqual(loc["task"]["task_time_ms"], 2)

    def test_invalid_values_become_none(self):
        loc = self.parse({
            "type": 2, "currentMowProgress": "n/a", "subtotalArea": None,
            "mowingWeekArea": "inf", "action": True, "mapWorkPosition": 7,
        })
        task = loc["task"]
        self.assertIsNone(task["route_progress"])
        self.assertIsNone(task["area_m2"])
        self.assertIsNone(task["week_area_m2"])
        self.assertIsNone(task["action"])
        self.assertEqual(task["map_work_position"], "7")

    def test_no_task_group_before_first_task_entry(self):
        loc = self.parse(POSE)
        self.assertNotIn("task", loc)


class ProgressAndDelayTest(unittest.TestCase):
    def setUp(self):
        self.cache = {}

    def parse(self, *entries):
        return parse_location_payload(self.cache, "dev-1", list(entries))

    def test_pose_only_gives_unknown_progress(self):
        self.assertEqual(progress_percent(self.parse(POSE)), (None, "none"))

    def test_no_location_gives_unknown_progress(self):
        self.assertEqual(progress_percent(None), (None, "none"))

    def test_route_progress_zero_is_zero_percent(self):
        loc = self.parse({"type": 2, "currentMowProgress": 0})
        self.assertEqual(progress_percent(loc), (0.0, "route"))

    def test_route_progress_scales_to_percent(self):
        loc = self.parse({"type": 2, "currentMowProgress": 2500})
        self.assertEqual(progress_percent(loc), (25.0, "route"))

    def test_percentage_is_the_fallback(self):
        loc = self.parse({"type": 2, "mowingPercentage": 12})
        self.assertEqual(progress_percent(loc), (12.0, "percentage"))

    def test_route_progress_wins_over_percentage(self):
        loc = self.parse({"type": 2, "currentMowProgress": 5000, "mowingPercentage": 12})
        self.assertEqual(progress_percent(loc), (50.0, "route"))

    def test_status_only_delay_entry_keeps_the_last_delay(self):
        self.parse({"type": 4, "taskDelay": True})
        self.assertIsNone(self.parse({"time": 1700000060000, "type": 4, "vehicleState": 1}))
        self.assertIs(self.cache["dev-1"]["task_delay"], True)
        loc = self.parse({"type": 4, "taskDelay": False})
        self.assertIs(loc["task_delay"], False)

    def test_status_only_delay_next_to_a_pose_is_ignored(self):
        self.parse({"type": 4, "taskDelay": True})
        loc = self.parse({"time": 1700000060000, "type": 4, "vehicleState": 1}, POSE)
        self.assertIs(loc["task_delay"], True)
        self.assertEqual(loc["x"], 1.5)

    def test_status_only_delay_alone_is_a_no_op(self):
        self.assertIsNone(self.parse({"time": 1, "type": 4, "vehicleState": 1}))
        self.assertNotIn("dev-1", self.cache)

    def test_empty_message_is_a_no_op(self):
        self.assertIsNone(self.parse())
        self.assertNotIn("dev-1", self.cache)


class PoseTest(unittest.TestCase):
    def setUp(self):
        self.cache = {}

    def parse(self, *entries, received_at=RECEIVED):
        return parse_location_payload(
            self.cache, "dev-1", list(entries), received_at=received_at
        )

    def test_full_pose_matches_the_entry(self):
        loc = self.parse(POSE)
        self.assertEqual(loc["x"], 1.5)
        self.assertEqual(loc["y"], 0.25)
        self.assertEqual(loc["theta"], 0.1)
        self.assertEqual(loc["vehicle_state"], 4)
        self.assertEqual(loc["pose_time"], 1700000000000)
        self.assertEqual(loc["received_at"], RECEIVED)

    def test_missing_theta_is_none_not_inherited(self):
        self.parse(POSE)
        loc = self.parse({
            "postureX": "1.600", "postureY": "0.300", "time": 1700000002000,
            "type": 1, "vehicleState": 4,
        })
        self.assertEqual(loc["x"], 1.6)
        self.assertIsNone(loc["theta"])

    def test_invalid_xy_leaves_the_previous_pose_intact(self):
        self.parse(POSE)
        result = self.parse({
            "postureX": "n/a", "postureY": "0.300", "postureTheta": "2.0",
            "time": 1700000002000, "type": 1, "vehicleState": 5,
        })
        self.assertIsNone(result)
        loc = self.cache["dev-1"]
        self.assertEqual(
            (loc["x"], loc["y"], loc["theta"], loc["vehicle_state"], loc["pose_time"]),
            (1.5, 0.25, 0.1, 4, 1700000000000),
        )

    def test_last_valid_pose_in_a_batch_wins(self):
        second = {**POSE, "postureX": "2.000", "time": 1700000002000}
        bad = {**POSE, "postureY": None, "time": 1700000004000}
        loc = self.parse(POSE, second, bad)
        self.assertEqual(loc["x"], 2.0)
        self.assertEqual(loc["pose_time"], 1700000002000)

    def test_received_at_belongs_to_the_pose(self):
        self.parse(POSE, received_at="2026-09-17T20:00:00+00:00")
        loc = self.parse(FULL_TASK, received_at="2026-09-17T20:00:05+00:00")
        self.assertEqual(loc["received_at"], "2026-09-17T20:00:00+00:00")

    def test_same_x_different_y_is_a_new_pose(self):
        self.parse(POSE)
        loc = self.parse({**POSE, "postureY": "0.500"})
        self.assertEqual((loc["x"], loc["y"]), (1.5, 0.5))


class RestoreTest(unittest.TestCase):
    def test_position_x_restores_the_whole_pose(self):
        groups = restore_location_groups("position_x", "2.068", {
            "y": 0.308, "theta_rad": 0.356, "vehicle_state": 1,
            "pose_time_ms": 1789692272968, "received_at": RECEIVED,
            "source": "mqtt_location", "is_restored": False,
        })
        self.assertEqual(groups, [("pose", {
            "x": 2.068, "y": 0.308, "theta": 0.356, "vehicle_state": 1,
            "pose_time": 1789692272968, "received_at": RECEIVED,
        })])

    def test_position_without_a_usable_pose_restores_nothing(self):
        self.assertEqual(restore_location_groups("position_x", "unknown", {}), [])
        self.assertEqual(restore_location_groups("position_x", "1.0", {}), [])

    def test_mowing_zone_restores_boundary_and_task(self):
        attrs = {
            "route_progress": 5000, "mowing_percentage": 50.0, "area_m2": 100.0,
            "week_area_m2": 250.0, "action": 1, "sub_action": None,
            "mow_start_type": 1, "map_work_position": "00", "task_time_ms": 1,
            "is_restored": False,
        }
        groups = restore_location_groups("mowing_zone", "2", attrs)
        self.assertEqual(len(groups), 1)
        group, fields = groups[0]
        self.assertEqual(group, "task")
        self.assertEqual(fields["mow_boundary"], 2)
        self.assertEqual(fields["task"], {k: attrs[k] for k in TASK_ATTRIBUTES})
        self.assertEqual(restore_location_groups("mowing_zone", "unknown", {}), [])

    def test_progress_restores_route_progress_only(self):
        self.assertEqual(
            restore_location_groups("mow_progress", "25.0", {"progress_source": "route"}),
            [("progress", {"mow_progress": 2500})],
        )
        self.assertEqual(
            restore_location_groups("mow_progress", "12.0", {"progress_source": "percentage"}),
            [],  # comes back with the task group
        )
        self.assertEqual(
            restore_location_groups("mow_progress", "unknown", {"progress_source": "none"}),
            [],
        )

    def test_zone_restores_target_and_delay(self):
        self.assertEqual(
            restore_location_groups("zone", "unknown", {"partition_ids": None, "task_delay": False}),
            [("target", {"partition_ids": None, "partition": None}),
             ("delay", {"task_delay": False})],
        )
        self.assertEqual(
            restore_location_groups("zone", "2", {"partition_ids": [2, 3]}),
            [("target", {"partition_ids": [2, 3], "partition": 2})],
        )
        self.assertEqual(restore_location_groups("zone", "unknown", {}), [])

    def test_live_entries_clear_their_own_restored_marker(self):
        cache = {"dev-1": {
            "device_id": "dev-1", "x": 1.0, "y": 2.0, "pose_restored": True,
            "task": {"route_progress": 10000}, "mow_progress": 10000,
            "task_restored": True, "progress_restored": True,
            "partition_ids": None, "partition": None, "target_restored": True,
            "task_delay": False, "delay_restored": True,
        }}
        parse = lambda *entries: parse_location_payload(cache, "dev-1", list(entries), received_at=RECEIVED)
        loc = parse({"type": 4, "taskDelay": True})
        self.assertNotIn("delay_restored", loc)
        self.assertTrue(loc["pose_restored"])
        self.assertEqual(loc["x"], 1.0)  # the restored pose survives a delay entry
        loc = parse(POSE)
        self.assertNotIn("pose_restored", loc)
        self.assertEqual(loc["x"], 1.5)
        self.assertTrue(loc["task_restored"])
        loc = parse(FULL_TASK)
        self.assertNotIn("task_restored", loc)
        self.assertNotIn("progress_restored", loc)
        loc = parse({"type": 3, "partitionIds": [2], "time": 1})
        self.assertNotIn("target_restored", loc)

    def test_status_only_delay_keeps_the_restored_marker(self):
        cache = {"dev-1": {"device_id": "dev-1", "task_delay": True, "delay_restored": True}}
        self.assertIsNone(parse_location_payload(cache, "dev-1", [{"time": 1, "type": 4, "vehicleState": 1}]))
        self.assertTrue(cache["dev-1"]["delay_restored"])
