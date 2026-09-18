# Changelog

> This Scythnet fork is based on [vahesoo/NavimowHA](https://github.com/vahesoo/NavimowHA),
> including position/zone work from [pgoutsos/NavimowHA](https://github.com/pgoutsos/NavimowHA)
> and the original [segwaynavimow/NavimowHA](https://github.com/segwaynavimow/NavimowHA).
> Fork releases are listed first; upstream history follows.

## Scythnet fork — 1.1.1.dev0 (development)

- Add `navimow.resume` and `navimow.stop` actions with standard mower entity targeting.
  Stop pauses the current task; it does not cancel or delete it.
- Use shared authentication, command submission and state refresh for mower controls
  and the new actions, with consistent logs identifying the command and device.
- Report unsupported blade-height requests with a warning and action error.
- Log follow-up refresh failures without failing submitted actions; start reauthentication
  immediately when command authentication fails.
- Remove the actions when the last integration entry unloads and restore them on reload.
- Keep the mower's full `type:2` task report as attributes on `sensor.<mower>_mowing_zone`:
  `route_progress`, `mowing_percentage`, `area_m2`, `week_area_m2`, `action`, `sub_action`,
  `mow_start_type`, `map_work_position` (kept as sent, not decoded) and `task_time_ms`.
  Each task entry replaces the whole group; fields the entry does not carry read `null`.
  The mower can repeat the previous task's totals in the first entry of a new task;
  compare `task_time_ms` to tell entries apart.
- **Breaking:** `mow_boundary` and `mow_progress` are no longer attributes of
  `sensor.<mower>_zone`. Read the `mowing_zone` and `mow_progress` sensors instead
  (or `route_progress` on the mowing-zone sensor for the raw 0–10000 value).

## 1.1.0+scythnet.1 (test build)

- Add explicit Navimow resume and stop actions using Home Assistant device IDs.

## 1.1.0+position.3

- Decode `type:2` location messages: new `sensor.<mower>_mowing_zone` (the live
  PHYSICAL partition the mower is on -- works for "mow all" too) and
  `sensor.<mower>_mow_progress` (planned-route progress 0-10000, hits 10000 at
  completion; not the app's coverage %). Also added as `mow_boundary` /
  `mow_progress` attributes on the zone sensor.
- Gate example: open logic now treats an unknown TARGET zone as gate-required
  (a "mow all" command reports no zone), so full-property mows open the gate
  too; config now lists the dock-side (no-gate) zones instead of front zones.

## 1.1.0+position.2

- **Zone sensor attributes:** expose extra real-time location fields as attributes
  on `sensor.<mower>_zone` — `task_delay`, `partition_ids`, `vehicle_state`,
  `pose_time` — for use in templates and automations.
- **New `examples/gate-automation/`:** a parameterized package that automatically
  opens a gate so the mower can reach zones on the far side of the gate from its
  dock, and closes it once docked. Includes a live position map card
  (`navimow-map-card.js`) and a full `SETUP.md`.
- **README:** added an Examples section.

## 1.1.0+position.1

- Initial position/zone fork. Subscribes to the `…/realtimeDate/location` MQTT
  topic and decodes the pose / partition / task-delay messages, exposing
  `sensor.<mower>_zone`, `_position_x`, `_position_y`, and `_heading`.
  Self-contained — no changes to the stock `navimow-sdk` required.

---

## Upstream history

## [1.1.0](https://github.com/segwaynavimow/NavimowHA/compare/NavimowHA-v1.0.0...NavimowHA-v1.1.0) (2026-04-10)


### Features

* add danish translation ([2de5eab](https://github.com/segwaynavimow/NavimowHA/commit/2de5eab0679b68bafc66cb61eff33f3de37c568d))
* Add french translation ([303d3c8](https://github.com/segwaynavimow/NavimowHA/commit/303d3c8187c48636ac23aec293dfcbd9300fc744))


### Bug Fixes

* **navimow:** Fix OAuth token expiration issue after MQTT disconnection ([26c8ed5](https://github.com/segwaynavimow/NavimowHA/commit/26c8ed5bd7853afdb00c61ad27e2344ca8794e3e))
* **navimow:** Fixed MQTT credential refresh and unload logic ([7381489](https://github.com/segwaynavimow/NavimowHA/commit/738148937b92e659ee9c000a1308db5e389ebf22))
* **navimow:** improve MQTT reconnection handling and entity availability ([c04ae31](https://github.com/segwaynavimow/NavimowHA/commit/c04ae312f0685705215b8cfc31c1400d6c96a5e0))
* **navimow:** Optimize re-authentication error handling, distinguishing between deterministic and transient failures ([cb2bd56](https://github.com/segwaynavimow/NavimowHA/commit/cb2bd56eea8be91a8cded3e76dd74c5a5a68301e))
* **navimow:** Optimizes MQTT connection keepalive and reconnection mechanisms ([0f38417](https://github.com/segwaynavimow/NavimowHA/commit/0f384173283644e1a93798993e4152eaeb7f40b3))

## Changelog

所有版本变更将由自动化发布流程生成。
