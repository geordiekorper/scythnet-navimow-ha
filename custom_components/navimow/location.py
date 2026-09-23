"""Real-time location / zone decoding for Navimow (fork addition).

The stock navimow-sdk subscribes to the .../realtimeDate/state, /event and
/attributes MQTT channels but NOT /location, and its router drops the location
payload (a JSON array, not a dict). This module decodes that topic so the
integration can expose live position and the current mowing zone.

Observed payload: a JSON array of objects keyed by ``type``:
  type 1  pose     {postureX, postureY (meters), postureTheta (radians), vehicleState, time}
  type 2  task     {currentMowBoundary (live physical partition id), currentMowProgress
                    (route progress 0-10000, reaches 10000 at completion), mowingPercentage,
                    subtotalArea / mowingWeekArea (m2, sent as strings), action, subAction,
                    mowStartType, mapWorkPosition (128-hex string), time (ms)}
  type 3  zone     {partitionIds: [int], time (ms)} -> the TARGET partition (set at
                    task start; absent for a "mow all" command)
  type 4  delay    {taskDelay: bool}       -> rain / schedule delay; no time field
NOTE: type 3 = target zone (drives gate pre-open); type 2 currentMowBoundary = the
live physical zone (updates only after the mower crosses). They are kept separate.
Coordinates are a local Cartesian grid in METERS whose origin is ~the dock /
RTK reference (NOT latitude/longitude).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable


def vehicle_topic(device_id: str, channel: str) -> str:
    """Cloud MQTT topic of one of a device's channels (state, event,
    attributes, location)."""
    return f"/downlink/vehicle/{device_id}/realtimeDate/{channel}"


def location_topic(device_id: str) -> str:
    """Cloud MQTT topic that carries real-time pose/zone for a device."""
    return vehicle_topic(device_id, "location")


# Mower status values during which the pose is the dock position. "idle" is
# deliberately excluded: the mower can sit idle mid-lawn after a manual stop.
DOCKED_STATES = frozenset({"docked", "charging"})

# Cap on the effective sample count for the dock average. Once reached, new
# samples keep a constant 1/DOCK_MAX_SAMPLES weight, so the estimate tracks a
# physically moved dock instead of being frozen by historical samples.
DOCK_MAX_SAMPLES = 200


def update_dock_estimate(
    dock: dict | None, x: float, y: float, max_samples: int = DOCK_MAX_SAMPLES
) -> dict:
    """Fold one docked pose sample into the running dock-position average.

    Returns a new dict {"x", "y", "n"}; pass the previous result (or None)
    as ``dock``. The capped incremental mean smooths RTK jitter while still
    converging on a new location if the dock is moved.
    """
    d = dock or {"x": 0.0, "y": 0.0, "n": 0}
    n = min(int(d.get("n", 0)), max_samples - 1)
    return {
        "x": (d["x"] * n + float(x)) / (n + 1),
        "y": (d["y"] * n + float(y)) / (n + 1),
        "n": n + 1,
    }


def _num(value: Any) -> float | None:
    """Vendor number (often sent as a string such as "100.00") as float, else None."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _int(value: Any) -> int | None:
    """Vendor integer (int, float or numeric string) as int, else None."""
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _str(value: Any) -> str | None:
    """Vendor string kept as sent; not decoded."""
    return None if value is None else str(value)


POSE_SOURCE = "mqtt_location"


def parse_pose_entry(item: dict) -> dict[str, Any] | None:
    """One type-1 entry as a complete pose, or None when X/Y are unusable.

    Theta, vehicleState and time come from the same entry; a missing theta
    is None rather than the previous pose's heading.
    """
    x = _num(item.get("postureX"))
    y = _num(item.get("postureY"))
    if x is None or y is None:
        return None
    return {
        "x": x,
        "y": y,
        "theta": _num(item.get("postureTheta")),
        "vehicle_state": _int(item.get("vehicleState")),
        "pose_time": _int(item.get("time")),
    }


# Type-2 task entry: (vendor key, attribute name, converter). Every key is read
# from the same entry so the group is one observation; keys absent from the
# entry are None rather than borrowed from an earlier entry.
TASK_FIELDS: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("currentMowProgress", "route_progress", _int),
    ("mowingPercentage", "mowing_percentage", _num),
    ("subtotalArea", "area_m2", _num),
    ("mowingWeekArea", "week_area_m2", _num),
    ("action", "action", _int),
    ("subAction", "sub_action", _int),
    ("mowStartType", "mow_start_type", _int),
    ("mapWorkPosition", "map_work_position", _str),
    ("time", "task_time_ms", _int),
)

