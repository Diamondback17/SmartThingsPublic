"""Hyperview alarm/asset matrix service.

Exposes hospital/clinic alarm health as a set of small JSON endpoints
consumed by a dashboard. Pulls alarms + assets from the Hyperview API,
buckets them by site/category, and serves rolled-up summaries.

Config is read from real env vars, or from a .env file (KEY=VALUE per
line, '#' comments allowed) placed next to this script - a real env var
always wins if both are set.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException


def _load_dotenv():
    """Read KEY=VALUE lines from a .env file next to this script into
    os.environ, without overriding anything already set in the real
    environment. No external dependency - just enough to keep local
    secrets (e.g. HYPERVIEW_CLIENT_SECRET) out of the process's real env
    config while still being picked up on startup."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hyperview_matrix")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TENANT_URL = os.environ.get("HYPERVIEW_TENANT_URL", "https://covhlth.hyperviewhq.com")
# NOTE: falls back to the values that were originally hardcoded here so the
# service keeps running as-is. When you get a chance, set HYPERVIEW_CLIENT_ID
# / HYPERVIEW_CLIENT_SECRET in the environment (systemd Environment= lines,
# etc.) and drop the fallback -- credentials shouldn't live in source control.
CLIENT_ID = os.environ.get("HYPERVIEW_CLIENT_ID", "7ecbda12-6dcb-4ace-b9ea-f2ab44b23486")
CLIENT_SECRET = os.environ.get("HYPERVIEW_CLIENT_SECRET", "3ff54714-90d4-46ea-9054-545ebc20d87d")

CACHE_SECONDS = 2
ASSET_CACHE_SECONDS = 3600

LOCATION_GROUPS = [
    "Centerpoint", "Covenant West", "Fort Hill", "Claiborne", "Cumberland",
    "Fort Loudoun", "Fort Sanders Regional", "LeConte", "Methodist",
    "Morristown-Hamblen", "Parkwest", "Peninsula", "Roane",
]
CLINIC_GROUPS = [
    "Covenant HomeCare", "Crossville Medical", "FamilyCare Specialists",
    "Morristown West", "Peninsula Lighthouse", "Southern Medical",
    "Thompson Proton", "Topside",
]
CATEGORIES = ["power", "facilities", "compute", "network"]

# Centerpoint and Fort Hill are datacenters, not hospitals -- rendered with
# a distinct icon and weighted higher (see location_multiplier below).
DATACENTER_LOCATIONS = {"Centerpoint", "Fort Hill"}

# ---------------------------------------------------------------------------
# DEVICE IDENTIFICATION (HIGH CONFIDENCE - via assetTypeId)
# ---------------------------------------------------------------------------
COMPUTE_TYPES = {"server", "virtualServer", "bladeServer", "nodeServer", "kvmSwitch"}
NETWORK_TYPES = {"networkDevice", "bladeNetwork"}
POWER_TYPES = {
    "ups", "smallUps", "rackPdu", "pduAndRpp", "powerMeter", "generator",
    "transferSwitch", "utilityBreaker", "dcRectifier", "batteryBank",
    "switchboard", "switchgear",
}
FACILITY_TYPES = {
    "crac", "crah", "chiller", "environmental", "inRowCooling",
    "fireControlPanel", "location", "rack",
}
RACK_LOCATION_TYPES = {"rack", "location"}

# ---------------------------------------------------------------------------
# ASSET-TYPE WEIGHTING (used only for the overall-health score, on top of
# the existing category and location weighting -- reflects how critical a
# given asset TYPE is to facility operations, e.g. a UPS or generator
# outage matters more than, say, a generic environmental sensor.
# Tune these sets/multipliers to taste.
# ---------------------------------------------------------------------------
UPS_ASSET_TYPES = {"ups"}
SMALL_UPS_ASSET_TYPES = {"smallUps"}
CRITICAL_INFRA_ASSET_TYPES = {
    "generator", "chiller", "switchgear", "switchboard", "transferSwitch",
    "dcRectifier", "batteryBank", "crac", "crah", "utilityBreaker", "location",
}
IMPORTANT_ASSET_TYPES = {
    "networkDevice", "bladeNetwork", "pduAndRpp", "powerMeter",
    "inRowCooling", "fireControlPanel", "rack",
}
# Nutanix/ESX hosts are technically "server"/"virtualServer" assetTypeIds
# (standard tier), but at some sites they house the entire local IT stack,
# so match on name instead and weight them like other "important" assets.
VIRTUALIZATION_HOST_NAME_PATTERNS = ["esx", "ntnx", "nutanix"]
ASSET_TYPE_WEIGHT_UPS = 2.0
ASSET_TYPE_WEIGHT_SMALL_UPS = 1.75
ASSET_TYPE_WEIGHT_CRITICAL = 1.5
ASSET_TYPE_WEIGHT_IMPORTANT = 1.2
ASSET_TYPE_WEIGHT_STANDARD = 1.0

# ---------------------------------------------------------------------------
# DEVICE NAME PATTERNS (MEDIUM CONFIDENCE)
# ---------------------------------------------------------------------------
POWER_DEVICE_PATTERNS = [
    "ups", "pdu", "rpp", "generator", "asco", "ats", "sts", "switchgear",
    "switchboard", "rectifier",
]
FACILITIES_DEVICE_PATTERNS = [
    "crac", "crah", "ahu", "au-", "-au-", "-au", "air unit", "chiller",
    "hvac", "rtu", "vesda", "fdc", "fpc",
]
NETWORK_DEVICE_PATTERNS = ["-ap", "_ap", ".covhlth.net", "firewall", "router", "switch"]

# ---------------------------------------------------------------------------
# ALARM CONTENT (LOWER CONFIDENCE)
# ---------------------------------------------------------------------------
POWER_ALARM_PATTERNS = [
    "battery", "runtime", "voltage", "current", "amps", "amperage",
    "breaker", "phase a", "phase b", "phase c", "ac input", "on battery",
    "utility power", "commercial power", "line voltage",
]
FACILITIES_ALARM_PATTERNS = [
    "fan", "fan failure", "thermal overload", "cooling", "temperature",
    "humidity", "airflow", "compressor", "condenser", "supply air",
    "return air", "water leak", "leak", "environmental", "smoke", "fire alarm",
]
NETWORK_ALARM_PATTERNS = ["interface", "uplink", "ethernet", "vlan", "port", "link down", "network"]
COMPUTE_ALARM_PATTERNS = [
    "server", "vmware", "esx", "esxi", "hyper-v", "storage", "cluster", "sql", "oracle",
]

