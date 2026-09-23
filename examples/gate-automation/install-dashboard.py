#!/usr/bin/env python3
"""Install the Navimow map card and a ready-made dashboard into Home Assistant.

Everything goes through Home Assistant's WebSocket API with a long-lived access
token, so this works from any machine that can reach your instance:

  1. optionally copies navimow-map-card.js into <config>/www/   (--config-dir)
  2. registers, or version-bumps, the /local/navimow-map-card.js resource
  3. looks up your Navimow entity IDs in the entity registry (they are not
     predictable from the mower's name, so a static YAML cannot ship them)
  4. fills navimow-dashboard.json with those IDs and creates a dedicated
     storage-mode dashboard (default URL: /dashboard-navimow)

Python 3.9 or newer, standard library only.

  python3 install-dashboard.py --url http://homeassistant.local:8123 --token <TOKEN>
  python3 install-dashboard.py ... --config-dir /path/to/config   # also copies the JS
  python3 install-dashboard.py ... --print                        # show the config, change nothing
  python3 install-dashboard.py ... --overwrite                    # replace an existing dashboard
  python3 install-dashboard.py ... --scythnet-url http://scythnet.local:5055   # Scythnet's map in place of the map card

Create the token from your profile page -> Security -> Long-lived access tokens
(the account must be an administrator), or export it as HASS_TOKEN.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CARD_FILE = HERE / "navimow-map-card.js"
TEMPLATE_FILE = HERE / "navimow-dashboard.json"
CARD_URL_PATH = "/local/navimow-map-card.js"
CARD_TYPE = "custom:navimow-map-card"
DOMAIN = "navimow"
SENSOR_KEYS = (
    "position_x", "position_y", "heading", "zone", "mowing_zone", "battery",
    "dock_x", "dock_y", "mow_progress", "data_source",
)
# Map-card settings that cannot be discovered and must survive --overwrite.
CARD_KEEP_KEYS = (
    "overlay_image", "overlay_opacity", "calibration", "straighten",
    "dock_x", "dock_y", "trail_length", "history_hours", "dock_samples",
)


class HAError(Exception):
    """A WebSocket command was rejected by Home Assistant."""


class HAWebSocket:
    """Minimal RFC 6455 client, enough for Home Assistant's request/response API."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        u = urllib.parse.urlsplit(base_url)
        secure = u.scheme in ("https", "wss")
        host = u.hostname or "localhost"
        port = u.port or (443 if secure else 80)
        path = u.path.rstrip("/") + "/api/websocket"
        sock = socket.create_connection((host, port), timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if port in (80, 443) else f"{host}:{port}"
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during the WebSocket handshake")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        if " 101 " not in lines[0]:
            raise ConnectionError(f"WebSocket handshake failed: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if headers.get("sec-websocket-accept") != expect:
            raise ConnectionError("WebSocket handshake failed: bad Sec-WebSocket-Accept")
        self._sock = sock
        self._buf = rest
        self._next_id = 0

        hello = self.recv_json()
        if hello.get("type") != "auth_required":
            raise ConnectionError(f"unexpected first message: {hello}")
        self.send_json({"type": "auth", "access_token": token})
        auth = self.recv_json()
        if auth.get("type") != "auth_ok":
            raise HAError(f"authentication failed: {auth.get('message', auth)}")
        self.ha_version = auth.get("ha_version", "?")

    # --- framing -------------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("connection closed by Home Assistant")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            header = bytes((0x80 | opcode, 0x80 | n))
        elif n < 65536:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", n)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _recv_message(self) -> bytes:
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode, masked, n = b1 & 0x80, b1 & 0x0F, b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("Home Assistant closed the connection")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            message += payload
            if fin:
                return message

    # --- messages ------------------------------------------------------------
    def send_json(self, msg: dict) -> None:
        self._send_frame(0x1, json.dumps(msg).encode())

    def recv_json(self) -> dict:
        return json.loads(self._recv_message())

    def call(self, msg_type: str, **fields):
        """Send one command and return its result, raising HAError on failure."""
        self._next_id += 1
        msg_id = self._next_id
        self.send_json({"id": msg_id, "type": msg_type, **fields})
        while True:
            msg = self.recv_json()
            if msg.get("id") != msg_id or msg.get("type") != "result":
                continue
            if not msg.get("success"):
                err = msg.get("error") or {}
                raise HAError(f"{msg_type}: {err.get('code', '?')}: {err.get('message', '')}")
            return msg.get("result")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self._sock.close()


# --- discovery ---------------------------------------------------------------
def discover_mowers(ws: HAWebSocket) -> list[dict]:
    """One dict per Navimow device: name, serial, device_id, entities{key: entity_id}."""
    mowers: dict[str, dict] = {}
    for dev in ws.call("config/device_registry/list"):
        serial = next((i[1] for i in dev.get("identifiers", []) if i and i[0] == DOMAIN), None)
        if serial is None:
            continue
        mowers[dev["id"]] = {
            "name": dev.get("name_by_user") or dev.get("name") or "Navimow",
            "serial": serial,
            "device_id": dev["id"],
            "entities": {},
        }
    for ent in ws.call("config/entity_registry/list"):
        if ent.get("platform") != DOMAIN or ent.get("disabled_by"):
            continue
        mower = mowers.get(ent.get("device_id"))
        if mower is None:
            continue
        uid, prefix = ent.get("unique_id") or "", f"{DOMAIN}_{mower['serial']}"
        if uid == prefix and ent["entity_id"].startswith("lawn_mower."):
            mower["entities"]["mower"] = ent["entity_id"]
        elif uid.startswith(prefix + "_") and uid[len(prefix) + 1:] in SENSOR_KEYS:
            mower["entities"][uid[len(prefix) + 1:]] = ent["entity_id"]
    return list(mowers.values())


def pick_mower(mowers: list[dict], wanted: str | None) -> dict:
    if not mowers:
        raise SystemExit("No Navimow device found. Is the integration set up and loaded?")
    if wanted:
        w = wanted.lower()
        for m in mowers:
            if w in (m["name"].lower(), m["serial"].lower(), m["device_id"].lower()):
                return m
        raise SystemExit(f"No Navimow device matches {wanted!r}. Known: " + ", ".join(m["name"] for m in mowers))
    if len(mowers) > 1:
        raise SystemExit(
            "Several Navimow devices found; choose one with --mower NAME (and give each its own --url-path):\n  "
            + "\n  ".join(f"{m['name']}  (serial {m['serial']})" for m in mowers)
        )
    return mowers[0]


# --- template ----------------------------------------------------------------
_MISSING = object()
_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")


def fill(node, values: dict):
    """Substitute ${key} placeholders. A string that resolves to a missing value is
    dropped; if it was an 'entity' or 'url' field the whole card/row is dropped."""
    if isinstance(node, str):
        whole = _PLACEHOLDER.fullmatch(node)
        if whole:
            v = values.get(whole.group(1))
            return _MISSING if v is None else v
        try:
            return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), node)
        except KeyError:
            return _MISSING
    if isinstance(node, list):
        return [x for x in (fill(v, values) for v in node) if x is not _MISSING]
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            fv = fill(v, values)
            if fv is _MISSING:
                if k in ("entity", "url"):
                    return _MISSING
                continue
            out[k] = fv
        return out
    return node


