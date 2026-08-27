"""
Ooma AirDial -> Grafana bridge.

Polls the Ooma AirDial API on an interval using mTLS (client cert from a
PKCS#12 bundle), caches the latest results in memory, and serves them as
plain JSON over HTTP so Grafana's JSON API datasource
(https://grafana.com/grafana/plugins/marcusolsson-json-datasource/) can
query it directly - no Prometheus/InfluxDB required.

All configuration lives in the CONFIG block below - edit it directly,
there is no separate config file.

Run:
    python ooma.py

Endpoints exposed to Grafana:
    GET /health           -> bridge + last-poll status (for a Grafana "Test" check)
    GET /devices           -> flat list of configured AirDial devices with current status
    GET /devices/{myx_id}  -> single device detail
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import uvicorn

# ============================================================================
# CONFIG - edit these values for your setup.
# ============================================================================

CONFIG = {
    # Device Monitoring Service base URL. Confirmed working - the deck's
    # slide 2 instruction "for all examples, change the base URL to
    # https://monapi.ooma.com/" is the real one; the
    # monapi-frame-external.ingress.ooma.com host from the per-endpoint
    # slides doesn't resolve outside Ooma's own network.
    "dms_url": "https://monapi.ooma.com/api/v1",

    # Path to the PKCS#12 bundle Ooma gave you (your client certificate).
    # Same file you'd load into Postman under Settings > Certificates >
    # Add Certificate as the "PFX file". This one is self-contained (has
    # both the cert and private key), so key_path below stays None.
    "p12_path": "/home/CovAdmin/ooma-airdial-bridge/certs/9d5d6b49-982d-11f1-b50c-40a8f01e1604.p12",

    # If Ooma gave you the private key as a SEPARATE file (e.g. the .p12
    # and a .key came together in one tar.gz), point key_path at it.
    # Leave this None if the .p12 already contains the private key
    # (the case here - confirmed the p12 above is self-contained).
    "key_path": None,

    # Passphrase to unlock the p12. Ooma's tar.gz included this as a plain
    # text file named "p12key" alongside the .p12 - point p12_password_file
    # at it directly (prefer the file over inlining p12_password below, so
    # the secret isn't sitting in this script in plaintext).
    "p12_password_file": "/home/CovAdmin/ooma-airdial-bridge/certs/p12key",
    "p12_password": None,  # e.g. "hunter2" - only used if p12_password_file is None

    # There is no "list all devices" endpoint in Ooma's Monitoring API -
    # every call is scoped to one device by its myx_id (a 6-octet device
    # ID, e.g. "540A14"). List every AirDial unit to monitor here, across
    # every account. "name" is a friendly label for the dashboard; "account"
    # is optional and lets you group/filter by customer account in Grafana -
    # neither is sent to Ooma, both are just for your own organization.
    "devices": [
        # --- Fort Sanders Regional Medical Center (15 devices) ---
        {"myx_id": "7942B8", "name": "Fort Sanders AD7", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "7904B8", "name": "Fort Sanders AD9", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "795FF8", "name": "Fort Sanders AD14", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "794FA8", "name": "Fort Sanders AD13", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "795CA8", "name": "Fort Sanders AD12", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "794428", "name": "Fort Sanders AD11", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "7912E8", "name": "Fort Sanders AD1", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "7947B0", "name": "Fort Sanders AD5", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "7933A8", "name": "Fort Sanders AD8", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "7948CC", "name": "Fort Sanders AD15", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "795CF8", "name": "Fort Sanders AD3", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "794A54", "name": "Fort Sanders AD4", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "794D08", "name": "Fort Sanders AD6", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "790720", "name": "Fort Sanders AD2", "account": "Fort Sanders Regional Medical Center"},
        {"myx_id": "793398", "name": "Fort Sanders AD10", "account": "Fort Sanders Regional Medical Center"},
        # --- Parkwest Medical Center (11 devices) ---
        {"myx_id": "7936E4", "name": "Parkwest Medical AD8", "account": "Parkwest Medical Center"},
        {"myx_id": "7934A0", "name": "Parkwest Medical AD7", "account": "Parkwest Medical Center"},
        {"myx_id": "7904F8", "name": "Parkwest Medical AD11", "account": "Parkwest Medical Center"},
        {"myx_id": "7931D8", "name": "Parkwest Medical AD5", "account": "Parkwest Medical Center"},
        {"myx_id": "790A9C", "name": "Parkwest Medical AD6", "account": "Parkwest Medical Center"},
        {"myx_id": "793104", "name": "Parkwest Medical AD3", "account": "Parkwest Medical Center"},
        {"myx_id": "7909AC", "name": "Parkwest Medical AD2", "account": "Parkwest Medical Center"},
        {"myx_id": "790F84", "name": "Parkwest Medical AD10", "account": "Parkwest Medical Center"},
        {"myx_id": "790B04", "name": "Parkwest Medical AD9", "account": "Parkwest Medical Center"},
        {"myx_id": "792E04", "name": "Parkwest Medical AD1", "account": "Parkwest Medical Center"},
        {"myx_id": "790688", "name": "Parkwest Medical AD4", "account": "Parkwest Medical Center"},
        # --- Claiborne Medical Center (5 devices) ---
        {"myx_id": "79065C", "name": "Claiborne AD1", "account": "Claiborne Medical Center"},
        {"myx_id": "79322C", "name": "Claiborne AD4", "account": "Claiborne Medical Center"},
        {"myx_id": "793074", "name": "Claiborne AD5", "account": "Claiborne Medical Center"},
        {"myx_id": "793270", "name": "Claiborne AD3", "account": "Claiborne Medical Center"},
        {"myx_id": "790670", "name": "Claiborne AD2", "account": "Claiborne Medical Center"},
        # --- LeConte Medical Center (5 devices) ---
        {"myx_id": "791B64", "name": "LeConte Medical AD2", "account": "LeConte Medical Center"},
        {"myx_id": "7942D8", "name": "LeConte Medical AD5", "account": "LeConte Medical Center"},
        {"myx_id": "791C68", "name": "LeConte Medical AD4", "account": "LeConte Medical Center"},
        {"myx_id": "7919BC", "name": "LeConte Medical AD3", "account": "LeConte Medical Center"},
        {"myx_id": "791C74", "name": "LeConte Medical AD1", "account": "LeConte Medical Center"},
        # --- Centerpoint (3 devices) ---
        {"myx_id": "6AB194", "name": "Centerpoint AD2", "account": "Centerpoint"},
        {"myx_id": "6B3198", "name": "Centerpoint AD3", "account": "Centerpoint"},
        {"myx_id": "6ABFD8", "name": "Centerpoint AD1", "account": "Centerpoint"},
        # --- Covenant IT (1 device) ---
        {"myx_id": "6B0488", "name": "Demo Airdial", "account": "Covenant IT"},
        # --- Methodist Medical Center (10 devices) ---
        {"myx_id": "795868", "name": "Methodist Medical AD10", "account": "Methodist Medical Center"},
        {"myx_id": "794930", "name": "Methodist Medical AD8", "account": "Methodist Medical Center"},
        {"myx_id": "794454", "name": "Methodist Medical AD3", "account": "Methodist Medical Center"},
        {"myx_id": "7945E4", "name": "Methodist Medical AD5", "account": "Methodist Medical Center"},
        {"myx_id": "7945A0", "name": "Methodist Medical AD9", "account": "Methodist Medical Center"},
        {"myx_id": "79495C", "name": "Methodist Medical AD2", "account": "Methodist Medical Center"},
        {"myx_id": "794918", "name": "Methodist Medical AD4", "account": "Methodist Medical Center"},
        {"myx_id": "794A50", "name": "Methodist Medical AD7", "account": "Methodist Medical Center"},
        {"myx_id": "794290", "name": "Methodist Medical AD1", "account": "Methodist Medical Center"},
        {"myx_id": "794688", "name": "Methodist Medical AD6", "account": "Methodist Medical Center"},
        # --- Peninsula Hospital (2 devices) ---
        {"myx_id": "792430", "name": "Peninsula AD2", "account": "Peninsula Hospital"},
        {"myx_id": "790468", "name": "Peninsula AD1", "account": "Peninsula Hospital"},
        # --- Fort Loudoun Medical Center (3 devices) ---
        {"myx_id": "791220", "name": "Fort Loudon AD1", "account": "Fort Loudoun Medical Center"},
        {"myx_id": "79297C", "name": "Fort Loudon AD3", "account": "Fort Loudoun Medical Center"},
        {"myx_id": "792970", "name": "Fort Loudon AD2", "account": "Fort Loudoun Medical Center"},
        # --- Covenant Health Roane (5 devices) ---
        {"myx_id": "790708", "name": "Roane AD4", "account": "Covenant Health Roane"},
        {"myx_id": "793070", "name": "Roane AD5", "account": "Covenant Health Roane"},
        {"myx_id": "790A4C", "name": "Roane AD1", "account": "Covenant Health Roane"},
        {"myx_id": "7900D0", "name": "Roane AD2", "account": "Covenant Health Roane"},
        {"myx_id": "7929A4", "name": "Roane AD3", "account": "Covenant Health Roane"},
        # --- Morristown-Hamblen Healthcare System (5 devices) ---
        {"myx_id": "7947DC", "name": "Morristown-Hamblen AD1", "account": "Morristown-Hamblen Healthcare System"},
        {"myx_id": "794CD8", "name": "Morristown-Hamblen AD5", "account": "Morristown-Hamblen Healthcare System"},
        {"myx_id": "794B44", "name": "Morristown-Hamblen AD3", "account": "Morristown-Hamblen Healthcare System"},
        {"myx_id": "794A24", "name": "Morristown-Hamblen AD2", "account": "Morristown-Hamblen Healthcare System"},
        {"myx_id": "794CF8", "name": "Morristown-Hamblen AD4", "account": "Morristown-Hamblen Healthcare System"},
        # --- Cumberland Medical Center (7 devices) ---
        {"myx_id": "793014", "name": "Cumberland AD5", "account": "Cumberland Medical Center"},
        {"myx_id": "792FA0", "name": "Cumberland AD7", "account": "Cumberland Medical Center"},
        {"myx_id": "792CC0", "name": "Cumberland AD2", "account": "Cumberland Medical Center"},
        {"myx_id": "7929E0", "name": "Cumberland AD3", "account": "Cumberland Medical Center"},
        {"myx_id": "793354", "name": "Cumberland AD6-New", "account": "Cumberland Medical Center"},
        {"myx_id": "7923DC", "name": "Cumberland AD4", "account": "Cumberland Medical Center"},
        {"myx_id": "792668", "name": "Cumberland AD1", "account": "Cumberland Medical Center"},
    ],

    # How often to poll EACH device, in seconds (status/fxs/lte every
    # cycle, battery separately throttled below - these run concurrently
    # within a device regardless of max_concurrent_polls, so this is
    # roughly 1 round-trip's worth of wall time per device, not several).
    # Keep this reasonable - it's a live partner API, not a metrics
    # endpoint built for high-frequency scraping.
    "poll_interval_seconds": 60,

    # Battery only actually gets re-fetched this often (default 5
    # minutes) - every other poll_interval_seconds cycle reuses the last
    # cached battery reading instead of calling /components/battery
    # again. Cuts total request volume to Ooma without affecting how
    # fresh status/WAN/LTE data is.
    "battery_poll_interval_seconds": 300,

    # How many DEVICES to poll in parallel (independent of the per-device
    # concurrency above, which always overlaps that device's own
    # requests). Set to 1 to poll strictly one device at a time; raise it
    # to shorten a poll cycle further with many devices, at the cost of
    # more simultaneous load on Ooma's API.
    "max_concurrent_polls": 1,

    # Request timeout in seconds.
    "request_timeout_seconds": 15,

    # Address/port the bridge's own JSON HTTP server listens on.
    # Grafana's JSON API datasource points at this.
    "listen_host": "0.0.0.0",
    "listen_port": 5003,

    # --- Maintenance windows -----------------------------------------
    # Same model as the iPro bridge's /maintenance page: an operator
    # marks a device (or a whole account) as acknowledged for a set
    # number of hours. While active, that device drops out of /issues,
    # /accounts, /category-summary, /overall-health, /operational-status,
    # and /sites-affected entirely - as if healthy - but /devices still
    # shows its real, unsuppressed status. If it hasn't actually
    # recovered by the time the window expires, the real status reappears
    # immediately - nothing about polling/issue-detection itself pauses.
    "maintenance_file": "/home/CovAdmin/ooma-airdial-bridge/maintenance_windows.json",
    "maintenance_max_hours": 168,  # 7 days

    # Same login as the iPro bridge's /maintenance page (chadmin /
    # 3Xce11@nce) - change this if you want Ooma's page to use a
    # different credential than iPro's.
    "maintenance_auth": "Basic Y2hhZG1pbjozWGNlMTFAbmNl",

    # --- Device event history --------------------------------------
    # Every time a device's top issue in a group (Connectivity/Battery)
    # changes - a new issue starts, an existing one's message/severity
    # changes (e.g. temporarily disconnected -> flapping -> recovered),
    # or one clears - a row goes into this SQLite file. Plain stdlib
    # sqlite3, no separate database server: a device this active
    # (see /events-search) still only produces a few thousand rows over
    # events_retention_days, well within what a single SQLite file
    # handles without any tuning.
    "events_db_path": "/home/CovAdmin/ooma-airdial-bridge/ooma_events.db",
    "events_retention_days": 90,

    # Where the combined maintenance/event-search portal lives - /maintenance
    # and /events-search below redirect here instead of serving their own
    # HTML, now that the portal is the one login surface for both this
    # bridge and the ipro bridge. Leave blank to fall back to a plain text
    # notice instead of a redirect (e.g. if the portal isn't deployed yet).
    "portal_url": os.environ.get("BRIDGE_PORTAL_URL", ""),
}

# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ooma-airdial-bridge")


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def read_p12_password(config: dict) -> bytes:
    if config.get("p12_password_file"):
        return Path(config["p12_password_file"]).read_text().strip().encode()
    if config.get("p12_password"):
        return str(config["p12_password"]).encode()
    raise ValueError("CONFIG must set either p12_password or p12_password_file")


# --------------------------------------------------------------------------
# mTLS client cert handling
#
# `requests` doesn't speak PKCS#12 directly, so we unpack the .p12 once at
# startup into PEM cert/key files in a private temp dir and hand those paths
# to requests via `cert=(certfile, keyfile)`. The temp dir is 0700 and the
# files are removed on process exit.
#
# Ooma sometimes ships the private key as a SEPARATE file alongside the
# .p12 (e.g. both inside one tar.gz) rather than embedding it in the p12
# bundle. If `key_path` is set in CONFIG, that file is used for the private
# key instead of whatever the p12 itself contains.
# --------------------------------------------------------------------------

class ClientCert:
    def __init__(self, p12_path: str, password: bytes, key_path: str | None = None):
        p12_private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
            Path(p12_path).read_bytes(), password
        )
        if certificate is None:
            raise ValueError(f"{p12_path} did not contain a certificate.")

        if key_path:
            private_key = self._load_private_key(Path(key_path).read_bytes(), password)
        else:
            private_key = p12_private_key
            if private_key is None:
                raise ValueError(
                    f"{p12_path} did not contain a private key, and CONFIG['key_path'] "
                    "is not set. If Ooma gave you the key as a separate file, set "
                    "key_path to point at it."
                )

        self._tmpdir = tempfile.TemporaryDirectory(prefix="ooma-airdial-cert-")
        tmp = Path(self._tmpdir.name)
        tmp.chmod(0o700)

        self.keyfile = tmp / "client.key.pem"
        self.keyfile.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.keyfile.chmod(0o600)

        self.certfile = tmp / "client.cert.pem"
        pem = certificate.public_bytes(serialization.Encoding.PEM)
        for extra in additional_certs or []:
            pem += extra.public_bytes(serialization.Encoding.PEM)
        self.certfile.write_bytes(pem)
        self.certfile.chmod(0o600)

        log.info("Loaded client certificate from %s (subject=%s)", p12_path, certificate.subject)

    @staticmethod
    def _load_private_key(key_bytes: bytes, password: bytes):
        """Load a standalone private key file - PEM or DER, encrypted or not."""
        loaders = (
            (serialization.load_pem_private_key, password),
            (serialization.load_pem_private_key, None),
            (serialization.load_der_private_key, password),
            (serialization.load_der_private_key, None),
        )
        last_exc: Exception | None = None
        for loader, pwd in loaders:
            try:
                return loader(key_bytes, password=pwd)
            except (ValueError, TypeError) as exc:
                last_exc = exc
        raise ValueError(f"Could not load private key from key_path: {last_exc}")

    @property
    def cert_tuple(self) -> tuple[str, str]:
        return (str(self.certfile), str(self.keyfile))

    def close(self) -> None:
        self._tmpdir.cleanup()


# --------------------------------------------------------------------------
# Ooma Monitoring API client (Device Monitoring Service / "DMS")
#
# Per Ooma's partner API docs: there is no bulk "list devices" endpoint -
# every call is scoped to one device by its myx_id (a 6-octet device
# identifier, e.g. "540A14"). Auth is mTLS only (the client cert from the
# .p12 bundle) - no bearer token or API key on top of it.
#
# Endpoints (all GET, under {dms_url} = https://monapi.ooma.com/api/v1):
#   /devices/{myx_id}                    overall status
#   /devices/{myx_id}/components         list of component names (fxs, lte, battery)
#   /devices/{myx_id}/components/battery battery level/state
#   /devices/{myx_id}/components/fxs     phone line (FXS port) hook status
#   /devices/{myx_id}/components/lte     cellular modem status
# --------------------------------------------------------------------------

class OomaAirDialClient:
    # One requests.Session per worker thread, not shared across threads.
    #
    # The poller runs devices through a ThreadPoolExecutor, and multiple
    # threads doing concurrent mTLS handshakes through a single shared
    # requests.Session's connection pool has been observed to segfault
    # (native crash inside OpenSSL/cryptography's Rust bindings, not a
    # catchable Python exception) - a known class of issue on some
    # platforms, ARM/Raspberry Pi included. threading.local() gives each
    # thread its own Session/SSL state instead.

    def __init__(self, dms_url: str, cert: ClientCert, timeout: int):
        self.dms_url = dms_url.rstrip("/")
        self.timeout = timeout
        self.cert_tuple = cert.cert_tuple
        self._local = threading.local()
        # Fires a single device's 4 component calls (status/battery/fxs/lte)
        # at once instead of back-to-back. This is independent of how many
        # devices Poller polls at a time (CONFIG["max_concurrent_polls"]) -
        # even with that set to 1 (one device in flight at a time), each
        # device's own 4 requests still overlap, which is most of the wall
        # -clock win: 4 sequential round-trips per device becomes ~1.
        self._request_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ooma-req")

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.cert = self.cert_tuple
            self._local.session = session
        return session

    def _get(self, path: str) -> dict:
        resp = self.session.get(f"{self.dms_url}{path}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_device_status(self, myx_id: str) -> dict:
        # e.g. {"type": "Ooma AirDial", "status": "IN_SERVICE",
        #       "activeWAN": "ETHERNET", "uptime": 172765, "powerState": "AC"}
        return self._get(f"/devices/{myx_id}")

    def get_battery(self, myx_id: str) -> dict:
        # e.g. {"level": 100, "state": "FULL"}
        return self._get(f"/devices/{myx_id}/components/battery")

    def get_fxs(self, myx_id: str) -> dict:
        # e.g. {"lines": [{"name": "port1", "hookStatus": "ON_HOOK", "vnum": "..."}]}
        return self._get(f"/devices/{myx_id}/components/fxs")

    def get_lte(self, myx_id: str) -> dict:
        # e.g. {"modems": [{"quality": "EXCELLENT", "status": "ACTIVE",
        #       "sinr": 17.1, "rsrp": -87, "rsrq": -11, "rssi": 52,
        #       "band": "B4", "imei": "...", "carrierStatus": "UP", "slot": 0}]}
        return self._get(f"/devices/{myx_id}/components/lte")

    @staticmethod
    def _optional_component(getter, myx_id: str) -> dict:
        """Run a component getter, treating 404 (component not present on
        this device) as an empty result rather than a failure."""
        try:
            return getter(myx_id)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            return {}

    def _lte_component(self, myx_id: str) -> tuple[dict, bool]:
        try:
            return self.get_lte(myx_id), True  # 200 = this device has LTE hardware
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            return {}, False  # 404 = no LTE component on this device at all

    def poll_device(
        self, myx_id: str, name: str, account: str | None = None, poll_battery: bool = True
    ) -> dict:
        """Fetch component calls CONCURRENTLY and merge into one flat
        record. status must succeed; battery/fxs/lte missing (404) is
        normal, not a failure. poll_battery=False skips the battery
        request entirely (battery_level_percent/battery_state come back
        None) - Poller fills those in from its own cache in that case, per
        battery_poll_interval_seconds."""
        status_future = self._request_pool.submit(self.get_device_status, myx_id)
        battery_future = (
            self._request_pool.submit(self._optional_component, self.get_battery, myx_id)
            if poll_battery else None
        )
        fxs_future = self._request_pool.submit(self._optional_component, self.get_fxs, myx_id)
        lte_future = self._request_pool.submit(self._lte_component, myx_id)

        # Each sub-call already has its own socket-level timeout
        # (self.timeout, applied inside _get() via requests' timeout=
        # kwarg), but that alone doesn't protect the CALLER: a hang
        # requests' timeout doesn't cleanly bound - a stalled DNS lookup,
        # a connection a firewall/NAT drops silently rather than
        # resetting, a peer that keeps the TCP connection open without
        # ever sending or closing - previously left .result() blocking
        # forever with no timeout of its own. That's exactly what let one
        # stuck device wedge Poller._poll_once()'s entire as_completed()
        # loop (see there), and with it the whole poller thread, since
        # _run() polls in one single sequential loop - the "hangs until
        # the service is restarted" failure. result_timeout gives every
        # future call a hard ceiling independent of whatever's actually
        # wrong on the wire; a little slack over self.timeout so a
        # legitimately-slow-but-still-completing request isn't cut off
        # right as requests' own timeout is about to handle it cleanly.
        result_timeout = self.timeout + 5
        status = status_future.result(timeout=result_timeout)
        battery = battery_future.result(timeout=result_timeout) if battery_future else {}
        fxs = fxs_future.result(timeout=result_timeout)
        lte, has_lte = lte_future.result(timeout=result_timeout)

        return self._normalize_device(myx_id, name, account, status, battery, fxs, lte, has_lte)

    @staticmethod
    def _normalize_device(
        myx_id: str, name: str, account: str | None, status: dict, battery: dict, fxs: dict,
        lte: dict, has_lte: bool,
    ) -> dict:
        lines = fxs.get("lines", [])
        modems = lte.get("modems", [])
        primary_modem = modems[0] if modems else {}

        record = {
            "myx_id": myx_id,
            "name": name or myx_id,
            "account": account,
            "type": status.get("type"),
            "status": status.get("status", "UNKNOWN"),
            "active_wan": status.get("activeWAN"),
            "uptime_seconds": status.get("uptime"),
            "power_state": status.get("powerState"),
            "on_battery": status.get("powerState") == "BATTERY",
            "battery_level_percent": battery.get("level"),
            "battery_state": battery.get("state"),
            "fxs_lines": [
                {"name": l.get("name"), "hook_status": l.get("hookStatus"), "vnum": l.get("vnum")}
                for l in lines
            ],
            "fxs_lines_off_hook": sum(1 for l in lines if l.get("hookStatus") == "OFF_HOOK"),
            "has_lte": has_lte,  # True if this device has LTE hardware at all (even if 0 modems currently registered)
            "lte_modem_count": len(modems),
            "lte_quality": primary_modem.get("quality"),
            "lte_status": primary_modem.get("carrierStatus"),  # e.g. "UP"/"DOWN" - the carrier link state, not the modem's own operational state
            "lte_rssi": primary_modem.get("rssi"),
            "lte_sinr": primary_modem.get("sinr"),
            "lte_rsrp": primary_modem.get("rsrp"),
            "lte_rsrq": primary_modem.get("rsrq"),
            "lte_band": primary_modem.get("band"),
            "lte_carrier_status": primary_modem.get("carrierStatus"),
        }

        # Ooma's own `status` field only reflects whether the unit is
        # reachable/registered - a Wired-primary unit with a dead LTE
        # backup (or an LTE-primary unit whose LTE is dead) can report
        # IN_SERVICE while functionally non-operational. `effective_status`
        # doesn't just trust that field - a device with a "lte" (or
        # "service") issue reads as DOWN outright, not merely degraded.
        # get_device_issues/compute_effective_status are defined further
        # down in this file but resolved at call time, so this works fine.
        record["effective_status"] = compute_effective_status(get_device_issues(record))
        return record


# --------------------------------------------------------------------------
# Device event history (SQLite - see CONFIG["events_db_path"])
# --------------------------------------------------------------------------

def _open_events_db(db_path: str) -> sqlite3.Connection:
    """One connection, reused for the process lifetime and always
    accessed under Poller._events_lock - sqlite3 connections aren't
    safe for concurrent use from multiple threads without external
    serialization, and this bridge writes from poll worker threads and
    reads from FastAPI request threads. Volume here (a few thousand rows
    over events_retention_days, per the CONFIG comment) doesn't come
    close to needing anything fancier than one lock."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            myx_id TEXT NOT NULL,
            device TEXT,
            account TEXT,
            group_name TEXT NOT NULL,
            old_severity TEXT,
            old_message TEXT,
            new_severity TEXT,
            new_message TEXT,
            changed_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_myx_id ON events(myx_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_account ON events(account)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_changed_at ON events(changed_at)")
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Background poller + in-memory cache
# --------------------------------------------------------------------------

class Poller:
    def __init__(
        self,
        client: OomaAirDialClient,
        devices: list[dict],
        interval_seconds: int,
        max_concurrent_polls: int = 8,
        battery_poll_interval_seconds: int = 60,
        events_db_path: str | None = None,
        events_retention_days: int = 90,
    ):
        self.client = client
        self.configured_devices = devices  # [{"myx_id": ..., "name": ..., "account": ...}, ...]
        self.interval_seconds = interval_seconds
        self.max_concurrent_polls = max(1, max_concurrent_polls)
        self.battery_poll_interval_seconds = battery_poll_interval_seconds
        self._lock = threading.Lock()
        self._devices: list[dict] = []
        self._last_poll_ts: float | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()

        # Event history: one row per (device, group) whenever its top
        # issue changes - a new issue starting, an existing one's
        # message/severity changing, or one clearing. See
        # _record_group_change and query_events. None disables history
        # entirely (no events_db_path configured).
        self._events_retention_days = events_retention_days
        self._events_lock = threading.Lock()
        self._events_conn = _open_events_db(events_db_path) if events_db_path else None
        self._last_group_issues: dict[str, dict[str, dict]] = {}  # myx_id -> {group: {"severity", "message"}}
        self._last_prune_ts: float = 0.0

        # Per-device battery cache, so battery only actually gets polled
        # every battery_poll_interval_seconds even though the main poll
        # loop runs every interval_seconds. Separate lock since it's
        # touched from worker threads (one per device being polled), not
        # just the poller thread.
        self._battery_lock = threading.Lock()
        self._battery_cache: dict[str, dict] = {}  # myx_id -> {"battery_level_percent": ..., "battery_state": ...}
        self._battery_last_polled: dict[str, float] = {}
        # Consecutive REAL battery polls (not every interval_seconds
        # cycle) that read battery_state == "CHARGING" - see
        # BATTERY_CHARGING_STREAK_REQUIRED. Reset to 0 on any poll that
        # doesn't read CHARGING. Shares _battery_lock since it's only
        # ever touched alongside the battery cache above.
        self._battery_charge_streak: dict[str, int] = {}

        # LTE flap tracking, one entry per device with LTE hardware - see
        # _update_lte_flap_state for the state machine. Its own lock
        # since it's updated once per device per real poll cycle from
        # worker threads, independent of the battery bookkeeping above.
        self._lte_lock = threading.Lock()
        self._lte_flap_trackers: dict[str, dict] = {}
        # Chronic flapper episode timestamps, one list per device - see
        # _update_lte_flap_state. Separate from _lte_flap_trackers above:
        # that dict's "transitions" list only covers a single episode
        # (cleared once a flap resolves), so it can't tell "flapped once
        # today" from "flapped 10 times today" on its own.
        self._chronic_flapper_episodes: dict[str, list[float]] = {}

        # Chronic connectivity instability, one entry per device - see
        # _update_connectivity_instability. Independent of the LTE-specific
        # tracker above: this watches the CONNECTIVITY GROUP's top issue as
        # a whole (service/wan/lte all count), so it catches a device
        # cycling between Critical "Out of service" and Degraded "Running
        # on cellular backup" too, not just LTE carrier flaps.
        self._instability_lock = threading.Lock()
        self._instability_trackers: dict[str, dict] = {}

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="ooma-poller", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.interval_seconds)

    def _should_poll_battery(self, myx_id: str) -> bool:
        with self._battery_lock:
            last = self._battery_last_polled.get(myx_id)
        return last is None or (time.time() - last) >= self.battery_poll_interval_seconds

    def _update_lte_flap_state(self, myx_id: str, connected: bool) -> tuple[str | None, bool, bool]:
        """Returns (flap_state, sustained_outage, chronic_flapper). One call per device per
        real poll cycle (from _poll_one_device
        only) - never from get_device_issues, which can run several times
        per cycle across different endpoints and would otherwise see the
        same sample repeatedly and miscount transitions.

        State machine: any change in `connected` since the last poll is a
        transition, timestamped and kept for LTE_FLAP_WINDOW_SECONDS.
        LTE_FLAP_TRANSITION_THRESHOLD+ transitions in that window means
        "flapping" - but once triggered, whether it STAYS "flapping" is
        judged by recency (time since the last transition), not by how
        many old transitions are still sitting in the 30-min window: a
        device that flapped 4 times and then sat still for 20 minutes
        should read as stable well before those old transitions age out
        of the window. Once stable (no new transition) for
        LTE_FLAP_RECOVERY_STABLE_SECONDS while connected, it shows
        "recovering" for exactly one call (this one) - the NEXT call,
        still stable, clears the tracker back to a clean slate. If it
        instead goes quiet while still disconnected, that's not a
        recovery - it's just plainly down now, so the tracker clears to
        None and get_device_issues' ordinary "not connected" branch takes
        over instead of holding the "flapping" label forever.

        sustained_outage is a separate, simpler flag: true only when
        flap_state is None (not flapping, not just-recovered) AND
        currently disconnected AND that's been true continuously for at
        least LTE_SUSTAINED_OUTAGE_SECONDS - see get_device_issues for
        what that changes about the message.

        chronic_flapper is true once this device has newly ENTERED
        "flapping" (not just continued in it) CHRONIC_FLAPPER_EPISODE_
        THRESHOLD+ separate times within the trailing CHRONIC_FLAPPER_
        WINDOW_SECONDS - a device that keeps flapping, resolving, and
        flapping again all day is a hardware/carrier-swap candidate, not
        a one-off blip, even though each individual episode still clears
        itself normally per the state machine above."""
        now = time.time()
        with self._lte_lock:
            tracker = self._lte_flap_trackers.setdefault(myx_id, {
                # last_change_at starts at "now" on a brand-new tracker,
                # not None - it means "since when have we observed the
                # current connected/disconnected state," not strictly
                # "since the last transition." A device already
                # disconnected the very first time this bridge sees it
                # needs a baseline to measure a sustained outage from too,
                # not just ones that transition while being watched.
                "transitions": [], "last_connected": None, "last_change_at": now, "flap_state": None,
            })
            prev_state = tracker["flap_state"]

            if tracker["last_connected"] is not None and connected != tracker["last_connected"]:
                tracker["transitions"].append(now)
                tracker["last_change_at"] = now
                tracker["flap_state"] = None
            tracker["last_connected"] = connected

            tracker["transitions"] = [t for t in tracker["transitions"] if now - t <= LTE_FLAP_WINDOW_SECONDS]
            flapping_now = len(tracker["transitions"]) >= LTE_FLAP_TRANSITION_THRESHOLD
            stable_seconds = (now - tracker["last_change_at"]) if tracker["last_change_at"] is not None else None

            # "recovering" always advances to cleared on the very next
            # call, no matter what flapping_now says - checked first, and
            # unconditionally, so stale transitions still sitting in the
            # 30-min window (which would otherwise keep flapping_now true
            # and re-derive "recovering" forever) can't stall it there.
            if tracker["flap_state"] == "recovering":
                tracker["flap_state"] = None
                tracker["transitions"] = []
            elif tracker["flap_state"] == "flapping" or flapping_now:
                if stable_seconds is not None and stable_seconds >= LTE_FLAP_RECOVERY_STABLE_SECONDS:
                    tracker["flap_state"] = "recovering" if connected else None
                else:
                    tracker["flap_state"] = "flapping"
            elif not connected:
                tracker["flap_state"] = None

            sustained_outage = (
                tracker["flap_state"] is None and not connected
                and (now - tracker["last_change_at"]) >= LTE_SUSTAINED_OUTAGE_SECONDS
            )

            # Episode start = newly entering "flapping" this call, not
            # merely continuing in it - so a single ongoing flap only
            # counts once, no matter how many polls it stays flapping for.
            if tracker["flap_state"] == "flapping" and prev_state != "flapping":
                episodes = self._chronic_flapper_episodes.setdefault(myx_id, [])
                episodes.append(now)
                self._chronic_flapper_episodes[myx_id] = [
                    t for t in episodes if now - t <= CHRONIC_FLAPPER_WINDOW_SECONDS
                ]
            episode_count = len(self._chronic_flapper_episodes.get(myx_id, []))
            chronic_flapper = episode_count >= CHRONIC_FLAPPER_EPISODE_THRESHOLD

            return tracker["flap_state"], sustained_outage, chronic_flapper

    def _update_connectivity_instability(self, myx_id: str, current_top: dict | None) -> bool:
        """One call per device per real poll cycle, same discipline as
        _update_lte_flap_state - and deliberately independent of it. This
        tracks the CONNECTIVITY GROUP's top issue as a whole (whichever
        of service/wan/lte is currently worst), not carrier state
        specifically - a device cycling between Critical "Out of
        service" and Degraded "Running on cellular backup" is a
        status/activeWAN flip, never an LTE carrierStatus change, so
        _update_lte_flap_state never sees it at all.

        current_top is {"category","severity","message"} or None (no
        connectivity issue right now) - compared by (severity, message)
        so a message-only change (e.g. one LTE wording to another) still
        counts as a change, same as a severity flip. CHRONIC_INSTABILITY_
        THRESHOLD+ changes within CHRONIC_INSTABILITY_WINDOW_SECONDS
        marks it unstable; ages out naturally as old changes fall out of
        the window, no separate "recovering" step like the LTE tracker
        has - this is a slower-moving signal, not one that needs its own
        confirmation delay."""
        now = time.time()
        current_key = (current_top["severity"], current_top["message"]) if current_top else None
        with self._instability_lock:
            tracker = self._instability_trackers.setdefault(myx_id, {"changes": [], "last_key": None})
            if tracker["last_key"] is not None and current_key != tracker["last_key"]:
                tracker["changes"].append(now)
            tracker["last_key"] = current_key
            tracker["changes"] = [t for t in tracker["changes"] if now - t <= CHRONIC_INSTABILITY_WINDOW_SECONDS]
            return len(tracker["changes"]) >= CHRONIC_INSTABILITY_THRESHOLD

    def _poll_one_device(self, entry: dict) -> dict:
        myx_id = entry["myx_id"]
        poll_battery = self._should_poll_battery(myx_id)
        record = self.client.poll_device(
            myx_id, entry.get("name", myx_id), entry.get("account"), poll_battery=poll_battery
        )
        with self._battery_lock:
            if poll_battery:
                self._battery_cache[myx_id] = {
                    "battery_level_percent": record["battery_level_percent"],
                    "battery_state": record["battery_state"],
                }
                self._battery_last_polled[myx_id] = time.time()
                streak = self._battery_charge_streak.get(myx_id, 0)
                streak = streak + 1 if record["battery_state"] == "CHARGING" else 0
                self._battery_charge_streak[myx_id] = streak
            else:
                cached = self._battery_cache.get(myx_id, {})
                record["battery_level_percent"] = cached.get("battery_level_percent")
                record["battery_state"] = cached.get("battery_state")
            record["battery_charging_streak"] = self._battery_charge_streak.get(myx_id, 0)

        if record.get("has_lte"):
            connected = record.get("lte_modem_count", 0) > 0 and record.get("lte_carrier_status") == "UP"
            record["lte_flap_status"], record["lte_sustained_outage"], record["lte_chronic_flapper"] = (
                self._update_lte_flap_state(myx_id, connected)
            )
        else:
            record["lte_flap_status"] = None
            record["lte_sustained_outage"] = False
            record["lte_chronic_flapper"] = False

        # connectivity_unstable needs a provisional read of the
        # connectivity group's top issue BEFORE it's known - it's not set
        # on the record yet at this point, so this first pass reads as
        # "not unstable" by default. That's fine: the marker it adds only
        # touches the message text, never severity or category, so it
        # can't change which issue pick_top_issues_by_group would pick as
        # top in the first place.
        provisional_by_group = {
            CATEGORY_GROUP[i["category"]]: i for i in pick_top_issues_by_group(get_device_issues(record))
        }
        connectivity_top = provisional_by_group.get("connectivity")
        record["connectivity_unstable"] = self._update_connectivity_instability(myx_id, connectivity_top)

        # Recomputed now that lte_flap_status/battery_charging_streak/
        # connectivity_unstable are actually set - the passes above ran
        # before any of them existed on the record, so they under-reported.
        issues = get_device_issues(record)
        record["effective_status"] = compute_effective_status(issues)

        if self._events_conn is not None:
            self._record_group_changes(myx_id, record, issues)

        return record

    def _record_group_changes(self, myx_id: str, record: dict, issues: list[dict]) -> None:
        """Diffs this poll's top-issue-per-group against the last poll's,
        one comparison per GROUP_CATEGORIES entry, and writes a row for
        anything that changed - including a group clearing entirely
        (old set, new None - a recovery). Deliberately off raw
        get_device_issues(), not effective_issues() - a history log
        should show what actually happened to the device, not what a
        maintenance window happened to be hiding from the dashboard at
        the time. Runs from _poll_one_device only (one real poll per
        device per cycle), same discipline as the LTE flap tracker, so a
        device isn't double-logged by /issues and /category-summary both
        asking for its issues within the same cycle."""
        # Normalized to exactly {"severity", "message"} - pick_top_issues_by_group
        # returns the raw issue dicts (which also carry "category"), and
        # comparing one of those against the {"severity","message"}-only
        # dict this same method stores as last-known would never compare
        # equal even when nothing actually changed, logging a spurious
        # no-op "changed" row on every single poll.
        current_by_group = {
            CATEGORY_GROUP[i["category"]]: {"severity": i["severity"], "message": i["message"]}
            for i in pick_top_issues_by_group(issues)
        }
        previous_by_group = self._last_group_issues.get(myx_id, {})
        now = time.time()

        rows = []
        for group in GROUP_CATEGORIES:
            old = previous_by_group.get(group)
            new = current_by_group.get(group)
            if old == new:
                continue
            rows.append((
                myx_id, record.get("name"), record.get("account"), group,
                old["severity"] if old else None, old["message"] if old else None,
                new["severity"] if new else None, new["message"] if new else None,
                now,
            ))

        self._last_group_issues[myx_id] = current_by_group

        if not rows:
            return
        with self._events_lock:
            self._events_conn.executemany(
                "INSERT INTO events "
                "(myx_id, device, account, group_name, old_severity, old_message, new_severity, new_message, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._events_conn.commit()
            self._prune_events_if_due(now)

    def _prune_events_if_due(self, now: float) -> None:
        """Called only while already holding _events_lock (from
        _record_group_changes) - checked at most once per day, so this
        never runs on every single poll cycle."""
        if now - self._last_prune_ts < 86400:
            return
        cutoff = now - self._events_retention_days * 86400
        self._events_conn.execute("DELETE FROM events WHERE changed_at < ?", (cutoff,))
        self._events_conn.commit()
        self._last_prune_ts = now

    def query_events(
        self, *, myx_id: str | None = None, account: str | None = None, group: str | None = None,
        since: float | None = None, until: float | None = None, limit: int = 200,
    ) -> list[dict]:
        if self._events_conn is None:
            return []
        query = (
            "SELECT id, myx_id, device, account, group_name, old_severity, old_message, "
            "new_severity, new_message, changed_at FROM events WHERE 1=1"
        )
        params: list[Any] = []
        if myx_id:
            query += " AND myx_id = ?"
            params.append(myx_id)
        if account:
            query += " AND account = ?"
            params.append(account)
        if group:
            query += " AND group_name = ?"
            params.append(group)
        if since is not None:
            query += " AND changed_at >= ?"
            params.append(since)
        if until is not None:
            query += " AND changed_at <= ?"
            params.append(until)
        query += " ORDER BY changed_at DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))

        with self._events_lock:
            cur = self._events_conn.execute(query, params)
            columns = [c[0] for c in cur.description]
            rows = cur.fetchall()

        return [dict(zip(columns, row)) for row in rows]

    def _poll_once(self) -> None:
        polled: list[dict] = []
        errors: list[str] = []

        # Not a `with ThreadPoolExecutor(...) as pool:` on purpose - that
        # context manager's __exit__ calls shutdown(wait=True), which
        # blocks until every submitted task finishes. If even one is
        # still stuck on a hung request past cycle_timeout below, that
        # would just move the hang from the as_completed() loop to here
        # instead of actually fixing it. shutdown() is called explicitly
        # in the finally block below, with wait=False, so a stuck worker
        # can't block this method's return.
        pool = ThreadPoolExecutor(max_workers=self.max_concurrent_polls)
        try:
            future_to_myx_id = {
                pool.submit(self._poll_one_device, entry): entry["myx_id"]
                for entry in self.configured_devices
            }
            # Same defense-in-depth reasoning as poll_device()'s
            # result_timeout (see there): every component call already
            # has its own socket-level timeout, but as_completed() itself
            # has no ceiling by default - one device stuck past its own
            # timeout would otherwise block this loop, and with it this
            # Poller's single _run() thread, forever. cycle_timeout gives
            # the WHOLE cycle a hard ceiling independent of what's
            # actually wrong with any one device.
            cycle_timeout = self.client.timeout + 30
            try:
                for future in as_completed(future_to_myx_id, timeout=cycle_timeout):
                    myx_id = future_to_myx_id[future]
                    try:
                        polled.append(future.result())
                    except (requests.RequestException, FuturesTimeoutError) as exc:
                        log.warning("Poll failed for device %s: %s", myx_id, exc)
                        errors.append(f"{myx_id}: {exc}")
            except FuturesTimeoutError:
                # Devices that already finished are still in `polled` -
                # only the ones that never came back within cycle_timeout
                # get dropped from this cycle. Their worker thread(s)
                # remain running in the background (Python can't cancel a
                # thread that's already executing); pool.shutdown(wait=False)
                # below doesn't wait on them, so they can't hold up this
                # or any future cycle - they'll exit on their own once
                # whatever's actually hung underneath finally gives up.
                still_pending = sorted(myx_id for f, myx_id in future_to_myx_id.items() if not f.done())
                log.error(
                    "Poll cycle exceeded %ss with %d device(s) still not responding (%s) - "
                    "moving on rather than blocking the poller thread",
                    cycle_timeout, len(still_pending), ", ".join(still_pending),
                )
                errors.extend(f"{myx_id}: poll cycle timed out after {cycle_timeout}s" for myx_id in still_pending)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        with self._lock:
            if polled:
                self._devices = polled
            self._last_error = "; ".join(errors) if errors else None
            self._last_poll_ts = time.time()
        log.info("Polled %d/%d device(s) from Ooma AirDial API", len(polled), len(self.configured_devices))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "devices": list(self._devices),
                "last_poll_ts": self._last_poll_ts,
                "last_error": self._last_error,
            }