RACK_FACILITIES_KEYWORDS = [
    "AVERAGE TEMPERATURE", "LOCATION AVERAGE TEMPERATURE",
    "RACKAVERAGEFRONTTEMPERATURE", "RACKAVERAGEREARTEMPERATURE",
    "LOCATIONAVERAGETEMPERATURE", "TEMPERATURE", "HUMIDITY", "AIRFLOW", "COOLING",
]
RACK_POWER_KEYWORDS = [
    "RACK TOTAL POWER", "RACKTOTALPOWER", "IT ENERGY", "ITENERGY", "POWER",
    "LOAD", "CURRENT", "VOLTAGE", "PDU", "UPS", "BREAKER",
]

# ---------------------------------------------------------------------------
# MANUAL OVERRIDES (checked before pattern matching, by asset name substring)
# ---------------------------------------------------------------------------
DEVICE_OVERRIDES = {
    # Facilities
    "MMC-AU": "facilities", "-AU-": "facilities", "AHU": "facilities",
    "CRAC": "facilities", "CRAH": "facilities", "CHILLER": "facilities",
    "FDC": "facilities", "FPC": "facilities", "VESDA": "facilities",
    # Power
    "UPS": "power", "PDU": "power", "RPP": "power", "GEN": "power",
    "ATS": "power", "STS": "power",
    # Network
    "-AP": "network", "FW-": "network", "RTR": "network", "SW-": "network",
}

# ---------------------------------------------------------------------------
# LOCATION PATH MAPS
# ---------------------------------------------------------------------------
HOSPITAL_PATH_PREFIXES = {
    "All / Hospitals / Claiborne Medical Center": "Claiborne",
    "All / Hospitals / Cumberland Medical Center": "Cumberland",
    "All / Hospitals / Fort Loudoun Medical Center": "Fort Loudoun",
    "All / Hospitals / Fort Sanders Regional Medical Center": "Fort Sanders Regional",
    "All / Hospitals / LeConte Medical Center": "LeConte",
    "All / Hospitals / Methodist Medical Center": "Methodist",
    "All / Hospitals / Morristown-Hamblen Medical Center": "Morristown-Hamblen",
    "All / Hospitals / Parkwest Medical Center": "Parkwest",
    "All / Hospitals / Peninsula Behavioral Health": "Peninsula",
    "All / Hospitals / Roane Medical Center": "Roane",
}
# Standalone prefixes that map directly to a location group (no sub-lookup needed).
DIRECT_PATH_PREFIXES = {
    "All / Centerpoint": "Centerpoint",
    "All / Covenant West": "Covenant West",
    "All / Fort Hill": "Fort Hill",
}
CLINIC_PATH_PREFIXES = {
    "All / Primary Care/Clinics / Covenant HomeCare": "Covenant HomeCare",
    "All / Primary Care/Clinics / Crossville Medical Group": "Crossville Medical",
    "All / Primary Care/Clinics / FamilyCare Specialists": "FamilyCare Specialists",
    "All / Primary Care/Clinics / Morristown West": "Morristown West",
    "All / Primary Care/Clinics / Peninsula Lighthouse": "Peninsula Lighthouse",
    "All / Primary Care/Clinics / Southern Medical Group": "Southern Medical",
    "All / Primary Care/Clinics / Thompson Proton Center": "Thompson Proton",
    "All / Primary Care/Clinics / Topside": "Topside",
}

# ---------------------------------------------------------------------------
# REDUNDANCY RULES
# ---------------------------------------------------------------------------
# The point-based overall-health score doesn't always reflect a real loss of
# redundancy fast enough (e.g. a datacenter can lose most of its cooling and
# still only read "Significant"). Each rule below names a specific group of
# units at a site and forces overall-health to "Critical" outright once
# >= critical_threshold of them are simultaneously critical, on top of
# whatever the point math says. Every site is different -- add rules here as
# unit counts/thresholds are confirmed per site.
# Prefer "asset_ids" (Hyperview assetId GUIDs) -- immune to device renames.
# "asset_names" is a fallback used only when a rule has no asset_ids yet;
# look up real IDs via GET /type/<device name> (returns "id") and move them
# into asset_ids once known.
REDUNDANCY_RULES = [
    {
        "label": "Centerpoint main CRAC units",
        "asset_ids": set(),
        "asset_names": {"CP-DC-CRAC2", "CP-DC-CRAC3", "CP-DC-CRAC4"},
        "critical_threshold": 2,
    },
]

# ---------------------------------------------------------------------------
# IN-MEMORY CACHES
# ---------------------------------------------------------------------------
access_token = None
token_expiry = 0

asset_cache = {}
asset_cache_time = 0

alarms_cache = None
alarms_cache_time = 0

matrix_cache = None
matrix_cache_time = 0
last_matrix_update = None

clinic_matrix_cache = None
clinic_matrix_cache_time = 0


# ---------------------------------------------------------------------------
# ALARM MESSAGE HELPER
# ---------------------------------------------------------------------------
def get_alarm_message(alarm):
    template = alarm.get("textTemplate", "")
    try:
        values = json.loads(alarm.get("propertyValues", "{}"))
        for key, value in values.items():
            template = template.replace(f"{{{key}}}", str(value))
    except (json.JSONDecodeError, TypeError):
        pass
    return template


