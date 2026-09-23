"""Location parser: each type-2 task entry is one complete observation."""
import unittest

from custom_components.navimow.location import (
    TASK_ATTRIBUTES,
    parse_location_message,
    parse_location_payload,
    progress_percent,
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
            {"type": 2, "subtotalArea": "1.00", "time": 1700000000001},
            {"type": 2, "subtotalArea": "2.00", "time": 1700000000002},
        )
        self.assertEqual(loc["task"]["area_m2"], 2.0)
        self.assertEqual(loc["task"]["task_time_ms"], 1700000000002)

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
        loc = self.parse({**POSE, "postureY": "0.500", "time": 1700000002000})
        self.assertEqual((loc["x"], loc["y"]), (1.5, 0.5))


class MessageSnapshotsTest(unittest.TestCase):
    """A message yields one snapshot per entry that changed the record."""

    def setUp(self):
        self.cache = {}

    def parse(self, *entries):
        return parse_location_message(self.cache, "dev-1", list(entries), received_at=RECEIVED).snapshots

    def test_each_pose_of_a_batch_is_its_own_snapshot(self):
        poses = [
            {**POSE, "postureX": f"{i}.000", "time": 1700000000000 + 2000 * i}
            for i in range(1, 5)
        ]
        snaps = self.parse(*poses)
        self.assertEqual([s["x"] for s in snaps], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(
            [s["pose_time"] for s in snaps],
            [1700000002000, 1700000004000, 1700000006000, 1700000008000],
        )
        self.assertEqual(self.cache["dev-1"]["x"], 4.0)

    def test_snapshots_are_independent_copies(self):
        first, second = self.parse(POSE, {**POSE, "postureX": "9.000", "time": 1700000002000})
        self.assertEqual(first["x"], 1.5)
        self.assertEqual(second["x"], 9.0)

    def test_pose_and_task_give_two_snapshots(self):
        pose_snap, task_snap = self.parse(POSE, FULL_TASK)
        self.assertNotIn("task", pose_snap)
        self.assertEqual(task_snap["task"]["route_progress"], 5000)
        self.assertEqual(task_snap["x"], 1.5)

    def test_entries_that_change_nothing_give_no_snapshot(self):
        snaps = self.parse(
            {**POSE, "postureX": None},
            {"time": 1700000060000, "type": 4, "vehicleState": 1},
            POSE,
        )
        self.assertEqual(len(snaps), 1)

    def test_nothing_usable_gives_no_snapshots_and_no_cache(self):
        self.assertEqual(self.parse(), [])
        self.assertEqual(parse_location_message(self.cache, "dev-1", {"type": 1}).snapshots, [])
        self.assertNotIn("dev-1", self.cache)


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
            [("target", {"partition_ids": None, "partition": None,
                         "target_time_ms": None, "target_last_time_ms": None}),
             ("delay", {"task_delay": False, "delay_received_at": None})],
        )
        self.assertEqual(
            restore_location_groups("zone", "2", {
                "partition_ids": [2, 3], "target_time_ms": 1700000000010,
                "target_last_time_ms": 1700000060010,
            }),
            [("target", {"partition_ids": [2, 3], "partition": 2,
                         "target_time_ms": 1700000000010,
                         "target_last_time_ms": 1700000060010})],
        )
        self.assertEqual(
            restore_location_groups("zone", "all", {"task_delay": True, "delay_received_at": RECEIVED}),
            [("delay", {"task_delay": True, "delay_received_at": RECEIVED})],
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
        loc = parse({"type": 3, "partitionIds": [2], "time": 1700000000010})
        self.assertNotIn("target_restored", loc)

    def test_status_only_delay_keeps_the_restored_marker(self):
        cache = {"dev-1": {"device_id": "dev-1", "task_delay": True, "delay_restored": True}}
        self.assertIsNone(parse_location_payload(cache, "dev-1", [{"time": 1, "type": 4, "vehicleState": 1}]))
        self.assertTrue(cache["dev-1"]["delay_restored"])


class TargetZoneTest(unittest.TestCase):
    NO_TARGET = {"time": 1700000242000, "type": 3}
    ZONE_2 = {"partitionIds": [2], "time": 1700000000010, "type": 3}

    def parse(self, *entries):
        return parse_location_payload({}, "dev-1", list(entries))

    def test_unknown_until_a_target_report_arrives(self):
        self.assertIsNone(target_zone(None, "mowing"))
        self.assertIsNone(target_zone(self.parse(POSE), "mowing"))

    def test_named_target_is_the_first_id(self):
        loc = self.parse({"partitionIds": [7, 19], "time": 1700000000010, "type": 3})
        self.assertEqual(target_zone(loc, "mowing"), 7)
        self.assertEqual(target_zone(loc, "docked"), 7)

    def test_empty_target_while_mowing_or_paused_is_all(self):
        loc = self.parse(self.NO_TARGET)
        self.assertEqual(target_zone(loc, "mowing"), "all")
        self.assertEqual(target_zone(loc, "paused"), "all")
        self.assertEqual(target_zone(loc, "Mowing"), "all")

    def test_empty_target_otherwise_is_none(self):
        loc = self.parse(self.NO_TARGET)
        for activity in ("docked", "charging", "idle", "returning", "error", "", None):
            self.assertEqual(target_zone(loc, activity), "none", activity)

    def test_empty_list_counts_as_no_target(self):
        loc = self.parse({"partitionIds": [], "time": 1700000000010, "type": 3})
        self.assertEqual(target_zone(loc, "docked"), "none")

    def test_dock_command_clears_a_named_target(self):
        cache = {}
        parse_location_payload(cache, "dev-1", [self.ZONE_2])
        loc = parse_location_payload(cache, "dev-1", [self.NO_TARGET])
        self.assertEqual(target_zone(loc, "returning"), "none")


class TargetAndDelayTimesTest(unittest.TestCase):
    ZONES = {"partitionIds": [2, 3], "time": 1700000000010, "type": 3}

    def setUp(self):
        self.cache = {}

    def parse(self, *entries, received_at=RECEIVED):
        return parse_location_payload(self.cache, "dev-1", list(entries), received_at=received_at)

    def test_first_report_sets_both_times(self):
        loc = self.parse(self.ZONES)
        self.assertEqual(loc["target_time_ms"], 1700000000010)
        self.assertEqual(loc["target_last_time_ms"], 1700000000010)

    def test_repeat_advances_only_the_last_time(self):
        self.parse(self.ZONES)
        loc = self.parse({**self.ZONES, "partitionIds": [3, 2], "time": 1700000060010})
        self.assertEqual(loc["target_time_ms"], 1700000000010)
        self.assertEqual(loc["target_last_time_ms"], 1700000060010)

    def test_change_of_target_restarts_both_times(self):
        self.parse(self.ZONES)
        loc = self.parse({"type": 3, "time": 1700000120010})  # no target
        self.assertEqual(loc["target_time_ms"], 1700000120010)
        self.assertEqual(loc["target_last_time_ms"], 1700000120010)

    def test_report_without_time_keeps_none(self):
        loc = self.parse({"type": 3, "partitionIds": [2]})
        self.assertIsNone(loc["target_time_ms"])

    def test_repeat_of_a_restored_target_keeps_its_first_time(self):
        self.cache["dev-1"] = {
            "device_id": "dev-1", "partition_ids": [2, 3], "partition": 2,
            "target_time_ms": 1600000000000, "target_last_time_ms": 1600000000000,
            "target_restored": True,
        }
        loc = self.parse(self.ZONES)
        self.assertEqual(loc["target_time_ms"], 1600000000000)
        self.assertEqual(loc["target_last_time_ms"], 1700000000010)

    def test_delay_report_carries_its_receipt_time(self):
        loc = self.parse({"type": 4, "taskDelay": True}, received_at="2026-09-17T21:00:00+00:00")
        self.assertEqual(loc["delay_received_at"], "2026-09-17T21:00:00+00:00")
        loc = self.parse({"time": 1, "type": 4, "vehicleState": 1}, POSE,
                         received_at="2026-09-17T22:00:00+00:00")
        self.assertEqual(loc["delay_received_at"], "2026-09-17T21:00:00+00:00")


class PlausibilityAndPlaceholderTest(unittest.TestCase):
    NOW_MS = 1700000100000

    def setUp(self):
        self.cache = {}

    def parse(self, *entries):
        return parse_location_message(
            self.cache, "dev-1", list(entries), received_at=RECEIVED, now_ms=self.NOW_MS
        )

    def test_target_stamped_1970_is_rejected_and_changes_nothing(self):
        self.parse({"type": 3, "partitionIds": [2], "time": 1700000000010})
        result = self.parse({"type": 3, "partitionIds": [5], "time": 12345})
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.reason, "implausible_time")
        self.assertEqual(self.cache["dev-1"]["partition_ids"], [2])

    def test_pose_from_the_future_is_rejected(self):
        future = {**POSE, "time": self.NOW_MS + 5 * 60 * 1000 + 1}
        result = self.parse(future)
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.reasons, ["implausible_time"])

    def test_edges_of_the_window_are_accepted(self):
        result = self.parse(
            {**POSE, "time": 1577836800000},
            {**POSE, "postureX": "2.000", "time": self.NOW_MS + 5 * 60 * 1000},
        )
        self.assertEqual(len(result.snapshots), 2)
        self.assertIsNone(result.reason)

    def test_entry_without_a_time_is_not_judged(self):
        result = self.parse({"type": 3, "partitionIds": [2]}, {"type": 4, "taskDelay": True})
        self.assertEqual(len(result.snapshots), 2)
        self.assertIsNone(result.reason)

    def test_all_zero_pose_is_a_placeholder(self):
        self.parse(POSE)
        result = self.parse({**POSE, "postureX": "0.000", "postureY": "0.000",
                             "postureTheta": "0.000", "time": 1700000002000})
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.reason, "placeholder")
        self.assertEqual(self.cache["dev-1"]["x"], 1.5)

    def test_zero_position_with_a_heading_is_a_real_pose(self):
        result = self.parse({**POSE, "postureX": "0", "postureY": "0", "postureTheta": "1.2"})
        self.assertEqual(result.snapshots[0]["x"], 0.0)
        self.assertIsNone(result.reason)

    def test_unusable_xy_is_unparsable(self):
        self.assertEqual(self.parse({**POSE, "postureX": "n/a"}).reason, "unparsable")

    def test_good_entries_of_a_mixed_message_still_apply(self):
        result = self.parse({"type": 3, "partitionIds": [9], "time": 1}, POSE)
        self.assertEqual([s["x"] for s in result.snapshots], [1.5])
        self.assertEqual(result.reason, "implausible_time")

    def test_reconnect_shape_is_neither_applied_nor_rejected(self):
        result = self.parse({"time": 1, "type": 4, "vehicleState": 1})
        self.assertEqual((result.snapshots, result.reasons), ([], []))

    def test_deciding_reason_follows_the_priority(self):
        result = self.parse(
            {**POSE, "postureX": "0", "postureY": "0", "postureTheta": "0"},
            {"type": 3, "partitionIds": [9], "time": 1},
        )
        self.assertEqual(result.reasons, ["placeholder", "implausible_time"])
        self.assertEqual(result.reason, "implausible_time")