# --------------------------------------------------------------------------
# Issue detection + rollups
#
# Mirrors the shape of the existing Hyperview matrix.py bridge (same idea:
# one row per site/account with a count per category, plus a flat log of
# only the currently-flagged devices) so the same panel patterns/JSON used
# for the Hyperview dashboard can be reused here - point them at these
# paths instead and adjust field names.
#
# Hyperview has an "acknowledged" state (alarms get ack'd by a human in
# their system) that Ooma's API has no equivalent of, so there's no
# open/ack split here - just present vs not.
# --------------------------------------------------------------------------

ISSUE_CATEGORIES = ("service", "wan", "lte", "battery")

# Display grouping: service/wan/lte all collapse into one "connectivity"
# column/label everywhere they're shown to the user (accounts rollup,
# category summary, issues table) - they're the same underlying signal
# (the unit can't communicate), just different root causes. Doesn't touch
# get_device_issues()'s messages (still specific) or compute_effective_status()
# (still tracks the DOWN-vs-DEGRADED distinction off the raw categories).
# Field NAMES built from these keys (e.g. "connectivity_issues") stay
# lowercase; only display VALUES (the "category" shown in /issues, and
# these labels) are capitalized.
CATEGORY_GROUP = {"service": "connectivity", "wan": "connectivity", "lte": "connectivity", "battery": "battery"}
GROUP_CATEGORIES = ("connectivity", "battery")
GROUP_META = {
    "connectivity": "\U0001F6DC Connectivity",  # 🛜
    "battery": "\U0001F50B Battery",  # 🔋
}