# ---------------------------------------------------------------------------
# CATEGORY DETECTION
# ---------------------------------------------------------------------------
def determine_category(alarm):
    asset_name = str(alarm.get("assetName", "")).upper()
    alarm_text = str(get_alarm_message(alarm) or "").upper()
    asset = get_asset_cache().get(alarm.get("assetId"))

    if asset:
        asset_type = asset.get("assetTypeId", "unknown")

        if asset_type in RACK_LOCATION_TYPES:
            if any(keyword in alarm_text for keyword in RACK_FACILITIES_KEYWORDS):
                return "facilities"
            if any(keyword in alarm_text for keyword in RACK_POWER_KEYWORDS):
                return "power"
        if asset_type in COMPUTE_TYPES:
            return "compute"
        if asset_type in NETWORK_TYPES:
            return "network"
        if asset_type in POWER_TYPES:
            return "power"
        if asset_type in FACILITY_TYPES:
            return "facilities"

    for pattern, category in DEVICE_OVERRIDES.items():
        if pattern in asset_name:
            return category

    if any(pattern.upper() in asset_name for pattern in POWER_DEVICE_PATTERNS):
        return "power"
    if any(pattern.upper() in asset_name for pattern in FACILITIES_DEVICE_PATTERNS):
        return "facilities"
    if any(pattern.upper() in asset_name for pattern in NETWORK_DEVICE_PATTERNS):
        return "network"

    if any(pattern.upper() in alarm_text for pattern in POWER_ALARM_PATTERNS):
        return "power"
    if any(pattern.upper() in alarm_text for pattern in FACILITIES_ALARM_PATTERNS):
        return "facilities"
    if any(pattern.upper() in alarm_text for pattern in NETWORK_ALARM_PATTERNS):
        return "network"
    if any(pattern.upper() in alarm_text for pattern in COMPUTE_ALARM_PATTERNS):
        return "compute"

    return "compute"


def get_asset_type_weight(alarm):
    """Multiplier reflecting how critical this asset's *type* is, independent
    of alarm category and site location. Used only in overall-health."""
    asset = get_asset_cache().get(alarm.get("assetId"))
    asset_type = asset.get("assetTypeId") if asset else None
    if asset_type in UPS_ASSET_TYPES:
        return ASSET_TYPE_WEIGHT_UPS
    if asset_type in SMALL_UPS_ASSET_TYPES:
        return ASSET_TYPE_WEIGHT_SMALL_UPS
    if asset_type in CRITICAL_INFRA_ASSET_TYPES:
        return ASSET_TYPE_WEIGHT_CRITICAL
    if asset_type in IMPORTANT_ASSET_TYPES:
        return ASSET_TYPE_WEIGHT_IMPORTANT

    asset_name = str(alarm.get("assetName", "")).lower()
    if any(pattern in asset_name for pattern in VIRTUALIZATION_HOST_NAME_PATTERNS):
        return ASSET_TYPE_WEIGHT_IMPORTANT

    return ASSET_TYPE_WEIGHT_STANDARD


# ---------------------------------------------------------------------------
# TOKEN / HYPERVIEW API
# ---------------------------------------------------------------------------
def get_token():
    global access_token, token_expiry
    if access_token and time.time() < token_expiry:
        return access_token

    response = requests.post(
        f"{TENANT_URL}/connect/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "HyperviewManagerApi",
        },
        timeout=30,
    )
    response.raise_for_status()
    token_json = response.json()
    access_token = token_json["access_token"]
    token_expiry = time.time() + token_json["expires_in"] - 60
    logger.info("Obtained new Hyperview token")
    return access_token


