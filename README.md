# Navimow for Home Assistant — position & zone fork

<p align="center">
  <img src="https://fra-navimow-prod.s3.eu-central-1.amazonaws.com/img/navimowhomeassistant.png" width="600">
</p>

Monitor and control Navimow robotic mowers in Home Assistant.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=geordiekorper&repository=scythnet-navimow-ha&category=Integration)

> **This Scythnet fork is based on [vahesoo/NavimowHA](https://github.com/vahesoo/NavimowHA),
> derived from [pgoutsos/NavimowHA](https://github.com/pgoutsos/NavimowHA) and the
> official [segwaynavimow/NavimowHA](https://github.com/segwaynavimow/NavimowHA).**
> It adds **real-time position and current mowing-zone sensors** on top of the
> official integration. All credit for the base integration goes to the Segway
> Navimow team. See [What this fork adds](#what-this-fork-adds-) below.

## What this fork adds ✨

The official integration exposes a `lawn_mower` entity and a battery sensor. The
mower also continuously publishes its **live pose and current map partition**
over MQTT (topic `/downlink/vehicle/{id}/realtimeDate/location`), but the bundled
`navimow-sdk` neither subscribes to that topic nor parses it, so none of it
reaches Home Assistant.

This fork makes the integration subscribe to that topic and turn it into
entities, giving you these **additional sensors** per mower (populated while the
mower is active):

| Entity | Meaning |
| --- | --- |
| `sensor.<mower>_zone` | Target partition id — the zone the current task is headed for (set at task start; empty for "mow all") |
| `sensor.<mower>_position_x` | X position in meters (local grid; origin = the RTK/mapping reference, usually *near* the dock). Attributes carry the complete latest pose: `y`, `theta_rad`, `vehicle_state`, `pose_time_ms`, `received_at`, `source` |
| `sensor.<mower>_position_y` | Y position in meters |
| `sensor.<mower>_heading` | Mower orientation in degrees (0–360) |
| `sensor.<mower>_mowing_zone` | Partition id the mower is *physically* mowing right now (works for "mow all" too). Attributes carry the rest of the mower's task report: `route_progress` (0–10000), `mowing_percentage`, `area_m2`, `week_area_m2`, `action`, `sub_action`, `mow_start_type`, `map_work_position`, `task_time_ms` |
| `sensor.<mower>_mow_progress` | Planned-route progress for the current task as a percentage (the mower's 0–10000 route progress ÷ 100, falling back to its `mowingPercentage`); `unknown` until a task report arrives. Not area coverage. The `progress_source` attribute names the field used |
| `sensor.<mower>_dock_x` / `_dock_y` | Dock position in meters — auto-learned by averaging the mower's pose while docked/charging; survives restarts. `unknown` until the mower has docked once. Used by the example map card to place the dock marker (the coordinate origin is **not** reliably the dock) |
| `sensor.<mower>_data_source` | Diagnostic: which source supplied the current mower state (`mqtt_push`, `mqtt_cache`, `http_fallback`, `none`). Attributes hold the last MQTT state message and the last REST poll side by side (`mqtt_*`, `rest_*`), each with the device's own timestamp when it sent one and Home Assistant's receipt or poll time |

These unlock zone-aware and position-aware automations — for example opening a
gate when the mower crosses between zones, geofencing, or live mapping. The
coordinates are a **local Cartesian grid in meters**, not latitude/longitude.

### How it works

No changes to `navimow-sdk` are required — this fork is self-contained and works
against the stock SDK from PyPI:

* On MQTT connect, the integration subscribes to the `…/realtimeDate/location`
  topic for each device.
* Incoming location messages (a JSON array of objects keyed by `type`:
  `1` = pose, `3` = partition/zone, `4` = task-delay) are decoded in
  `location.py` and merged into a per-device record, so a pose update never
  wipes the last-known zone.
* The record is pushed to the coordinator and exposed via the four sensors above.

Because the location stream is delivered through Segway's cloud, these sensors
update only while the mower is active and depend on internet connectivity. Build
any safety-critical automation (e.g. a gate) with a fallback on the mower
`status` and a physical sensor.

## Scythnet fork: resume and stop actions

The [Scythnet fork](https://github.com/geordiekorper/scythnet-navimow-ha) adds
explicit resume and stop actions with shared command handling and logging.
The entity targeting and shared command handling described here are development changes (`1.1.1.dev0`);
see [the changelog](CHANGELOG.md).

All five controls are available in **Developer Tools → Actions**:

| Action | Purpose | Selection |
| --- | --- | --- |
| `lawn_mower.start_mowing` | Start mowing | Mower entity |
| `lawn_mower.pause` | Pause mowing | Mower entity |
| `lawn_mower.dock` | Return to the dock | Mower entity |
| `navimow.resume` | Explicitly resume the current task | Mower entity |
| `navimow.stop` | Pause using the stop command; does not cancel or delete the task | Mower entity |

Home Assistant's standard `lawn_mower` domain provides start, pause, and dock.
Resume and stop are additional integration actions in the `navimow` domain.

For example, start mowing by targeting the mower entity (replace the example
entity ID with yours):

```yaml
action: lawn_mower.start_mowing
target:
  entity_id: lawn_mower.navimow_x430
```

Resume and stop use the same entity target:

```yaml
action: navimow.resume
target:
  entity_id: lawn_mower.navimow_x430
```

Use `action: navimow.stop` with the same entity target to stop mowing.
Entity IDs can be renamed in HA; use the current full ID, including `lawn_mower.`.
Multiple mowers can be selected with a list of entity IDs.

HA handles missing or unavailable entities using its standard targeting behavior;
no command is sent to those entities.

Mower controls and these actions refresh authentication before sending a command
and request a state update afterward. Command submissions are logged at INFO level;
submission failures at ERROR level. Each log identifies the command and device.
State refresh failures do not fail an accepted command; Home Assistant reports
coordinator update errors through its normal logging.
INFO messages require INFO logging to be enabled for `custom_components.navimow.commands`.

Successful submission does not confirm that the mower completed the action; check
its reported state. A subsequent refresh failure is logged without failing the submitted action.
Authentication failures before submission start the integration's reauthentication flow.
Blade-height adjustment is unsupported. Requests log a warning with the device
and requested height, then report an action error without sending a command.

## Examples 📦

[`examples/gate-automation/`](examples/gate-automation/) contains a complete,
parameterized package that uses the zone/position sensors to **automatically open
a gate** so the mower can reach zones on the far side of the gate from its dock,
and close it again once docked. See
[`examples/gate-automation/SETUP.md`](examples/gate-automation/SETUP.md) for the
full walkthrough (it also includes a live position **map card**,
`navimow-map-card.js`).

## Prerequisites 📋

- **Home Assistant** minimum version **2026.1.0**
- A **Navimow account** that can sign in to the official app (used for authorization)

## Installation 🛠️

This integration is not in the default HACS store; add this fork as a custom
repository:

1. HACS → top-right menu → **Custom repositories**
2. Repository: `https://github.com/geordiekorper/scythnet-navimow-ha`
3. Category: **Integration**
4. Search for `Navimow` in HACS and download it
5. Restart Home Assistant
6. Settings → Devices & Services → Add Integration → search `Navimow`

**Switching from the official integration?** In HACS, **Remove** the existing
Navimow download first (this deletes the files but keeps your configured device
and login), then add this fork as above and download it. Do **not** delete the
Navimow integration from Settings → Devices & Services, or you'll have to
re-authenticate.

## Usage 🎮

After setup you should see:

- A `lawn_mower` entity (start / pause / dock)
- Explicit `navimow.resume` and `navimow.stop` actions (see the command section above)
- A battery `sensor`
- `zone`, `position_x`, `position_y`, `heading`, `mowing_zone`, `mow_progress`,
  `dock_x`, `dock_y`, and a diagnostic `data_source` sensor (this fork)

While docked the mower still reports a pose every few minutes, so the position
and heading sensors stay populated; the target-zone sensor reads `unknown` while
there is no active target, and the task sensors (`mowing_zone`, `mow_progress`)
update only during a mow. On startup each of these sensors restores its last
recorded value and marks it `is_restored: true` until live data of the same kind
replaces it, so a restart no longer blanks them. The dock sensors learn (and
refine) the dock position *while* the mower sits docked/charging, keep their
value across restarts, and read `unknown` only until the first docking after
install — their `samples`/`source` attributes show the learning state.

See the upstream [Getting Started](https://github.com/segwaynavimow/NavimowHA/wiki/Getting-Started)
guide for general setup.

## Troubleshooting 🔧

Check the Home Assistant logs for error messages, and:

- Ensure the mower is connected to your network and reachable from Home Assistant.
- Restart Home Assistant and check if the issue persists.
- Make sure you are not blocking network access to Segway's services.
- If you use DNS filtering/ad-blocking, try disabling it temporarily.

If the position/zone sensors never populate while mowing, enable debug logging
for `mower_sdk.mqtt` (Developer Tools → Actions → `logger.set_level`) and confirm
that `…/realtimeDate/location` messages are arriving.

## Navimow SDK Library 📚

This integration uses the `navimow-sdk` package to communicate with Navimow
mowers. This fork does **not** modify the SDK.

## Relationship to upstream & contributing back

This Scythnet fork is based on [vahesoo/NavimowHA](https://github.com/vahesoo/NavimowHA),
which includes the position/zone work from [pgoutsos/NavimowHA](https://github.com/pgoutsos/NavimowHA)
and the official [segwaynavimow/NavimowHA](https://github.com/segwaynavimow/NavimowHA).
The position/zone functionality is a candidate for upstreaming — if/when the
official integration adds it natively, this fork can be retired in favor of the
official release. Issues and PRs specific to the position/zone sensors can be
filed against this repository; general integration issues belong upstream.

### Release versioning

`1.1.1.dev0` identifies the current development build, not a published release.
Release Please manages the next stable version and its changelog entry from
conventional commits, updating the integration manifest through `extra-files`.
Stable releases use its normal version numbers without a `+scythnet` suffix.
Before publishing a release, replace development-status wording here and in
`info.md` and reconcile these development notes with the generated release entry.