# Critical: the unit is entirely out of service, or its battery has
# dropped to BATTERY_CRITICAL_PERCENT or below (about to lose backup
# power entirely). Everything else - cellular failover, an LTE problem, a
# WAN reading that's merely missing, a battery that's low but not yet
# critical - is Degraded: worth knowing about, but the unit is still
# working.
_SEVERITY_RANK = {"Critical": 2, "Degraded": 1}
BATTERY_DEGRADED_PERCENT = 75
BATTERY_CRITICAL_PERCENT = 20

# LTE flap tracking (see Poller._update_lte_flap_state): 3+ up/down
# transitions within a 30-minute window counts as flapping, held there
# until it's been stable (no further transitions) for 15 minutes, at
# which point it shows as recovering for exactly one more poll cycle
# before dropping out of /issues entirely.
LTE_FLAP_TRANSITION_THRESHOLD = 3
LTE_FLAP_WINDOW_SECONDS = 1800
LTE_FLAP_RECOVERY_STABLE_SECONDS = 900

# A carrier that's been continuously disconnected (no flapping, just
# steadily down) for this long stops reading as "temporarily
# disconnected" and switches to prolonged-outage wording instead - a
# fixed relabel at the threshold, not a live duration counter (a message
# that keeps changing every poll would make ooma_alert.sh re-alert on
# every single run). Severity stays Degraded either way - the unit is
# still working, same as an indefinite WAN-unavailable/cellular-failover
# reading.
LTE_SUSTAINED_OUTAGE_SECONDS = 3600