TASK_ATTRIBUTES: tuple[str, ...] = tuple(attr for _, attr, _ in TASK_FIELDS)


def parse_task_entry(item: dict) -> dict[str, Any]:
    """One type-2 entry as a complete task observation (see TASK_FIELDS)."""
    return {attr: conv(item.get(key)) for key, attr, conv in TASK_FIELDS}


# Restore groups. Each location sensor restores its last recorded state on
# startup into the shared cache under one of these groups; the record then
# carries "<group>_restored": True until the first live entry of the same
# type replaces it (the parser pops the marker).
RESTORE_GROUPS = ("pose", "task", "progress", "target", "delay")


def restore_location_groups(
    key: str, state: str | None, attributes: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Cache fields to seed from a sensor's last recorded state, by sensor key.

    Returns (group, fields) pairs; empty when the last state holds nothing
    usable (for example ``unknown`` with no attributes).
    """
    attrs = attributes or {}
    if key == "position_x":
        x, y = _num(state), _num(attrs.get("y"))
        if x is None or y is None:
            return []
        return [("pose", {
            "x": x,
            "y": y,
            "theta": _num(attrs.get("theta_rad")),
            "vehicle_state": _int(attrs.get("vehicle_state")),
            "pose_time": _int(attrs.get("pose_time_ms")),
            "received_at": _str(attrs.get("received_at")),
        })]
    if key == "mowing_zone":
        fields: dict[str, Any] = {}
        boundary = _int(state)
        if boundary is not None:
            fields["mow_boundary"] = boundary
        if any(attr in attrs for attr in TASK_ATTRIBUTES):
            fields["task"] = {attr: attrs.get(attr) for attr in TASK_ATTRIBUTES}
        return [("task", fields)] if fields else []
    if key == "mow_progress":
        pct = _num(state)
        if pct is None or attrs.get("progress_source") != "route":
            return []  # a percentage fallback is restored with the task group
        return [("progress", {"mow_progress": int(round(pct * 100))})]
    if key == "zone":
        groups: list[tuple[str, dict[str, Any]]] = []
        if "partition_ids" in attrs:
            pids = attrs.get("partition_ids")
            groups.append(("target", {
                "partition_ids": pids,
                "partition": pids[0] if isinstance(pids, list) and pids else None,
                "target_time_ms": _int(attrs.get("target_time_ms")),
                "target_last_time_ms": _int(attrs.get("target_last_time_ms")),
            }))
        if "task_delay" in attrs:
            groups.append(("delay", {
                "task_delay": attrs.get("task_delay"),
                "delay_received_at": _str(attrs.get("delay_received_at")),
            }))
        return groups
    return []


# Target-zone sensor states beyond a partition id. The mower never reports
# "mow all": it sends the same empty target report whether it is idle or mowing
# everything, so TARGET_ALL is inferred from the mower's activity.
TARGET_NONE = "none"
TARGET_ALL = "all"
# Activities during which an empty target means a mow-all task is under way.
# "returning" is excluded: a dock command clears the target for the trip home.
MOW_ALL_ACTIVITIES = frozenset({"mowing", "paused"})


def target_zone(loc: dict | None, activity: str | None) -> int | str | None:
    """State of the target-zone sensor.

    None (unknown) until a target report has been received; the first
    partition id when the report names zones; otherwise TARGET_ALL while the
    mower is mowing or paused, and TARGET_NONE when it is not.
    """
    if not loc or "partition_ids" not in loc:
        return None
    partition = loc.get("partition")
    if partition is not None:
        return partition
    if (activity or "").lower() in MOW_ALL_ACTIVITIES:
        return TARGET_ALL
    return TARGET_NONE


def progress_percent(loc: dict | None) -> tuple[float | None, str]:
    """Route progress as a percentage, with the field it came from.

    ``currentMowProgress`` (0-10000, kept in ``mow_progress``) wins;
    ``mowingPercentage`` from the latest task entry is the fallback; with
    neither the value is None (unknown), never a manufactured zero. A
    reported zero stays 0.0. The source is ``route``, ``percentage`` or
    ``none``.
    """
    if not loc:
        return None, "none"
    route = _int(loc.get("mow_progress"))
    if route is not None:
        return route / 100, "route"
    pct = _num((loc.get("task") or {}).get("mowing_percentage"))
    if pct is not None:
        return pct, "percentage"
    return None, "none"


# Event times outside this window are not believed: before 2020, or more than
# five minutes after receipt (two target reports have arrived stamped January
# 1970). Such an entry changes nothing and is recorded as rejected input.
PLAUSIBLE_MIN_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z
TIME_AHEAD_MAX_MS = 5 * 60 * 1000


def plausible_time(event_ms: int, now_ms: int) -> bool:
    """Whether an event time can be believed (both ends included)."""
    return PLAUSIBLE_MIN_MS <= event_ms <= now_ms + TIME_AHEAD_MAX_MS


def mower_time_ms(value: Any) -> int | None:
    """A mower timestamp as epoch milliseconds, whether sent in seconds or
    milliseconds; None when absent or not a positive number."""
    number = _int(value)
    if number is None or number <= 0:
        return None
    return number if number > 100_000_000_000 else number * 1000


def is_placeholder_pose(pose: dict[str, Any]) -> bool:
    """An all-zero posture a standing mower may send instead of a position."""
    return pose["x"] == 0 and pose["y"] == 0 and not pose["theta"]


# Every field and entry type the decoder knows (as in Scythnet). An entry with
# another field still applies what it knows and the message is recorded as
# rejected input, so a field the mower starts sending is never lost; an entry
# of another type applies nothing.
KNOWN_FIELDS = frozenset({
    "type", "time", "postureX", "postureY", "postureTheta", "vehicleState",
    "currentMowBoundary", "currentMowProgress", "mowingPercentage",
    "subtotalArea", "mowingWeekArea", "partitionIds", "taskDelay", "action",
    "subAction", "mowStartType", "mapWorkPosition",
})
ENTRY_TYPES = frozenset({1, 2, 3, 4})

# When a message earns several rejection reasons, the one recorded as
# `reason` is the first of these present (all are listed in `reasons`).
REASON_PRIORITY = (
    "unparsable", "implausible_time", "unknown_type", "unknown_field",
    "stale", "placeholder",
)


@dataclass
class ParsedLocation:
    """What one location message did: a snapshot of the record after each
    entry that changed it, and why any part of the message was not applied."""

    snapshots: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def reason(self) -> str | None:
        """The deciding reason, or None when everything was applied."""
        return next((r for r in REASON_PRIORITY if r in self.reasons), None)


# Per-type high-water marks kept in the record, apart from the entry times
# the sensors show: an entry without a time must not reset the mark.
MARK_KEYS = {1: "mark_pose_ms", 2: "mark_task_ms", 3: "mark_target_ms"}


def newest_time(loc: dict[str, Any], entry_type: Any) -> int | None:
    """Mower time of the newest applied entry of ``entry_type``, or None.

    This is the per-type high-water mark: messages arrive late and out of
    order, and an entry at or below it is not applied. It is the kept mark
    or the time of the entry the record holds, whichever is newer, so the
    restored sensor states seed it after a restart. Type 4 carries no time
    and is never guarded.
    """
    if entry_type == 1:
        shown = _int(loc.get("pose_time"))
    elif entry_type == 2:
        shown = _int((loc.get("task") or {}).get("task_time_ms"))
    elif entry_type == 3:
        shown = _int(loc.get("target_last_time_ms"))
    else:
        return None
    times = [t for t in (_int(loc.get(MARK_KEYS[entry_type])), shown) if t is not None]
    return max(times) if times else None


def _zone_set(pids: Any) -> frozenset:
    """A target report's zones as a set: the vendor's list order means nothing."""
    return frozenset(pids) if isinstance(pids, list) else frozenset()


def parse_location_message(
    cache: dict[str, dict],
    device_id: str,
    data: Any,
    received_at: str | None = None,
    now_ms: int | None = None,
) -> ParsedLocation:
    """Merge one location message into the per-device cache, entry by entry.

    Fields persist across messages (a pose update keeps the last-known zone).
    ``received_at`` is HA's receipt time for this message (UTC ISO 8601); it
    is stored with the pose the message carried. The result holds one
    snapshot of the record after each entry that changed it, in message
    order, so a message carrying several poses or task entries publishes each
    of them, and the reasons any entry was not applied (REASON_PRIORITY).
    ``now_ms`` is the receipt time the plausibility window is measured from.
    ``data`` is the decoded JSON, or None when the payload was not JSON.
    """
    result = ParsedLocation()
    if isinstance(data, dict):
        data = [data]  # a lone entry, not wrapped in the usual array
    if not isinstance(data, list):
        result.reject("unparsable")  # not JSON, or neither array nor object
        return result
    now_ms = round(time.time() * 1000) if now_ms is None else now_ms
    loc = dict(cache.get(device_id) or {})
    loc["device_id"] = device_id
    for item in data:
        if not isinstance(item, dict):
            continue
        changed = False
        t = item.get("type")
        if not set(item) <= KNOWN_FIELDS:
            result.reject("unknown_field")
        if t not in ENTRY_TYPES:
            result.reject("unknown_type")
            continue
        if t == 4 and "taskDelay" not in item:
            # The reconnect-time shape {"time", "type": 4, "vehicleState"}
            # carries no delay; the pose already carries the state.
            continue
        entry_time = _int(item.get("time"))
        if entry_time is not None and entry_time <= 0:
            entry_time = None  # not a usable time
        if entry_time is not None and not plausible_time(entry_time, now_ms):
            result.reject("implausible_time")
            continue
        newest = newest_time(loc, t)
        if entry_time is not None and newest is not None and entry_time <= newest:
            # Late or repeated delivery: history, not news.
            result.reject("stale")
            continue
        if t == 1:
            pose = parse_pose_entry(item)
            if pose is None:
                # unusable X/Y: the previous pose stays untouched
                result.reject("unparsable")
                continue
            if is_placeholder_pose(pose):
                result.reject("placeholder")
                continue
            loc.update(pose)  # replaced whole, never mixed with an older pose
            loc["received_at"] = received_at
            loc.pop("pose_restored", None)
            changed = True
        elif t == 2:
            # Live physical-mowing progress. currentMowBoundary is the
            # partition the mower is actually mowing now (works for "mow all"
            # too); currentMowProgress is route progress (0-10000, hits 10000
            # at completion -- planned-path progress, not area coverage %).
            if "currentMowBoundary" in item:
                loc["mow_boundary"] = item.get("currentMowBoundary")
            if "currentMowProgress" in item:
                loc["mow_progress"] = item.get("currentMowProgress")
                loc.pop("progress_restored", None)
            # The full task report, replaced whole per entry. The mower may
            # repeat the previous task's totals in the first entry of a new
            # task; task_time_ms tells the entries apart.
            loc["task"] = parse_task_entry(item)
            loc.pop("task_restored", None)
            changed = True
        elif t == 3:
            pids = item.get("partitionIds")
            entry_time = _int(item.get("time"))
            # target_time_ms is the mower time of the first report of this
            # target; a repeat of the same set only advances
            # target_last_time_ms, so each repeat is still recorded.
            if "partition_ids" not in loc or _zone_set(pids) != _zone_set(loc["partition_ids"]):
                loc["target_time_ms"] = entry_time
            loc["target_last_time_ms"] = entry_time
            loc["partition_ids"] = pids
            loc["partition"] = pids[0] if isinstance(pids, list) and pids else None
            loc.pop("target_restored", None)
            changed = True
        elif t == 4:
            loc["task_delay"] = item.get("taskDelay")
            # The delay report carries no time of its own.
            loc["delay_received_at"] = received_at
            loc.pop("delay_restored", None)
            changed = True
        if changed:
            if entry_time is not None and t in MARK_KEYS:
                loc[MARK_KEYS[t]] = entry_time
            result.snapshots.append(dict(loc))
    if result.snapshots:
        cache[device_id] = loc
    return result


def parse_location_payload(
    cache: dict[str, dict],
    device_id: str,
    data: Any,
    received_at: str | None = None,
) -> dict | None:
    """The record after the whole message, or None if nothing relevant changed."""
    snapshots = parse_location_message(cache, device_id, data, received_at).snapshots
    return snapshots[-1] if snapshots else None
