# Navimow auto-mower gate — setup guide

Automatically open a gate so a Segway Navimow can pass through to mow zones on
the far side of the gate from its dock, and close it again once the mower is
docked. Built around four pieces:

1. The **position/zone fork** of the Navimow integration (provides the zone sensor).
2. A **switch** in Home Assistant that *pulses* your gate opener (LocalTuya, ESPHome, Shelly, etc.).
3. Two **contact sensors** for the gate's fully-open and fully-closed positions.
4. This **package** (`navimow_gate.yaml`) + an optional dashboard card and live map card.

> The gate logic assumes a **single-button cyclic** opener (one pulse =
> open→stop→close→reverse) and reads the *true* gate state from the two contact
> sensors. If your opener has separate open/close inputs you can still use this,
> but the pulse-and-verify scripts are written for the single-button case.

---

## Prerequisites

- **Home Assistant 2026.1.0+** with the Navimow integration installed from the
  position/zone fork: <https://github.com/pgoutsos/NavimowHA>. After setup you'll
  have, per mower: `lawn_mower.<name>`, `sensor.<name>_zone`,
  `sensor.<name>_position_x/_y`, `sensor.<name>_heading`, and (v1.1.0+position.4)
  `sensor.<name>_dock_x/_dock_y` — the dock position, auto-learned by averaging
  the mower's pose while docked/charging and persisted across HA restarts. The
  map card uses these to place the dock marker; before the first docking event
  they're `unknown` and the card falls back to local learning / the origin.
- A **gate opener controllable from HA as a switch**. Any integration works as
  long as turning the switch *on* pulses the opener. (LocalTuya, ESPHome, Shelly…)
- **Two contact sensors**, one mounted so it reads at the gate's fully-**closed**
  position and one at the fully-**open** position. Local/fast sensors (Z-Wave,
  Zigbee, ESPHome) are strongly preferred over cloud ones for timing.

---

## Step 1 — Identify your entity IDs

In Developer Tools → States, find and note:

| Purpose | Example | Yours |
| --- | --- | --- |
| Mower | `lawn_mower.peter_griffin` | |
| Zone sensor (from the fork) | `sensor.peter_griffin_zone` | |
| Gate opener switch (pulse) | `switch.garden_gate_opener_switch_1` | |
| Gate **closed** contact | `binary_sensor.garden_gate_closed_sensor` | |
| Gate **open** contact | `binary_sensor.garden_gate_open_sensor` | |

## Step 2 — Verify sensor polarity

Move the gate to each position and read the two contacts. The package assumes:

- fully **closed** → `closed_sensor == off`
- fully **open** → `open_sensor == off`
- **mid-travel** → both `on`

Contact sensors can be wired either way, so confirm this with the actual gate (not
by hand-sliding a magnet). If yours are inverted, flip the `off`/`on` checks in the
`garden_gate_status` template and the two scripts.

## Step 3 — Find your zone ids

Each mowing zone has a numeric partition id. Watch `sensor.<name>_zone` while the
mower works each area (or send a "mow this zone" command per zone) and record which
id is which. Note which zone(s) are on the **dock side** (no gate needed) — usually
just the one containing the dock.

> **"Mow all" note:** a full-property / "mow all" command reports **no zone**. Only
> per-zone commands report a specific id. The sensor shows `all` while the mower
> mows or pauses without a named zone, `none` when it is idle or returning, and
> `unknown` only until the first target report arrives. The gate logic reads the
> `partition_ids` attribute and treats anything that isn't a known dock-side zone
> (including an empty list) as gate-required, so "mow all" opens the gate too.

## Step 4 — Configure the package

Open `navimow_gate.yaml` and:

1. **Find-and-replace** the five example entity IDs (every spot is also tagged
   `# CHANGE:`) with yours:
   - `lawn_mower.peter_griffin`
   - `sensor.peter_griffin_zone`
   - `switch.garden_gate_opener_switch_1`
   - `binary_sensor.garden_gate_closed_sensor`
   - `binary_sensor.garden_gate_open_sensor`