# Chronic flapper escalation (see Poller._update_lte_flap_state): counts
# distinct flap EPISODES (entries into "flapping", not raw transitions)
# per device over a rolling day - a device that's flapped this many
# separate times in the window is a hardware/carrier-swap candidate, not
# a one-off blip, even though each individual episode still clears itself
# after LTE_FLAP_RECOVERY_STABLE_SECONDS of stability like normal.
CHRONIC_FLAPPER_WINDOW_SECONDS = 86400
CHRONIC_FLAPPER_EPISODE_THRESHOLD = 3

# Battery is considered "confirmed charging" (see Poller._poll_one_device)
# once battery_state has read CHARGING for this many consecutive REAL
# battery polls (not every poll_interval_seconds cycle - battery is only
# actually re-fetched every battery_poll_interval_seconds, see CONFIG).
# The other way to prove recovery - the level climbing back above
# BATTERY_DEGRADED_PERCENT - needs no special handling: it already stops
# being flagged at all once that happens.
BATTERY_CHARGING_STREAK_REQUIRED = 2

# Chronic connectivity instability (see Poller._update_connectivity_instability):
# distinct from LTE_FLAP_* above, which only watches the LTE carrier
# specifically over a 30-minute window. This tracks the CONNECTIVITY
# GROUP's top issue as a whole - service/wan/lte all count, so a device
# cycling between Critical "Out of service" and Degraded "Running on
# cellular backup" (never actually an LTE flap, since that's driven by
# activeWAN/status, not carrierStatus) still gets caught. A device whose
# top connectivity issue has changed this many times within the window
# gets a fixed "[recently unstable]" marker appended to whatever its
# current message is - visibility that this is a repeat offender, not a
# one-off. Deliberately NOT a live-updating count in the message (same
# reasoning as LTE_SUSTAINED_OUTAGE_SECONDS above) and deliberately NOT a
# severity override - a chronically unstable device that's genuinely
# Critical right now still shows Critical, this only adds context.
CHRONIC_INSTABILITY_WINDOW_SECONDS = 14400
CHRONIC_INSTABILITY_THRESHOLD = 5