def hv_get(path):
    token = get_token()
    response = requests.get(
        f"{TENANT_URL}/api/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def hv_put(path, json_body):
    token = get_token()
    response = requests.put(
        f"{TENANT_URL}/api/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        json=json_body,
        timeout=30,
    )
    response.raise_for_status()
    return response


def hv_delete(path):
    token = get_token()
    response = requests.delete(
        f"{TENANT_URL}/api/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    return response


def delete_asset(asset_id):
    """Permanently removes an asset from Hyperview - used by the rack
    audit workflow when an auditor flags a device to come out of
    inventory. No undo on this end once it's sent; Hyperview's own trash/
    recovery (if any) is the only safety net past this point."""
    hv_delete(f"asset/assets/{asset_id}")


def get_asset_cache():
    global asset_cache, asset_cache_time
    if asset_cache and (time.time() - asset_cache_time) < ASSET_CACHE_SECONDS:
        return asset_cache

    cache = {}
    offset = 0
    while True:
        page = hv_get(f"asset/assets?(limit)=100&(after)={offset}")
        assets = page.get("data", [])
        if not assets:
            break
        for asset in assets:
            cache[asset["id"]] = asset
        offset += 100
        if offset >= page["_metadata"]["total"]:
            break

    asset_cache = cache
    asset_cache_time = time.time()
    return cache


def get_real_active_alarms():
    global alarms_cache, alarms_cache_time
    if alarms_cache is not None and time.time() - alarms_cache_time < CACHE_SECONDS:
        return alarms_cache

    alarms = hv_get("asset/alarmEvents/allAssets/advancedCollection").get("data", [])
    filtered = [
        alarm for alarm in alarms
        if alarm.get("isActive") and str(alarm.get("severity", "")).lower() != "information"
    ]

    alarms_cache = filtered
    alarms_cache_time = time.time()
    return filtered


def is_acknowledged(alarm):
    return str(alarm.get("acknowledgementState", "")).lower() == "acknowledged"


# ---------------------------------------------------------------------------
# RACK AUDIT
# ---------------------------------------------------------------------------
# The custom field's display name as set up in Hyperview - matched against
# CustomAssetPropertyDto.name, not a fixed key id, since a custom field
# (unlike Hyperview's own built-in asset properties) has no stable
# cross-tenant identifier this service can hardcode.
RACK_AUDIT_DATE_FIELD_NAME = os.environ.get("HYPERVIEW_AUDIT_DATE_FIELD_NAME", "Last Audit Date")
RACK_AUDIT_FREQUENCY_FIELD_NAME = os.environ.get("HYPERVIEW_AUDIT_FREQUENCY_FIELD_NAME", "Audit Frequency")
RACK_AUDIT_CACHE_SECONDS = int(os.environ.get("RACK_AUDIT_CACHE_SECONDS", "3600"))

# How many days a rack's stated frequency allows between audits.
RACK_AUDIT_FREQUENCY_DAYS = {"annually": 365, "biennially": 730}

rack_audit_cache = None
rack_audit_cache_time = 0


def _unwrap_list(resp):
    """This tenant's list endpoints wrap results as {"data": [...],
    "_metadata": {...}} (see get_asset_cache() above) rather than the bare
    array some Hyperview API doc versions describe - accept either shape
    defensively rather than assuming one."""
    if isinstance(resp, dict):
        return resp.get("data", [])
    return resp or []


def _rack_site_name(asset):
    """No dedicated 'site' field on an asset - approximate it from its
    tab-delimited hierarchy path (Site > Room > Row > Rack). The first
    segment is Hyperview's generic top-level "All" grouping node, not a
    real location - skip it and use the next segment down (the actual
    site/location name), falling back to the immediate parent's name if
    the path is too short to have one."""
    path = asset.get("tabDelimitedPath") or ""
    segments = [s.strip() for s in path.split("\t") if s.strip()]
    if segments and segments[0].lower() == "all":
        segments = segments[1:]
    if segments:
        return segments[0]
    return asset.get("parentName") or "Unknown"


def _rack_site_path(asset):
    """Display-only companion to _rack_site_name: the whole hierarchy path
    (Site > Room > Row > Rack) with the generic "All" root stripped, instead
    of just the first segment. Not used for site filtering/scoping - only
    for showing an auditor exactly where a rack lives."""
    path = asset.get("tabDelimitedPath") or ""
    segments = [s.strip() for s in path.split("\t") if s.strip()]
    if segments and segments[0].lower() == "all":
        segments = segments[1:]
    if segments:
        return " › ".join(segments)
    return asset.get("parentName") or "Unknown"


def get_audit_date_property(asset_id):
    """The 'Last Audit Date' custom property record for one asset, or None
    if that asset has no such property (not applicable to its asset type,
    or the field hasn't been configured for it in Hyperview)."""
    props = _unwrap_list(hv_get(f"asset/customAssetProperties/{asset_id}"))
    for p in props:
        if p.get("name") == RACK_AUDIT_DATE_FIELD_NAME:
            return p
    return None


def get_audit_frequency_property(asset_id):
    """The 'Audit Frequency' custom property record for one asset (its
    value is "Annually" or "Biennially"), or None if the asset has no such
    property configured."""
    props = _unwrap_list(hv_get(f"asset/customAssetProperties/{asset_id}"))
    for p in props:
        if p.get("name") == RACK_AUDIT_FREQUENCY_FIELD_NAME:
            return p
    return None


def rack_audit_compliance(last_audit, frequency):
    """How out of compliance a rack is, from its 'Last Audit Date' and
    'Audit Frequency' custom properties. Returns a dict with a status
    ("never_audited" / "overdue" / "due_soon" / "current" / "unknown") and,
    where computable, days_overdue (negative once "due soon" turns
    positive-in-the-future would be misleading, so this is only set once a
    rack is actually overdue) and a due date. "unknown" covers a frequency
    Hyperview holds that isn't one of the two values this tenant uses."""
    if not last_audit:
        return {"status": "never_audited", "due": None, "days_overdue": None}
    freq_days = RACK_AUDIT_FREQUENCY_DAYS.get((frequency or "").strip().lower())
    if freq_days is None:
        return {"status": "unknown", "due": None, "days_overdue": None}
    try:
        audited = datetime.fromisoformat(last_audit.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return {"status": "unknown", "due": None, "days_overdue": None}
    if audited.tzinfo is None:
        audited = audited.replace(tzinfo=timezone.utc)
    due = audited + timedelta(days=freq_days)
    now = datetime.now(timezone.utc)
    if now > due:
        return {"status": "overdue", "due": due, "days_overdue": (now - due).days}
    if now > due - timedelta(days=30):
        return {"status": "due_soon", "due": due, "days_overdue": None}
    return {"status": "current", "due": due, "days_overdue": None}


def set_audit_date_now(asset_id):
    """Stamps this asset's 'Last Audit Date' custom property to now.
    Returns False (no-op) if the asset has no such property - nothing to
    write back to."""
    prop = get_audit_date_property(asset_id)
    if not prop or not prop.get("id"):
        return False
    hv_put(f"asset/customAssetProperties/{prop['id']}", {
        "id": prop["id"],
        "customAssetPropertyKeyId": prop["customAssetPropertyKeyId"],
        "value": datetime.now(timezone.utc).isoformat(),
        "dataType": prop.get("dataType", "dateTime"),
    })
    return True


def get_rack_audit_cache():
    """Every rack asset with its 'Last Audit Date' custom property value,
    rebuilt at most once every RACK_AUDIT_CACHE_SECONDS - avoids re-fetching
    every single rack's custom properties (one call each) on every
    /rack-audit/next request."""
    global rack_audit_cache, rack_audit_cache_time
    if rack_audit_cache is not None and time.time() - rack_audit_cache_time < RACK_AUDIT_CACHE_SECONDS:
        return rack_audit_cache

    racks = []
    offset = 0
    while True:
        page = hv_get(f"asset/assets?assetType=rack&includeDimensions=true&(limit)=100&(after)={offset}")
        page_racks = _unwrap_list(page)
        if not page_racks:
            break
        racks.extend(page_racks)
        offset += 100
        total = page.get("_metadata", {}).get("total") if isinstance(page, dict) else None
        if (total is not None and offset >= total) or (total is None and len(page_racks) < 100):
            break

    entries = []
    for rack in racks:
        rack_id = rack.get("id")
        if not rack_id:
            continue
        try:
            audit_prop = get_audit_date_property(rack_id)
        except requests.exceptions.RequestException:
            logger.exception("rack audit: could not read custom properties for rack %s", rack_id)
            audit_prop = None
        try:
            frequency_prop = get_audit_frequency_property(rack_id)
        except requests.exceptions.RequestException:
            logger.exception("rack audit: could not read audit frequency for rack %s", rack_id)
            frequency_prop = None
        last_audit = audit_prop["value"] if audit_prop else None
        audit_frequency = frequency_prop["value"] if frequency_prop else None
        entries.append({
            "id": rack_id,
            "name": rack.get("name"),
            "site": _rack_site_name(rack),
            "site_path": _rack_site_path(rack),
            "last_audit": last_audit,
            "audit_frequency": audit_frequency,
            "compliance": rack_audit_compliance(last_audit, audit_frequency),
            # Total rack height in U - the elevation always shows every U
            # slot the rack actually has, not just the range that happens
            # to be occupied. None if Hyperview has no dimension on record
            # for this rack; the elevation renderer falls back sensibly.
            "total_u": (rack.get("dimension") or {}).get("providedRackUnits"),
        })

    rack_audit_cache = entries
    rack_audit_cache_time = time.time()
    return entries


def get_device_power_sources(asset_id):
    """Which PDU + outlet is powering this asset (a dual-corded device has
    two), e.g. "PDU-A2 Outlet 18". providingSourceAssetDisplayName on the
    association is the outlet's own name ("Outlet 18") - it doesn't say
    which PDU that outlet belongs to. The outlet is itself an asset in the
    general asset directory though, and its parentName there is the PDU
    that owns it, so look that up and lead with it. Falls back to just the
    outlet name if the outlet isn't resolvable in the asset cache (no
    providingSourceAssetId on the association, or it's not in the
    directory) rather than dropping the power info entirely."""
    try:
        assocs = _unwrap_list(hv_get(f"asset/powerSourceAssociations?consumingDestinationAssetId={asset_id}"))
    except requests.exceptions.RequestException:
        logger.exception("rack audit: could not read power sources for asset %s", asset_id)
        return []
    cache = get_asset_cache()
    out = []
    for a in assocs:
        outlet_name = a.get("providingSourceAssetDisplayName")
        if not outlet_name:
            continue
        outlet = cache.get(a.get("providingSourceAssetId"))
        pdu_name = outlet.get("parentName") if outlet else None
        out.append(f"{pdu_name} {outlet_name}" if pdu_name else outlet_name)
    return out


def get_rack_contained_assets(rack_id):
    """Elevation entries (U position, side, power source PDUs) for
    everything mounted in this rack. Deliberately NOT
    assetTrackerContainedAssets - that belongs to Hyperview's separate
    "Asset Tracker" feature, an add-on module this tenant doesn't have.
    Every asset's own record (from the same general asset directory
    get_asset_cache() already builds for other uses) already carries
    locationData - parentId, rackULocation, rackSide - inline, so a
    rack's contents are just every asset whose parentId is this rack, no
    separate elevation-specific call needed."""
    cache = get_asset_cache()
    out = []
    for asset in cache.values():
        loc = asset.get("locationData") or {}
        if loc.get("parentId") != rack_id:
            continue
        asset_id = asset.get("id")
        out.append({
            "id": asset_id,
            "name": asset.get("name") or "(unknown)",
            "type": asset.get("assetTypeId"),
            "manufacturer": asset.get("manufacturerName"),
            "model": asset.get("productName"),
            "serial": asset.get("serialNumber"),
            "u_location": loc.get("rackULocation"),
            "side": loc.get("rackSide"),
            "power_sources": get_device_power_sources(asset_id) if asset_id else [],
        })
    return out


def find_next_rack_to_audit(sites=None):
    """Walks racks most-overdue-first (never-audited racks sort first, then
    oldest audit date first), silently stamping any EMPTY rack's audit date
    and skipping it, until it lands on a rack that actually has something
    mounted in it - that's the one returned for a real, physical audit.
    sites=None (or empty) means every site is eligible."""
    racks = get_rack_audit_cache()
    if sites:
        sites_ci = {s.strip().lower() for s in sites}
        racks = [r for r in racks if (r["site"] or "").strip().lower() in sites_ci]
    ordered = sorted(racks, key=lambda r: (r["last_audit"] is not None, r["last_audit"] or ""))

    for rack in ordered:
        contained = get_rack_contained_assets(rack["id"])
        if contained:
            return rack, contained
        try:
            set_audit_date_now(rack["id"])
        except requests.exceptions.RequestException:
            logger.exception("rack audit: failed to auto-stamp empty rack %s", rack["id"])
            continue
    return None, None


# ---------------------------------------------------------------------------
# LOCATION GROUPING
# ---------------------------------------------------------------------------
def get_location_group(path):
    if not path:
        return None
    for prefix, location in DIRECT_PATH_PREFIXES.items():
        if path.startswith(prefix):
            return location
    for prefix, location in HOSPITAL_PATH_PREFIXES.items():
        if path.startswith(prefix):
            return location
    return None


def get_clinic_group(path):
    if not path:
        return None
    for prefix, location in CLINIC_PATH_PREFIXES.items():
        if path.startswith(prefix):
            return location
    return None


# ---------------------------------------------------------------------------
# MATRIX BUILDING (shared by hospital + clinic matrices)
# ---------------------------------------------------------------------------
def _score_category_total(open_count, ack_count):
    """Open alarms count at face value; all-acknowledged sites get pushed
    into the 1000+ range so the UI can distinguish "open" from "acked"."""
    total = open_count + ack_count
    if total > 0 and open_count == 0:
        return 1000 + total
    return total


def _build_group_matrix(groups, group_fn, display_fn):
    device_states = {}
    for alarm in get_real_active_alarms():
        asset_id = alarm.get("assetId")
        if not asset_id:
            continue
        location = group_fn(alarm.get("assetLocationPath"))
        if not location:
            continue
        category = determine_category(alarm)
        if category not in CATEGORIES:
            continue

        key = (location, category, asset_id)
        state = device_states.setdefault(key, {"open": False, "ack": False})
        if is_acknowledged(alarm):
            state["ack"] = True
        else:
            state["open"] = True

    counts = {
        location: {f"{cat}_{state}": 0 for cat in CATEGORIES for state in ("open", "ack")}
        for location in groups
    }
    for (location, category, _asset_id), state in device_states.items():
        bucket = "open" if state["open"] else "ack" if state["ack"] else None
        if bucket:
            counts[location][f"{category}_{bucket}"] += 1

    result = []
    for location in groups:
        c = counts[location]
        row = {"location": location, "locationDisplay": display_fn(location)}
        site = False
        for category in CATEGORIES:
            open_count = c[f"{category}_open"]
            ack_count = c[f"{category}_ack"]
            row[category] = _score_category_total(open_count, ack_count)
            site = site or (open_count + ack_count) > 0
        row["site"] = 1 if site else 0
        row["unknown"] = 0
        result.append(row)

    result.sort(key=lambda x: (-x["site"], x["location"]))
    return result


def _hospital_display(location):
    icon = "\U0001F3E2" if location in DATACENTER_LOCATIONS else "\U0001F3E5"  # datacenter / hospital
    return f"{icon} {location}"


def _clinic_display(location):
    return f"\U0001FA7A {location}"  # stethoscope


def build_matrix():
    global matrix_cache, matrix_cache_time, last_matrix_update
    if matrix_cache is not None and time.time() - matrix_cache_time < CACHE_SECONDS:
        return matrix_cache

    matrix_cache = _build_group_matrix(LOCATION_GROUPS, get_location_group, _hospital_display)
    matrix_cache_time = time.time()
    last_matrix_update = datetime.now()
    return matrix_cache


def build_clinic_matrix():
    global clinic_matrix_cache, clinic_matrix_cache_time
    if clinic_matrix_cache is not None and time.time() - clinic_matrix_cache_time < CACHE_SECONDS:
        return clinic_matrix_cache

    clinic_matrix_cache = _build_group_matrix(CLINIC_GROUPS, get_clinic_group, _clinic_display)
    clinic_matrix_cache_time = time.time()
    return clinic_matrix_cache


# ---------------------------------------------------------------------------
# ALARM PRIORITY / DEVICE ROLLUP HELPERS
# ---------------------------------------------------------------------------
SEVERITY_RANK = {"critical": 2, "warning": 1}


def _highest_severity_per_device(alarms):
    """Collapse alarms to one (highest severity) alarm per assetId."""
    devices = {}
    for alarm in alarms:
        asset_id = alarm.get("assetId")
        if not asset_id:
            continue
        rank = SEVERITY_RANK.get(str(alarm.get("severity", "")).lower(), 0)
        existing = devices.get(asset_id)
        if existing is None or rank > existing["rank"]:
            devices[asset_id] = {"alarm": alarm, "rank": rank}
    return devices


def _check_redundancy_overrides(devices):
    """Check REDUNDANCY_RULES against the current highest-severity-per-device
    alarms. Returns the list of rule labels that are currently tripped (i.e.
    >= critical_threshold of a rule's units are simultaneously critical) --
    an empty list means no override is in effect. Rules with asset_ids match
    on assetId (robust to renames); rules without fall back to assetName."""
    critical_ids = {
        asset_id for asset_id, device in devices.items()
        if str(device["alarm"].get("severity", "")).lower() == "critical"
    }
    critical_names = {
        str(device["alarm"].get("assetName", "")).upper()
        for device in devices.values()
        if str(device["alarm"].get("severity", "")).lower() == "critical"
    }
    triggered = []
    for rule in REDUNDANCY_RULES:
        asset_ids = rule.get("asset_ids") or set()
        if asset_ids:
            matched = len(critical_ids & asset_ids)
        else:
            rule_names = {name.upper() for name in rule.get("asset_names", set())}
            matched = len(critical_names & rule_names)
        if matched >= rule["critical_threshold"]:
            triggered.append(rule["label"])
    return triggered


def _alarm_priority(alarm):
    severity = str(alarm.get("severity", "")).lower()
    category = str(alarm.get("alarmEventCategory", "")).lower()
    if severity == "critical" and category != "hostunreachable":
        return 3
    if category == "hostunreachable":
        return 2
    if severity == "warning":
        return 1
    return 0


# ---------------------------------------------------------------------------
# ERROR HANDLING
# ---------------------------------------------------------------------------
@app.errorhandler(requests.exceptions.RequestException)
def handle_upstream_error(exc):
    """Hyperview API unreachable, timed out, or returned a bad status."""
    logger.error("Hyperview API request failed: %s", exc)
    return jsonify({"error": "Upstream Hyperview API error", "detail": str(exc)}), 502


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unhandled error while serving %s", exc)
    return jsonify({"error": "Internal server error", "detail": str(exc)}), 500


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def root():
    return jsonify({"service": "Hyperview Matrix", "status": "running"})


@app.route("/health")
def health():
    try:
        get_token()
        return jsonify({"status": "healthy"})
    except Exception as ex:
        return jsonify({"status": "failed", "error": str(ex)}), 500


@app.route("/last-updated")
def last_updated():
    build_matrix()
    return jsonify([{"timestamp": int(last_matrix_update.timestamp() * 1000)}])


@app.route("/sites-affected")
def sites_affected():
    hospital_matrix = build_matrix()
    clinic_matrix = build_clinic_matrix()
    affected = sum(row["site"] for row in hospital_matrix) + sum(row["site"] for row in clinic_matrix)
    total = len(hospital_matrix) + len(clinic_matrix)
    return jsonify([{
        "affected": affected,
        "healthy": total - affected,
        "total": total,
        "display": f"{affected}/{total}",
    }])


@app.route("/clinic-health-matrix")
def clinic_health_matrix():
    return jsonify(build_clinic_matrix())


@app.route("/location-health-matrix")
def location_health_matrix():
    return jsonify(build_matrix())


@app.route("/category-summary")
def category_summary():
    device_states = {}
    for alarm in get_real_active_alarms():
        path = alarm.get("assetLocationPath")

        location = (
            get_location_group(path)
            or get_clinic_group(path)
        )

        # Ignore All Discovered / unmapped devices
        if not location:
            continue
        category = determine_category(alarm)
        if category not in CATEGORIES:
            continue
        asset_id = alarm.get("assetId")
        if not asset_id:
            continue
        key = (category, asset_id)
        state = device_states.setdefault(key, {"open": False, "ack": False})
        if is_acknowledged(alarm):
            state["ack"] = True
        else:
            state["open"] = True

    summary = {cat: {"open": 0, "ack": 0} for cat in CATEGORIES}
    for (category, _asset_id), state in device_states.items():
        if state["open"]:
            summary[category]["open"] += 1
        elif state["ack"]:
            summary[category]["ack"] += 1

    total_open = sum(summary[c]["open"] for c in summary)
    total_ack = sum(summary[c]["ack"] for c in summary)

    labels = {
        "power": "⚡ Power",
        "facilities": "❄ Cooling",
        "compute": "\U0001F5A5 Server",
        "network": "\U0001F310 Network",
    }
    result = [
        {"category": labels[cat], "open": summary[cat]["open"], "ack": summary[cat]["ack"]}
        for cat in CATEGORIES
    ]
    result.append({"category": "\U0001F4CB All", "open": total_open, "ack": total_ack})
    return jsonify(result)


@app.route("/operational-status")
def operational_status():
    total_open = 0
    total_ack = 0
    for alarm in get_real_active_alarms():
        if is_acknowledged(alarm):
            total_ack += 1
        else:
            total_open += 1

    if total_open > 0:
        payload = {
            "status": "Action Required", "color": "red", "value": 2,
            "display": f"\U0001F6A8 {total_open} Open Alarm(s)",
        }
    elif total_ack > 0:
        payload = {
            "status": "In Progress", "color": "blue", "value": 1,
            "display": f"\U0001F535 {total_ack} Ack'd Alarm(s)",
        }
    else:
        payload = {
            "status": "Normal", "color": "green", "value": 0,
            "display": "✅ No Active Alarms",
        }
    payload["open"] = total_open
    payload["ack"] = total_ack
    return jsonify([payload])


@app.route("/overall-health")
def overall_health():
    devices = _highest_severity_per_device(get_real_active_alarms())

    health = 100.0
    critical_devices = 0
    warning_devices = 0
    for device in devices.values():
        alarm = device["alarm"]
        severity = str(alarm.get("severity", "")).lower()
        category = determine_category(alarm)
        path = alarm.get("assetLocationPath", "")
        location = get_location_group(path)

        if location in DATACENTER_LOCATIONS:
            location_multiplier = 2.0
        elif location:
            location_multiplier = 1.5
        else:
            location_multiplier = 1.0

        asset_type_multiplier = get_asset_type_weight(alarm)
        multiplier = location_multiplier * asset_type_multiplier

        if category == "compute":
            critical_impact, warning_impact = 3, 1
        else:
            critical_impact, warning_impact = 6, 2

        if severity == "critical":
            critical_devices += 1
            health -= critical_impact * multiplier
        elif severity == "warning":
            warning_devices += 1
            health -= warning_impact * multiplier

    # Floor once at the end instead of per-device, so fractional impact
    # (e.g. 3.6 points) accumulates across devices instead of quietly
    # disappearing each time it's individually truncated.
    health = max(0, min(100, int(health)))

    # A tripped redundancy rule (e.g. a datacenter losing most of its
    # cooling) forces Critical outright, even if the point-based score
    # above hasn't dropped below the Critical cutoff yet.
    redundancy_alerts = _check_redundancy_overrides(devices)
    if redundancy_alerts:
        health = min(health, 39)

    hospital_matrix = build_matrix()
    clinic_matrix = build_clinic_matrix()
    hospital_sites = sum(row["site"] for row in hospital_matrix)
    clinic_sites = sum(row["site"] for row in clinic_matrix)

    if health == 100:
        emoji, state, banner = "✅", "Perfect", 4
    elif health >= 70:
        emoji, state, banner = "⚠️", "Degraded", 2
    elif health >= 40:
        emoji, state, banner = "\U0001F6A8", "Significant", 1
    else:
        emoji, state, banner = "\U0001F525", "Critical", 0

    return jsonify([{
        "health": health,
        "banner": banner,
        "emoji": emoji,
        "state": state,
        "hospitalSites": hospital_sites,
        "clinicSites": clinic_sites,
        "criticalDevices": critical_devices,
        "warningDevices": warning_devices,
        "redundancyAlerts": redundancy_alerts,
    }])


@app.route("/top-impacted-sites-stat")
def top_impacted_sites_stat():
    devices = _highest_severity_per_device(get_real_active_alarms())

    site_counts = {}
    for device in devices.values():
        alarm = device["alarm"]
        path = alarm.get("assetLocationPath")
        location = get_location_group(path) or get_clinic_group(path)
        if not location:
            continue
        site_counts[location] = site_counts.get(location, 0) + 1

    top_sites = sorted(site_counts.items(), key=lambda x: -x[1])[:3]

    lines = []
    for location, count in top_sites:
        if location in DATACENTER_LOCATIONS:
            icon = "\U0001F3E2"
        elif location in CLINIC_GROUPS:
            icon = "\U0001FA7A"
        else:
            icon = "\U0001F3E5"
        lines.append(f"{icon} {location} ({count})")

    return jsonify([{"display": "\n".join(lines)}])


@app.route("/active-alarm-log")
def active_alarm_log():
    devices = {}
    all_alarms_by_device = {}
    for alarm in get_real_active_alarms():
        asset_id = alarm.get("assetId")
        if not asset_id:
            continue
        all_alarms_by_device.setdefault(asset_id, []).append(alarm)
        existing = devices.get(asset_id)
        if existing is None or _alarm_priority(alarm) > _alarm_priority(existing):
            devices[asset_id] = alarm

    rows = []
    for asset_id, alarm in devices.items():
        path = alarm.get("assetLocationPath")

        location = (
            get_location_group(path)
            or get_clinic_group(path)
        )

        if not location:
            continue

        # A device can have more than one active alarm at once, and which
        # one wins _alarm_priority (and so gets shown here) can shift poll
        # to poll as severities change. If 'acknowledged' only reflected
        # that one winning alarm, a device someone fully acknowledged could
        # flicker back to unacknowledged (and forward again) purely because
        # a different one of its alarms briefly outranked it - which reads
        # as a fresh acknowledgment each time to anything watching this feed
        # for changes. Aggregating over every alarm on the device keeps it
        # stable: acknowledged only once none of them are still open.
        acknowledged = all(is_acknowledged(a) for a in all_alarms_by_device[asset_id])

        row = {
            "id": alarm.get("id"),
            "location": location,
            "device": alarm.get("assetName"),
            "category": determine_category(alarm),
            "severity": alarm.get("severity"),
            "alarm": get_alarm_message(alarm),
            "acknowledged": acknowledged,
        }
        rows.append((alarm, row))

    # Sort by the real alarm's priority (alarmEventCategory is right here on
    # the source alarm) rather than re-guessing "hostUnreachable" from the
    # rendered alarm text, which silently mis-sorted any alarm whose message
    # didn't happen to contain the substring "not reachable".
    rows.sort(key=lambda pair: (-_alarm_priority(pair[0]), pair[1]["location"] or "", pair[1]["device"] or ""))
    return jsonify([row for _alarm, row in rows])


_ACK_STATES = {"acknowledged", "unacknowledged"}


@app.route("/alarm-event/<alarm_event_id>/acknowledgement-state/<state>", methods=["PUT"])
def set_alarm_acknowledgement_state(alarm_event_id, state):
    if state not in _ACK_STATES:
        return jsonify({"error": f"state must be one of {sorted(_ACK_STATES)}"}), 400
    hv_put(f"asset/alarmEvents/acknowledgementState/{alarm_event_id}", {"acknowledgementState": state})
    # The next /active-alarm-log read should reflect this right away rather
    # than waiting up to CACHE_SECONDS for a stale cached list - a caller
    # that just acted on this alarm expects to see the change immediately.
    global alarms_cache
    alarms_cache = None
    return jsonify({"ok": True})


@app.route("/alarm-events/bulk-acknowledgement-state", methods=["PUT"])
def set_bulk_alarm_acknowledgement_state():
    body = request.get_json(silent=True) or {}
    alarm_event_ids = body.get("alarmEventIds") or []
    state = body.get("acknowledgementState")
    if state not in _ACK_STATES:
        return jsonify({"error": f"acknowledgementState must be one of {sorted(_ACK_STATES)}"}), 400
    if not alarm_event_ids or not isinstance(alarm_event_ids, list):
        return jsonify({"error": "alarmEventIds must be a non-empty array"}), 400
    hv_put("asset/alarmEvents/bulkAcknowledgementStates",
           {"alarmEventIds": alarm_event_ids, "acknowledgementState": state})
    global alarms_cache
    alarms_cache = None
    return jsonify({"ok": True})


@app.route("/rack-audit/next")
def rack_audit_next():
    sites_param = request.args.get("sites", "")
    sites = {s.strip() for s in sites_param.split(",") if s.strip()} or None
    try:
        rack, contained = find_next_rack_to_audit(sites)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach Hyperview: {e}"}), 502
    if rack is None:
        return jsonify({"error": "No eligible rack with any mounted assets was found"}), 404
    return jsonify({"rack": rack, "assets": contained})


@app.route("/rack-audit/compliance")
def rack_audit_compliance_list():
    """Every rack with its site, last audit date, audit frequency, and
    computed compliance status - the data set behind the Rack Audit
    Management page's compliance table. Unlike /rack-audit/next this
    doesn't walk/stamp anything, it's a read-only report."""
    try:
        racks = get_rack_audit_cache()
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach Hyperview: {e}"}), 502
    out = []
    for r in racks:
        entry = dict(r)
        compliance = dict(entry.get("compliance") or {})
        if compliance.get("due") is not None:
            compliance["due"] = compliance["due"].isoformat()
        entry["compliance"] = compliance
        out.append(entry)
    return jsonify({"racks": out})


@app.route("/rack-audit/complete/<rack_id>", methods=["POST"])
def rack_audit_complete(rack_id):
    try:
        stamped = set_audit_date_now(rack_id)
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach Hyperview: {e}"}), 502
    if not stamped:
        return jsonify({"error": "This asset has no 'Last Audit Date' custom property to update"}), 404
    global rack_audit_cache
    rack_audit_cache = None  # force a fresh fetch next time rather than serving a stale date
    return jsonify({"ok": True})


@app.route("/rack-audit/delete-devices", methods=["POST"])
def rack_audit_delete_devices():
    """Deletes one or more assets from Hyperview outright - the rack audit
    page's device-removal flag. Best-effort per device: one failure
    doesn't block the rest, and the caller gets back exactly which ones
    went through."""
    body = request.get_json(silent=True) or {}
    device_ids = body.get("device_ids") or []
    deleted, failed = [], []
    for device_id in device_ids:
        try:
            delete_asset(device_id)
            deleted.append(device_id)
        except requests.exceptions.RequestException as e:
            failed.append({"id": device_id, "error": str(e)})
    return jsonify({"deleted": deleted, "failed": failed})


@app.route("/diagnostics/application-event-logs")
def diagnostics_application_event_logs():
    """Temporary, investigative only - not used by anything else here or in
    InfraWatch. applicationEventLogs is a general audit trail (username,
    eventType, eventDetails per entry) that may or may not record alarm
    acknowledgments; the alarm object and the acknowledgementState endpoint
    itself never expose who performed an acknowledgment, so this is the one
    remaining place to check for that. Hit it, acknowledge a test alarm in
    Hyperview, hit it again, and diff the two to see if an entry appeared
    naming that alarm/asset. If it does, matrix.py can be taught to read
    real acknowledger names from here instead of only knowing true/false."""
    limit = request.args.get("limit", "50")
    after = request.args.get("after", "0")
    try:
        page = hv_get(f"setting/applicationEventLogs?(limit)={limit}&(after)={after}")
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Could not reach Hyperview: {e}"}), 502
    return jsonify(page)


@app.route("/asset-by-id/<asset_id>")
def asset_by_id(asset_id):
    asset = get_asset_cache().get(asset_id)
    if asset:
        return jsonify(asset)
    return jsonify({"error": "Asset not found"}), 404


@app.route("/type/<path:device_name>")
def type_lookup(device_name):
    search = device_name.strip().upper()
    for asset in get_asset_cache().values():
        if str(asset.get("name", "")).upper() == search:
            return jsonify({
                "id": asset.get("id"),
                "name": asset.get("name"),
                "assetTypeId": asset.get("assetTypeId"),
                "manufacturer": asset.get("manufacturerName"),
                "product": asset.get("productName"),
                "location": asset.get("tabDelimitedPath"),
            })
    return jsonify({"error": "not found"}), 404


# ---------------------------------------------------------------------------
# STARTUP CHECKS
# ---------------------------------------------------------------------------
def _require_config():
    missing = [
        name for name, value in (
            ("HYPERVIEW_CLIENT_ID", CLIENT_ID),
            ("HYPERVIEW_CLIENT_SECRET", CLIENT_SECRET),
        )
        if not value
    ]
    if missing:
        logger.critical(
            "Missing required environment variable(s): %s. Set them before "
            "starting the service.", ", ".join(missing),
        )
        raise SystemExit(1)


_require_config()

# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Hyperview Matrix Service starting on port 5001 (cache=%ss)", CACHE_SECONDS)
    # Flask's built-in app.run() is a dev server -- not meant for sustained
    # production load. Serve with waitress instead (pip install waitress).
    # All the in-memory caches assume a single process, so keep this to one
    # process with a thread pool rather than multiple worker processes.
    from waitress import serve
    serve(app, host="0.0.0.0", port=5001, threads=8)
