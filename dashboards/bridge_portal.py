#!/usr/bin/env python3
"""
Bridge Portal - the one human-facing login for Covenant Health's facility
systems. Today that's the iPRO camera bridge (ipro2.py, port 5002), the
Ooma AirDial bridge (ooma.py, port 5003), and the Hyperview facility-health
dashboard (matrix.py, port 5001); SYSTEMS below is where a future system's
maintenance/events wiring gets added, and /overview, /ipro-, /ooma- and
/hyperview-style dashboard routes are the pattern a new one would follow.

For iPRO and Ooma specifically: this replaces each bridge's own
/maintenance and /events-search pages, which were retired (see their
BRIDGE_PORTAL_URL / portal_url config) and now just redirect here. Their
underlying JSON/CSV feeds and write-protected POST routes are UNCHANGED and
still require each bridge's own maintenance_auth secret - this portal
authenticates the HUMAN, then calls those routes server-to-server using
that secret, same as an operator's browser used to. Hyperview never had
its own login (its JSON feeds are unauthenticated - see HYPERVIEW_BASE_URL
below); the portal just puts a login in front of its dashboard page,
nothing server-to-server to forward there.

Four accounts, per-system scoped:
    chadmin  - ipro, ooma, and hyperview (same password as before: 3Xce11@nce)
    security - ipro only
    network  - ooma only
Passwords are salted+hashed (PBKDF2-HMAC-SHA256, 200k iterations) rather
than compared as plaintext - see USERS below. HTTP Basic auth is still the
transport (matches every other login on these bridges, works natively with
both a browser prompt and curl/API callers), it's just no longer a single
shared secret string - each request's username/password is verified
against that user's own stored hash.

Run:
    python bridge_portal.py

Config (env vars, all optional - defaults assume everything runs on one
host):
    IPRO_BASE_URL          default http://localhost:5002
    OOMA_BASE_URL          default http://localhost:5003
    HYPERVIEW_BASE_URL     default http://localhost:5001
    IPRO_MAINTENANCE_AUTH  default matches ipro2.py's own default
    OOMA_MAINTENANCE_AUTH  default matches ooma.py's own default
    PORTAL_PORT            default 5004
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote, urlencode

import requests
from flask import Flask, Response, request, redirect
from waitress import serve

app = Flask(__name__)

REQUEST_TIMEOUT = 15

SYSTEMS = {
    "ipro": {
        "label": "iPRO Cameras",
        "base_url": os.environ.get("IPRO_BASE_URL", "http://localhost:5002"),
        "maintenance_auth": os.environ.get("IPRO_MAINTENANCE_AUTH", "Basic Y2hhZG1pbjozWGNlMTFAbmNl"),
        "target_types": [("camera", "Single camera"), ("server", "Entire server")],
        "windows_route": "/maintenance-windows",  # one row per real window, has 'key'
        "device_id_param": "device_id",  # /events query param this backend filters on
    },
    "ooma": {
        "label": "Ooma AirDial",
        "base_url": os.environ.get("OOMA_BASE_URL", "http://localhost:5003"),
        "maintenance_auth": os.environ.get("OOMA_MAINTENANCE_AUTH", "Basic Y2hhZG1pbjozWGNlMTFAbmNl"),
        "target_types": [("device", "Single device"), ("account", "Entire account")],
        "windows_route": "/maintenance-log",  # already 1:1 per window, has 'key'
        "device_id_param": "myx_id",  # ooma.py's /events route names it myx_id, not device_id
    },
}

# Hyperview (matrix.py, port 5001) isn't a SYSTEMS entry above - it has no
# maintenance windows or device event history, just live alarm state, so
# none of the maintenance/events machinery above applies to it. Its own
# JSON feeds are entirely unauthenticated already (see matrix.py), so the
# portal's /hyperview/api/* proxy stays open too, same "read-only feeds
# don't need a login" convention the bridges themselves use - only the
# human-facing /hyperview dashboard page requires one.
HYPERVIEW_BASE_URL = os.environ.get("HYPERVIEW_BASE_URL", "http://localhost:5001")

# Whitelisted, not a raw path passthrough - proxying an arbitrary
# caller-supplied path to an internal service is an SSRF footgun even
# when that service is itself unauthenticated.
HYPERVIEW_ENDPOINTS = {
    "overall-health", "category-summary", "location-health-matrix",
    "clinic-health-matrix", "active-alarm-log", "sites-affected", "last-updated",
}

# --- iPRO / Ooma dashboard data -------------------------------------------
# ipro2.py and ooma.py don't yet expose a health-summary endpoint the way
# matrix.py does (see HYPERVIEW_ENDPOINTS above) - there's no live
# /overall-health or /location-health-matrix on either bridge yet. These
# two constants are the fallback _fetch_dashboard_snapshot() below uses
# whenever GET {base_url}/health-summary fails, which today is always
# (neither bridge listens on that route). They also double as the
# CONTRACT for that future endpoint: shape a real /health-summary
# response exactly like the dict below and it starts rendering live with
# no changes to _fetch_dashboard_snapshot, the HTML builders, or the
# routes that call them - only these two dicts become dead weight, safe
# to delete once the fallback path never fires.
IPRO_DASHBOARD = {
    "gauge": {"score": 94, "state": "Needs Attention", "tone": "warn"},
    "totals": {"total_cameras": 429, "unhealthy_cameras": 26},
    "summary": [
        {"label": "Offline Cams", "open": 15, "ack": 0},
        {"label": "Degraded Cams", "open": 1, "ack": 0},
        {"label": "Infrastructure Issues", "open": 1, "ack": 0},
    ],
    "hospitals": [
        {"site": "Cumberland", "infra": 0, "degraded": 0, "offline": 10},
        {"site": "LeConte", "infra": 0, "degraded": 1, "offline": 4},
        {"site": "Peninsula", "infra": 0, "degraded": 0, "offline": 1},
        {"site": "Centerpoint", "infra": 0, "degraded": 0, "offline": 0},
        {"site": "Claiborne", "infra": None, "degraded": None, "offline": None},
        {"site": "Covenant West", "infra": None, "degraded": None, "offline": None},
        {"site": "Fort Hill", "infra": None, "degraded": None, "offline": None},
        {"site": "Fort Loudoun", "infra": 0, "degraded": 0, "offline": 0},
        {"site": "Fort Sanders Regional", "infra": None, "degraded": None, "offline": None},
        {"site": "Methodist", "infra": None, "degraded": None, "offline": None},
        {"site": "Morristown-Hamblen", "infra": None, "degraded": None, "offline": None},
        {"site": "Parkwest", "infra": None, "degraded": None, "offline": None},
        {"site": "Roane", "infra": None, "degraded": None, "offline": None},
    ],
    "clinics": [
        {"site": "Covenant HomeCare"}, {"site": "Crossville Medical"},
        {"site": "Morristown West"}, {"site": "Peninsula Lighthouse"},
        {"site": "Southern Medical"}, {"site": "Thompson Proton"},
    ],
    "devices": [
        {"device": "Centerpoint", "status": "Infrastructure Issue",
         "detail": "Correlated/Segment Event: 0 cameras went down together within "
                   "5.0 min — likely shared infrastructure (switch/PoE), not independent faults",
         "duration": "6m", "priority": "Critical"},
        {"device": "LCMC | 1st Floor | ED West | Med Cart .38", "status": "Offline",
         "detail": "down 30+ min; network OK, video issue", "duration": "1d 17h", "priority": "Critical"},
        {"device": "LCMC | 2nd Floor | CV Stepdown Med Cart .34", "status": "Offline",
         "detail": "down 30+ min; network OK, video issue", "duration": "1d 17h", "priority": "Critical"},
        {"device": "LCMC | 1st Floor | Pharmacy .89", "status": "Offline",
         "detail": "down 30+ min; no ping data", "duration": "13d 18h", "priority": "Critical"},
        {"device": "LCMC | 1st Floor | Chest Pain Med Cart .39", "status": "Offline",
         "detail": "down 30+ min; network OK, video issue", "duration": "1d 17h", "priority": "Critical"},
        {"device": "PENH | Kids Unit | ISO .43", "status": "Offline",
         "detail": "down 30+ min; no ping data", "duration": "13d 18h", "priority": "Critical"},
    ],
}

OOMA_DASHBOARD = {
    "gauge": {"score": 91, "state": "Action Required", "tone": "warn"},
    "summary": [
        {"label": "Connectivity", "open": 7, "ack": 1},
        {"label": "Battery", "open": 0, "ack": 0},
    ],
    "sites": [
        {"site": "Covenant IT", "connectivity": "live", "battery": 0},
        {"site": "Fort Sanders Regional Medical Center", "connectivity": 4, "battery": 0},
        {"site": "Methodist Medical Center", "connectivity": 2, "battery": 0},
        {"site": "Parkwest Medical Center", "connectivity": 1, "battery": 0},
        {"site": "Centerpoint", "connectivity": 0, "battery": 0},
        {"site": "Claiborne Medical Center", "connectivity": 0, "battery": 0},
        {"site": "Covenant Health Roane", "connectivity": 0, "battery": 0},
        {"site": "Cumberland Medical Center", "connectivity": 0, "battery": 0},
        {"site": "Fort Loudoun Medical Center", "connectivity": 0, "battery": 0},
        {"site": "LeConte Medical Center", "connectivity": 0, "battery": 0},
        {"site": "Morristown-Hamblen Healthcare System", "connectivity": 0, "battery": 0},
        {"site": "Peninsula Hospital", "connectivity": 0, "battery": 0},
    ],
    "alarms": [
        {"location": "Fort Sanders Regional Medical Center", "device": "Fort Sanders AD1",
         "severity": "Degraded", "category": "Connectivity", "detail": "LTE carrier temporarily disconnected"},
        {"location": "Fort Sanders Regional Medical Center", "device": "Fort Sanders AD10",
         "severity": "Degraded", "category": "Connectivity", "detail": "LTE carrier temporarily disconnected"},
        {"location": "Fort Sanders Regional Medical Center", "device": "Fort Sanders AD14",
         "severity": "Degraded", "category": "Connectivity", "detail": "LTE carrier temporarily disconnected"},
        {"location": "Fort Sanders Regional Medical Center", "device": "Fort Sanders AD9",
         "severity": "Degraded", "category": "Connectivity", "detail": "LTE carrier temporarily disconnected"},
        {"location": "Methodist Medical Center", "device": "Methodist Medical AD4",
         "severity": "Degraded", "category": "Connectivity",
         "detail": "LTE unstable — flapping (3+ changes in the last 30 min)"},
        {"location": "Methodist Medical Center", "device": "Methodist Medical AD5",
         "severity": "Degraded", "category": "Connectivity",
         "detail": "Running on cellular backup — primary WAN is down"},
        {"location": "Parkwest Medical Center", "device": "Parkwest Medical AD11",
         "severity": "Degraded", "category": "Connectivity",
         "detail": "LTE unstable — flapping (3+ changes in the last 30 min)"},
    ],
}


def _fetch_dashboard_snapshot(system, fallback):
    """Tries {base_url}/health-summary - the live endpoint /ipro and
    /ooma would use once ipro2.py/ooma.py grow one, shaped like
    IPRO_DASHBOARD/OOMA_DASHBOARD above (see the comment there). Same
    fail-soft shape as _fetch_windows/_fetch_options: any connection
    failure, non-2xx, or response that isn't valid JSON just falls back
    to the fixed snapshot rather than breaking the page - which is what
    happens on every call today, since that route doesn't exist yet."""
    cfg = SYSTEMS[system]
    try:
        resp = requests.get(f"{cfg['base_url']}/health-summary", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return fallback


# --- users -------------------------------------------------------------
# PBKDF2-HMAC-SHA256, 200k iterations, per-user random salt - generated
# once (see the delivery notes for how) and hardcoded here the same way
# the OLD shared MAINTENANCE_AUTH secret was hardcoded in both bridges -
# override any of them via env vars below if you want to rotate a
# password without editing this file.
PBKDF2_ITERATIONS = 200_000

USERS = {
    "chadmin": {
        "salt": bytes.fromhex("475fd83c0a46bd511ff1868ce520f356"),
        "hash": bytes.fromhex("27ae64ae94bb51e859791a08826769ecec0c1f4612f3109f55d5b5af3c559fc9"),
        # "hyperview" isn't a SYSTEMS key (see HYPERVIEW_BASE_URL above) -
        # it's just a membership check against this same set, gating the
        # /hyperview dashboard page instead of a maintenance/events system.
        "systems": {"ipro", "ooma", "hyperview"},
    },
    "security": {
        "salt": bytes.fromhex("97ee6f4495209254060d21bfb84b3420"),
        "hash": bytes.fromhex("115d5cef28e66142631ed1df71a5fdc67dbb51474621145b180c56bb388d3cd5"),
        "systems": {"ipro"},
    },
    "network": {
        "salt": bytes.fromhex("7a75fa1147c7adf2d614e17d746affc2"),
        "hash": bytes.fromhex("dee5a6f2f453ae67417dadb7a88db04ab1a40c2fa826b303d6494c339c4bf5ba"),
        "systems": {"ooma"},
    },
}


def _verify_password(username, password):
    user = USERS.get(username)
    if not user:
        # Still run a PBKDF2 round even for an unknown username, against a
        # fixed dummy salt/hash - otherwise "unknown user" returns
        # immediately while "known user, wrong password" takes ~200k
        # rounds, and that timing difference is enough to enumerate valid
        # usernames from response latency alone.
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"\x00" * 16, PBKDF2_ITERATIONS)
        return False
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), user["salt"], PBKDF2_ITERATIONS)
    return hmac.compare_digest(computed, user["hash"])