def get_device_issues(d: dict) -> list[dict]:
    """Each message is written to be read on the dashboard by someone who
    isn't looking at raw API field names - plain language first, the raw
    Ooma value included only where it adds detail. Each issue carries its
    own "severity" - severity is no longer purely a function of category:
    "service" (device confirmed out of service) and a critically low
    battery (see BATTERY_CRITICAL_PERCENT) are the only Critical cases; a
    missing/unreported WAN reading, a cellular-failover WAN, LTE trouble,
    and a merely-low battery are all Degraded - real enough to flag, but
    not a confirmed outage on their own."""
    issues = []

    out_of_service = bool(d.get("status")) and d["status"] != "IN_SERVICE"
    if out_of_service:
        issues.append({"category": "service", "severity": "Critical", "message": "Out of service"})

    active_wan = d.get("active_wan")
    if not active_wan:
        # A missing activeWAN reading isn't the same claim as "the WAN is
        # down" - if the device were actually offline, the `status` check
        # above already covers that as its own Critical "service" issue.
        # This is Ooma simply not reporting a WAN reading for an otherwise
        # in-service device - insufficient data, not a confirmed outage -
        # so it's Degraded, not Critical.
        issues.append({
            "category": "wan", "severity": "Degraded",
            "message": "WAN status unavailable - insufficient data from Ooma",
        })
    elif active_wan == "LTE":
        # activeWAN only ever comes back as "ETHERNET" or "LTE" (confirmed
        # against real data) - "LTE" here means the wired path failed and
        # it's running on cellular failover. There's no field available
        # from this endpoint that distinguishes a Wired-only unit from a
        # MultiPath one - both report "ETHERNET" - so that distinction
        # isn't checked here.
        issues.append({
            "category": "wan", "severity": "Degraded",
            "message": "Running on cellular backup - primary WAN is down",
        })

    if d.get("has_lte"):
        # A separate "LTE modem not registered" message used to fire when
        # the modems list came back empty (lte_modem_count == 0),
        # distinct from a registered modem's carrierStatus reading down.
        # That distinction turned out not to be reliable in practice - an
        # empty modems list wasn't a trustworthy signal on its own, just
        # more Ooma-side noise - so both cases now collapse into the same
        # single check/message: no modem registered counts the same as a
        # registered modem with its carrier down, either way the LTE path
        # isn't usable right now. Judge health off lte_carrier_status, not
        # lte_quality - Ooma's own `quality` field unreliably returns
        # "NONE" even on modems with excellent signal (seen in real data:
        # SINR 24-28, RSRP -72 to -79, carrierStatus "UP", yet quality:
        # "NONE"). carrierStatus ("UP"/"DOWN") matched real up/down state
        # in every observed case, including genuinely dead modems.
        modem_registered = d.get("lte_modem_count", 0) > 0
        carrier_up = d.get("lte_carrier_status") == "UP"
        connected = modem_registered and carrier_up

        # lte_flap_status is set once per real poll cycle by
        # Poller._update_lte_flap_state (not computed here - this
        # function may be called several times per cycle, from different
        # endpoints, and flap detection needs to see exactly one sample
        # per actual poll to count transitions correctly). "flapping"
        # means 3+ up/down transitions within the last 30 min - held
        # under that name rather than bouncing the normal disconnected
        # message on and off. "recovering" means it just went stable for
        # 15 min after flapping - shown for exactly one poll cycle as a
        # visible confirmation before it drops off /issues entirely.
        flap_status = d.get("lte_flap_status")
        if flap_status == "flapping":
            message = f"LTE unstable - flapping ({LTE_FLAP_TRANSITION_THRESHOLD}+ changes in the last 30 min)"
            # lte_chronic_flapper (also set once per real poll by
            # Poller._update_lte_flap_state) flags a device that's
            # entered flapping this many separate times today, not just
            # one prolonged episode - repeat-offender visibility, same
            # spirit as connectivity_unstable's "[recently unstable]"
            # marker below.
            if d.get("lte_chronic_flapper"):
                message += " [chronic flapper]"
            issues.append({"category": "lte", "severity": "Degraded", "message": message})
        elif flap_status == "recovering":
            issues.append({
                "category": "lte", "severity": "Degraded",
                "message": "LTE recovered from flapping - holding stable",
            })
        elif not connected:
            # lte_sustained_outage (also set once per real poll by
            # Poller._update_lte_flap_state) relabels a carrier that's
            # been continuously down - no flapping, just steadily
            # disconnected - for at least LTE_SUSTAINED_OUTAGE_SECONDS.
            # Severity stays Degraded either way; only the wording
            # changes, so it's obvious at a glance whether this is a
            # fresh drop or one that's been sitting unresolved a while.
            if d.get("lte_sustained_outage"):
                issues.append({
                    "category": "lte", "severity": "Degraded",
                    "message": "LTE carrier disconnected - prolonged outage",
                })
            else:
                issues.append({
                    "category": "lte", "severity": "Degraded",
                    "message": "LTE carrier temporarily disconnected",
                })

    # Same thresholds whether or not the device is currently out of
    # service. While out of service, battery_level_percent is whatever
    # Ooma last relayed before it went dark (possibly null, if it never
    # reported one) - not a live reading, but still the only evidence of
    # whether a battery problem was already in progress when the device
    # dropped off. A device that was fine (e.g. 100%) right before going
    # dark shouldn't flag a battery issue just because it's now
    # unreachable; a device that was already low (e.g. 50%) should keep
    # flagging it - the offline device isn't charging, so a pre-existing
    # problem hasn't gone away, it's just unconfirmed. battery_level being
    # None (no reading was ever relayed) means there's nothing to judge
    # either way, so nothing is flagged.
    battery_level = d.get("battery_level_percent")
    if battery_level is not None:
        # battery_state is Ooma's own raw charge-state field (e.g.
        # "CHARGING"/"DISCHARGING"/"FULL" - see OomaAirDialClient.get_battery's
        # sample response comment) - surfaced here as-is, title-cased,
        # rather than this bridge trying to interpret or validate specific
        # values it hasn't independently confirmed.
        detail_parts = []
        battery_state = d.get("battery_state")
        if battery_state:
            detail_parts.append(str(battery_state).capitalize())
        if out_of_service:
            detail_parts.append("last known reading before device went offline")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""

        # battery_charging_streak is set once per REAL battery poll by
        # Poller._poll_one_device (battery is only actually re-fetched
        # every battery_poll_interval_seconds, not every call to this
        # function) - counts consecutive real readings of "CHARGING".
        # Once that streak proves out, a still-low battery shows as
        # recovering instead of the usual low-battery wording - the
        # other way to prove recovery (level climbing back above
        # BATTERY_DEGRADED_PERCENT) needs nothing extra here, since that
        # already exits this whole block on its own. Not offered while
        # out_of_service - there's no live charging confirmation to be
        # had from a device that can't be reached.
        charging_confirmed = (
            not out_of_service and d.get("battery_charging_streak", 0) >= BATTERY_CHARGING_STREAK_REQUIRED
        )
        if battery_level <= BATTERY_DEGRADED_PERCENT:
            if charging_confirmed:
                issues.append({
                    "category": "battery", "severity": "Degraded",
                    "message": f'Battery recovering - confirmed charging, currently {battery_level}%{detail}',
                })
            elif battery_level <= BATTERY_CRITICAL_PERCENT:
                issues.append({
                    "category": "battery", "severity": "Critical",
                    "message": f'Battery critically low at {battery_level}%{detail}',
                })
            else:
                issues.append({
                    "category": "battery", "severity": "Degraded",
                    "message": f'Battery at {battery_level}%{detail}',
                })

    # connectivity_unstable is set once per real poll by
    # Poller._update_connectivity_instability - flags EVERY connectivity
    # issue (whichever category it is) rather than trying to pick "the"
    # one, since pick_top_issues_by_group (downstream of this function)
    # is what actually decides which one surfaces.
    if d.get("connectivity_unstable"):
        for issue in issues:
            if CATEGORY_GROUP[issue["category"]] == "connectivity":
                issue["message"] += " [recently unstable]"

    return issues


def compute_effective_status(issues: list[dict]) -> str:
    """DOWN / DEGRADED / OK - a truer read on the device than Ooma's raw
    `status` field. A "service" issue means Ooma itself reports the unit
    out of service; a "lte" issue means the unit is functionally dead
    even if Ooma still reports IN_SERVICE (these units are basically
    nonfunctional without LTE, so that counts as DOWN, not just degraded).
    "wan" (Wired-only, no redundancy) and "battery" issues alone only
    knock it down to DEGRADED - the unit is still working."""
    categories = {issue["category"] for issue in issues}
    if "service" in categories or "lte" in categories:
        return "DOWN"
    if categories:
        return "DEGRADED"
    return "OK"


def _issue_rank(issue: dict) -> tuple[int, int]:
    """Lower sorts first: Critical outranks Degraded; ties broken by
    category order - service, then wan, then lte, then battery."""
    return (-_SEVERITY_RANK[issue["severity"]], ISSUE_CATEGORIES.index(issue["category"]))


def pick_top_issue(issues: list[dict]) -> dict | None:
    """The single highest-priority issue for a device, collapsed across
    ALL groups. None if no issues. Used only for a one-line overall
    summary (e.g. /maintenance-log's status label) - NOT for the
    per-column counts, which need pick_top_issues_by_group instead so a
    device with both a connectivity AND a battery problem shows up in
    both columns, not just its single worst issue overall."""
    if not issues:
        return None
    return min(issues, key=_issue_rank)


def pick_top_issues_by_group(issues: list[dict]) -> list[dict]:
    """Up to one issue PER GROUP (connectivity, battery) - so a device
    with both a wan problem and a low battery contributes one alert to
    EACH column, not just its single worst issue overall. Within a
    group, multiple issues (e.g. wan + lte, both connectivity) still
    collapse to that group's single worst one, via the same
    _issue_rank priority pick_top_issue uses."""
    best_by_group: dict[str, dict] = {}
    for issue in issues:
        group = CATEGORY_GROUP[issue["category"]]
        current = best_by_group.get(group)
        if current is None or _issue_rank(issue) < _issue_rank(current):
            best_by_group[group] = issue
    return [best_by_group[g] for g in GROUP_CATEGORIES if g in best_by_group]


def build_account_rollup(devices: list[dict]) -> list[dict]:
    """One row per account: device_count + a count per issue GROUP
    (connectivity, battery). Each device counts toward AT MOST ONE issue
    PER GROUP via pick_top_issues_by_group, ignoring maintenance
    suppression when deciding what's real - so a device with both a wan
    problem (connectivity) and a low battery adds 1 to EACH column, but a
    device with both a wan and an lte problem (both connectivity) still
    only adds 1 to connectivity_issues, not 2.

    Each cell's value follows the same 1000+n encoding as
    /category-summary: if that account has any OPEN (unacknowledged)
    issue in that group, the cell is the open count at face value; if
    every issue in that group for that account is currently acknowledged
    (an active maintenance window), the cell becomes 1000+n instead of
    silently dropping to 0 - a fully-acknowledged account still reads as
    "handled," not "nothing's wrong." Analogous to Hyperview's
    location-health-matrix. Sorted affected-accounts-first, like
    matrix.py's `-site` sort key - "affected" here means open OR
    acknowledged, same as matrix.py's own site flag."""
    accounts: dict[str, dict] = {}
    for d in devices:
        acct = d.get("account") or "Unknown"
        row = accounts.setdefault(
            acct,
            {
                "account": acct, "device_count": 0,
                **{f"{g}_open": 0 for g in GROUP_CATEGORIES},
                **{f"{g}_ack": 0 for g in GROUP_CATEGORIES},
            },
        )
        row["device_count"] += 1
        in_maintenance = device_maintenance_info(d.get("myx_id"), d.get("account")) is not None
        for top in pick_top_issues_by_group(get_device_issues(d)):
            group = CATEGORY_GROUP[top["category"]]
            row[f"{group}_ack" if in_maintenance else f"{group}_open"] += 1

    result = list(accounts.values())
    for row in result:
        row["site"] = 0
        for g in GROUP_CATEGORIES:
            open_c, ack_c = row.pop(f"{g}_open"), row.pop(f"{g}_ack")
            row[f"{g}_issues"] = _score_category_total(open_c, ack_c)
            if open_c or ack_c:
                row["site"] = 1
    result.sort(key=lambda r: (-r["site"], r["account"]))
    return result


def build_issues_table(devices: list[dict]) -> list[dict]:
    """Flat log of currently-flagged devices - ONE ROW PER (device, group)
    issue, so a device with both a connectivity problem AND a low battery
    contributes two separate rows, never more than two (one per entry in
    GROUP_CATEGORIES). Each row's "category"/"severity"/"message" describe
    that ONE group's issue only - no more concatenating multiple groups
    into a single row, so a consumer never has to reconstruct which
    severity belongs to which problem, or guess where one group's message
    ends and another's begins. Within a single group, multiple raw issues
    (e.g. wan + lte, both connectivity) still collapse to that group's one
    worst message, via pick_top_issues_by_group. Analogous to Hyperview's
    active-alarm-log. Empty when everything's healthy.

    NOTE: this used to return exactly one row per device, concatenating
    every group into it - changed because a device's two issues sharing
    one row made it impossible for a downstream consumer (an alert script
    diffing this feed) to tell "one group's message changed" apart from
    "a different, unrelated group's message changed," causing spurious
    re-alerts, and made a group silently dropping out of the concatenated
    string indistinguishable from that group being fixed."""
    rows = []
    for d in devices:
        for top in pick_top_issues_by_group(effective_issues(d)):
            rows.append({
                "account": d.get("account") or "Unknown",
                "device": d.get("name"),
                "myx_id": d.get("myx_id"),
                "category": CATEGORY_GROUP[top["category"]].capitalize(),
                "severity": top["severity"],
                "message": top["message"],
            })

    rows.sort(key=lambda r: (-_SEVERITY_RANK.get(r["severity"], 0), r["account"], r["device"] or ""))
    return rows


def _score_category_total(open_count: int, ack_count: int) -> int:
    """Open alarms count at face value; all-acknowledged categories get
    pushed into the 1000+ range so a single numeric field (Grafana
    threshold-friendly) can distinguish "open" from "fully acked" -
    same encoding as the Hyperview bridge's _score_category_total()."""
    total = open_count + ack_count
    if total > 0 and open_count == 0:
        return 1000 + total
    return open_count


def build_category_summary(devices: list[dict]) -> list[dict]:
    """Open + acknowledged device count per GROUP (connectivity,
    battery), plus an "All" row. Each device counts toward AT MOST ONE
    issue PER GROUP via pick_top_issues_by_group (ignoring maintenance
    suppression when deciding what's real, unlike /accounts and /issues -
    this panel needs to know what's acknowledged, not just what's
    currently visible) - so a device with both a connectivity problem
    and a low battery counts in both. "acknowledged" = that device is
    currently under an active maintenance window; "open" = it isn't.
    "count" is just open_counts[g] at face value - no 1000+ acknowledged
    encoding here (that's still used by /accounts' per-group columns, see
    _score_category_total, just not by this endpoint)."""
    open_counts = {g: 0 for g in GROUP_CATEGORIES}
    ack_counts = {g: 0 for g in GROUP_CATEGORIES}
    for d in devices:
        in_maintenance = device_maintenance_info(d.get("myx_id"), d.get("account")) is not None
        for top in pick_top_issues_by_group(get_device_issues(d)):
            group = CATEGORY_GROUP[top["category"]]
            if in_maintenance:
                ack_counts[group] += 1
            else:
                open_counts[group] += 1

    result = [
        {
            "category": GROUP_META[g],
            "count": open_counts[g],
            "open": open_counts[g],
            "acknowledged": ack_counts[g],
        }
        for g in GROUP_CATEGORIES
    ]
    total_open = sum(open_counts.values())
    total_ack = sum(ack_counts.values())
    result.append({
        "category": "\U0001F4CB All", "count": total_open,  # 📋
        "open": total_open, "acknowledged": total_ack,
    })
    return result


