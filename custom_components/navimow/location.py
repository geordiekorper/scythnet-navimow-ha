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
  type 3  zone     {partitionIds: [int]}   -> the TARGET partition (set at task start;
                    absent for a "mow all" command)
  type 4  delay    {taskDelay: bool}       -> rain / schedule delay
NOTE: type 3 = target zone (drives gate pre-open); type 2 currentMowBoundary = the
live physical zone (updates only after the mower crosses). They are kept separate.
Coordinates are a local Cartesian grid in METERS whose origin is ~the dock /
RTK reference (NOT latitude/longitude).
"""
from __future__ import annotations

import math
from typing import Any, Callable


def location_topic(device_id: str) -> str:
    """Cloud MQTT topic that carries real-time pose/zone for a device."""
    return f"/downlink/vehicle/{device_id}/realtimeDate/location"


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


def parse_location_payload(
    cache: dict[str, dict], device_id: str, data: Any
) -> dict | None:
    """Merge one location message into the per-device cache.

    Fields persist across messages (a pose update keeps the last-known zone).
    Returns the updated record, or None if nothing relevant changed.
    """
    if not isinstance(data, list):
        return None
    loc = dict(cache.get(device_id) or {})
    loc["device_id"] = device_id
    changed = False
    for item in data:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == 1:
            try:
                loc["x"] = float(item["postureX"])
                loc["y"] = float(item["postureY"])
                loc["theta"] = float(item["postureTheta"])
            except (TypeError, ValueError, KeyError):
                pass
            if "vehicleState" in item:
                loc["vehicle_state"] = item["vehicleState"]
            if "time" in item:
                loc["pose_time"] = item["time"]
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
            # The full task report, replaced whole per entry. The mower may
            # repeat the previous task's totals in the first entry of a new
            # task; task_time_ms tells the entries apart.
            loc["task"] = parse_task_entry(item)
            changed = True
        elif t == 3:
            pids = item.get("partitionIds")
            loc["partition_ids"] = pids
            loc["partition"] = pids[0] if isinstance(pids, list) and pids else None
            changed = True
        elif t == 4:
            # Only a real delay report updates the flag. The reconnect-time
            # shape {"time", "type": 4, "vehicleState"} carries no taskDelay
            # and must not clear the last value; the pose carries the state.
            if "taskDelay" in item:
                loc["task_delay"] = item.get("taskDelay")
                changed = True
    if not changed:
        return None
    cache[device_id] = loc
    return loc
