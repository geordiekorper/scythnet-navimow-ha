"""Location-channel helpers for the entities (fork addition).

The SDK subscribes to the .../realtimeDate/location MQTT channel and decodes
it (mower_sdk.location): each applied entry reaches the coordinator as a
DeviceLocationMessage carrying the merged DeviceLocation record. What stays
here is what the entities need around that record: the dock estimate, the
target-zone state, and the restore of the location sensors' last recorded
states into a record after a restart.

Coordinates are a local Cartesian grid in METERS whose origin is ~the dock /
RTK reference (NOT latitude/longitude).
"""
from __future__ import annotations

import math
from typing import Any

from mower_sdk.models import DeviceLocation


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


POSE_SOURCE = "mqtt_location"

# The mowing-zone sensor's attributes: attribute name -> DeviceLocation field.
# The task entry's fields, and route_progress, the record's kept reading.
TASK_ATTRIBUTES: dict[str, str] = {
    "route_progress": "route_progress",
    "mowing_percentage": "mowing_percentage",
    "area_m2": "area_m2",
    "week_area_m2": "week_area_m2",
    "action": "action",
    "sub_action": "sub_action",
    "mow_start_type": "mow_start_type",
    "map_work_position": "map_work_position",
    "task_time_ms": "task_at",
}
# The task entry's own fields (route_progress is the progress group's).
TASK_FIELDS = tuple(name for name in TASK_ATTRIBUTES.values() if name != "route_progress")


# Restore groups. Each location sensor restores its last recorded state on
# startup; the coordinator assembles the groups into one DeviceLocation and
# hands it to the SDK, and a group counts as restored until a live entry of
# the type that replaces it is applied.
RESTORE_GROUPS = ("pose", "task", "progress", "target", "delay")


def _target_group(state: str | None, attrs: dict[str, Any]) -> dict[str, Any] | None:
    """The target fields a zone sensor's last state proves, or None.

    The zone sensor's partition_ids attribute is None whenever no target was
    reported, so it cannot say on its own whether one was; the recorded state
    can. A partition id with a non-empty list restores that list; all or none
    (a report with no active target) restores the list, or an empty one when
    the attribute is None; anything else (unknown, unavailable, or missing
    attributes) restores nothing, so the target stays unreported.
    """
    if "partition_ids" not in attrs:
        return None
    pids = attrs.get("partition_ids")
    ids = [pid for pid in (_int(v) for v in pids)] if isinstance(pids, list) else None
    if ids is not None and None in ids:
        ids = None
    if state in (TARGET_ALL, TARGET_NONE):
        partition_ids = ids if ids is not None else []
    elif _int(state) is not None and ids:
        partition_ids = ids
    else:
        return None
    return {
        "partition_ids": partition_ids,
        "target_at": _int(attrs.get("target_time_ms")),
        "target_last_at": _int(attrs.get("target_last_time_ms")),
    }


def restore_location_groups(
    key: str, state: str | None, attributes: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """DeviceLocation fields to restore from a sensor's last recorded state, by sensor key.

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
            "pose_at": _int(attrs.get("pose_time_ms")),
            "pose_received_at": _str(attrs.get("received_at")),
        })]
    if key == "mowing_zone":
        fields: dict[str, Any] = {}
        zone = _int(state)
        if zone is not None:
            fields["current_zone"] = zone
        for attr, name in TASK_ATTRIBUTES.items():
            if name in TASK_FIELDS and attr in attrs:
                fields[name] = attrs.get(attr)
        return [("task", fields)] if fields else []
    if key == "mow_progress":
        pct = _num(state)
        if pct is None or attrs.get("progress_source") != "route":
            return []  # a percentage fallback is restored with the task group
        return [("progress", {"route_progress": int(round(pct * 100))})]
    if key == "zone":
        groups: list[tuple[str, dict[str, Any]]] = []
        target = _target_group(state, attrs)
        if target is not None:
            groups.append(("target", target))
        if isinstance(attrs.get("task_delay"), bool):
            groups.append(("delay", {
                "task_delay": attrs["task_delay"],
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


def target_zone(location: DeviceLocation | None, activity: str | None) -> int | str | None:
    """State of the target-zone sensor.

    None (unknown) until a target report has been received; the first
    partition id when the report names zones; otherwise TARGET_ALL while the
    mower is mowing or paused, and TARGET_NONE when it is not.
    """
    if location is None or location.partition_ids is None:
        return None
    if location.partition_ids:
        return location.partition_ids[0]
    if (activity or "").lower() in MOW_ALL_ACTIVITIES:
        return TARGET_ALL
    return TARGET_NONE