def build_operational_status(devices: list[dict]) -> dict:
    """Single rolled-up status, for a big stat/gauge panel. Analogous to
    Hyperview's operational-status."""
    total_issues = sum(len(effective_issues(d)) for d in devices)
    if total_issues > 0:
        return {
            "status": "Action Required", "color": "red", "value": 1,
            "display": f"\U0001F6A8 {total_issues} Issue(s)",  # 🚨
            "issues": total_issues,
        }
    return {
        "status": "Normal", "color": "green", "value": 0,
        "display": "✅ No Active Issues",  # ✅
        "issues": 0,
    }


_EFFECTIVE_STATUS_CREDIT = {"OK": 1.0, "DEGRADED": 0.5, "DOWN": 0.0}


def build_overall_health(devices: list[dict]) -> dict:
    """0-100 health score, weighted by how bad each device's worst issue
    is - not just a binary healthy/unhealthy count. A DOWN device (dead
    service/LTE) costs full credit; a DEGRADED device (Wired-only or low
    battery) only costs half credit, since it's still working. Simpler
    than Hyperview's weighted point system (no asset-type/location
    weighting to apply here - every AirDial unit is the same kind of
    thing)."""
    total = len(devices)
    ok_count = degraded_count = down_count = 0
    credit = 0.0

    for d in devices:
        status = compute_effective_status(effective_issues(d))
        credit += _EFFECTIVE_STATUS_CREDIT[status]
        if status == "OK":
            ok_count += 1
        elif status == "DEGRADED":
            degraded_count += 1
        else:
            down_count += 1

    health = round(100 * credit / total) if total else 100

    if health == 100:
        emoji, state = "✅", "Healthy"  # ✅
    elif health >= 90:
        emoji, state = "⚠️", "Degraded"  # ⚠️
    elif health >= 70:
        emoji, state = "\U0001F6A8", "Significant"  # 🚨
    else:
        emoji, state = "\U0001F525", "Critical"  # 🔥

    return {
        "health": health, "emoji": emoji, "state": state,
        "device_count": total, "healthy_device_count": ok_count,
        "degraded_device_count": degraded_count, "down_device_count": down_count,
    }


def build_sites_affected(devices: list[dict]) -> dict:
    """Accounts with >=1 issue vs total accounts. Analogous to
    Hyperview's sites-affected."""
    rollup = build_account_rollup(devices)
    affected = sum(row["site"] for row in rollup)
    total = len(rollup)
    return {
        "affected": affected, "healthy": total - affected, "total": total,
        "display": f"{affected}/{total}",
    }


# --------------------------------------------------------------------------
# Maintenance windows
#
# Ported from the iPro bridge's /maintenance page: operator-entered
# suppression, not derived from polling. A device (or every device under
# an account) marked this way is excluded from every rollup below -
# /issues, /accounts, /category-summary, /overall-health,
# /operational-status, /sites-affected - as if it had no issues at all,
# for the duration of the window. /devices is NEVER suppressed - it
# always reflects ground truth, same as iPro's raw_camera_status()
# staying available underneath maintenance_info(). If the device hasn't
# actually recovered by the time the window expires, its real status
# reappears immediately on the very next poll cycle's worth of rollups -
# nothing about polling or issue detection itself pauses while
# "acknowledged."
# --------------------------------------------------------------------------

maintenance_lock = threading.Lock()
maintenance_windows: dict[str, dict] = {}  # key "device:{myx_id}" or "account:{account}" -> window dict


def load_maintenance(path: str) -> None:
    global maintenance_windows
    try:
        with open(path, "r") as f:
            maintenance_windows = json.load(f)
        log.info("Loaded %d maintenance window(s) from %s", len(maintenance_windows), path)
    except FileNotFoundError:
        maintenance_windows = {}
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not load maintenance windows (%s), starting fresh", exc)
        maintenance_windows = {}


def save_maintenance(path: str) -> None:
    try:
        dir_name = os.path.dirname(path) or "."
        os.makedirs(dir_name, exist_ok=True)

        with maintenance_lock:
            data = json.dumps(maintenance_windows)

        fd, tmp_path = tempfile.mkstemp(dir=dir_name)
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except OSError as exc:
        log.error("Failed to save maintenance windows: %s", exc)


def set_maintenance(
    path: str, target_type: str, target_key: str, target_display: str,
    minutes: float, reason: str, set_by: str,
) -> str:
    now = time.time()
    key = f"{target_type}:{target_key}"
    with maintenance_lock:
        maintenance_windows[key] = {
            "target_type": target_type,
            "target_key": target_key,
            "target_display": target_display,
            "until": now + minutes * 60,
            "reason": (reason or "").strip()[:200],
            "set_by": (set_by or "").strip()[:100],
            "set_at": now,
        }
    save_maintenance(path)
    return key


def clear_maintenance(path: str, key: str) -> bool:
    with maintenance_lock:
        existed = maintenance_windows.pop(key, None) is not None
    if existed:
        save_maintenance(path)
    return existed


def active_maintenance_windows() -> dict[str, dict]:
    """Live-computed - expired windows are simply not returned, not
    eagerly deleted on every read. They get cleaned up for real the next
    time someone writes (set/clear)."""
    now = time.time()
    with maintenance_lock:
        return {k: v for k, v in maintenance_windows.items() if v["until"] > now}


def device_maintenance_info(myx_id: str, account: str | None) -> dict | None:
    """Active window dict if this device (or its account) is currently
    under maintenance, else None. Device-level window checked first,
    falling back to an account-wide one - same precedence as iPro's
    camera-then-server fallback."""
    now = time.time()
    with maintenance_lock:
        entry = maintenance_windows.get(f"device:{myx_id}")
        if entry and entry["until"] > now:
            return entry
        if account:
            entry = maintenance_windows.get(f"account:{account}")
            if entry and entry["until"] > now:
                return entry
    return None


def effective_issues(d: dict) -> list[dict]:
    """get_device_issues(d), or [] if this device is currently under an
    active maintenance window. Every rollup (accounts/issues/category-
    summary/operational-status/overall-health/sites-affected) should call
    this instead of get_device_issues() directly - /devices is the one
    place that intentionally does NOT, since it's meant to show ground
    truth regardless of acknowledgment."""
    if device_maintenance_info(d.get("myx_id"), d.get("account")):
        return []
    return get_device_issues(d)


def format_duration_human(seconds: float | None) -> str | None:
    """Scales a duration to the largest sensible unit - minutes, then
    hours, then days - rather than raw seconds."""
    if seconds is None:
        return None
    seconds = max(0, int(round(seconds)))
    minutes = seconds // 60
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_minutes = minutes % 60
    if hours < 24:
        return f"{hours}h {rem_minutes}m" if rem_minutes else f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"


def build_device_options(devices_cfg: list[dict]) -> list[tuple[str, str]]:
    """(myx_id, display name) pairs for the maintenance page's picker,
    sorted by name."""
    return sorted(
        [(e["myx_id"], e.get("name") or e["myx_id"]) for e in devices_cfg],
        key=lambda x: x[1].lower(),
    )


def build_account_options(devices_cfg: list[dict]) -> list[str]:
    return sorted({e.get("account") for e in devices_cfg if e.get("account")})


def resolve_device_target(devices_cfg: list[dict], target_input: str) -> tuple[str | None, str | None]:
    """By-myx_id-or-by-name device lookup for the maintenance form.
    Returns (myx_id, display_name) or (None, None) if no match."""
    for e in devices_cfg:
        if e["myx_id"] == target_input:
            return e["myx_id"], e.get("name") or e["myx_id"]
    for e in devices_cfg:
        if (e.get("name") or "").strip().lower() == target_input.strip().lower():
            return e["myx_id"], e.get("name") or e["myx_id"]
    return None, None