class UnknownInputTest(unittest.TestCase):
    def setUp(self):
        self.cache = {}

    def parse(self, *entries):
        return parse_location_message(self.cache, "dev-1", list(entries), received_at=RECEIVED)

    def test_unknown_field_still_applies_the_known_ones(self):
        result = self.parse({**POSE, "postureZ": "0.1"})
        self.assertEqual(result.snapshots[0]["x"], 1.5)
        self.assertEqual(result.reasons, ["unknown_field"])

    def test_unknown_type_applies_nothing(self):
        result = self.parse({"type": 7, "time": 1700000000000, "foo": 1})
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.reasons, ["unknown_field", "unknown_type"])
        self.assertEqual(result.reason, "unknown_type")

    def test_missing_or_odd_type_is_unknown(self):
        self.assertEqual(self.parse({"time": 1700000000000}).reasons, ["unknown_type"])
        self.assertEqual(self.parse({"type": "1", **{k: v for k, v in POSE.items() if k != "type"}}).reason,
                         "unknown_type")

    def test_every_field_of_the_known_shapes_is_known(self):
        result = self.parse(
            POSE, FULL_TASK, {**FULL_TASK, "subAction": 6, "time": 1700000000033},
            {"type": 3, "partitionIds": [2], "time": 1700000000010},
            {"type": 4, "taskDelay": True},
            {"time": 1700000060000, "type": 4, "vehicleState": 1},
        )
        self.assertEqual(result.reasons, [])

    def test_reconnect_shape_with_an_unknown_field_is_recorded(self):
        result = self.parse({"time": 1700000060000, "type": 4, "vehicleState": 1, "new": 1})
        self.assertEqual((result.snapshots, result.reasons), ([], ["unknown_field"]))


