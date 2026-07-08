"""
gx-redeem — category-scoped Personal Training session redemption kiosk.

Barcode scan -> member lookup -> find active PT package -> auto-redeem one
session. No staff/member interaction beyond the scan itself. See README.md
for the full flow and the handoff spec this was built from.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request

if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gx-redeem")

app = Flask(__name__)

# --- Config -----------------------------------------------------------
DAXKO_BASE = "https://api.partners.daxko.com/api/v1"
DAXKO_TOKEN_URL = "https://api.partners.daxko.com/auth/token/"
DAXKO_OAUTH_SCOPE = os.environ.get("DAXKO_OAUTH_SCOPE", "OPS_5413")

DAXKO_CLIENT_ID = os.environ["DAXKO_CLIENT_ID"]
DAXKO_CLIENT_SECRET = os.environ["DAXKO_CLIENT_SECRET"]

BRANCH_ID = os.environ.get("BRANCH_ID", "B725")

# Which category this kiosk is scoped to. Set per-deployment (env var), so
# you could run multiple instances of this app for different programs
# without touching code. This deployment is Personal Training specifically.
CATEGORY_ID = os.environ["REDEEM_CATEGORY_ID"]  # e.g. "TAG8557"

CACHE_REFRESH_INTERVAL_SECONDS = int(
    os.environ.get("CACHE_REFRESH_INTERVAL_SECONDS", 24 * 60 * 60)
)

# Local instructor mapping (Daxko's API has no clean instructor-list
# endpoint outside the cart-registration flow, so this is maintained by
# hand). Override in production via the INSTRUCTORS_JSON env var
# (JSON object of instructor_id -> instructor name) without a code change;
# falls back to these placeholder values otherwise.
#
# TODO: these are TEST/PLACEHOLDER ids. Pull real instructor_ids from
# Daxko admin (or a live cart-registration response) before production use.
_DEFAULT_INSTRUCTORS = {
    "INS22124": "John Doe",
    "INS22125": "Sally Jones",
}
if os.environ.get("INSTRUCTORS_JSON"):
    INSTRUCTORS = json.loads(os.environ["INSTRUCTORS_JSON"])
else:
    INSTRUCTORS = _DEFAULT_INSTRUCTORS

# --- OAuth token cache --------------------------------------------------
_token_cache = {"access_token": None, "expires_at": 0}
_token_lock = threading.Lock()
TOKEN_REFRESH_MARGIN_SECONDS = 60


def get_access_token():
    """Client-credentials OAuth against the Daxko Operations token endpoint.
    Caches the token in memory and refreshes it shortly before it expires.
    """
    with _token_lock:
        if (
            _token_cache["access_token"]
            and time.time() < _token_cache["expires_at"] - TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return _token_cache["access_token"]

        resp = requests.post(
            DAXKO_TOKEN_URL,
            json={
                "client_id": DAXKO_CLIENT_ID,
                "client_secret": DAXKO_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": DAXKO_OAUTH_SCOPE,
            },
            timeout=10,
        )
        if not resp.ok:
            log.error(
                "Daxko OAuth token request failed: %s %s | body: %s",
                resp.status_code, resp.reason, resp.text,
            )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
        return _token_cache["access_token"]


# --- Cache of valid package offerings in this category -----------------
_offering_cache = {"offerings": [], "last_refreshed": None}
_offering_cache_lock = threading.Lock()


def refresh_offering_cache():
    """Pull all package-type offerings scoped to CATEGORY_ID.

    NOTE: the search API nests program_id under offering["program"]["id"],
    NOT a flat "program_id" key — verified against the OpenAPI spec. We
    flatten it here on ingest so the rest of the app can use simple
    offering["program_id"] / offering["offering_id"] access.
    """
    access_token = get_access_token()
    resp = requests.get(
        f"{DAXKO_BASE}/programs/offerings/search",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "category_ids": CATEGORY_ID,
            "offering_types": "package",
            "location_ids": BRANCH_ID,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    flattened = []
    for offering in data.get("offerings", []):
        program = offering.get("program") or {}
        flattened.append({
            "offering_id": offering.get("id"),
            "program_id": program.get("id"),
            "name": offering.get("name") or program.get("name"),
        })

    with _offering_cache_lock:
        _offering_cache["offerings"] = flattened
        _offering_cache["last_refreshed"] = datetime.now(timezone.utc)
    log.info(
        "Offering cache refreshed: %d offering(s) in category %s",
        len(flattened),
        CATEGORY_ID,
    )


def refresh_offering_cache_loop():
    """Initial synchronous refresh, then periodic background refresh.
    Started as a daemon thread from gunicorn's post_fork hook (or from
    the __main__ block for local dev) — never at bare module import time.
    """
    while True:
        try:
            refresh_offering_cache()
        except requests.RequestException:
            log.exception("Offering cache refresh failed; will retry")
        time.sleep(CACHE_REFRESH_INTERVAL_SECONDS)


def cache_is_ready():
    return _offering_cache["last_refreshed"] is not None


# --- Routes -------------------------------------------------------------


@app.route("/")
def kiosk():
    return render_template("kiosk.html")


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "offering_cache_last_refreshed": (
            _offering_cache["last_refreshed"].isoformat()
            if _offering_cache["last_refreshed"]
            else None
        ),
    })


@app.route("/api/debug/offerings")
def debug_offerings():
    """
    TEMPORARY debug endpoint — remove before going live. Dumps the current
    offering cache so we can confirm what CATEGORY_ID actually resolved to,
    without digging through log history.
    """
    return jsonify({
        "category_id": CATEGORY_ID,
        "branch_id": BRANCH_ID,
        "offering_count": len(_offering_cache["offerings"]),
        "offerings": _offering_cache["offerings"],
        "last_refreshed": (
            _offering_cache["last_refreshed"].isoformat()
            if _offering_cache["last_refreshed"]
            else None
        ),
    })


@app.route("/api/debug/roster/<member_id>")
def debug_roster(member_id):
    """
    TEMPORARY debug endpoint — remove before going live. Shows the raw
    roster response Daxko returns for a given member against each cached
    offering, so we can see exactly why a match is or isn't happening.
    """
    access_token = get_access_token()
    results = []
    for offering in _offering_cache["offerings"]:
        program_id = offering["program_id"]
        offering_id = offering["offering_id"]
        resp = requests.get(
            f"{DAXKO_BASE}/programs/{program_id}/offerings/{offering_id}/roster/{member_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"location_id": BRANCH_ID},
            timeout=10,
        )
        results.append({
            "program_id": program_id,
            "offering_id": offering_id,
            "status_code": resp.status_code,
            "body": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
        })
    return jsonify(results)


@app.route("/api/scan", methods=["POST"])
def scan_member():
    """
    Fully automatic: scan -> lookup -> redeem, no staff/member interaction.
    Returns a result the kiosk UI displays for ~3 seconds then resets.
    """
    barcode = (request.json or {}).get("barcode", "").strip()
    if not barcode:
        return jsonify({
            "status": "error",
            "reason": "member_not_found",
            "message": "No barcode received",
        }), 200

    try:
        access_token = get_access_token()
        # NOTE: the /members/ search endpoint's active-filter param is
        # named "active_only" (verified against spec), not "is_active_only".
        resp = requests.get(
            f"{DAXKO_BASE}/members/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"barcode": barcode, "active_only": True},
            timeout=10,
        )
        resp.raise_for_status()
        members = resp.json().get("members", [])
    except requests.RequestException:
        log.exception("Daxko API error during member lookup")
        return jsonify({
            "status": "error",
            "reason": "api_error",
            "message": "Unable to process — see front desk",
        }), 200

    if not members:
        return jsonify({
            "status": "error",
            "reason": "member_not_found",
            "message": "Member not found",
        }), 200

    member = members[0]
    member_id = member["member_id"]
    member_name = member.get("name", {})

    # The /members/ search response does NOT include photos (verified
    # against spec) — only GET /members/{member_id} does. Fetch it as a
    # separate, best-effort call so a photo-lookup hiccup never blocks
    # a redemption.
    member_photo = fetch_member_photo(member_id, access_token)

    member_summary = {
        "member_id": member_id,
        "first_name": member_name.get("preferred_name") or member_name.get("first_name"),
        "last_name": member_name.get("last_name"),
        "photo_url": member_photo,
    }

    if not cache_is_ready():
        log_failure_note(
            member_id, access_token,
            "Auto-redeem failed: offering cache not yet loaded"
        )
        return jsonify({
            "status": "error",
            "reason": "api_error",
            "message": "System not ready — see front desk",
            "member": member_summary,
        }), 200

    try:
        matches = find_active_packages_for_member(member_id, access_token)
    except requests.RequestException as e:
        log.exception("Daxko API error while checking packages for member %s", member_id)
        log_failure_note(
            member_id, access_token,
            f"Auto-redeem API error while checking packages: {e}"
        )
        return jsonify({
            "status": "error",
            "reason": "api_error",
            "message": "Unable to process — see front desk",
            "member": member_summary,
        }), 200

    if not matches:
        log_failure_note(
            member_id, access_token,
            "Auto-redeem failed: no active package found in this category"
        )
        return jsonify({
            "status": "error",
            "reason": "no_active_package",
            "message": "No active sessions found — see front desk",
            "member": member_summary,
        }), 200

    if len(matches) > 1:
        matches.sort(key=lambda m: m["expiration_date"])

    chosen = matches[0]

    if already_redeemed_today(chosen):
        return jsonify({
            "status": "declined",
            "reason": "daily_cap",
            "message": "Already redeemed today",
            "member": member_summary,
            "offering_name": chosen["offering_name"],
        }), 200

    instructor_id = resolve_instructor_id(chosen["default_instructor_name"])
    if not instructor_id:
        log_failure_note(
            member_id, access_token,
            f"Auto-redeem failed: unmapped instructor "
            f"'{chosen['default_instructor_name']}' for {chosen['offering_name']}"
        )
        return jsonify({
            "status": "error",
            "reason": "unmapped_instructor",
            "message": "Unable to process — see front desk",
            "member": member_summary,
        }), 200

    try:
        redeem_resp = requests.post(
            f"{DAXKO_BASE}/packages/redeem",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "registration_id": chosen["registration_id"],
                "redemption_datetime": datetime.now().strftime("%m-%d-%Y %H:%M:%S"),
                "instructor_id": instructor_id,
            },
            timeout=10,
        )
        redeem_resp.raise_for_status()
    except requests.RequestException as e:
        log.exception("Daxko API error during redeem for member %s", member_id)
        log_failure_note(
            member_id, access_token,
            f"Auto-redeem API error for {chosen['offering_name']}: {e}"
        )
        return jsonify({
            "status": "error",
            "reason": "api_error",
            "message": "Unable to process — see front desk",
            "member": member_summary,
        }), 200

    return jsonify({
        "status": "redeemed",
        "member": member_summary,
        "offering_name": chosen["offering_name"],
        "instructor_name": chosen["default_instructor_name"],
        "remaining_instances": chosen["remaining_instances"] - 1,
        "total_instances": chosen["total_instances"],
    })


def fetch_member_photo(member_id, access_token):
    """Best-effort photo lookup via the single-member detail endpoint.
    Never blocks or fails the scan response if it errors out.
    """
    try:
        resp = requests.get(
            f"{DAXKO_BASE}/members/{member_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        return next((p["url"] for p in photos if p.get("type") == "regular"), None)
    except requests.RequestException:
        log.warning("Photo lookup failed for member %s", member_id)
        return None


def already_redeemed_today(package):
    """Check redeemed_sessions for any redemption dated today (local date)."""
    today = datetime.now().strftime("%m/%d/%Y")
    for session in package.get("redeemed_sessions", []):
        if session["redeemed_date"] == today:
            return True
    return False


def resolve_instructor_id(instructor_name):
    for iid, name in INSTRUCTORS.items():
        if name == instructor_name:
            return iid
    return None


def log_failure_note(member_id, access_token, message):
    """Best-effort logging; never let a note failure crash the kiosk response."""
    try:
        requests.post(
            f"{DAXKO_BASE}/members/{member_id}/notes",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "note_type": "Staff Action",
                "note": message,
                "author": "gx-redeem kiosk",
            },
            timeout=10,
        )
    except requests.RequestException:
        log.warning("Failed to write audit note for member %s: %s", member_id, message)


def find_active_packages_for_member(member_id, access_token):
    """
    Loop only over offerings in the configured category (never touches
    offerings outside CATEGORY_ID, so other paid programs are structurally
    invisible to this kiosk). offering["program_id"] / offering["offering_id"]
    are pre-flattened by refresh_offering_cache().
    """
    matches = []
    for offering in _offering_cache["offerings"]:
        program_id = offering["program_id"]
        offering_id = offering["offering_id"]
        if not program_id or not offering_id:
            continue

        resp = requests.get(
            f"{DAXKO_BASE}/programs/{program_id}/offerings/{offering_id}/roster/{member_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"location_id": BRANCH_ID},
            timeout=10,
        )
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        roster_entry = resp.json()

        session_details = roster_entry.get("session_details")
        if session_details and session_details.get("status") == "active" \
                and session_details.get("remaining_instances", 0) > 0:
            matches.append({
                "program_id": program_id,
                "offering_id": offering_id,
                "offering_name": offering.get("name"),
                "registration_id": session_details["registration_id"],
                "expiration_date": session_details.get("expiration_date", ""),
                "remaining_instances": session_details["remaining_instances"],
                "total_instances": session_details["total_instances"],
                "default_instructor_name": session_details.get("instructor_name"),
                "redeemed_sessions": session_details.get("redeemed_sessions", []),
            })
    return matches


if __name__ == "__main__":
    threading.Thread(target=refresh_offering_cache_loop, daemon=True).start()
    app.run(debug=True)