MAINTENANCE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Acknowledge Issues - Ooma AirDial Bridge</title>
<style>
  :root {{
    --bg: #14171a; --panel: #1b1f24; --panel-raised: #21262c;
    --border: #2a2f36; --border-bright: #3a4048;
    --text: #e7ecef; --text-dim: #8b95a1; --text-faint: #5b6470;
    --accent: #5b9dd9; --warn: #f0a824; --danger: #ef4a5a; --ok: #3ddc84;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 28px 20px 60px;
  }}
  .wrap {{ max-width: 1300px; width: 95%; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: var(--text-dim); font-size: 13px; margin: 0 0 20px; }}
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; margin-bottom: 18px;
  }}
  .card h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-dim); margin: 0 0 14px; }}
  .msg {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }}
  .msg.ok {{ background: rgba(61,220,132,0.12); color: var(--ok); border: 1px solid rgba(61,220,132,0.3); }}
  .msg.err {{ background: rgba(239,74,90,0.12); color: var(--danger); border: 1px solid rgba(239,74,90,0.3); }}
  label {{ display: block; font-size: 12px; color: var(--text-dim); margin: 12px 0 5px; }}
  label:first-child {{ margin-top: 0; }}
  input[type=text], input[type=number], textarea, select {{
    width: 100%; max-width: 480px; padding: 9px 11px; border-radius: 7px; border: 1.5px solid var(--border-bright);
    background: var(--panel-raised); color: var(--text); font-size: 14px; font-family: inherit;
  }}
  #target-input {{ max-width: 100%; }}
  #hours-input {{ max-width: 200px; }}
  textarea {{ resize: vertical; min-height: 60px; max-width: 100%; }}
  .radio-row {{ display: flex; gap: 18px; margin: 12px 0 5px; }}
  .radio-row label {{ display: flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; color: var(--text); }}
  .autocomplete-wrap {{ position: relative; max-width: 100%; }}
  .suggestions {{
    position: absolute; top: 100%; left: 0; right: 0; margin-top: -1px;
    max-height: 340px; overflow-y: auto; background: var(--panel-raised);
    border: 1.5px solid var(--accent); border-radius: 0 0 7px 7px; z-index: 30;
    display: none; box-shadow: 0 8px 20px rgba(0,0,0,0.35);
  }}
  .suggestions.show {{ display: block; }}
  .suggestions .item {{ padding: 9px 12px; cursor: pointer; font-size: 14px; }}
  .suggestions .item:hover, .suggestions .item.active {{ background: rgba(91,157,217,0.18); }}
  .suggestions .empty {{ padding: 9px 12px; }}
  button {{
    font-family: inherit; font-weight: 600; font-size: 14px; padding: 11px 18px;
    border-radius: 8px; border: 1.5px solid var(--accent); background: rgba(91,157,217,0.15);
    color: var(--accent); cursor: pointer; margin-top: 16px;
  }}
  button:hover {{ background: rgba(91,157,217,0.25); }}
  button.cancel-btn {{
    border-color: var(--danger); background: rgba(239,74,90,0.1); color: var(--danger);
    font-size: 12px; padding: 5px 10px; margin: 0;
  }}
  button.cancel-btn:hover {{ background: rgba(239,74,90,0.2); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: var(--text-faint); font-weight: 600; text-transform: uppercase;
    font-size: 11px; letter-spacing: 0.04em; padding: 8px 10px; border-bottom: 1px solid var(--border-bright); }}
  td {{ padding: 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  .empty {{ color: var(--text-faint); font-size: 13px; padding: 10px 0; }}
  .tag {{ font-size: 10px; text-transform: uppercase; padding: 2px 6px; border-radius: 4px;
    background: var(--panel-raised); border: 1px solid var(--border-bright); color: var(--text-dim); }}
  .hint {{ font-size: 11px; color: var(--text-faint); margin-top: 4px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Acknowledge issues</h1>
  <p class="sub">Acknowledge a device or an entire account as addressed, suppressing it from the issue rollups for a set duration. Underlying polling keeps running - if it hasn't actually recovered by the time acknowledgment expires, real status shows immediately.</p>

  {message_html}
  {error_html}

  <div class="card">
    <h2>Acknowledge an issue</h2>
    <form method="post" action="/maintenance">
      <div class="radio-row">
        <label><input type="radio" name="target_type" value="device" checked onclick="toggleTarget(this)"> Single device</label>
        <label><input type="radio" name="target_type" value="account" onclick="toggleTarget(this)"> Entire account</label>
      </div>

      <label id="target-label">Device (name or myx_id)</label>
      <div class="autocomplete-wrap">
        <input type="text" name="target" id="target-input" placeholder="Start typing a device name..." autocomplete="off" required>
        <div id="suggestions" class="suggestions"></div>
      </div>

      <div id="duration-section">
        <label for="hours-input">Acknowledge for (hours)</label>
        <input type="number" id="hours-input" name="hours" min="1" max="{max_hours}" step="1" value="24" required>
        <p class="hint">1-{max_hours} hours.</p>
      </div>

      <label>Reason / action taken</label>
      <textarea name="reason" placeholder="e.g. site network maintenance, device swap, known carrier outage" required></textarea>

      <label>Acknowledged by</label>
      <input type="text" name="set_by" placeholder="e.g. jsmith" required>

      <button type="submit" id="submit-btn">Acknowledge</button>
    </form>
  </div>

  <div class="card">
    <h2>Currently acknowledged ({window_count})</h2>
    {windows_html}
  </div>
</div>

<script>
var DEVICE_NAMES = {device_names_json};
var ACCOUNT_NAMES = {account_names_json};
var currentList = DEVICE_NAMES;

var targetInput = document.getElementById('target-input');
var suggestBox = document.getElementById('suggestions');

function toggleTarget(radio) {{
  var label = document.getElementById('target-label');
  if (radio.value === 'account') {{
    label.textContent = 'Account';
    currentList = ACCOUNT_NAMES;
    targetInput.placeholder = 'Start typing an account name...';
  }} else {{
    label.textContent = 'Device (name or myx_id)';
    currentList = DEVICE_NAMES;
    targetInput.placeholder = 'Start typing a device name...';
  }}
  targetInput.value = '';
  suggestBox.classList.remove('show');
}}

function renderSuggestions(list) {{
  suggestBox.innerHTML = '';
  if (!list.length) {{
    suggestBox.innerHTML = '<div class="empty hint">No matches</div>';
    suggestBox.classList.add('show');
    return;
  }}
  list.slice(0, 30).forEach(function(name) {{
    var item = document.createElement('div');
    item.className = 'item';
    item.textContent = name;
    item.onmousedown = function(e) {{
      e.preventDefault();
      targetInput.value = name;
      suggestBox.classList.remove('show');
    }};
    suggestBox.appendChild(item);
  }});
  suggestBox.classList.add('show');
}}

targetInput.addEventListener('input', function() {{
  var q = targetInput.value.trim().toLowerCase();
  if (!q) {{ suggestBox.classList.remove('show'); return; }}
  renderSuggestions(currentList.filter(function(n) {{ return n.toLowerCase().indexOf(q) !== -1; }}));
}});
targetInput.addEventListener('focus', function() {{
  if (targetInput.value.trim()) targetInput.dispatchEvent(new Event('input'));
}});
targetInput.addEventListener('blur', function() {{
  setTimeout(function() {{ suggestBox.classList.remove('show'); }}, 150);
}});
</script>
</body>
</html>
"""


def render_maintenance_page(
    devices_cfg: list[dict], max_hours: int, message: str | None, error: str | None,
) -> str:
    now = time.time()
    windows = active_maintenance_windows()
    rows = sorted(windows.items(), key=lambda kv: kv[1]["until"])

    if rows:
        row_html = "".join(
            f"<tr>"
            f"<td>{html.escape(w['target_display'])} <span class=\"tag\">{html.escape(w['target_type'])}</span></td>"
            f"<td>{html.escape(w['reason']) or '-'}</td>"
            f"<td>{html.escape(w['set_by']) or '-'}</td>"
            f"<td>{format_duration_human(w['until'] - now)}</td>"
            f"<td><form method=\"post\" action=\"/maintenance/{quote(key)}/cancel\" style=\"margin:0;\">"
            f"<button type=\"submit\" class=\"cancel-btn\">Un-acknowledge</button></form></td>"
            f"</tr>"
            for key, w in rows
        )
        windows_html = (
            "<table><tr><th>Target</th><th>Reason</th><th>Acknowledged by</th>"
            f"<th>Expires in</th><th></th></tr>{row_html}</table>"
        )
    else:
        windows_html = '<p class="empty">Nothing currently acknowledged.</p>'

    device_names = [name for _, name in build_device_options(devices_cfg)]
    account_names = build_account_options(devices_cfg)

    return MAINTENANCE_PAGE_TEMPLATE.format(
        message_html=f'<div class="msg ok">{html.escape(message)}</div>' if message else "",
        error_html=f'<div class="msg err">{html.escape(error)}</div>' if error else "",
        max_hours=max_hours,
        window_count=len(rows),
        windows_html=windows_html,
        device_names_json=json.dumps(device_names),
        account_names_json=json.dumps(account_names),
    )


# Uses __TOKEN__ substitution rather than str.format() - this page's JS is
# dense enough with its own {} that escaping every brace for .format()
# would be its own source of bugs. Colors match MAINTENANCE_PAGE_TEMPLATE
# above so the bridge's own served pages read as one family.
EVENTS_SEARCH_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Device Event History - Ooma AirDial Bridge</title>
<style>
  :root {
    --bg: #14171a; --panel: #1b1f24; --panel-raised: #21262c;
    --border: #2a2f36; --border-bright: #3a4048;
    --text: #e7ecef; --text-dim: #8b95a1; --text-faint: #5b6470;
    --accent: #5b9dd9; --warn: #f0a824; --danger: #ef4a5a; --ok: #3ddc84;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 28px 20px 60px;
  }
  .wrap { max-width: 1300px; width: 95%; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--text-dim); font-size: 13px; margin: 0 0 20px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; margin-bottom: 18px;
  }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-dim); margin: 0 0 14px; }
  .filters {
    display: flex; flex-wrap: wrap; gap: 14px; align-items: end;
  }
  .field { display: flex; flex-direction: column; gap: 5px; min-width: 160px; }
  .field.wide { min-width: 220px; }
  label { font-size: 12px; color: var(--text-dim); }
  input[type=text], input[type=number], input[type=datetime-local], select {
    padding: 9px 11px; border-radius: 7px; border: 1.5px solid var(--border-bright);
    background: var(--panel-raised); color: var(--text); font-size: 13px; font-family: inherit;
  }
  #limit-input { width: 90px; }
  button {
    font-family: inherit; font-weight: 600; font-size: 14px; padding: 10px 18px;
    border-radius: 8px; border: 1.5px solid var(--accent); background: rgba(91,157,217,0.15);
    color: var(--accent); cursor: pointer;
  }
  button:hover { background: rgba(91,157,217,0.25); }
  button.ghost {
    border-color: var(--border-bright); background: transparent; color: var(--text-dim);
    font-size: 12px; padding: 9px 14px;
  }
  button.ghost:hover { color: var(--text); border-color: var(--text-dim); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--text-faint); font-weight: 600; text-transform: uppercase;
    font-size: 11px; letter-spacing: 0.04em; padding: 8px 10px; border-bottom: 1px solid var(--border-bright); }
  td { padding: 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
  td.time { color: var(--text-dim); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .device-cell { color: var(--text); }
  .device-cell .account { display: block; color: var(--text-faint); font-size: 11px; }
  .tag { font-size: 10px; text-transform: uppercase; padding: 2px 6px; border-radius: 4px;
    background: var(--panel-raised); border: 1px solid var(--border-bright); color: var(--text-dim); }
  .change .new { font-weight: 600; }
  .change .was { display: block; color: var(--text-faint); font-size: 11px; margin-top: 2px; }
  .sev-critical { color: var(--danger); }
  .sev-degraded { color: var(--warn); }
  .sev-recovered { color: var(--ok); }
  .empty { color: var(--text-faint); font-size: 13px; padding: 20px 0; text-align: center; }
  .status-line { color: var(--text-faint); font-size: 12px; margin-bottom: 10px; }
  .table-scroll { overflow-x: auto; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Device event history</h1>
  <p class="sub">Every time a device's Connectivity or Battery status changed - a new issue starting, its message changing (e.g. temporarily disconnected &rarr; flapping &rarr; recovered), or one clearing. Kept for __RETENTION_DAYS__ days.</p>

  <div class="card">
    <h2>Search</h2>
    <div class="filters">
      <div class="field wide">
        <label for="account-select">Account</label>
        <select id="account-select">
          <option value="">Any account</option>
        </select>
      </div>
      <div class="field wide">
        <label for="device-select">Device</label>
        <select id="device-select">
          <option value="">Any device</option>
        </select>
      </div>
      <div class="field">
        <label for="group-select">Group</label>
        <select id="group-select">
          <option value="">Any</option>
          <option value="connectivity">Connectivity</option>
          <option value="battery">Battery</option>
        </select>
      </div>
      <div class="field">
        <label for="since-input">Since</label>
        <input type="datetime-local" id="since-input">
      </div>
      <div class="field">
        <label for="until-input">Until</label>
        <input type="datetime-local" id="until-input">
      </div>
      <div class="field">
        <label for="limit-input">Limit</label>
        <input type="number" id="limit-input" min="1" max="2000" value="200">
      </div>
      <button type="button" onclick="runSearch()">Search</button>
      <button type="button" class="ghost" onclick="resetFilters()">Reset</button>
      <button type="button" class="ghost" onclick="downloadCsv()">Download CSV</button>
    </div>
  </div>

  <div class="card">
    <div id="status-line" class="status-line"></div>
    <div class="table-scroll">
      <table>
        <thead>
          <tr><th>Time</th><th>Device</th><th>Group</th><th>Change</th></tr>
        </thead>
        <tbody id="results-body"></tbody>
      </table>
      <div id="empty-msg" class="empty" style="display:none;">No matching events.</div>
    </div>
  </div>
</div>

<script>
const DEVICES = __DEVICE_OPTIONS_JSON__;   // [{myx_id, name, account}, ...]
const ACCOUNTS = __ACCOUNT_NAMES_JSON__;   // [account, ...]

function populateSelects() {
  const accountSel = document.getElementById('account-select');
  for (const acct of ACCOUNTS) {
    const opt = document.createElement('option');
    opt.value = acct; opt.textContent = acct;
    accountSel.appendChild(opt);
  }
  const deviceSel = document.getElementById('device-select');
  for (const d of DEVICES) {
    const opt = document.createElement('option');
    opt.value = d.myx_id; opt.textContent = d.name;
    opt.dataset.account = d.account || '';
    deviceSel.appendChild(opt);
  }
  accountSel.addEventListener('change', () => {
    const chosen = accountSel.value;
    for (const opt of deviceSel.options) {
      if (!opt.value) continue;
      opt.hidden = chosen && opt.dataset.account !== chosen;
    }
    if (chosen && deviceSel.options[deviceSel.selectedIndex].hidden) {
      deviceSel.value = '';
    }
  });
}

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

  if (!rows.length) {
    emptyMsg.style.display = 'block';
    return;
  }
  emptyMsg.style.display = 'none';

  for (const r of rows) {
    const tr = document.createElement('tr');
    const when = new Date(r.changed_at * 1000).toLocaleString();

    let changeHtml;
    if (r.new_message == null) {
      changeHtml = '<span class="sev-recovered new">Recovered</span>'
        + '<span class="was">was: ' + escapeHtml(r.old_message) + '</span>';
    } else if (r.old_message == null) {
      changeHtml = '<span class="' + severityClass(r.new_severity) + ' new">'
        + escapeHtml(r.new_severity) + ': ' + escapeHtml(r.new_message) + '</span>';
    } else {
      changeHtml = '<span class="' + severityClass(r.new_severity) + ' new">'
        + escapeHtml(r.new_severity) + ': ' + escapeHtml(r.new_message) + '</span>'
        + '<span class="was">was: ' + escapeHtml(r.old_message) + '</span>';
    }

    tr.innerHTML =
      '<td class="time">' + when + '</td>'
      + '<td class="device-cell">' + escapeHtml(r.device)
      + '<span class="account">' + escapeHtml(r.account) + '</span></td>'
      + '<td><span class="tag">' + escapeHtml(r.group_name) + '</span></td>'
      + '<td class="change">' + changeHtml + '</td>';
    body.appendChild(tr);
  }
}

function currentFilterParams() {
  const params = new URLSearchParams();
  const account = document.getElementById('account-select').value;
  const myxId = document.getElementById('device-select').value;
  const group = document.getElementById('group-select').value;
  const since = document.getElementById('since-input').value;
  const until = document.getElementById('until-input').value;
  const limit = document.getElementById('limit-input').value;

  if (account) params.set('account', account);
  if (myxId) params.set('myx_id', myxId);
  if (group) params.set('group', group);
  if (since) params.set('since', since);
  if (until) params.set('until', until);
  if (limit) params.set('limit', limit);
  return params;
}

async function runSearch() {
  const params = currentFilterParams();
  const statusLine = document.getElementById('status-line');
  statusLine.textContent = 'Searching...';
  try {
    const resp = await fetch('/events?' + params.toString());
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const rows = await resp.json();
    renderRows(rows);
    statusLine.textContent = rows.length + ' event(s)';
  } catch (err) {
    statusLine.textContent = 'Search failed: ' + err.message;
    renderRows([]);
  }
}

function downloadCsv() {
  // A plain navigation, not fetch - the response's
  // Content-Disposition: attachment header is what makes the browser
  // download it instead of navigating there, so no JS-side file
  // handling is needed for this part.
  const params = currentFilterParams();
  window.location.href = '/events.csv?' + params.toString();
}

function resetFilters() {
  document.getElementById('account-select').value = '';
  document.getElementById('device-select').value = '';
  document.getElementById('group-select').value = '';
  document.getElementById('since-input').value = '';
  document.getElementById('until-input').value = '';
  document.getElementById('limit-input').value = '200';
  for (const opt of document.getElementById('device-select').options) opt.hidden = false;
  runSearch();
}

populateSelects();
runSearch();
</script>
</body>
</html>
"""


def render_events_search_page(devices_cfg: list[dict]) -> str:
    device_options = [
        {"myx_id": d.get("myx_id"), "name": d.get("name") or d.get("myx_id"), "account": d.get("account") or ""}
        for d in devices_cfg
    ]
    account_names = build_account_options(devices_cfg)
    return (
        EVENTS_SEARCH_PAGE_TEMPLATE
        .replace("__DEVICE_OPTIONS_JSON__", json.dumps(device_options))
        .replace("__ACCOUNT_NAMES_JSON__", json.dumps(account_names))
        .replace("__RETENTION_DAYS__", str(CONFIG.get("events_retention_days", 90)))
    )


# --------------------------------------------------------------------------
# HTTP app (what Grafana talks to)
# --------------------------------------------------------------------------

def build_app(poller: Poller) -> FastAPI:
    app = FastAPI(title="Ooma AirDial Grafana Bridge")

    @app.get("/")
    def root():
        # Grafana's JSON API datasource "Save & Test" health check hits
        # the bare base URL - without this route it 404s even though the
        # bridge and /devices are working fine.
        return {"service": "ooma-airdial-bridge", "ok": True}

    @app.get("/health")
    def health():
        snap = poller.snapshot()
        healthy = snap["last_error"] is None and snap["last_poll_ts"] is not None
        return {
            "healthy": healthy,
            "last_poll_ts": snap["last_poll_ts"],
            "last_error": snap["last_error"],
            "device_count": len(snap["devices"]),
        }

    @app.get("/devices")
    def devices() -> list[dict[str, Any]]:
        return poller.snapshot()["devices"]

    @app.get("/devices/{myx_id}")
    def device(myx_id: str) -> dict[str, Any]:
        for d in poller.snapshot()["devices"]:
            if d["myx_id"] == myx_id:
                return d
        raise HTTPException(status_code=404, detail="device not found")

    @app.get("/accounts")
    def accounts() -> list[dict[str, Any]]:
        """One row per account: device_count + a count per issue category
        (service/wan/lte/battery), all 0 when healthy. Build the
        top-left HyperView-style table off this."""
        return build_account_rollup(poller.snapshot()["devices"])

    @app.get("/issues")
    def issues() -> list[dict[str, Any]]:
        """Flat list of only the devices currently flagged - one row per
        issue. Empty when everything's healthy. Build the bottom
        "active issues" table off this."""
        return build_issues_table(poller.snapshot()["devices"])

    @app.get("/category-summary")
    def category_summary() -> list[dict[str, Any]]:
        """Issue counts per category across all devices, for a center
        "Alarm Summary"-style panel."""
        return build_category_summary(poller.snapshot()["devices"])

    @app.get("/operational-status")
    def operational_status() -> list[dict[str, Any]]:
        return [build_operational_status(poller.snapshot()["devices"])]

    @app.get("/overall-health")
    def overall_health() -> list[dict[str, Any]]:
        return [build_overall_health(poller.snapshot()["devices"])]

    @app.get("/sites-affected")
    def sites_affected() -> list[dict[str, Any]]:
        return [build_sites_affected(poller.snapshot()["devices"])]

    @app.get("/last-updated")
    def last_updated() -> list[dict[str, Any]]:
        """When the most recent poll cycle finished, as an epoch-ms
        timestamp - same shape as Hyperview's /last-updated, for a
        "Last Updated" panel. null until the first poll completes."""
        last_poll_ts = poller.snapshot()["last_poll_ts"]
        timestamp = int(last_poll_ts * 1000) if last_poll_ts is not None else None
        return [{"timestamp": timestamp}]

    def _check_maintenance_auth(request: Request) -> Response | None:
        auth = request.headers.get("Authorization")
        if auth != CONFIG["maintenance_auth"]:
            return Response(
                content="Authentication required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ooma-bridge acknowledgment"'},
            )
        return None

    @app.get("/maintenance")
    def maintenance_page():
        """Retired as a standalone login page - the combined portal
        (CONFIG["portal_url"]) is now the one place operators log in to
        manage maintenance windows and search event history for both this
        bridge and the ipro bridge. POST /maintenance and POST
        /maintenance/{key}/cancel below are UNCHANGED and still require
        maintenance_auth - the portal calls those directly,
        server-to-server, using that same secret. No auth needed here
        since this route no longer serves anything but a pointer to where
        the real page moved."""
        if CONFIG["portal_url"]:
            return RedirectResponse(f"{CONFIG['portal_url'].rstrip('/')}/maintenance?system=ooma")
        return Response(
            content="The maintenance page has moved to the combined portal. "
                    "Set portal_url in CONFIG to enable the redirect.",
            status_code=410,
        )

    @app.post("/maintenance")
    def maintenance_create(
        request: Request,
        target_type: str = Form("device"),
        target: str = Form(""),
        hours: str = Form("24"),
        reason: str = Form(""),
        set_by: str = Form(""),
    ):
        unauthorized = _check_maintenance_auth(request)
        if unauthorized:
            return unauthorized

        target_input = target.strip()
        reason = reason.strip()
        set_by = set_by.strip()

        # Every field required server-side - the HTML form's `required`
        # attributes are a UI convenience only, same reasoning as the
        # hours clamp below (a raw POST bypasses them entirely).
        if not target_input:
            return RedirectResponse(f"/maintenance?error={quote('Device or account is required')}", status_code=303)
        if not reason:
            return RedirectResponse(f"/maintenance?error={quote('Reason / action taken is required')}", status_code=303)
        if not set_by:
            return RedirectResponse(f"/maintenance?error={quote('Acknowledged by is required')}", status_code=303)

        try:
            hours_val = int(hours)
        except ValueError:
            return RedirectResponse(f"/maintenance?error={quote('Duration must be a number')}", status_code=303)

        max_hours = CONFIG["maintenance_max_hours"]
        if hours_val < 1 or hours_val > max_hours:
            return RedirectResponse(
                f"/maintenance?error={quote(f'Duration must be between 1 and {max_hours} hours')}", status_code=303
            )

        devices_cfg = CONFIG["devices"]
        if target_type == "account":
            known_accounts = set(build_account_options(devices_cfg))
            if target_input not in known_accounts:
                return RedirectResponse(f"/maintenance?error={quote(f'Unknown account: {target_input}')}", status_code=303)
            key_target = target_input
            display = target_input
        else:
            myx_id, display = resolve_device_target(devices_cfg, target_input)
            if not myx_id:
                return RedirectResponse(f"/maintenance?error={quote(f'Device not found: {target_input}')}", status_code=303)
            key_target = myx_id

        set_maintenance(CONFIG["maintenance_file"], target_type, key_target, display, hours_val * 60, reason, set_by)
        return RedirectResponse(f"/maintenance?message={quote(f'Acknowledged {display}')}", status_code=303)

    @app.post("/maintenance/{key:path}/cancel")
    def maintenance_cancel(key: str, request: Request):
        unauthorized = _check_maintenance_auth(request)
        if unauthorized:
            return unauthorized
        existed = clear_maintenance(CONFIG["maintenance_file"], key)
        msg = "Un-acknowledged" if existed else "Not found (already expired?)"
        return RedirectResponse(f"/maintenance?message={quote(msg)}", status_code=303)

    @app.get("/maintenance-log")
    def maintenance_log() -> list[dict[str, Any]]:
        """Read-only JSON feed of currently-suppressed devices/accounts -
        what's acknowledged and what it's actually doing underneath. No
        auth (matches every other JSON route on this bridge) -
        /maintenance itself is the only route with a login, same split as
        iPro's."""
        now = time.time()
        devices = poller.snapshot()["devices"]
        rows = []
        for key, w in active_maintenance_windows().items():
            if w["target_type"] == "device":
                matching = [d for d in devices if d.get("myx_id") == w["target_key"]]
            else:
                matching = [d for d in devices if d.get("account") == w["target_key"]]

            alerting = [(d, get_device_issues(d)) for d in matching]
            alerting = [(d, issues) for d, issues in alerting if issues]

            if alerting:
                worst = max(
                    (pick_top_issue(issues)["severity"] for _, issues in alerting),
                    key=lambda s: _SEVERITY_RANK.get(s, 0),
                )
                status_label = worst
                detail = f"{len(alerting)} of {len(matching)} device(s) still alerting"
            else:
                status_label = "OK"
                detail = "No devices currently alerting" if matching else "No matching device(s)"

            rows.append({
                "key": key,  # for POST /maintenance/{key}/cancel - the combined portal needs this to cancel
                "target_type": w["target_type"],
                "target": w["target_display"],
                "status": status_label,
                "detail": detail,
                "reason": w["reason"],
                "set_by": w["set_by"],
                "remaining": format_duration_human(w["until"] - now),
            })

        rows.sort(key=lambda r: r["target"].lower())
        return rows

    @app.get("/maintenance-options")
    def maintenance_options() -> dict[str, Any]:
        """Picker data for the (now-portal-hosted) maintenance form -
        device names for a single-device window, account names for an
        entire-account window. Same 'single'/'grouped' shape as
        ipro-bridge's /maintenance-options so the combined portal can
        build both bridges' dropdowns generically. No auth - matches
        every other read-only JSON route on this bridge; this is
        name/id pairs only, nothing sensitive."""
        return {
            "single": [{"id": myx_id, "name": name} for myx_id, name in build_device_options(CONFIG["devices"])],
            "grouped": build_account_options(CONFIG["devices"]),
        }

    def _events_from_request_params(
        myx_id: str | None, account: str | None, group: str | None,
        since: str | None, until: str | None, limit: int,
    ) -> list[dict[str, Any]]:
        """Shared by /events and /events.csv - since/until accept the
        same "YYYY-MM-DDTHH:MM" string a <input type="datetime-local">
        produces; both are optional."""
        try:
            since_ts = datetime.fromisoformat(since).timestamp() if since else None
            until_ts = datetime.fromisoformat(until).timestamp() if until else None
        except ValueError:
            raise HTTPException(status_code=400, detail="since/until must look like YYYY-MM-DDTHH:MM")

        rows = poller.query_events(
            myx_id=myx_id or None, account=account or None, group=group or None,
            since=since_ts, until=until_ts, limit=limit,
        )
        for row in rows:
            row["changed_at_iso"] = datetime.fromtimestamp(row["changed_at"], tz=timezone.utc).isoformat()
        return rows

    @app.get("/events")
    def events_api(
        myx_id: str | None = None, account: str | None = None, group: str | None = None,
        since: str | None = None, until: str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Device event history - one row per (device, group) change. No
        auth, same as every other read-only JSON route on this bridge
        (/maintenance is the only one that needs a login)."""
        return _events_from_request_params(myx_id, account, group, since, until, limit)

    @app.get("/events.csv")
    def events_csv(
        myx_id: str | None = None, account: str | None = None, group: str | None = None,
        since: str | None = None, until: str | None = None, limit: int = 200,
    ) -> Response:
        """Same rows and filters as /events, as a CSV download instead
        of JSON - what the "Download CSV" button on /events-search
        links to. A GET with Content-Disposition: attachment, not a
        POST/fetch, so the browser's own download handling does the
        work - no JS needed on the page for this part."""
        rows = _events_from_request_params(myx_id, account, group, since, until, limit)

        buf = io.StringIO()
        columns = [
            "changed_at_iso", "device", "account", "group_name",
            "old_severity", "old_message", "new_severity", "new_message",
        ]
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

        filename = f"ooma-events-{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/events-search")
    def events_search_page():
        """Retired the same way /maintenance was above - the combined
        portal has its own event-search page now, calling /events and
        /events.csv below directly (both UNCHANGED, still unauthenticated
        read-only JSON/CSV feeds)."""
        if CONFIG["portal_url"]:
            return RedirectResponse(f"{CONFIG['portal_url'].rstrip('/')}/events-search?system=ooma")
        return Response(
            content="The event search page has moved to the combined portal. "
                    "Set portal_url in CONFIG to enable the redirect.",
            status_code=410,
        )

    return app


def main() -> None:
    password = read_p12_password(CONFIG)
    cert = ClientCert(CONFIG["p12_path"], password, key_path=CONFIG.get("key_path"))
    client = OomaAirDialClient(
        dms_url=CONFIG["dms_url"],
        cert=cert,
        timeout=int(CONFIG.get("request_timeout_seconds", 15)),
    )

    devices_cfg = CONFIG.get("devices", [])
    if not devices_cfg:
        raise ValueError("CONFIG['devices'] must list at least one {myx_id, name} entry")

    load_maintenance(CONFIG["maintenance_file"])

    poller = Poller(
        client,
        devices=devices_cfg,
        interval_seconds=int(CONFIG.get("poll_interval_seconds", 60)),
        max_concurrent_polls=int(CONFIG.get("max_concurrent_polls", 8)),
        battery_poll_interval_seconds=int(CONFIG.get("battery_poll_interval_seconds", 60)),
        events_db_path=CONFIG.get("events_db_path"),
        events_retention_days=int(CONFIG.get("events_retention_days", 90)),
    )
    poller.start()

    app = build_app(poller)
    try:
        uvicorn.run(
            app,
            host=CONFIG.get("listen_host", "0.0.0.0"),
            port=int(CONFIG.get("listen_port", 5003)),
        )
    finally:
        poller.stop()
        cert.close()


if __name__ == "__main__":
    main()