class HighWaterTest(unittest.TestCase):
    """Per-type ordering guard: an entry at or below the newest applied time
    of its type is late or repeated, applies nothing and is recorded."""

    def setUp(self):
        self.cache = {}

    def parse(self, *entries):
        return parse_location_message(self.cache, "dev-1", list(entries), received_at=RECEIVED)

    def test_late_pose_does_not_move_the_position(self):
        self.parse({**POSE, "postureX": "5.000", "time": 1700000010000})
        result = self.parse(POSE)  # sent earlier, delivered later
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.reason, "stale")
        self.assertEqual(self.cache["dev-1"]["x"], 5.0)

    def test_repeated_delivery_is_stale(self):
        self.parse(POSE)
        self.assertEqual(self.parse(POSE).reason, "stale")

    def test_reordering_inside_one_message(self):
        result = self.parse(
            {**POSE, "time": 1700000004000, "postureX": "4.000"},
            {**POSE, "time": 1700000002000, "postureX": "2.000"},
            {**POSE, "time": 1700000006000, "postureX": "6.000"},
        )
        self.assertEqual([s["x"] for s in result.snapshots], [4.0, 6.0])
        self.assertEqual(result.reasons, ["stale"])

    def test_each_type_has_its_own_mark(self):
        self.parse({**FULL_TASK, "time": 1700000100000})
        result = self.parse(POSE, {"type": 3, "partitionIds": [2], "time": 1700000000010})
        self.assertEqual(len(result.snapshots), 2)
        self.assertIsNone(result.reason)

    def test_late_task_reading_keeps_the_newer_one(self):
        self.parse({**FULL_TASK, "currentMowProgress": 6000, "time": 1700000100000})
        result = self.parse(FULL_TASK)
        self.assertEqual(result.reason, "stale")
        self.assertEqual(self.cache["dev-1"]["task"]["route_progress"], 6000)
        self.assertEqual(self.cache["dev-1"]["mow_progress"], 6000)

    def test_late_target_is_stale_but_a_repeat_is_not(self):
        self.parse({"type": 3, "partitionIds": [2], "time": 1700000060000})
        self.assertEqual(self.parse({"type": 3, "partitionIds": [5], "time": 1700000000000}).reason, "stale")
        self.assertEqual(self.cache["dev-1"]["partition_ids"], [2])
        result = self.parse({"type": 3, "partitionIds": [2], "time": 1700000120000})
        self.assertIsNone(result.reason)

    def test_entries_without_a_time_are_not_guarded(self):
        self.parse(POSE)
        result = self.parse({k: v for k, v in POSE.items() if k != "time"} | {"postureX": "3.000"})
        self.assertEqual(result.snapshots[0]["x"], 3.0)
        self.assertEqual(self.parse({"type": 4, "taskDelay": True}).reasons, [])

    def test_restored_state_seeds_the_marks(self):
        # What restore_location_groups puts back after a restart.
        self.cache["dev-1"] = {
            "device_id": "dev-1", "x": 7.0, "y": 1.0, "pose_time": 1700000050000,
            "pose_restored": True,
            "task": {"task_time_ms": 1700000050000, "route_progress": 9000},
            "task_restored": True,
            "partition_ids": [2], "target_time_ms": 1700000050000,
            "target_last_time_ms": 1700000050000, "target_restored": True,
        }
        result = self.parse(POSE, FULL_TASK, {"type": 3, "partitionIds": [4], "time": 1700000000010})
        self.assertEqual(result.snapshots, [])
        self.assertEqual(result.reasons, ["stale"])
        self.assertEqual(self.cache["dev-1"]["x"], 7.0)
        newer = self.parse({**POSE, "time": 1700000060000})
        self.assertEqual(newer.snapshots[0]["x"], 1.5)