def prune_empty(node):
    """Tidy what `fill` left behind: drop an entities-card `section` row with no row
    under it before the next section, and a card whose `entities` list ended up empty."""
    if isinstance(node, dict):
        return {k: prune_empty(v) for k, v in node.items()}
    if not isinstance(node, list):
        return node
    items = [prune_empty(v) for v in node]
    items = [v for v in items if not (isinstance(v, dict) and v.get("entities") == [])]

    def is_section(v) -> bool:
        return isinstance(v, dict) and v.get("type") == "section"

    return [v for i, v in enumerate(items)
            if not (is_section(v) and (i + 1 == len(items) or is_section(items[i + 1])))]


def find_map_cards(node, out: list) -> list:
    if isinstance(node, dict):
        if node.get("type") == CARD_TYPE:
            out.append(node)
        for v in node.values():
            find_map_cards(v, out)
    elif isinstance(node, list):
        for v in node:
            find_map_cards(v, out)
    return out


def drop_cards(node, card_type: str):
    """The config without the cards of `card_type`, wherever they sit."""
    if isinstance(node, dict):
        return {k: drop_cards(v, card_type) for k, v in node.items()}
    if isinstance(node, list):
        return [drop_cards(v, card_type) for v in node if not (isinstance(v, dict) and v.get("type") == card_type)]
    return node