2. **Set your zones** in the two tables tagged `# EDIT ZONE MAP`:
   - the id → friendly-name map (display only), and
   - `no_gate_zones` — the **dock-side** zone ids that do NOT need the gate,
     e.g. `['2']`. Everything else (front zones, and "mow all" / `unknown`) is
     treated as gate-required.

## Step 5 — Install the package

> Throughout this guide `<config>` means your **Home Assistant configuration
> directory** — the folder that holds `configuration.yaml` (and `custom_components/`,
> `packages/`, `www/`). It's `/config` on HA OS/Supervised and inside a Docker
> container; on a Core/venv install it's usually `~/.homeassistant`. For Docker,
> reach it with `docker cp <file> homeassistant:/config/...`.

Put the file at `<config>/packages/navimow_gate.yaml`, and make sure
`configuration.yaml` loads packages:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Then Developer Tools → **Check Configuration**, restart Home Assistant, and turn on
`input_boolean.navimow_gate_automation`.

## Step 6 — Test

With the gate area clear (watch on a camera if you have one):

1. Developer Tools → Actions → run `script.navimow_gate_ensure_open`, then
   `script.navimow_gate_ensure_closed`. Each should pulse and the matching contact
   should confirm the endpoint.
2. Then a live mow: when the mower targets a gate-side zone, the gate opens; it
   closes once the mower docks.

`input_boolean.navimow_gate_automation` is your kill-switch — off disables all the
automations while leaving the manual scripts/switch usable.

---

## What gets created

- `input_boolean.navimow_gate_automation` — master enable
- `input_boolean.navimow_gate_manual_open` — override to prevent automated gate closing (both close-on-dock and close-retry respect this; auto-clears when a new mow starts)
- `sensor.navimow_current_zone` — current target zone, friendly name
- `sensor.garden_gate_status` — Closed / Open / Opening / Closing
- `binary_sensor.navimow_gate_zone_required` — on when the target needs the gate
- `binary_sensor.navimow_charging`, `binary_sensor.navimow_task_delayed`
- `sensor.navimow_mowing_zone` — live physical zone (from `type:2`; works for "mow all")
- `sensor.navimow_mow_route_progress` — route progress % (reaches 100 just before the physical finish)
- scripts `navimow_gate_ensure_open` / `_ensure_closed`
- three automations (open on gate-side zone, ensure-open while returning, close on dock)

## Optional — dashboard card

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Garden Gate
    show_header_toggle: false
    entities:
      - { entity: sensor.garden_gate_status, name: Gate }
      - { entity: input_boolean.navimow_gate_automation, name: Automation enabled }
      - { entity: input_boolean.navimow_gate_manual_open, name: Prevent automation from closing gate }
      - { entity: sensor.navimow_current_zone, name: Mower target zone }
      - { entity: binary_sensor.navimow_charging, name: Charging }
      - { entity: binary_sensor.navimow_task_delayed, name: Mow delayed }
      - { entity: sensor.navimow_mowing_zone, name: Mowing zone }
      - { entity: sensor.navimow_mow_route_progress, name: Mow progress (route) }
  - type: horizontal-stack
    cards:
      - type: button
        name: Open Gate
        icon: mdi:gate-open
        tap_action: { action: perform-action, perform_action: script.navimow_gate_ensure_open, confirmation: { text: Open the garden gate? } }
      - type: button
        name: Close Gate
        icon: mdi:gate
        tap_action: { action: perform-action, perform_action: script.navimow_gate_ensure_closed, confirmation: { text: Close the garden gate? } }