def _current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return None
    return username if _verify_password(username, password) else None


def require_login(f):
    """Applies to every route below - this portal has no public routes at
    all, unlike the bridges themselves (which keep their read-only JSON
    feeds open). Sends 401 + WWW-Authenticate, triggering a browser's
    native login prompt, same UX as the pages this replaces."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        username = _current_user()
        if username is None:
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="bridge portal"'},
            )
        return f(username, *args, **kwargs)
    return wrapper


def accessible_systems(username):
    allowed = USERS[username]["systems"]
    return {k: v for k, v in SYSTEMS.items() if k in allowed}


def _forbidden_or_unknown(username, system):
    if system not in SYSTEMS:
        return Response(f"Unknown system: {system}", 404)
    if system not in USERS[username]["systems"]:
        return Response(f"Your account does not have access to '{system}'", 403)
    return None


# --- HTML ----------------------------------------------------------------
# Light, teal-and-white palette approximating Covenant Health's own site
# (covenanthealth.com): white/light-gray backgrounds, a teal brand color
# carried through headers/links/primary actions, a red reserved for
# destructive actions (un-acknowledge) and errors - not just a retint of
# the bridges' old dark admin theme, since the real site itself is a
# clean, clinical white/teal look, not a dark one.

PAGE_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - Bridge Portal</title>
<style>
  :root {{
    --bg: #f4f7f7; --panel: #ffffff; --panel-raised: #f7fafa;
    --border: #dbe6e6; --border-bright: #c3d4d4;
    --text: #1f2b2b; --text-dim: #5b6c6c; --text-faint: #8a9797;
    /* Actual Covenant Health brand colors */
    --teal: #005898; --teal-dark: #083880; --teal-tint: #d9e6f0;
    --danger: #a80030; --danger-dark: #a00018; --danger-tint: #f5e0e6;
    --warn: #b9770e; --ok: #1f7a4d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
    padding: 0 0 60px;
  }}
  header.brand {{
    background: var(--teal); color: #fff; padding: 16px 20px;
    display: flex; align-items: center; justify-content: space-between;
  }}
  header.brand .brand-name {{ font-size: 17px; font-weight: 700; letter-spacing: 0.01em; }}
  header.brand .brand-sub {{ font-size: 12px; color: rgba(255,255,255,0.8); font-weight: 400; margin-top: 2px; }}
  .wrap {{ max-width: 1300px; width: 95%; margin: 0 auto; padding-top: 28px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; color: var(--teal-dark); }}
  h2.system-heading {{ font-size: 15px; margin: 28px 0 10px; color: var(--teal); border-bottom: 2px solid var(--teal-tint); padding-bottom: 6px; }}
  .sub {{ color: var(--text-dim); font-size: 13px; margin: 0 0 20px; }}
  nav.top {{ display: flex; flex-wrap: wrap; row-gap: 8px; justify-content: space-between; align-items: center; padding: 10px 20px; background: var(--panel); border-bottom: 1px solid var(--border); }}
  nav.top a {{ color: var(--text-dim); font-size: 13px; text-decoration: none; margin-left: 18px; padding-bottom: 2px; border-bottom: 2px solid transparent; }}
  nav.top a.active {{ color: var(--teal); border-bottom-color: var(--teal); font-weight: 600; }}
  nav.top a:hover {{ color: var(--teal); }}
  nav.top a.logout {{ color: var(--danger); }}
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 20px; margin-bottom: 18px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .card h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--teal); font-weight: 700; margin: 0 0 14px; }}
  .msg {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }}
  .msg.ok {{ background: var(--teal-tint); color: var(--teal-dark); border: 1px solid var(--teal); }}
  .msg.err {{ background: var(--danger-tint); color: var(--danger); border: 1px solid var(--danger); }}
  label {{ display: block; font-size: 12px; color: var(--text-dim); margin: 12px 0 5px; font-weight: 600; }}
  label:first-child {{ margin-top: 0; }}
  input[type=text], input[type=number], input[type=datetime-local], select, textarea {{
    width: 100%; max-width: 480px; padding: 9px 11px; border-radius: 6px; border: 1.5px solid var(--border-bright);
    background: var(--panel-raised); color: var(--text); font-size: 14px; font-family: inherit;
  }}
  input:focus, select:focus, textarea:focus {{ outline: none; border-color: var(--teal); box-shadow: 0 0 0 3px var(--teal-tint); }}
  textarea {{ resize: vertical; min-height: 50px; }}
  .radio-row {{ display: flex; gap: 18px; margin: 12px 0 5px; }}
  .radio-row label {{ display: flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; color: var(--text); font-weight: 400; }}
  button {{
    font-family: inherit; font-weight: 600; font-size: 14px; padding: 10px 18px;
    border-radius: 7px; border: 1.5px solid var(--teal); background: var(--teal);
    color: #fff; cursor: pointer; margin-top: 16px;
  }}
  button:hover {{ background: var(--teal-dark); border-color: var(--teal-dark); }}
  button.cancel-btn {{
    border-color: var(--danger); background: #fff; color: var(--danger);
    font-size: 12px; padding: 5px 10px; margin: 0; font-weight: 600;
  }}
  button.cancel-btn:hover {{ background: var(--danger-tint); }}
  button.ghost {{ border-color: var(--border-bright); background: #fff; color: var(--text-dim); font-size: 12px; padding: 9px 14px; }}
  button.ghost:hover {{ border-color: var(--teal); color: var(--teal); background: #fff; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: var(--text-dim); font-weight: 700; text-transform: uppercase;
    font-size: 11px; letter-spacing: 0.04em; padding: 8px 10px; border-bottom: 2px solid var(--teal-tint); }}
  td {{ padding: 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tbody tr:hover td {{ background: var(--panel-raised); }}
  td.time {{ color: var(--text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .empty {{ color: var(--text-faint); font-size: 13px; padding: 10px 0; }}
  .table-scroll {{ overflow-x: auto; }}
  .tag {{ font-size: 10px; text-transform: uppercase; padding: 2px 6px; border-radius: 4px;
    background: var(--teal-tint); border: 1px solid var(--border-bright); color: var(--teal-dark); font-weight: 600; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: end; }}
  .field {{ display: flex; flex-direction: column; gap: 5px; min-width: 160px; }}
  .field.wide {{ min-width: 220px; }}
  .sev-critical {{ color: var(--danger); font-weight: 600; }}
  .sev-degraded {{ color: var(--warn); font-weight: 600; }}
  .sev-recovered {{ color: var(--ok); font-weight: 600; }}
  .change .was {{ display: block; color: var(--text-faint); font-size: 11px; margin-top: 2px; }}
</style>
</head>
<body>
<header class="brand">
  <div>
    <div class="brand-name">Covenant Health</div>
    <div class="brand-sub">Bridge Portal &middot; Facility Systems</div>
  </div>
</header>
<nav class="top">
  <div class="sub" style="margin:0;">Signed in as <strong>{username}</strong></div>
  <div>
    <a href="/overview" class="{overview_active}">Overview</a>
    <a href="/maintenance" class="{maint_active}">Maintenance</a>
    <a href="/events-search" class="{events_active}">Event search</a>
    <a href="/hyperview" class="{hyperview_active}">Hyperview</a>
    <a href="/ipro" class="{ipro_active}">iPRO Cameras</a>
    <a href="/ooma" class="{ooma_active}">Ooma AirDial</a>
    <a href="/logout" class="logout">Log out</a>
  </div>
</nav>
<div class="wrap">
  {body}
</div>
</body>
</html>"""


def render_shell(title, body, active, username=""):
    return PAGE_SHELL.format(
        title=title, body=body, username=_esc(username),
        overview_active="active" if active == "overview" else "",
        maint_active="active" if active == "maintenance" else "",
        events_active="active" if active == "events" else "",
        hyperview_active="active" if active == "hyperview" else "",
        ipro_active="active" if active == "ipro" else "",
        ooma_active="active" if active == "ooma" else "",
    )


def _msg_html():
    message = request.args.get("message")
    error = request.args.get("error")
    parts = []
    if message:
        parts.append(f'<div class="msg ok">{_esc(message)}</div>')
    if error:
        parts.append(f'<div class="msg err">{_esc(error)}</div>')
    return "".join(parts)


def _esc(s):
    from markupsafe import escape
    return escape(s or "")


def _json_for_script(obj):
    """json.dumps, with '<' escaped so a value containing the literal
    text "</script>" (a device/camera name, however unlikely) can't
    break out of the <script> block it's embedded in."""
    return json.dumps(obj).replace("<", "\\u003c")


# --- backend calls ---------------------------------------------------

