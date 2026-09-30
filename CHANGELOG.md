# Changelog

> This Scythnet fork is based on [vahesoo/NavimowHA](https://github.com/vahesoo/NavimowHA),
> including position/zone work from [pgoutsos/NavimowHA](https://github.com/pgoutsos/NavimowHA)
> and the original [segwaynavimow/NavimowHA](https://github.com/segwaynavimow/NavimowHA).
> Fork releases are listed first; upstream history follows.

## [1.3.0](https://github.com/geordiekorper/scythnet-navimow-ha/compare/NavimowHA-v1.2.0...NavimowHA-v1.3.0) (2026-09-30)


### Features

* cloud_connected binary sensor ([73bdfa2](https://github.com/geordiekorper/scythnet-navimow-ha/commit/73bdfa292c40bc49c37f8a5e2130e12b09090566))
* collector_status diagnostic sensor ([da3fe13](https://github.com/geordiekorper/scythnet-navimow-ha/commit/da3fe133209b4d92d7b58b5e8f90bd17b4e687f1))
* last_message timestamp sensor ([03d2a79](https://github.com/geordiekorper/scythnet-navimow-ha/commit/03d2a79461e4e420082dcd8f393cbffc07855b93))
* mower times for target-zone and delay reports ([57a3c12](https://github.com/geordiekorper/scythnet-navimow-ha/commit/57a3c1226c03332f7d6e3afdd8a88e3f617059ff))
* MQTT watchdog rebuilds after 180 s of location silence while mowing ([bd663e8](https://github.com/geordiekorper/scythnet-navimow-ha/commit/bd663e84ee93e742bb5a50c1dfd38dae5d45ee24))
* MQTT watchdog rebuilds the client when REST shows a missed transition ([fb93b5c](https://github.com/geordiekorper/scythnet-navimow-ha/commit/fb93b5c81831090a134748062287a60ffb225c58))
* navimow.command action with the vendor's verdict for every command ([e16e704](https://github.com/geordiekorper/scythnet-navimow-ha/commit/e16e70411ab7cea5c260a74ce1a777afcc5372b9))
* poll REST 5 s after each command ([84050c7](https://github.com/geordiekorper/scythnet-navimow-ha/commit/84050c78b9aaea3c6c53e7f36ee968b5dca6c237))
* record messages on the MQTT event and attributes channels ([fc40f83](https://github.com/geordiekorper/scythnet-navimow-ha/commit/fc40f836931c22a036fb411c32467807a5f33108))
* record MQTT payloads that are not JSON or not the expected shape ([2d79d8a](https://github.com/geordiekorper/scythnet-navimow-ha/commit/2d79d8af3648ed6cc3dd2f4f2d999899fb8b3af2))
* record MQTT state messages that carry unknown fields ([c92da91](https://github.com/geordiekorper/scythnet-navimow-ha/commit/c92da918e5d45319fecbfbb8177cf2deeda7cb49))
* record unknown location entry types and fields ([4692d67](https://github.com/geordiekorper/scythnet-navimow-ha/commit/4692d670cc8367a45baa45bdcbc3ec777fd8e0f1))
* rejected_input diagnostic sensor ([8ce0343](https://github.com/geordiekorper/scythnet-navimow-ha/commit/8ce0343c8a74d539f8d80feac1fba48830b5f11c))
* REST poll interval as an integration option ([476aaa7](https://github.com/geordiekorper/scythnet-navimow-ha/commit/476aaa7cabf3f5c718d582c0b5371827316bffe8))
* rest_status diagnostic sensor with every REST reply as sent ([b7b330b](https://github.com/geordiekorper/scythnet-navimow-ha/commit/b7b330b607123836b994e3f176934882abcfb329))
* return the vendor's verdict from the navimow resume and stop actions ([81d4f1b](https://github.com/geordiekorper/scythnet-navimow-ha/commit/81d4f1bbffa2ad47b17f63584d07768a64fdebd0))
* steady REST status poll independent of MQTT ([8a10b41](https://github.com/geordiekorper/scythnet-navimow-ha/commit/8a10b41d4453eff51c9f809f74e11ffbfc752b1d))


### Bug Fixes

* a lone-entry location message is not an unknown field ([f6e37f6](https://github.com/geordiekorper/scythnet-navimow-ha/commit/f6e37f664980b12c48da482836ce667850763e23))
* apply a location message's entries in time order ([7bc780a](https://github.com/geordiekorper/scythnet-navimow-ha/commit/7bc780a52de8c6b0833cf1b957cab2ce518466cf))
* distinguish no-target and mow-all from unknown in the zone sensor ([66d8355](https://github.com/geordiekorper/scythnet-navimow-ha/commit/66d8355d1d185a259d29fda4c34eddadfa79cb19))
* drop late location entries instead of letting the position jump back ([de0dc60](https://github.com/geordiekorper/scythnet-navimow-ha/commit/de0dc606e760bc2e3b5c46cd1ed1fc36974514fa))
* first steady REST poll 5 s after start-up, not a full interval ([9214968](https://github.com/geordiekorper/scythnet-navimow-ha/commit/9214968897884c1398f61fc54ebde7e5f54517b0))
* keep a late MQTT state message from becoming the current state ([9a0de3b](https://github.com/geordiekorper/scythnet-navimow-ha/commit/9a0de3b026c23674c6af7cbb94211f5962b61102))
* last_message starts from messages before it existed; interval shown at once ([4ee4950](https://github.com/geordiekorper/scythnet-navimow-ha/commit/4ee4950d185236100c3651b21258e002d641fb62))
* late-message guard survives untimed entries and start-up ([9403a05](https://github.com/geordiekorper/scythnet-navimow-ha/commit/9403a05734e3246907b7357ab9380731b4f8bf40))
* MQTT keepalive 60 s instead of 2400 s ([88e27a9](https://github.com/geordiekorper/scythnet-navimow-ha/commit/88e27a94175c637375630674cd63a4502549f94b))
* no MQTT client rebuild or credential refresh after the entry unloads ([e9cfb8b](https://github.com/geordiekorper/scythnet-navimow-ha/commit/e9cfb8b475ee9c6a9abb60dddb7b0b70d27c83fe))
* publish every pose and task entry of a location message ([0d10641](https://github.com/geordiekorper/scythnet-navimow-ha/commit/0d10641cab1ad5af453534a551793824ff1ce4ed))
* record late and implausibly stamped state messages as received ([ea7dff5](https://github.com/geordiekorper/scythnet-navimow-ha/commit/ea7dff59d2bb6971426d1e7d7d963edb65a29e1d))
* reject implausibly stamped and placeholder location entries ([ae8c253](https://github.com/geordiekorper/scythnet-navimow-ha/commit/ae8c253cdd33fbe9dba026f3518d9ba206296bba))
* REST follow-up polls are no longer postponed or dropped ([5d02c2b](https://github.com/geordiekorper/scythnet-navimow-ha/commit/5d02c2b503fa7445aac2675d5194b2001ee5c0a0))
* run the REST watchdog on the poll's own replies, and only after success ([c4f65f2](https://github.com/geordiekorper/scythnet-navimow-ha/commit/c4f65f2eb90e076aad676723d0895944c08b07e2))
* run the watchdog's silence check on the event loop ([dcd4994](https://github.com/geordiekorper/scythnet-navimow-ha/commit/dcd4994523dd849c21d34a06d2fd928f8aa31b45))

## [1.2.0](https://github.com/geordiekorper/scythnet-navimow-ha/compare/NavimowHA-v1.1.0...NavimowHA-v1.2.0) (2026-09-18)


### Features

* atomic latest pose with receipt time on the position-X sensor ([e8c608b](https://github.com/geordiekorper/scythnet-navimow-ha/commit/e8c608b788b2e1c159478fab4a189483f29817b7))
* diagnostic data-source sensor with separate MQTT and REST snapshots ([d61a777](https://github.com/geordiekorper/scythnet-navimow-ha/commit/d61a777879ede56990b348e82e5359b3c6cdf489))
* **gate:** add keep-open override to prevent automated gate closing ([fc7a2fe](https://github.com/geordiekorper/scythnet-navimow-ha/commit/fc7a2fe0a3fe6aafc958ac734a26f090e009ace3))
* keep the full type-2 task report as mowing-zone attributes ([2c69edb](https://github.com/geordiekorper/scythnet-navimow-ha/commit/2c69edb3c5201981712957798fc6781c4888dd20))
* restore location sensors across restarts ([33dd4c8](https://github.com/geordiekorper/scythnet-navimow-ha/commit/33dd4c8b37aa613ad4d5f522c89e83b6de187450))
* **sensor:** mow_progress unit % and 0-100 scaling ([86c404e](https://github.com/geordiekorper/scythnet-navimow-ha/commit/86c404eea195897f1265dfa9891b7f0cd5184da0))
* smooth mower marker animation on map card ([a4bd2f4](https://github.com/geordiekorper/scythnet-navimow-ha/commit/a4bd2f48ed5087fc8a27552d2e936743b1463e0d))


### Bug Fixes

* handle assumed-open when gate stuck in Opening due to wind ([297b242](https://github.com/geordiekorper/scythnet-navimow-ha/commit/297b2425df3437eaabedf8e68012a70c7be87e22))
* import the SDK and coordinator at module level ([5d4b8ba](https://github.com/geordiekorper/scythnet-navimow-ha/commit/5d4b8baabe995a4444aa97316ffcec9c1cc9360c))
* prevent spurious gate open on backyard-only mow ([bcb4eb2](https://github.com/geordiekorper/scythnet-navimow-ha/commit/bcb4eb2b3394651d4c5165010d200c25f142a0de))
* stop long-term statistics for the position sensors ([d52361e](https://github.com/geordiekorper/scythnet-navimow-ha/commit/d52361ecac47069b63fd7cc4c82718b1306d08a7))
* unknown progress without a task report; keep delay on status-only entries ([7609893](https://github.com/geordiekorper/scythnet-navimow-ha/commit/7609893cba05842ee9d38d4e891f5782eb560d3b))
* use entity targets and shared handling for mower commands ([6f8bbe0](https://github.com/geordiekorper/scythnet-navimow-ha/commit/6f8bbe0d53da42ecda6311a45bf00c260a584980))
* zone display, mow_progress %, in_transit threshold, MQTT hook race ([1380b02](https://github.com/geordiekorper/scythnet-navimow-ha/commit/1380b02257c1489215df2335851e0cf851a3016f))

## Unreleased

- `sensor.<mower>_zone` now distinguishes the mower's empty target reports: `all`
  while it mows or pauses with no named zone (a "mow all" task), `none` when it
  reports no target and is not mowing, and `unknown` only until the first target
  report. Previously all three read `unknown`. `all` is inferred from the mower's
  activity, since the mower sends the same empty report in both cases; a charging
  break during a mow-all reads `none` until the mower resumes. Templates that
  compared the zone state to `unknown` should compare to `none` or `all`; the
  `partition_ids` attribute is unchanged, so the gate example needed no change
  beyond showing "All zones" in its friendly-name sensor.

## 1.2.0 in detail

The generated entry above lists the commits; this section describes what they mean for a user.

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
- Report `sensor.<mower>_mow_progress` as `unknown` until the mower sends a task
  report, instead of a manufactured 0 %. Fall back to the mower's `mowingPercentage`
  when route progress is missing, and name the field used in a `progress_source`
  attribute (`route`, `percentage` or `none`). A reported zero still shows 0 %.
- Keep the last `task_delay` value when a `type:4` entry arrives without `taskDelay`
  (the shape sent on MQTT reconnect); it used to clear the flag.
- Treat each `type:1` pose entry as one observation: X and Y must both be valid or
  the entry is ignored and the previous pose stays; a missing theta reads `null`
  instead of the previous heading; `vehicleState` and `time` come from the same entry.
  When one message carries several poses, the last valid one is kept.
- Expose the complete latest pose as attributes on `sensor.<mower>_position_x`:
  `y`, `theta_rad` (original precision), `vehicle_state`, `pose_time_ms`,
  `received_at` (Home Assistant's receipt time, UTC) and `source` (`mqtt_location`).
- **Breaking:** `vehicle_state` and `pose_time` are no longer attributes of
  `sensor.<mower>_zone`; read them from the position-X sensor. The zone sensor now
  changes only when the target zone or the delay flag changes, not on every pose.
- New diagnostic sensor `sensor.<mower>_data_source`: which source supplied the
  current mower state (`mqtt_push`, `mqtt_cache`, `http_fallback` or `none`), with the
  last MQTT state message and the last REST poll side by side as attributes
  (`mqtt_state`, `mqtt_raw_state`, `mqtt_battery`, `mqtt_timestamp`, `mqtt_received_at`,
  `rest_status`, `rest_vehicle_state`, `rest_battery`, `rest_battery_level`,
  `rest_timestamp`, `rest_polled_at`). Device timestamps are what the source sent and
  stay `null` when it sent none; Home Assistant's clock appears only in
  `mqtt_received_at` and `rest_polled_at`.
- Fix the source bookkeeping in the coordinator: the 30-second poll relabelled every
  update as `mqtt_cache`, and after an HTTP fallback the next poll reverted the mower
  state to the stale cached MQTT message. A cached message is now adopted once, and
  the state and its label change only when a newer message or a REST result arrives.
- Stop generating long-term statistics for `sensor.<mower>_position_x` and
  `_position_y` (their `state_class` is removed): an hourly mean of a coordinate means
  nothing, and the rows were never purged. Existing statistics stay until removed under
  Developer Tools → Statistics. History and the entity graph are unaffected.
- Restore the location sensors across restarts. The location cache lived only in
  memory, so after every restart `position_x`/`_y`, `heading`, `mowing_zone`,
  `mow_progress` and `zone` read `unknown` until their message type arrived again:
  up to five minutes for a docked pose, and not before the next mow for the task
  report. Each of these sensors now restores its last recorded state on startup and
  seeds the shared cache, so the derived sensors follow. A restored value carries
  `is_restored: true` (and its original `received_at`) until live data of the same
  kind replaces it, and a restored pose is never used to train the dock estimate.
- Import the SDK and the coordinator at module level instead of inside
  `async_setup_entry`. Home Assistant imports integration modules in an executor
  thread, so this removes the "detected blocking call" warnings the first SDK import
  produced on the event loop at every startup.
- SDK limits, for the record: the REST status endpoint returns only the device id,
  battery, `vehicleState` and `descriptiveCapacityRemaining`, all of which the SDK
  keeps, and no timestamp, so `rest_timestamp` is always `null`. For MQTT state
  messages the SDK keeps timestamp, normalized state, the raw vendor label, battery,
  signal strength, position and error; other keys are dropped before the integration
  sees them.

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