```

## Optional — live position map card

`navimow-map-card.js` plots the mower's live position, heading, and the path of
the **current mowing session** — optionally over a satellite image of your
property.

The session path is rebuilt from Home Assistant's recorder each time the card
loads (it finds the most recent docked → mowing transition and replays the
position history since), so it survives page reloads, navigating away, and
shows the same path on every device. It resets automatically when a new session
starts. Requires the recorder (on by default) to be recording the position
sensors; if it isn't, the card falls back to a live-only trail.

1. Copy it to `<config>/www/navimow-map-card.js`.
2. Settings → Dashboards → ⋮ → Resources → add `/local/navimow-map-card.js?v=1`
   as a **JavaScript Module** (the `?v=N` query dodges the frontend cache — bump it
   when you update the file).
3. Add a card: `type: custom:navimow-map-card` (override `x_entity`, `y_entity`,
   `heading_entity`, `zone_entity` if your entity IDs differ from the defaults).

### Satellite / aerial overlay

The card can draw the mower on top of an image of your property. You need the
image plus **two calibration points** (spots whose mower-meter coordinates AND
image-pixel coordinates you know — they determine scale, rotation, and offset,
so the image doesn't need to be north-up):

1. Take a satellite screenshot of your property (Google Maps, county GIS, or a
   drone photo), crop it, and save it to `<config>/www/yard.png`.
2. **Point 1 — the dock.** Its meter coordinates are the `dock_x`/`dock_y`
   sensors; find the dock in the image and note its pixel x/y (most image
   viewers show pixel coordinates; macOS Preview: Tools → Show Inspector).
3. **Point 2 — any landmark** as far from the dock as practical. Park the mower
   on it (or watch live during a mow) and read `position_x`/`position_y`, then
   note the same spot's pixel coordinates in the image.
4. Configure the card:

   ```yaml
   type: custom:navimow-map-card
   overlay_image: /local/yard.png
   overlay_opacity: 0.9
   calibration:
     - m: [0.0, 0.0]        # dock: [dock_x, dock_y] in meters
       px: [512, 800]       # dock: pixel [x, y] in the image
     - m: [12.4, -3.1]      # landmark: [position_x, position_y]
       px: [220, 410]       # landmark: pixel [x, y]
   ```

The view auto-fits to the whole image, which is drawn upright — the mower's
trail and heading are rotated into the image's frame (the mower's RTK
coordinate frame is usually not north-aligned). Set `straighten: false` to
draw in the mower's frame instead (tilted image). If the overlay looks
mirrored or rotated wrong, a pixel coordinate was probably read y-up — pixel
y counts DOWN from the image's top-left.

### Dock marker

The mower's coordinate origin is its RTK/mapping anchor, which is *near* but not
always *at* the dock — so the card no longer assumes dock = (0,0). The dock
marker position is resolved in this order:

1. **Manual override** — set `dock_x:` / `dock_y:` (meters) in the card config.
2. **Integration dock sensors** (fork v1.1.0+position.4+) —
   `sensor.<name>_dock_x/_dock_y`, learned server-side by averaging the mower's
   pose while it reports docked/charging, persisted across HA restarts. The card
   derives these entity names from `x_entity`/`y_entity` automatically; set
   `dock_x_entity:` / `dock_y_entity:` if yours differ.
3. **Local fallback** (older fork versions) — the card learns the same average
   in the browser and keeps it in localStorage (per browser, so each device
   learns separately).
4. **Origin (0,0)** until the mower has docked once.

The sensors read `unknown` until the first docked/charging pose sample after
upgrading; the marker corrects itself the next time the mower docks. The
sensors' `samples` / `source` attributes show whether live learning is working —
if `samples` stays 0 while docked, your mower stops streaming pose at the dock
(please report this).

---

## Notes & tuning

- **Open/close timeout** is 45s in the scripts (`timeout: "00:00:45"`); raise it if
  your gate travels slowly.
- **Close is immediate** on dock. If your mower briefly docks mid-job to recharge,
  the gate closes and re-opens when it heads back out — add a `for:` to the close
  triggers if you'd rather wait.
- **Cancelled tasks** set the zone to `unknown`; the "ensure open while returning"
  automation guarantees the mower can always cross back home.