def _fetch_windows(system):
    """Returns a normalized list of {key, target_type, target, reason,
    set_by, remaining} regardless of which bridge it came from - ipro's
    /maintenance-windows and ooma's /maintenance-log use different field
    names for the same concepts (target_display vs target)."""
    cfg = SYSTEMS[system]
    try:
        resp = requests.get(f"{cfg['base_url']}{cfg['windows_route']}", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException:
        return None  # bridge unreachable - caller shows a notice, not an empty table
    rows = []
    for w in raw:
        rows.append({
            "key": w.get("key"),
            "target_type": w.get("target_type"),
            "target": w.get("target_display") or w.get("target"),
            "reason": w.get("reason"),
            "set_by": w.get("set_by"),
            "remaining": w.get("remaining"),
        })
    return rows


def _fetch_options(system):
    """{'single': [{'id','name'}, ...], 'grouped': [name, ...]} for the
    maintenance form's dropdowns, or None if the bridge can't be reached
    (caller falls back to a plain text input in that case, same as
    before - creating a window still works, just without autocomplete)."""
    cfg = SYSTEMS[system]
    try:
        resp = requests.get(f"{cfg['base_url']}/maintenance-options", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def _proxy_redirect_query(resp, fallback_system):
    """ipro/ooma's own POST /maintenance[/*/cancel] redirect to their own
    (now-retired) GET /maintenance with ?message=.../?error=... - pull
    that query string off and reattach it to THIS portal's /maintenance
    instead of following the backend's redirect into its dead page."""
    location = resp.headers.get("Location", "")
    query = location.split("?", 1)[1] if "?" in location else ""
    qs = f"?{query}" if query else f"?error={quote('No response from bridge')}"
    sep = "&" if query else ""
    return redirect(f"/maintenance{qs}{sep}system={fallback_system}" if query else f"/maintenance{qs}")


# --- maintenance page --------------------------------------------------

@app.route("/maintenance", methods=["GET"])
@require_login
def maintenance_page(username):
    systems = accessible_systems(username)
    sections = []
    for key, cfg in systems.items():
        windows = _fetch_windows(key)
        if windows is None:
            table_html = f'<p class="empty">Could not reach {_esc(cfg["label"])} ({_esc(cfg["base_url"])}).</p>'
        elif not windows:
            table_html = '<p class="empty">Nothing currently acknowledged.</p>'
        else:
            rows_html = "".join(
                f"<tr><td>{_esc(w['target'])} <span class=\"tag\">{_esc(w['target_type'])}</span></td>"
                f"<td>{_esc(w['reason']) or '-'}</td><td>{_esc(w['set_by']) or '-'}</td>"
                f"<td>{_esc(w['remaining'])}</td>"
                f"<td><form method=\"post\" action=\"/maintenance/{key}/{quote(w['key'], safe='')}/cancel\" style=\"margin:0;\">"
                f"<button type=\"submit\" class=\"cancel-btn\">Un-acknowledge</button></form></td></tr>"
                for w in windows
            )
            table_html = (
                '<div class="table-scroll"><table><tr><th>Target</th><th>Reason</th>'
                f'<th>Acknowledged by</th><th>Expires in</th><th></th></tr>{rows_html}</table></div>'
            )

        # target_types[0] = single device/camera, [1] = grouped account/server -
        # two <select> elements sharing name="target", only one enabled at a
        # time (a disabled <select> doesn't get submitted with the form) so
        # picking "Entire server" swaps which dropdown - and which option
        # list - actually supplies the value.
        single_type, single_label = cfg["target_types"][0]
        grouped_type, grouped_label = cfg["target_types"][1]
        options = _fetch_options(key)

        radio_html = (
            f'<label><input type="radio" name="target_type" value="{single_type}" checked '
            f'onchange="toggleTarget(\'{key}\', true)"> {single_label}</label>'
            f'<label><input type="radio" name="target_type" value="{grouped_type}" '
            f'onchange="toggleTarget(\'{key}\', false)"> {grouped_label}</label>'
        )

        if options is None:
            # bridge unreachable right now - fall back to plain free text
            # rather than block window creation entirely
            target_fields = (
                f'<label>Target (name or id) - could not load suggestions, type it manually</label>'
                f'<input type="text" name="target" required>'
            )
        else:
            # <input list=...> + <datalist> - free typing with autocomplete
            # suggestions, not a closed picklist: matches how the original
            # per-bridge pages worked (both backends already resolve a
            # target by name-or-id, so a typed name that isn't in the
            # suggestion list still works - the datalist is a convenience,
            # not a constraint).
            single_opts = "".join(f'<option value="{_esc(o["name"])}">' for o in options.get("single", []))
            grouped_opts = "".join(f'<option value="{_esc(g)}">' for g in options.get("grouped", []))
            target_fields = f"""
            <label>{_esc(single_label)}</label>
            <input type="text" name="target" id="target-single-{key}" list="single-list-{key}" autocomplete="off">
            <datalist id="single-list-{key}">{single_opts}</datalist>
            <label>{_esc(grouped_label)}</label>
            <input type="text" name="target" id="target-grouped-{key}" list="grouped-list-{key}" autocomplete="off" disabled style="display:none;">
            <datalist id="grouped-list-{key}">{grouped_opts}</datalist>
            """

        sections.append(f"""
        <h2 class="system-heading">{_esc(cfg['label'])}</h2>
        <div class="card">
          <h3>Currently acknowledged</h3>
          {table_html}
        </div>
        <div class="card">
          <h3>Acknowledge {_esc(cfg['label'])}</h3>
          <form method="post" action="/maintenance">
            <input type="hidden" name="system" value="{key}">
            <div class="radio-row">{radio_html}</div>
            {target_fields}
            <label>Acknowledge for (hours)</label>
            <input type="number" name="hours" min="1" max="168" value="24" required>
            <label>Reason / action taken</label>
            <textarea name="reason" required></textarea>
            <label>Acknowledged by</label>
            <input type="text" name="set_by" required>
            <button type="submit">Acknowledge</button>
          </form>
        </div>
        """)

    body = f"""
    <h1>Acknowledge issues</h1>
    <p class="sub">{_esc(', '.join(cfg['label'] for cfg in systems.values()))}.</p>
    {_msg_html()}
    {''.join(sections)}
    <script>
    function toggleTarget(system, singleChosen) {{
      const single = document.getElementById('target-single-' + system);
      const grouped = document.getElementById('target-grouped-' + system);
      if (!single || !grouped) return;  // options failed to load - plain text input instead, nothing to toggle
      single.disabled = !singleChosen;
      single.style.display = singleChosen ? '' : 'none';
      grouped.disabled = singleChosen;
      grouped.style.display = singleChosen ? 'none' : '';
    }}
    </script>
    """
    return Response(render_shell("Maintenance", body, "maintenance", username), mimetype="text/html")


@app.route("/maintenance", methods=["POST"])
@require_login
def maintenance_create(username):
    system = request.form.get("system", "")
    denied = _forbidden_or_unknown(username, system)
    if denied:
        return denied
    cfg = SYSTEMS[system]
    try:
        resp = requests.post(
            f"{cfg['base_url']}/maintenance",
            data={
                "target_type": request.form.get("target_type", ""),
                "target": request.form.get("target", ""),
                "hours": request.form.get("hours", ""),
                "reason": request.form.get("reason", ""),
                "set_by": request.form.get("set_by", ""),
            },
            headers={"Authorization": cfg["maintenance_auth"]},
            timeout=REQUEST_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        return redirect(f"/maintenance?error={quote('Could not reach ' + cfg['label'])}")
    return _proxy_redirect_query(resp, system)


@app.route("/maintenance/<system>/<path:key>/cancel", methods=["POST"])
@require_login
def maintenance_cancel(username, system, key):
    denied = _forbidden_or_unknown(username, system)
    if denied:
        return denied
    cfg = SYSTEMS[system]
    try:
        resp = requests.post(
            f"{cfg['base_url']}/maintenance/{quote(key, safe='')}/cancel",
            headers={"Authorization": cfg["maintenance_auth"]},
            timeout=REQUEST_TIMEOUT, allow_redirects=False,
        )
    except requests.RequestException:
        return redirect(f"/maintenance?error={quote('Could not reach ' + cfg['label'])}")
    return _proxy_redirect_query(resp, system)


# --- event search page ---------------------------------------------------

EVENTS_SCRIPT = """
<script>
const SYSTEMS = %(systems_json)s;
const DEVICE_ID_BY_NAME = %(device_id_by_name_json)s;

function severityClass(sev) {
  if (sev === 'Critical') return 'sev-critical';
  if (sev === 'Degraded') return 'sev-degraded';
  return '';
}
function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}
function renderRows(rows) {
  const body = document.getElementById('results-body');
  const emptyMsg = document.getElementById('empty-msg');
  body.innerHTML = '';
  if (!rows.length) { emptyMsg.style.display = 'block'; return; }
  emptyMsg.style.display = 'none';
  for (const r of rows) {
    const tr = document.createElement('tr');
    const when = new Date(r.changed_at * 1000).toLocaleString();
    let changeHtml;
    if (r.new_message == null) {
      changeHtml = '<span class="sev-recovered">Recovered</span><span class="was">was: ' + escapeHtml(r.old_message) + '</span>';
    } else if (r.old_message == null) {
      changeHtml = '<span class="' + severityClass(r.new_severity) + '">' + escapeHtml(r.new_severity) + ': ' + escapeHtml(r.new_message) + '</span>';
    } else {
      changeHtml = '<span class="' + severityClass(r.new_severity) + '">' + escapeHtml(r.new_severity) + ': ' + escapeHtml(r.new_message) + '</span>'
        + '<span class="was">was: ' + escapeHtml(r.old_message) + '</span>';
    }
    tr.innerHTML = '<td class="time">' + when + '</td>'
      + '<td>' + escapeHtml(r.system) + '</td>'
      + '<td>' + escapeHtml(r.device) + '<span class="was">' + escapeHtml(r.account) + '</span></td>'
      + '<td><span class="tag">' + escapeHtml(r.device_type || r.group_name) + '</span></td>'
      + '<td class="change">' + changeHtml + '</td>';
    body.appendChild(tr);
  }
}
function currentParams() {
  const params = new URLSearchParams();
  const system = document.getElementById('system-select').value;
  const deviceTyped = document.getElementById('device-id-input').value;
  // The field shows/holds the friendly name (so a selected suggestion
  // displays correctly instead of collapsing to a raw id) - resolve it
  // to the id /events actually filters on; if it's not a known name
  // (typed free-hand, or already an id), send it through as-is.
  const deviceId = DEVICE_ID_BY_NAME[deviceTyped] || deviceTyped;
  const account = document.getElementById('account-input').value;
  const since = document.getElementById('since-input').value;
  const until = document.getElementById('until-input').value;
  const limit = document.getElementById('limit-input').value;
  if (deviceId) params.set('device_id', deviceId);
  if (account) params.set('account', account);
  if (since) params.set('since', since);
  if (until) params.set('until', until);
  if (limit) params.set('limit', limit);
  return {system, params};
}
async function runSearch() {
  const {system, params} = currentParams();
  const statusLine = document.getElementById('status-line');
  statusLine.textContent = 'Searching...';
  const systemsToQuery = system ? [system] : SYSTEMS;
  let all = [];
  try {
    for (const sys of systemsToQuery) {
      const resp = await fetch('/events?system=' + sys + '&' + params.toString());
      if (!resp.ok) throw new Error(sys + ': HTTP ' + resp.status);
      const rows = await resp.json();
      for (const r of rows) r.system = sys;
      all = all.concat(rows);
    }
    all.sort((a, b) => b.changed_at - a.changed_at);
    renderRows(all);
    statusLine.textContent = all.length + ' event(s)';
  } catch (err) {
    statusLine.textContent = 'Search failed: ' + err.message;
    renderRows([]);
  }
}
function downloadCsv() {
  const {system, params} = currentParams();
  if (!system) { alert('Pick one system to export (CSV export covers one system at a time).'); return; }
  window.location.href = '/events.csv?system=' + system + '&' + params.toString();
}
function resetFilters() {
  document.getElementById('device-id-input').value = '';
  document.getElementById('account-input').value = '';
  document.getElementById('since-input').value = '';
  document.getElementById('until-input').value = '';
  document.getElementById('limit-input').value = '200';
  runSearch();
}
runSearch();
</script>
"""


@app.route("/events-search", methods=["GET"])
@require_login
def events_search_page(username):
    systems = accessible_systems(username)
    system_options = "".join(
        f'<option value="{key}">{_esc(cfg["label"])}</option>' for key, cfg in systems.items()
    )
    # single-system accounts get a locked selector - nothing to pick between
    system_select_html = (
        f'<select id="system-select" onchange="runSearch()">'
        f'<option value="">All ({_esc(", ".join(systems))})</option>{system_options}</select>'
        if len(systems) > 1 else
        f'<select id="system-select" disabled><option value="{next(iter(systems))}" selected>'
        f'{_esc(next(iter(systems.values()))["label"])}</option></select>'
        f'<input type="hidden" id="system-select-value" value="{next(iter(systems))}">'
    )

    # Device/account pickers - merged across every system this login can
    # see, rather than re-fetched client-side when the System filter
    # changes. Simpler, and a device_id/account value that doesn't apply
    # to whichever system is actually queried just matches nothing - no
    # different from picking a stale value out of any dropdown.
    seen_devices, device_opts = set(), []
    seen_accounts, account_opts = set(), []
    for sys_key in systems:
        opts = _fetch_options(sys_key)
        if not opts:
            continue
        for d in opts.get("single", []):
            dedupe_key = (d["id"], d["name"])
            if dedupe_key not in seen_devices:
                seen_devices.add(dedupe_key)
                device_opts.append(d)
        for a in opts.get("grouped", []):
            if a not in seen_accounts:
                seen_accounts.add(a)
                account_opts.append(a)
    device_opts.sort(key=lambda d: d["name"].lower())
    account_opts.sort(key=str.lower)

    # <input list=...> + <datalist> - free typing with autocomplete
    # suggestions, not a closed picklist. The datalist's value is the
    # NAME, not the id - a datalist option's value is what actually gets
    # inserted into the field when you click a suggestion, so if that
    # differed from what's shown as the label (the id, invisible-ish vs
    # the friendly name you clicked), selecting a suggestion left the
    # field showing a raw id instead of the name you picked - looked
    # broken, and made the dropdown feel like it never really
    # "selected" anything. Device search still needs an id (that's what
    # /events' device_id filter expects) - DEVICE_ID_BY_NAME below
    # resolves the typed name back to its id client-side at search time,
    # so what you see in the field is always the friendly name, and what
    # gets sent to the API is still the id it actually needs.
    device_id_by_name = {}
    for d in device_opts:
        device_id_by_name.setdefault(d["name"], d["id"])
    device_opts_html = "".join(f'<option value="{_esc(d["name"])}">' for d in device_opts)
    account_opts_html = "".join(f'<option value="{_esc(a)}">' for a in account_opts)
    device_select_html = (
        f'<input type="text" id="device-id-input" list="device-datalist" autocomplete="off">'
        f'<datalist id="device-datalist">{device_opts_html}</datalist>'
    )
    account_select_html = (
        f'<input type="text" id="account-input" list="account-datalist" autocomplete="off">'
        f'<datalist id="account-datalist">{account_opts_html}</datalist>'
    )

    body = f"""
    <h1>Device event history</h1>
    <p class="sub">Every time a device's status changed, across
    {_esc(', '.join(cfg['label'] for cfg in systems.values()))}.</p>

    <div class="card">
      <h3>Search</h3>
      <div class="filters">
        <div class="field"><label>System</label>{system_select_html}</div>
        <div class="field wide"><label>Device / camera</label>{device_select_html}</div>
        <div class="field wide"><label>Account / location</label>{account_select_html}</div>
        <div class="field"><label>Since</label><input type="datetime-local" id="since-input"></div>
        <div class="field"><label>Until</label><input type="datetime-local" id="until-input"></div>
        <div class="field"><label>Limit</label><input type="number" id="limit-input" min="1" max="2000" value="200"></div>
        <button type="button" onclick="runSearch()">Search</button>
        <button type="button" class="ghost" onclick="resetFilters()">Reset</button>
        <button type="button" class="ghost" onclick="downloadCsv()">Download CSV</button>
      </div>
    </div>
    <div class="card">
      <div id="status-line" class="sub"></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Time</th><th>System</th><th>Device</th><th>Group</th><th>Change</th></tr></thead>
          <tbody id="results-body"></tbody>
        </table>
        <div id="empty-msg" class="empty" style="display:none;">No matching events.</div>
      </div>
    </div>
    {EVENTS_SCRIPT % {
        'systems_json': _json_for_script(list(systems.keys())),
        'device_id_by_name_json': _json_for_script(device_id_by_name),
    }}
    """
    return Response(render_shell("Event search", body, "events", username), mimetype="text/html")


def _events_params_for_backend(system, cfg):
    """The portal's UI always sends the filter as 'device_id', but each
    backend's own /events route names that query param differently
    (ipro's is device_id, ooma's is myx_id) - translate here so the
    filter actually reaches the field the backend binds to."""
    params = {k: v for k, v in request.args.items() if k != "system"}
    backend_param = cfg.get("device_id_param", "device_id")
    if backend_param != "device_id" and "device_id" in params:
        params[backend_param] = params.pop("device_id")
    return params


@app.route("/events")
@require_login
def events_api(username):
    system = request.args.get("system", "")
    denied = _forbidden_or_unknown(username, system)
    if denied:
        return denied
    cfg = SYSTEMS[system]
    params = _events_params_for_backend(system, cfg)
    try:
        resp = requests.get(f"{cfg['base_url']}/events", params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return Response(f"Could not reach {cfg['label']}: {e}", 502)
    return Response(resp.content, resp.status_code, content_type="application/json")


@app.route("/events.csv")
@require_login
def events_csv(username):
    system = request.args.get("system", "")
    denied = _forbidden_or_unknown(username, system)
    if denied:
        return denied
    cfg = SYSTEMS[system]
    params = _events_params_for_backend(system, cfg)
    try:
        resp = requests.get(f"{cfg['base_url']}/events.csv", params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return Response(f"Could not reach {cfg['label']}: {e}", 502)
    return Response(
        resp.content, mimetype="text/csv",
        headers={"Content-Disposition": resp.headers.get("Content-Disposition", "attachment")},
    )


# --- shared dashboard components (Hyperview / iPRO / Ooma) ---------------
# Component CSS (matrix/gauge/tiles/alarm log) shared by /hyperview,
# /ipro and /ooma - built on top of whichever :root token set the
# including page already defines (PAGE_SHELL's brand tokens for the
# logged-in pages, HYPERVIEW_KIOSK_SHELL's dark tokens for the kiosk), so
# it's written entirely in var(--...) with no colors of its own.
HYPERVIEW_COMPONENT_CSS = """
  .redundancy-alert { display: flex; align-items: center; gap: 10px; background: var(--danger-tint);
    border: 1px solid var(--danger); color: var(--danger-dark); border-radius: 8px; padding: 12px 18px;
    font-size: 14px; margin-bottom: 18px; }
  .board { display: grid; grid-template-columns: 1fr 300px 1fr; gap: 18px; align-items: start; margin-bottom: 18px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04); overflow: hidden; margin-bottom: 18px; }
  .panel-head { padding: 14px 18px 11px; border-bottom: 1px solid var(--border); display: flex;
    align-items: baseline; justify-content: space-between; gap: 14px; }
  .panel-head h2 { margin: 0; font-size: 15px; color: var(--teal); }
  .panel-head .count-note { font-size: 12px; color: var(--text-faint); white-space: nowrap; }
  .matrix-wrap { overflow-x: auto; }
  table.matrix, table.summary, table.alarmlog { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.matrix th, table.matrix td, table.summary th, table.summary td,
  table.alarmlog th, table.alarmlog td { padding: 9px 10px; text-align: center; border-bottom: 1px solid var(--border); }
  table.matrix th:first-child, table.matrix td:first-child,
  table.summary th:first-child, table.summary td:first-child,
  table.alarmlog th, table.alarmlog td { text-align: left; }
  table.matrix thead th, table.summary thead th, table.alarmlog thead th {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-faint);
    background: var(--panel-raised); font-weight: 700; }
  table.matrix tbody tr:hover td, table.summary tbody tr:hover td, table.alarmlog tbody tr:hover td {
    filter: brightness(0.97); }
  .site-cell { font-weight: 600; white-space: nowrap; }
  tr.affected td { background: var(--danger-tint); }
  tr.affected .site-cell { color: var(--danger-dark); }
  .clear-mark { color: var(--ok); }
  .badge { display: inline-flex; align-items: center; justify-content: center; min-width: 22px; height: 20px;
    padding: 0 5px; border-radius: 5px; font-weight: 700; font-size: 12px; }
  .badge.open { background: var(--danger); color: #fff; }
  .badge.ack { background: var(--teal-tint); color: var(--teal-dark); border: 1px solid var(--teal); }
  .center-col { display: flex; flex-direction: column; }
  .gauge-card { text-align: center; padding: 18px 16px 22px; }
  .gauge-card .panel-head { justify-content: center; border-bottom: none; padding-bottom: 0; }
  .gauge-svg { width: 170px; height: 106px; margin: 4px auto -4px; display: block; }
  .gauge-track { fill: none; stroke: var(--border); stroke-width: 14; stroke-linecap: round; }
  .gauge-fill { fill: none; stroke-width: 14; stroke-linecap: round; transition: stroke-dashoffset 0.4s ease; }
  .gauge-score-num { font-size: 34px; font-weight: 700; line-height: 1; }
  .gauge-score-lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-faint); margin-top: 3px; }
  .status-banner { margin-top: 14px; padding: 9px 8px; border-radius: 8px; font-weight: 700; font-size: 15px; color: #fff; }
  table.summary .n { font-weight: 700; }
  table.summary .open-n { color: var(--danger); }
  table.summary .ack-n { color: var(--teal); }
  table.summary tr.row-all td { background: var(--panel-raised); font-weight: 700; }
  .sev-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  table.alarmlog tr.sev-critical { box-shadow: inset 3px 0 0 var(--danger); }
  table.alarmlog tr.sev-critical .sev-dot { background: var(--danger); }
  table.alarmlog tr.sev-critical td.loc { color: var(--danger-dark); }
  table.alarmlog tr.sev-warning { box-shadow: inset 3px 0 0 var(--warn); }
  table.alarmlog tr.sev-warning .sev-dot { background: var(--warn); }
  table.alarmlog td.dev { font-family: monospace; color: var(--text-dim); white-space: nowrap; }
  @media (max-width: 1080px) {
    .board { grid-template-columns: 1fr; }
    .center-col { flex-direction: row; flex-wrap: wrap; gap: 18px; }
    .center-col > .panel { flex: 1 1 320px; }
  }
"""

# Extra component CSS specific to /ipro and /ooma (vendor lockup, stat
# tiles, warn-tone badges, the gauge+summary+red-phone summary row) - kept
# separate from HYPERVIEW_COMPONENT_CSS above since /hyperview doesn't use
# any of it.
DASHBOARD_EXTRA_CSS = """
  .badge.warn { background: var(--warn); color: #fff; }
  .dash-mark { color: var(--text-faint); }
  .stat-row { display: flex; }
  .stat-tile { flex: 1; padding: 14px 10px; text-align: center; border-right: 1px solid var(--border); }
  .stat-tile:last-child { border-right: none; }
  .stat-tile .stat-lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-faint); }
  .stat-tile .stat-num { font-size: 26px; font-weight: 700; }
  .stat-tile.unhealthy .stat-num { color: var(--danger); }
  .updated-note { padding: 8px 18px 14px; font-size: 12px; color: var(--text-faint); text-align: center; }
  td.status-cell { font-weight: 700; color: var(--danger); }
  td.priority-cell.critical { color: var(--danger); font-weight: 700; }
  /* A capped-height scroll area with a frozen header - NOT the
     display:table-per-row trick this used to use (tr{display:table}
     inside a display:block tbody breaks the fixed-layout column-width
     algorithm each row relied on, since every row becomes its own
     unrelated table with no shared column metrics - that's what was
     making Device/Status text run into each other in the Device Detail
     table). A plain scrollable wrapper with a sticky <thead> gets the
     same "long list, frozen header" effect without touching how the
     table itself lays out its columns. */
  .device-scroll { max-height: 420px; overflow-y: auto; }
  .device-scroll table.alarmlog thead th { position: sticky; top: 0; z-index: 1; }
  .summary-row { display: grid; grid-template-columns: minmax(220px, 320px) minmax(220px, 320px) 1fr;
    gap: 18px; align-items: start; margin-bottom: 18px; }
  @media (max-width: 1080px) { .summary-row { grid-template-columns: 1fr; } }

  /* /overview's per-system status cards - same .panel/.panel-head shell
     every dashboard already uses, just wrapped in a link and given a
     status dot + state line instead of a table. */
  .status-grid { display: flex; flex-wrap: wrap; gap: 16px; }
  a.panel.status-panel { flex: 1 1 220px; text-decoration: none; color: inherit;
    display: block; transition: border-color 0.15s ease; }
  a.panel.status-panel:hover { border-color: var(--teal); }
  .status-panel .panel-head { border-bottom: none; }
  .status-body { display: flex; align-items: center; gap: 10px; padding: 0 18px 8px; }
  .status-dot { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }
  .status-state { font-size: 13px; color: var(--text-dim); }
  .status-detail { padding: 0 18px 16px; font-size: 12px; color: var(--text-faint); }
  .quick-links { display: flex; flex-wrap: wrap; gap: 8px 20px; padding: 0 18px 18px; }
  .quick-link { color: var(--teal); text-decoration: none; font-weight: 600; font-size: 13px; }
  .quick-link:hover { text-decoration: underline; }
"""

# Responsive overrides for /ipro, /ooma and /hyperview - no separate
# /mobile routes or URL to remember, this is just a @media breakpoint on
# the same page: below 720px it forces every board grid to a single
# column and turns each matrix/summary/alarmlog table into a stacked
# label:value card list (the standard responsive-table pattern) instead
# of a wide table a phone has to scroll sideways to read. Relies on the
# data-label attributes the HTML builders below (and the HYPERVIEW_SCRIPT
# render functions) put on every <td> - those attributes are inert above
# the breakpoint, this media query is the only place that reads them.
RESPONSIVE_DASHBOARD_CSS = """
  @media (max-width: 720px) {
    .wrap { width: 100%; padding: 16px 12px 40px; }
    nav.top a { margin-left: 12px; font-size: 12px; }
    .board, .summary-row { display: flex !important; flex-direction: column; }
    .center-col { flex-direction: column !important; }
    table.matrix thead, table.summary thead, table.alarmlog thead { display: none; }
    table.matrix tbody tr, table.summary tbody tr, table.alarmlog tbody tr {
      display: block; padding: 10px 4px; border-bottom: 1px solid var(--border);
    }
    table.matrix td, table.summary td, table.alarmlog td {
      display: flex; justify-content: space-between; align-items: center; gap: 10px;
      padding: 6px 4px; border-bottom: none; text-align: right;
    }
    table.matrix td::before, table.summary td::before, table.alarmlog td::before {
      content: attr(data-label); font-weight: 600; color: var(--text-faint);
      text-align: left; flex-shrink: 0;
    }
    table.matrix td.site-cell, table.alarmlog td.loc, table.alarmlog td.dev {
      font-weight: 700; text-align: left;
    }
    table.matrix td.site-cell::before, table.alarmlog td.loc::before, table.alarmlog td.dev::before { content: ""; }
    /* Stacked cards already keep each device's fields readable without a
       capped-height nested scroll area - let the list flow with the page
       instead of scrolling twice (page scroll + a tiny inner scrollbar). */
    .device-scroll { max-height: none; overflow-y: visible; }
    .device-scroll table.alarmlog thead th { position: static; }
    button, nav.top a { min-height: 40px; }
  }
"""

# Every dashboard page's <style> tag was independently concatenating the
# same 2-3 constants (HYPERVIEW_COMPONENT_CSS + DASHBOARD_EXTRA_CSS,
# sometimes + RESPONSIVE_DASHBOARD_CSS) - collected here once so a future
# dashboard route just uses DASHBOARD_CSS and there's one place that
# decides what "the dashboard family's styling" means.
DASHBOARD_BASE_CSS = HYPERVIEW_COMPONENT_CSS + DASHBOARD_EXTRA_CSS
DASHBOARD_CSS = DASHBOARD_BASE_CSS + RESPONSIVE_DASHBOARD_CSS


def _gauge_html(score, state, tone):
    dashoffset = round(251.2 * (1 - score / 100), 1)
    return f"""
    <div class="panel gauge-card">
      <div class="panel-head"><h2>System Health</h2></div>
      <svg class="gauge-svg" viewBox="0 0 190 118">
        <path class="gauge-track" d="M15 105 A80 80 0 0 1 175 105" />
        <path class="gauge-fill" d="M15 105 A80 80 0 0 1 175 105"
              stroke="var(--{tone})" stroke-dasharray="251.2" stroke-dashoffset="{dashoffset}" />
      </svg>
      <div class="gauge-score-num" style="color:var(--{tone})">{score}</div>
      <div class="gauge-score-lbl">Health score</div>
      <div class="status-banner" style="background:var(--{tone})">{_esc(state)}</div>
    </div>"""


def _summary_table_html(rows):
    total_open = sum(r["open"] for r in rows)
    total_ack = sum(r["ack"] for r in rows)
    body_rows = "".join(
        f'<tr><td data-label="Category">{_esc(r["label"])}</td>'
        f'<td class="n open-n" data-label="Open">{r["open"]}</td>'
        f'<td class="n ack-n" data-label="Ack\'d">{r["ack"]}</td></tr>'
        for r in rows
    )
    body_rows += (
        f'<tr class="row-all"><td data-label="Category">All</td>'
        f'<td class="n open-n" data-label="Open">{total_open}</td>'
        f'<td class="n ack-n" data-label="Ack\'d">{total_ack}</td></tr>'
    )
    return f"""
    <div class="panel">
      <div class="panel-head"><h2>Alarm Summary</h2></div>
      <table class="summary"><thead><tr><th>Category</th><th>Open</th><th>Ack'd</th></tr></thead>
        <tbody>{body_rows}</tbody></table>
      <div class="updated-note">Last Updated: {datetime.now().strftime('%I:%M:%S %p').lstrip('0')}</div>
    </div>"""


def _cam_badge(n, tone="open"):
    if n is None:
        return '<span class="dash-mark">&mdash;</span>'
    if n == 0:
        return '<span class="clear-mark">&#10003;</span>'
    return f'<span class="badge {tone}">{n}</span>'


def _ipro_matrix_html(title, count_note, rows, has_counts=True):
    body_rows = []
    for r in rows:
        if has_counts:
            affected = any(r.get(k) for k in ("infra", "degraded", "offline"))
            cls = ' class="affected"' if affected else ""
            body_rows.append(
                f'<tr{cls}><td class="site-cell" data-label="Location">&#127973; {_esc(r["site"])}</td>'
                f'<td data-label="Infra Issues">{_cam_badge(r["infra"], "warn")}</td>'
                f'<td data-label="Degraded Cams">{_cam_badge(r["degraded"], "warn")}</td>'
                f'<td data-label="Offline Cams">{_cam_badge(r["offline"], "open")}</td></tr>'
            )
        else:
            body_rows.append(
                f'<tr><td class="site-cell" data-label="Location">&#129658; {_esc(r["site"])}</td>'
                f'<td data-label="Infra Issues"><span class="dash-mark">&mdash;</span></td>'
                f'<td data-label="Degraded Cams"><span class="dash-mark">&mdash;</span></td>'
                f'<td data-label="Offline Cams"><span class="dash-mark">&mdash;</span></td></tr>'
            )
    return f"""
    <div class="panel">
      <div class="panel-head"><h2>{_esc(title)}</h2><span class="count-note">{_esc(count_note)}</span></div>
      <div class="matrix-wrap">
        <table class="matrix">
          <thead><tr><th>Location</th><th>Infra Issues</th><th>Degraded Cams</th><th>Offline Cams</th></tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </div>
    </div>"""


def _ipro_board_html(data):
    affected = sum(1 for r in data["hospitals"] if any(r.get(k) for k in ("infra", "degraded", "offline")))
    device_rows = "".join(
        f'<tr class="sev-critical"><td class="dev" data-label="Device">{_esc(d["device"])}</td>'
        f'<td class="status-cell" data-label="Status">{_esc(d["status"])}</td>'
        f'<td data-label="Detail">{_esc(d["detail"])}</td>'
        f'<td class="dur" data-label="Duration">{_esc(d["duration"])}</td>'
        f'<td class="priority-cell critical" data-label="Priority">{_esc(d["priority"])}</td></tr>'
        for d in data["devices"]
    )
    return f"""
    <div class="board">
      {_ipro_matrix_html("Datacenters / Hospitals", f"{affected} of {len(data['hospitals'])} affected", data["hospitals"])}
      <div class="center-col">
        {_summary_table_html(data["summary"])}
        {_gauge_html(data["gauge"]["score"], data["gauge"]["state"], data["gauge"]["tone"])}
        <div class="panel">
          <div class="stat-row">
            <div class="stat-tile"><div class="stat-lbl">Total Cameras</div><div class="stat-num">{data['totals']['total_cameras']}</div></div>
            <div class="stat-tile unhealthy"><div class="stat-lbl">Unhealthy Cameras</div><div class="stat-num">{data['totals']['unhealthy_cameras']}</div></div>
          </div>
        </div>
      </div>
      {_ipro_matrix_html("Primary Care / Clinics", f"0 of {len(data['clinics'])} affected", data["clinics"], has_counts=False)}
    </div>
    <div class="panel">
      <div class="panel-head"><h2>Device Detail</h2><span class="count-note">{data['totals']['unhealthy_cameras']} unhealthy devices</span></div>
      <div class="matrix-wrap device-scroll">
        <table class="alarmlog">
          <thead><tr><th>Device</th><th>Status</th><th>Detail</th><th>Duration</th><th>Priority</th></tr></thead>
          <tbody>{device_rows}</tbody>
        </table>
      </div>
    </div>"""


def _ooma_matrix_html(sites):
    rows = []
    for s in sites:
        conn = s["connectivity"]
        if conn == "live":
            conn_html = '<span class="badge ack" title="Live">&#9679;</span>'
            affected = False
        else:
            conn_html = _cam_badge(conn, "open")
            affected = isinstance(conn, int) and conn > 0
        battery_html = _cam_badge(s["battery"], "open")
        cls = ' class="affected"' if affected else ""
        rows.append(
            f'<tr{cls}><td class="site-cell" data-label="Location">&#127973; {_esc(s["site"])}</td>'
            f'<td data-label="Connectivity">{conn_html}</td>'
            f'<td data-label="Battery">{battery_html}</td></tr>'
        )
    affected_count = sum(1 for s in sites if isinstance(s["connectivity"], int) and s["connectivity"] > 0)
    return f"""
    <div class="panel">
      <div class="panel-head"><h2>Site Status</h2><span class="count-note">{affected_count} of {len(sites)} affected</span></div>
      <div class="matrix-wrap">
        <table class="matrix">
          <thead><tr><th>Location</th><th>Connectivity</th><th>Battery</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </div>"""


def _ooma_board_html(data):
    alarm_rows = "".join(
        f'<tr class="sev-warning"><td class="loc" data-label="Location">{_esc(a["location"])}</td>'
        f'<td class="dev" data-label="Device">{_esc(a["device"])}</td>'
        f'<td data-label="Severity"><span class="sev-degraded">{_esc(a["severity"])}</span></td>'
        f'<td data-label="Category">{_esc(a["category"])}</td>'
        f'<td data-label="Detail">{_esc(a["detail"])}</td></tr>'
        for a in data["alarms"]
    )
    return f"""
    <div class="summary-row">
      {_gauge_html(data["gauge"]["score"], data["gauge"]["state"], data["gauge"]["tone"])}
      {_summary_table_html(data["summary"])}
      <div class="panel">
        <div class="panel-head" style="background:var(--teal-dark); color:#fff; border-bottom:none;">
          <h2 style="color:#fff;">Ooma AirDial &mdash; Emergency Red Phone System</h2>
        </div>
        <div class="matrix-wrap">
          <table class="alarmlog">
            <thead><tr><th>Location</th><th>Device</th><th>Severity</th><th>Category</th><th>Detail</th></tr></thead>
            <tbody>{alarm_rows}</tbody>
          </table>
        </div>
      </div>
    </div>
    {_ooma_matrix_html(data["sites"])}"""


# --- iPRO / Ooma dashboard pages ------------------------------------------

@app.route("/ipro")
@require_login
def ipro_page(username):
    denied = _forbidden_or_unknown(username, "ipro")
    if denied:
        return denied
    data = _fetch_dashboard_snapshot("ipro", IPRO_DASHBOARD)
    body = f"""
    <h1>iPRO Cameras</h1>
    <p class="sub">Camera health &amp; infrastructure status across all sites.
    &middot; <a href="/ipro/kiosk" target="_blank" rel="noopener">Open kiosk view</a>
    (no login, for a wall display &mdash; opens in a new tab)</p>
    <style>{DASHBOARD_CSS}</style>
    {_ipro_board_html(data)}
    """
    return Response(render_shell("iPRO Cameras", body, "ipro", username), mimetype="text/html")


@app.route("/ooma")
@require_login
def ooma_page(username):
    denied = _forbidden_or_unknown(username, "ooma")
    if denied:
        return denied
    data = _fetch_dashboard_snapshot("ooma", OOMA_DASHBOARD)
    body = f"""
    <h1>Ooma AirDial</h1>
    <p class="sub">Emergency red phone connectivity &amp; battery health across all sites.
    &middot; <a href="/ooma/kiosk" target="_blank" rel="noopener">Open kiosk view</a>
    (no login, for a wall display &mdash; opens in a new tab)</p>
    <style>{DASHBOARD_CSS}</style>
    {_ooma_board_html(data)}
    """
    return Response(render_shell("Ooma AirDial", body, "ooma", username), mimetype="text/html")


# --- overview page ---------------------------------------------------------

@app.route("/overview")
@require_login
def overview_page(username):
    allowed = USERS[username]["systems"]
    cards = []
    if "hyperview" in allowed:
        state, tone, detail = "Could not reach Hyperview", "text-faint", ""
        try:
            resp = requests.get(f"{HYPERVIEW_BASE_URL}/overall-health", timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            health = resp.json()[0]
            state = health["state"]
            tone = "ok" if health["health"] == 100 else "warn" if health["health"] >= 70 else "danger"
            summary_resp = requests.get(f"{HYPERVIEW_BASE_URL}/category-summary", timeout=REQUEST_TIMEOUT)
            summary_resp.raise_for_status()
            all_row = next((c for c in summary_resp.json() if "All" in c.get("category", "")), None)
            if all_row:
                detail = f"{all_row['open']} open issue(s) &middot; {all_row['ack']} acknowledged"
        except (requests.RequestException, KeyError, IndexError, ValueError):
            pass
        cards.append(("Hyperview", "/hyperview", state, tone, detail))
    if "ipro" in allowed:
        ipro_data = _fetch_dashboard_snapshot("ipro", IPRO_DASHBOARD)
        g = ipro_data["gauge"]
        totals = ipro_data["totals"]
        total_open = sum(r["open"] for r in ipro_data["summary"])
        detail = (f"{total_open} open issue(s) &middot; {totals['unhealthy_cameras']} of "
                  f"{totals['total_cameras']} cameras unhealthy")
        cards.append(("iPRO Cameras", "/ipro", g["state"], g["tone"], detail))
    if "ooma" in allowed:
        ooma_data = _fetch_dashboard_snapshot("ooma", OOMA_DASHBOARD)
        g = ooma_data["gauge"]
        sites = ooma_data["sites"]
        total_open = sum(r["open"] for r in ooma_data["summary"])
        affected = sum(1 for s in sites if isinstance(s["connectivity"], int) and s["connectivity"] > 0)
        detail = f"{total_open} open alarm(s) &middot; {affected} of {len(sites)} sites affected"
        cards.append(("Ooma AirDial", "/ooma", g["state"], g["tone"], detail))

    # Reuses the exact .panel/.panel-head shell every other dashboard page
    # is built from (see HYPERVIEW_COMPONENT_CSS) rather than a bespoke
    # card style, so this page reads as part of the same family instead
    # of a one-off landing screen. New systems just append another card
    # here the same way ipro/ooma/hyperview do above - nothing about the
    # markup or CSS is specific to today's three.
    cards_html = "".join(f"""
      <a class="panel status-panel" href="{href}">
        <div class="panel-head"><h2>{_esc(name)}</h2></div>
        <div class="status-body">
          <span class="status-dot" style="background:var(--{tone})"></span>
          <span class="status-state">{_esc(state)}</span>
        </div>
        {f'<div class="status-detail">{detail}</div>' if detail else ''}
      </a>""" for name, href, state, tone, detail in cards)

    # Maintenance/event-search only ever cover the auth-forwarding
    # bridges (see accessible_systems) - Hyperview has no windows/events
    # of its own, so an account with only hyperview access sees no
    # Maintenance/Event search link here, same as it sees no
    # Maintenance/Event search nav items with anything to act on. Kiosk
    # mode is offered unconditionally - its routes are unauthenticated
    # already (see the "kiosk" section below), so the link exposes
    # nothing this account couldn't already reach by URL.
    quick_link_items = ["<a class=\"quick-link\" href=\"/kiosk\">Kiosk mode &rarr;</a>"]
    if accessible_systems(username):
        quick_link_items = [
            "<a class=\"quick-link\" href=\"/maintenance\">Maintenance &rarr;</a>",
            "<a class=\"quick-link\" href=\"/events-search\">Event search &rarr;</a>",
        ] + quick_link_items
    quick_links_html = f"""
        <div class="panel">
          <div class="panel-head"><h2>Quick Links</h2></div>
          <div class="quick-links">{''.join(quick_link_items)}</div>
        </div>"""

    body = f"""
    <h1>System overview</h1>
    <p class="sub">One login for every Covenant Health facility system you have access to
    &mdash; camera health, emergency phone lines, and site infrastructure today, with more
    systems landing here over time.</p>
    <style>{DASHBOARD_BASE_CSS}</style>
    <div class="status-grid">{cards_html or '<p class="empty">Your account has no systems assigned.</p>'}</div>
    {quick_links_html}
    """
    return Response(render_shell("Overview", body, "overview", username), mimetype="text/html")


# --- Hyperview dashboard --------------------------------------------------
# Shared between the logged-in /hyperview page (responsive down to phone
# widths via RESPONSIVE_DASHBOARD_CSS) and the unauthenticated
# /hyperview/kiosk wall display - same rendering logic, different CSS
# theme and auto-refresh interval (see the %(...)s placeholders each page
# fills in). Talks only to /hyperview/api/* (this portal's own proxy),
# never straight to HYPERVIEW_BASE_URL - keeps the real matrix.py host out
# of client-side JS entirely.

HYPERVIEW_SCRIPT = """
<script>
function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

// location-health-matrix / clinic-health-matrix rows score each category
// as: 0 = clear, 1-999 = that many OPEN issues, 1000+ = ALL currently
// acknowledged (subtract 1000 for the real count) - see
// _score_category_total in matrix.py.
function categoryCellHtml(count) {
  if (count === 0) return '<span class="clear-mark">&#10003;</span>';
  if (count >= 1000) return '<span class="badge ack">' + (count - 1000) + '</span>';
  return '<span class="badge open">' + count + '</span>';
}

// data-label attributes below are inert on the kiosk page and above the
// RESPONSIVE_DASHBOARD_CSS breakpoint - only that media query's
// responsive-table rules read them, to turn each row into a stacked
// label:value card on a narrow /hyperview viewport.
function renderMatrixTable(tbodyId, rows) {
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML = '';
  for (const r of rows) {
    const tr = document.createElement('tr');
    if (r.site) tr.className = 'affected';
    tr.innerHTML =
      '<td class="site-cell" data-label="Location">' + escapeHtml(r.locationDisplay) + '</td>' +
      '<td data-label="Power">' + categoryCellHtml(r.power) + '</td>' +
      '<td data-label="Cooling">' + categoryCellHtml(r.facilities) + '</td>' +
      '<td data-label="Server">' + categoryCellHtml(r.compute) + '</td>' +
      '<td data-label="Network">' + categoryCellHtml(r.network) + '</td>';
    tbody.appendChild(tr);
  }
}

function renderSummary(categories) {
  const tbody = document.getElementById('tiles-summary-body');
  tbody.innerHTML = '';
  for (const c of categories) {
    const isAll = c.category.indexOf('All') !== -1;
    const tr = document.createElement('tr');
    if (isAll) tr.className = 'row-all';
    tr.innerHTML =
      '<td data-label="Category">' + escapeHtml(c.category) + '</td>' +
      '<td class="n open-n" data-label="Open">' + c.open + '</td>' +
      '<td class="n ack-n" data-label="Ack\\'d">' + c.ack + '</td>';
    tbody.appendChild(tr);
  }
}

// Gauge is a fixed 251.2-length semicircle arc (see the SVG path in both
// pages) - dashoffset walks it from fully-hidden (0%% health) to
// fully-drawn (100%% health).
const GAUGE_ARC_LENGTH = 251.2;

function renderGauge(health) {
  const scoreEl = document.getElementById('gauge-score');
  const stateEl = document.getElementById('gauge-state');
  const fillEl = document.getElementById('gauge-fill');
  const bannerEl = document.getElementById('status-banner');
  const redundancyEl = document.getElementById('redundancy-alert');

  const color = health.health === 100 ? 'var(--ok)' : health.health >= 70 ? 'var(--warn)' : 'var(--danger)';
  scoreEl.textContent = health.health;
  scoreEl.style.color = color;
  stateEl.textContent = health.state;
  fillEl.setAttribute('stroke', color);
  fillEl.setAttribute('stroke-dashoffset', String(GAUGE_ARC_LENGTH * (1 - health.health / 100)));
  bannerEl.textContent = health.emoji + ' ' + health.state;
  bannerEl.style.background = color;

  if (health.redundancyAlerts && health.redundancyAlerts.length) {
    redundancyEl.style.display = 'flex';
    redundancyEl.querySelector('.text').innerHTML =
      '<strong>Redundancy lost &mdash;</strong> ' + escapeHtml(health.redundancyAlerts.join('; ')) + '.';
  } else {
    redundancyEl.style.display = 'none';
  }
}

function renderAlarmLog(rows) {
  const tbody = document.getElementById('alarmlog-body');
  tbody.innerHTML = '';
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="empty">No active alarms.</td></tr>';
    return;
  }
  for (const r of rows) {
    const tr = document.createElement('tr');
    tr.className = String(r.severity).toLowerCase() === 'critical' ? 'sev-critical' : 'sev-warning';
    tr.innerHTML =
      '<td class="loc" data-label="Location"><span class="sev-dot"></span>' + escapeHtml(r.location) + '</td>' +
      '<td class="dev" data-label="Device">' + escapeHtml(r.device) + '</td>' +
      '<td data-label="Alarm">' + escapeHtml(r.alarm) + '</td>';
    tbody.appendChild(tr);
  }
}

async function refreshAll() {
  const statusEl = document.getElementById('fetch-status');
  try {
    const [health, summary, hospitals, clinics, alarms] = await Promise.all([
      fetch('/hyperview/api/overall-health').then(r => r.json()).then(rows => rows[0]),
      fetch('/hyperview/api/category-summary').then(r => r.json()),
      fetch('/hyperview/api/location-health-matrix').then(r => r.json()),
      fetch('/hyperview/api/clinic-health-matrix').then(r => r.json()),
      fetch('/hyperview/api/active-alarm-log').then(r => r.json()),
    ]);
    renderGauge(health);
    renderSummary(summary);
    renderMatrixTable('hospitals-body', hospitals);
    renderMatrixTable('clinics-body', clinics);
    renderAlarmLog(alarms);
    document.getElementById('hosp-note').textContent =
      hospitals.filter(r => r.site).length + ' of ' + hospitals.length + ' affected';
    document.getElementById('clinic-note').textContent =
      clinics.filter(r => r.site).length + ' of ' + clinics.length + ' affected';
    document.getElementById('alarm-note').textContent =
      alarms.length + ' device' + (alarms.length === 1 ? '' : 's');
    const lastUpdatedEl = document.getElementById('hv-last-updated');
    if (lastUpdatedEl) lastUpdatedEl.textContent = new Date().toLocaleTimeString();
    // No-op on the regular /hyperview page (kioskCriticalCue only exists
    // on /hyperview/kiosk, see DASHBOARD_KIOSK_SHELL) - this same
    // refreshAll() runs on both.
    if (window.kioskCriticalCue) {
      window.kioskCriticalCue(alarms.some(a => String(a.severity).toLowerCase() === 'critical'));
    }
    if (statusEl) statusEl.textContent = '';
  } catch (err) {
    if (statusEl) statusEl.textContent = 'Could not reach Hyperview: ' + err.message;
  }
}

function tickClock() {
  const el = document.getElementById('kiosk-clock');
  if (el) el.textContent = new Date().toLocaleTimeString([], { hour12: false });
}

refreshAll();
tickClock();
setInterval(tickClock, 1000);
const AUTO_REFRESH_MS = %(auto_refresh_ms)s;
if (AUTO_REFRESH_MS > 0) setInterval(refreshAll, AUTO_REFRESH_MS);
</script>
"""

# Shared markup for /hyperview and /hyperview/kiosk - the site matrices,
# gauge, category tiles, and alarm log. Each page wraps this in its own
# <style> (branded-light plus RESPONSIVE_DASHBOARD_CSS for the logged-in
# page, dark/oversized for the kiosk) and its own header, then appends
# HYPERVIEW_SCRIPT.
def _hyperview_board_html():
    return """
    <div class="redundancy-alert" id="redundancy-alert" style="display:none;">
      <span>&#128680;</span><span class="text"></span>
    </div>
    <div class="board">
      <div class="panel">
        <div class="panel-head"><h2>Datacenters / Hospitals</h2><span class="count-note" id="hosp-note"></span></div>
        <div class="matrix-wrap">
          <table class="matrix">
            <thead><tr><th>Site</th><th title="Power">&#9889;</th><th title="Cooling">&#10052;</th>
              <th title="Server">&#128421;</th><th title="Network">&#127760;</th></tr></thead>
            <tbody id="hospitals-body"></tbody>
          </table>
        </div>
      </div>
      <div class="center-col">
        <div class="panel gauge-card">
          <div class="panel-head"><h2>Overall Health</h2></div>
          <svg class="gauge-svg" viewBox="0 0 190 118">
            <path class="gauge-track" d="M15 105 A80 80 0 0 1 175 105" />
            <path class="gauge-fill" id="gauge-fill" d="M15 105 A80 80 0 0 1 175 105"
                  stroke="var(--warn)" stroke-dasharray="251.2" stroke-dashoffset="251.2" />
          </svg>
          <div class="gauge-score-num" id="gauge-score">&mdash;</div>
          <div class="gauge-score-lbl" id="gauge-state">Loading&hellip;</div>
          <div class="status-banner" id="status-banner"></div>
        </div>
        <div class="panel">
          <div class="panel-head"><h2>Alarm Summary</h2></div>
          <table class="summary"><thead><tr><th>Category</th><th>Open</th><th>Ack'd</th></tr></thead>
            <tbody id="tiles-summary-body"></tbody></table>
          <div class="updated-note">Last Updated: <span id="hv-last-updated">&mdash;</span></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head"><h2>Primary Care / Clinics</h2><span class="count-note" id="clinic-note"></span></div>
        <div class="matrix-wrap">
          <table class="matrix">
            <thead><tr><th>Site</th><th title="Power">&#9889;</th><th title="Cooling">&#10052;</th>
              <th title="Server">&#128421;</th><th title="Network">&#127760;</th></tr></thead>
            <tbody id="clinics-body"></tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-head"><h2>Active Alarms</h2><span class="count-note" id="alarm-note"></span></div>
      <div class="matrix-wrap">
        <table class="alarmlog">
          <thead><tr><th>Location</th><th>Device</th><th>Alarm</th></tr></thead>
          <tbody id="alarmlog-body"></tbody>
        </table>
      </div>
    </div>
    <div id="fetch-status" class="sub"></div>
    """


@app.route("/hyperview/api/<endpoint>")
def hyperview_api_proxy(endpoint):
    # Deliberately no @require_login - matches matrix.py's own feeds,
    # which have never had auth (see HYPERVIEW_BASE_URL comment above).
    if endpoint not in HYPERVIEW_ENDPOINTS:
        return Response("Unknown Hyperview endpoint", 404)
    try:
        resp = requests.get(f"{HYPERVIEW_BASE_URL}/{endpoint}", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        return Response(json.dumps({"error": f"Could not reach Hyperview: {e}"}), 502, content_type="application/json")
    return Response(resp.content, resp.status_code, content_type="application/json")


@app.route("/hyperview")
@require_login
def hyperview_page(username):
    if "hyperview" not in USERS[username]["systems"]:
        return Response("Your account does not have access to Hyperview", 403)
    body = f"""
    <h1>Hyperview</h1>
    <p class="sub">Live alarm status across Covenant Health hospitals, datacenters, and clinics.
    &middot; <a href="/hyperview/kiosk" target="_blank" rel="noopener">Open kiosk view</a>
    (no login, for a wall display &mdash; opens in a new tab)</p>
    <style>{DASHBOARD_CSS}</style>
    {_hyperview_board_html()}
    {HYPERVIEW_SCRIPT % {'auto_refresh_ms': 30000}}
    """
    return Response(render_shell("Hyperview", body, "hyperview", username), mimetype="text/html")


# Deliberately outside PAGE_SHELL/render_shell and unauthenticated - a
# wall-mounted display has no one there to log in, and shouldn't need a
# browser session kept alive. Dark, oversized treatment for readability
# across a room; own <style> block rather than PAGE_SHELL's light tokens.
# Shared by all three dashboards' /kiosk routes below (and whatever the
# next dashboard's /kiosk route turns out to be) - {title} is what goes
# in the browser tab and the small subtitle under the big Covenant Health
# heading, {board}/{script} are that dashboard's own markup/JS, and
# {rotate} is the auto-rotate badge from _kiosk_rotate_html() below.
DASHBOARD_KIOSK_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - Kiosk</title>
<style>
  :root {{
    --bg: #060b0d; --panel: #0e181b; --panel-raised: #122024; --border: #223338;
    --text: #f2f6f6; --text-dim: #a9bcbc; --text-faint: #7d9494;
    --teal: #6cb2ef; --teal-dark: #8fc0ef; --teal-tint: #163049;
    --danger: #ff5d80; --danger-dark: #ff5d80; --danger-tint: #401323;
    --warn: #f2b447; --ok: #57d999;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
    padding: 28px 34px 40px; font-size: 16px;
  }}
  .kiosk-header {{ display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 20px; }}
  .kiosk-header h1 {{ margin: 0; font-size: 26px; }}
  .kiosk-header .kiosk-subtitle {{ margin-top: 2px; font-size: 15px; color: var(--text-dim); }}
  .kiosk-header .clock {{ font-size: 22px; font-weight: 700; color: var(--text-dim); font-variant-numeric: tabular-nums; }}
  {component_css}
  .panel-head h2 {{ font-size: 18px; color: var(--text); }}
  table.matrix, table.summary, table.alarmlog {{ font-size: 15px; }}
  table.matrix thead th, table.summary thead th, table.alarmlog thead th {{ font-size: 12px; }}
  .clear-mark {{ font-size: 17px; }}
  .gauge-score-num {{ font-size: 40px; }}
  .status-banner {{ font-size: 17px; }}
  @media (prefers-reduced-motion: no-preference) {{
    .sev-dot {{ animation: kiosk-pulse 2.4s ease-in-out infinite; }}
  }}
  @keyframes kiosk-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.45; }} }}
  .kiosk-rotate-badge {{
    position: fixed; bottom: 18px; right: 22px; background: var(--panel);
    border: 1px solid var(--border); border-radius: 999px; padding: 8px 16px;
    font-size: 13px; color: var(--text-dim); box-shadow: 0 4px 14px rgba(0,0,0,0.35);
  }}
  .kiosk-rotate-badge a {{ color: var(--teal); text-decoration: none; margin-left: 6px; }}
  .kiosk-rotate-badge a:hover {{ text-decoration: underline; }}
  /* Unattended wall display, so a color change alone isn't enough - this
     is what kioskCriticalCue() below turns on when the current dashboard
     has any Critical item, a pulsing red vignette around the whole
     screen that reads from across a room even before anyone's close
     enough to read the table it's tied to. */
  body.kiosk-alert::after {{
    content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 9999;
    animation: kiosk-critical-pulse 1.6s ease-in-out infinite;
  }}
  @keyframes kiosk-critical-pulse {{
    0%, 100% {{ box-shadow: inset 0 0 0 0 rgba(255, 93, 128, 0); }}
    50% {{ box-shadow: inset 0 0 0 14px rgba(255, 93, 128, 0.55); }}
  }}
</style>
</head>
<body>
  <script>
    // Shared by every /kiosk page (see kioskCriticalCue call sites in
    // HYPERVIEW_SCRIPT and the ipro/ooma kiosk routes below) - one place
    // that owns the flash + beep so a future dashboard's kiosk route
    // just calls window.kioskCriticalCue(true/false) too, nothing new to
    // wire up here. Wrapped in try/catch because WebAudio can throw if
    // the browser is blocking autoplay before any user gesture on this
    // page - common for a kiosk browser depending on how it's launched;
    // the CSS flash still applies either way, only the beep is at risk.
    (function () {{
      var audioCtx = null;
      var wasCritical = false;
      window.kioskCriticalCue = function (hasCritical) {{
        try {{
          document.body.classList.toggle('kiosk-alert', !!hasCritical);
          if (hasCritical && !wasCritical) {{
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            var osc = audioCtx.createOscillator();
            var gain = audioCtx.createGain();
            osc.frequency.value = 880;
            gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.5);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + 0.5);
          }}
          wasCritical = !!hasCritical;
        }} catch (e) {{ /* autoplay blocked or WebAudio unsupported - flash still ran above */ }}
      }};
    }})();
  </script>
  <div class="kiosk-header">
    <div>
      <h1>Covenant Health &middot; Facility Systems</h1>
      <div class="kiosk-subtitle">{title}</div>
    </div>
    <div class="clock" id="kiosk-clock">--:--:--</div>
  </div>
  {board}
  {script}
  {rotate}
</body>
</html>"""

# Order a wall display cycles through when auto-rotating (see
# _kiosk_rotate_html) and what /kiosk itself starts on - a future
# dashboard's own /<name>/kiosk route just gets appended to both of these
# to join the rotation, nothing else below needs to change.
KIOSK_CYCLE = ["/hyperview/kiosk", "/ipro/kiosk", "/ooma/kiosk"]
KIOSK_LABELS = {
    "/hyperview/kiosk": "Hyperview",
    "/ipro/kiosk": "iPRO Cameras",
    "/ooma/kiosk": "Ooma AirDial",
}
KIOSK_ROTATE_SECONDS = 30


def _kiosk_rotate_html(current_path):
    """Renders the bottom-right badge on a /kiosk page. Stateless by
    design - there's no server-side timer keeping the rotation going,
    just a per-page setTimeout that navigates to the next dashboard in
    KIOSK_CYCLE with ?rotate=1 still attached, so each hop re-arms the
    same countdown on the next page. Landing on any kiosk URL WITHOUT
    ?rotate=1 (typing it directly, or clicking Pause) just stays parked
    there - only /kiosk and links carrying ?rotate=1 start the cycle."""
    idx = KIOSK_CYCLE.index(current_path)
    next_path = KIOSK_CYCLE[(idx + 1) % len(KIOSK_CYCLE)]
    if request.args.get("rotate") != "1":
        return (f'<div class="kiosk-rotate-badge">'
                 f'<a href="{current_path}?rotate=1">&#9654; Start rotating</a></div>')
    next_url = f"{next_path}?rotate=1"
    return f"""
    <div class="kiosk-rotate-badge">
      <span id="kiosk-rotate-countdown">{KIOSK_ROTATE_SECONDS}</span>s &middot;
      next: {_esc(KIOSK_LABELS[next_path])}
      &middot; <a href="{current_path}">Pause</a>
    </div>
    <script>
      (function () {{
        var remaining = {KIOSK_ROTATE_SECONDS};
        var el = document.getElementById('kiosk-rotate-countdown');
        var timer = setInterval(function () {{
          remaining -= 1;
          if (remaining <= 0) {{ clearInterval(timer); return; }}
          if (el) el.textContent = remaining;
        }}, 1000);
        setTimeout(function () {{ location.href = {json.dumps(next_url)}; }}, {KIOSK_ROTATE_SECONDS * 1000});
      }})();
    </script>
    """


# iPRO/Ooma kiosk pages render from the same fixed snapshot as their
# logged-in pages (see IPRO_DASHBOARD/OOMA_DASHBOARD's comment) rather
# than live-fetching like Hyperview's HYPERVIEW_SCRIPT does, so they need
# their own clock tick and - only when NOT auto-rotating, since rotating
# already re-fetches this page fresh on every lap - a periodic reload to
# pick up any change made server-side since the page loaded.
KIOSK_CLOCK_SCRIPT = """
<script>
(function () {
  function tickClock() {
    var el = document.getElementById('kiosk-clock');
    if (el) el.textContent = new Date().toLocaleTimeString([], { hour12: false });
  }
  tickClock();
  setInterval(tickClock, 1000);
  if (!location.search.includes('rotate=1')) {
    setTimeout(function () { location.reload(); }, 30000);
  }
})();
</script>
"""


def _kiosk_critical_script(has_critical):
    """iPRO/Ooma kiosk counterpart to the Hyperview refreshAll() call
    into kioskCriticalCue() - their data is a fixed snapshot computed
    once per request rather than live-polled, so this just calls it once
    on load with whatever the current snapshot says, instead of on every
    fetch like Hyperview does."""
    return f"<script>if (window.kioskCriticalCue) window.kioskCriticalCue({'true' if has_critical else 'false'});</script>"


@app.route("/hyperview/kiosk")
def hyperview_kiosk():
    path = "/hyperview/kiosk"
    return Response(
        DASHBOARD_KIOSK_SHELL.format(
            title="Hyperview",
            component_css=HYPERVIEW_COMPONENT_CSS + DASHBOARD_EXTRA_CSS,
            board=_hyperview_board_html(),
            script=HYPERVIEW_SCRIPT % {'auto_refresh_ms': 15000},
            rotate=_kiosk_rotate_html(path),
        ),
        mimetype="text/html",
    )


@app.route("/ipro/kiosk")
def ipro_kiosk():
    path = "/ipro/kiosk"
    data = _fetch_dashboard_snapshot("ipro", IPRO_DASHBOARD)
    has_critical = any(d["priority"].lower() == "critical" for d in data["devices"])
    return Response(
        DASHBOARD_KIOSK_SHELL.format(
            title="iPRO Cameras",
            component_css=HYPERVIEW_COMPONENT_CSS + DASHBOARD_EXTRA_CSS,
            board=_ipro_board_html(data),
            script=KIOSK_CLOCK_SCRIPT + _kiosk_critical_script(has_critical),
            rotate=_kiosk_rotate_html(path),
        ),
        mimetype="text/html",
    )


@app.route("/ooma/kiosk")
def ooma_kiosk():
    path = "/ooma/kiosk"
    data = _fetch_dashboard_snapshot("ooma", OOMA_DASHBOARD)
    has_critical = any(a["severity"].lower() == "critical" for a in data["alarms"])
    return Response(
        DASHBOARD_KIOSK_SHELL.format(
            title="Ooma AirDial",
            component_css=HYPERVIEW_COMPONENT_CSS + DASHBOARD_EXTRA_CSS,
            board=_ooma_board_html(data),
            script=KIOSK_CLOCK_SCRIPT + _kiosk_critical_script(has_critical),
            rotate=_kiosk_rotate_html(path),
        ),
        mimetype="text/html",
    )


@app.route("/kiosk")
def kiosk_menu():
    """Landing page for a wall display - unauthenticated, same as every
    /*/kiosk route it links to. Lists each dashboard's kiosk view
    individually (for parking a display on just one, same as visiting
    its own /kiosk URL directly) plus a button that starts the
    auto-rotating cycle across all of KIOSK_CYCLE. Bookmark this instead
    of any one dashboard's own /kiosk route - it's the one URL that
    doesn't change as dashboards are added or reordered."""
    items_html = "".join(
        f'<a class="kiosk-menu-item" href="{path}">{_esc(KIOSK_LABELS[path])}</a>'
        for path in KIOSK_CYCLE
    )
    return Response(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kiosk</title>
<style>
  :root {{
    --bg: #060b0d; --panel: #0e181b; --border: #223338;
    --text: #f2f6f6; --text-dim: #a9bcbc; --teal: #6cb2ef; --teal-dark: #8fc0ef;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif;
    display: flex; align-items: center; justify-content: center; padding: 2rem;
  }}
  .kiosk-menu {{ width: 100%; max-width: 420px; display: flex; flex-direction: column; gap: 0.7rem; text-align: center; }}
  .kiosk-menu h1 {{ margin: 0 0 0.3rem; font-size: 1.6rem; }}
  .kiosk-menu .sub {{ margin: 0 0 1rem; color: var(--text-dim); font-size: 0.9rem; }}
  .kiosk-menu-item, .kiosk-menu-rotate {{
    display: block; padding: 0.9rem; border-radius: 10px; border: 1px solid var(--border);
    background: var(--panel); color: var(--text); text-decoration: none; font-weight: 600;
    transition: border-color 0.15s ease;
  }}
  .kiosk-menu-item:hover {{ border-color: var(--teal); }}
  .kiosk-menu-rotate {{ background: var(--teal-dark); color: #06121c; border-color: var(--teal-dark); margin-top: 0.5rem; }}
  .kiosk-menu-rotate:hover {{ opacity: 0.92; }}
</style>
</head>
<body>
  <div class="kiosk-menu">
    <div>
      <h1>Covenant Health &middot; Facility Systems</h1>
      <p class="sub">Pick a dashboard to park on, or auto-rotate through all {len(KIOSK_CYCLE)}.</p>
    </div>
    {items_html}
    <a class="kiosk-menu-rotate" href="{KIOSK_CYCLE[0]}?rotate=1">&#9654; Start rotating (every {KIOSK_ROTATE_SECONDS}s)</a>
  </div>
</body>
</html>""", mimetype="text/html")


@app.route("/logout")
def logout():
    """HTTP Basic auth has no real server-side session to end - the
    browser just re-sends its cached credentials on every request. This
    is the standard workaround: answering with 401 makes most browsers
    discard the cached credentials and re-prompt on the next visit to a
    protected page. It's not universal (some browsers/tabs hang onto
    Basic auth credentials until fully closed regardless), so the page
    also says to close the browser if a stale login persists."""
    return Response(
        render_shell(
            "Logged out",
            '<div class="card"><h3>Logged out</h3>'
            '<p class="sub">You have been logged out. If your browser still shows you as signed in when you '
            'go back to <a href="/maintenance">Maintenance</a> or '
            '<a href="/events-search">Event search</a>, close all browser windows/tabs for this site - '
            'some browsers keep a login active until then.</p></div>',
            "", "",
        ),
        401, {"WWW-Authenticate": 'Basic realm="bridge portal - logged out"'},
    )


@app.route("/")
def index():
    return redirect("/overview")


if __name__ == "__main__":
    port = int(os.environ.get("PORTAL_PORT", "5004"))
    serve(app, host="0.0.0.0", port=port, threads=8)
