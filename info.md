# Navimow for Home Assistant

Monitor and control Navimow robotic mowers in Home Assistant.

> **Alpha software.** Entity ids, attributes, actions and options can change
> between releases without a migration path. Releases are GitHub pre-releases;
> turn on **Show beta versions** for this repository in HACS to see them.

## Features

- **Mower control**: Start, pause, dock, and explicit resume/stop actions
- **Live position and zones**: position, heading, target and physical zone, task progress, learned dock position, and a diagnostic data-source sensor (see README)
- **Device monitoring**: Real-time state, battery level sensor, dashboards
- **Real-time communication**: MQTT-based, fast state updates
- **Native integration**: `lawn_mower` entity, full automation support

## Prerequisites

- Home Assistant **2026.1.0** or newer
- Navimow account that can sign in to the official app (used for authorization)

## Installation

1. HACS → Integrations → menu → **Custom repositories**
2. Add: `https://github.com/geordiekorper/scythnet-navimow-ha`, Category: **Integration**
3. Search **Navimow** in HACS, turn on **Show beta versions** in the download dialog and install the newest alpha. Releases of this fork are pre-releases, which HACS hides while that switch is off.
4. Restart Home Assistant
5. Settings → Devices & Services → Add Integration → search **Navimow**

## Documentation

Full documentation and troubleshooting: [README](https://github.com/geordiekorper/scythnet-navimow-ha) · [Getting Started](https://github.com/segwaynavimow/NavimowHA/wiki/Getting-Started) · [Issues](https://github.com/geordiekorper/scythnet-navimow-ha/issues)

---

*This integration is under active development. More features are being added over time.*