# --- steps -------------------------------------------------------------------
def copy_card(config_dir: Path) -> None:
    www = config_dir / "www"
    fresh = not www.is_dir()
    www.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CARD_FILE, www / CARD_FILE.name)
    print(f"Copied {CARD_FILE.name} -> {www / CARD_FILE.name}")
    if fresh:
        print("  NOTE: www/ did not exist before. Home Assistant only starts serving /local/ after a restart.")


def fetch_served_card(base_url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(base_url + CARD_URL_PATH, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def ensure_resource(ws: HAWebSocket, base_url: str) -> None:
    served = fetch_served_card(base_url)
    if served is None:
        raise SystemExit(
            f"{base_url}{CARD_URL_PATH} is not served (404). Copy {CARD_FILE.name} to <config>/www/ "
            "(or re-run with --config-dir), restart Home Assistant if www/ is new, then run again."
        )
    local = CARD_FILE.read_bytes() if CARD_FILE.is_file() else None
    if local is not None and local != served:
        print("  WARNING: the card served by Home Assistant differs from the copy next to this script.")
    want = f"{CARD_URL_PATH}?v={hashlib.sha1(served).hexdigest()[:8]}"

    mode = (ws.call("lovelace/info") or {}).get("resource_mode")
    if mode != "storage":
        print(f"Lovelace resources are in {mode!r} mode; add this to configuration.yaml yourself:\n"
              f"  lovelace:\n    resources:\n      - url: {want}\n        type: module")
        return
    existing = [r for r in ws.call("lovelace/resources") if r.get("url", "").split("?")[0] == CARD_URL_PATH]
    if not existing:
        ws.call("lovelace/resources/create", res_type="module", url=want)
        print(f"Registered resource {want}")
    elif existing[0].get("url") != want or existing[0].get("type") != "module":
        ws.call("lovelace/resources/update", resource_id=existing[0]["id"], res_type="module", url=want)
        print(f"Updated resource {existing[0].get('url')} -> {want}")
    else:
        print(f"Resource {want} already registered")
    if len(existing) > 1:
        print("  NOTE: more than one resource points at the card; remove the extras under Settings -> Dashboards -> Resources.")


def install_dashboard(ws: HAWebSocket, config: dict, url_path: str, title: str, overwrite: bool) -> None:
    existing = next((d for d in ws.call("lovelace/dashboards/list") if d.get("url_path") == url_path), None)
    if existing is None:
        ws.call("lovelace/dashboards/create", url_path=url_path, title=title,
                icon="mdi:robot-mower", show_in_sidebar=True, require_admin=False)
        print(f"Created dashboard '{title}' at /{url_path}")
    else:
        if existing.get("mode") != "storage":
            raise SystemExit(f"/{url_path} is a YAML-mode dashboard; this script only writes storage-mode dashboards.")
        if not overwrite:
            raise SystemExit(f"Dashboard /{url_path} already exists. Re-run with --overwrite to replace its "
                             "contents (map-card overlay/calibration settings are carried over), or pick another --url-path.")
        try:
            old = ws.call("lovelace/config", url_path=url_path) or {}
        except HAError:
            old = {}
        old_cards, new_cards = find_map_cards(old, []), find_map_cards(config, [])
        kept = [k for k in CARD_KEEP_KEYS if old_cards and k in old_cards[0]]
        for card in new_cards:
            for k in kept:
                card[k] = old_cards[0][k]
        if kept:
            print("Carried over map-card settings: " + ", ".join(kept))
    ws.call("lovelace/config/save", url_path=url_path, config=config)
    print(f"Saved dashboard config for /{url_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--url", default="http://homeassistant.local:8123", help="Home Assistant base URL")
    ap.add_argument("--token", default=os.environ.get("HASS_TOKEN"),
                    help="long-lived access token of an admin user (or set HASS_TOKEN)")
    ap.add_argument("--config-dir", type=Path, help="Home Assistant config directory; copies the card JS into its www/")
    ap.add_argument("--mower", help="device name or serial when more than one Navimow is set up")
    ap.add_argument("--url-path", default="dashboard-navimow", help="dashboard URL path (must contain a hyphen)")
    ap.add_argument("--title", help="dashboard title (default: the mower's device name)")
    ap.add_argument("--overwrite", action="store_true", help="replace the dashboard if it already exists")
    ap.add_argument("--scythnet-url", help="Scythnet's address as the dashboard's browser reaches it, e.g. "
                    "http://scythnet.local:5055: its card view, as a Webpage card, takes the place of the position map card")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print the filled dashboard config as JSON and change nothing")
    args = ap.parse_args()

    if not args.token:
        ap.error("--token (or HASS_TOKEN) is required")
    if "-" not in args.url_path:
        ap.error("--url-path must contain a hyphen, e.g. dashboard-navimow")
    base_url = args.url.rstrip("/")
    if not TEMPLATE_FILE.is_file():
        raise SystemExit(f"template not found: {TEMPLATE_FILE}")

    if args.config_dir and args.print_only:
        print(f"--print changes nothing: {CARD_FILE.name} is not copied to --config-dir.")
    elif args.config_dir and args.scythnet_url:
        print(f"--scythnet-url replaces the map card: {CARD_FILE.name} is not copied to --config-dir.")
    elif args.config_dir:
        if not CARD_FILE.is_file():
            raise SystemExit(f"card file not found next to this script: {CARD_FILE}")
        copy_card(args.config_dir)

    ws = HAWebSocket(base_url, args.token)
    try:
        print(f"Connected to Home Assistant {ws.ha_version} at {base_url}")
        mower = pick_mower(discover_mowers(ws), args.mower)
        values = dict(mower["entities"], name=mower["name"], title=args.title or mower["name"])
        if args.scythnet_url:
            serial = urllib.parse.quote(mower["serial"], safe="")
            values["scythnet_card"] = f"{args.scythnet_url.rstrip('/')}/?view=card&device={serial}"
        print(f"Mower: {mower['name']} (serial {mower['serial']})")
        for key in ("mower",) + SENSOR_KEYS:
            print(f"  {key:13s} {values.get(key) or '(not found, left out)'}")
        for key in ("mower", "position_x", "position_y"):
            if key not in values:
                raise SystemExit(f"required entity '{key}' not found; is the integration loaded and up to date?")

        config = prune_empty(fill(json.loads(TEMPLATE_FILE.read_text()), values))
        if args.scythnet_url:  # Scythnet's map in place of the map card, which then needs no resource
            config = drop_cards(config, CARD_TYPE)
        if args.print_only:
            print(json.dumps(config, indent=2))
            return 0
        if not args.scythnet_url:
            ensure_resource(ws, base_url)
        install_dashboard(ws, config, args.url_path, values["title"], args.overwrite)
        print(f"\nDone. Open {base_url}/{args.url_path} (reload the browser once so it picks up the card resource).")
        if args.scythnet_url:
            print(f"Scythnet card: put {base_url} (and every other address this dashboard is opened by) in "
                  "SCYTHNET_FRAME_ANCESTORS on the Scythnet side and restart it, or the frame stays blank.")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HAError, ConnectionError, OSError) as e:
        sys.exit(f"error: {e}")
