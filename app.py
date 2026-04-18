"""Flask dashboard for Warcraft Logs parses — designed for a Raspberry Pi kiosk.

Shows:
  * the latest dungeon run in the current report
  * the session parse-average across all completed dungeons in the report

The dashboard auto-refreshes so new pulls appear without user interaction.
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from wcl_client import WCLClient, extract_report_code
from scraper import WCLScraper

# Force local .env precedence so stale process-level env vars (e.g. from
# systemd unit overrides) cannot shadow updated credentials.
load_dotenv(override=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

# --- singletons -----------------------------------------------------------
_client: WCLClient | None = None
_scraper: WCLScraper | None = None


def get_scraper() -> WCLScraper:
    """Return the shared scraper. Cheap to construct but reusing the
    underlying ``curl_cffi`` session keeps the cookie warm-up state."""
    global _scraper
    if _scraper is None:
        _scraper = WCLScraper()
    return _scraper


def get_client() -> WCLClient:
    global _client
    if _client is None:
        _client = WCLClient(
            client_id=os.environ.get("WCL_CLIENT_ID", ""),
            client_secret=os.environ.get("WCL_CLIENT_SECRET", ""),
        )
    return _client


def _get_user_id() -> int | None:
    """Return the configured WCL user id to auto-track, or ``None``."""
    raw = (os.environ.get("WCL_USER_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        app.logger.warning("Invalid WCL_USER_ID=%r; expected integer", raw)
        return None


# Very small in-memory "session" — which report code is currently tracked.
# ``WCL_REPORT_CODE`` acts as a *fallback* seed only; ``WCL_USER_ID`` — when
# set — always wins so the dashboard auto-tracks the user's newest upload.
# ``manual_override`` is flipped to True only when a user explicitly POSTs a
# code to ``/api/session``.
_state: dict[str, Any] = {
    "report_code": os.environ.get("WCL_REPORT_CODE", "").strip(),
    # Cache of the most recent report code resolved from WCL_USER_ID.
    "auto_code": "",
    "auto_checked_at": 0.0,
    "manual_override": False,
}

# Re-check WCL for a newer report at most this often (seconds).
_AUTO_POLL_SECONDS = 60.0

_OAUTH_STATE_KEY = "wcl_oauth_state"
_OAUTH_PKCE_VERIFIER_KEY = "wcl_oauth_pkce_verifier"
_OAUTH_TOKEN_KEY = "wcl_oauth_token"


def _oauth_redirect_uri() -> str:
    return (os.environ.get("WCL_OAUTH_REDIRECT_URI") or "").strip()


def _oauth_enabled() -> bool:
    return bool(_oauth_redirect_uri())


def _oauth_token_from_session() -> dict[str, Any] | None:
    token = session.get(_OAUTH_TOKEN_KEY)
    return token if isinstance(token, dict) else None


def _store_oauth_token(payload: dict[str, Any]) -> None:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("OAuth token response missing access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    try:
        ttl = float(expires_in) if expires_in is not None else 0.0
    except (TypeError, ValueError):
        ttl = 0.0
    session[_OAUTH_TOKEN_KEY] = {
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) else "",
        "expires_at": time.time() + max(0.0, ttl),
    }
    session.modified = True


def _clear_oauth_state() -> None:
    session.pop(_OAUTH_STATE_KEY, None)
    session.pop(_OAUTH_PKCE_VERIFIER_KEY, None)
    session.modified = True


def _clear_oauth_token() -> None:
    session.pop(_OAUTH_TOKEN_KEY, None)
    session.modified = True


def _get_user_access_token(client: WCLClient) -> str | None:
    token = _oauth_token_from_session()
    if not token:
        return None
    access = token.get("access_token")
    expires_at = token.get("expires_at")
    if isinstance(access, str) and isinstance(expires_at, (int, float)):
        if expires_at - 30 > time.time():
            return access
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        _clear_oauth_token()
        return None
    try:
        refreshed = client.refresh_user_access_token(refresh_token)
        _store_oauth_token(refreshed)
    except Exception:
        app.logger.exception("Refreshing user OAuth token failed")
        _clear_oauth_token()
        return None
    token = _oauth_token_from_session()
    access = (token or {}).get("access_token")
    return access if isinstance(access, str) and access else None


def _resolve_auto_code(client: WCLClient) -> str:
    """Return the newest report code for ``WCL_USER_ID``, cached briefly.

    Falls back to the cached value on transient API errors so the dashboard
    keeps rendering even if WCL is briefly unreachable.
    """
    uid = _get_user_id()
    if uid is None:
        return ""
    now = time.time()
    if _state.get("auto_code") and (now - _state.get("auto_checked_at", 0.0)) < _AUTO_POLL_SECONDS:
        return _state["auto_code"]
    try:
        latest = client.get_latest_user_report_code(uid) or ""
    except Exception:
        app.logger.exception("Auto-resolving latest report for user %s failed", uid)
        return _state.get("auto_code", "")
    _state["auto_code"] = latest
    _state["auto_checked_at"] = now
    return latest


def _role_character_count(fight_rank: dict[str, Any]) -> int:
    roles = fight_rank.get("roles") or {}
    total = 0
    for bucket in ("tanks", "healers", "dps"):
        chars = ((roles.get(bucket) or {}).get("characters") or [])
        total += len(chars)
    return total


def _merge_rankings(
    primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge ranking payloads per fight, preferring richer character data."""
    primary_rows = (primary or {}).get("data") or []
    secondary_rows = (secondary or {}).get("data") or []

    by_fight: dict[int, dict[str, Any]] = {}
    ordered_ids: list[int] = []

    for row in secondary_rows:
        fid = row.get("fightID")
        if not isinstance(fid, int):
            continue
        by_fight[fid] = row

    for row in primary_rows:
        fid = row.get("fightID")
        if not isinstance(fid, int):
            continue
        ordered_ids.append(fid)
        other = by_fight.pop(fid, None)
        if other and _role_character_count(other) > _role_character_count(row):
            by_fight[fid] = other
            continue
        by_fight[fid] = row

    for fid in sorted(by_fight):
        if fid not in ordered_ids:
            ordered_ids.append(fid)

    merged_rows: list[dict[str, Any]] = []
    for fid in ordered_ids:
        row = by_fight.get(fid)
        if not isinstance(row, dict):
            continue
        merged_rows.append(row)

    return {"data": merged_rows}


# --- transforms -----------------------------------------------------------
def _flatten_characters(fight_rank: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the tanks/healers/dps role buckets into a single list."""
    out: list[dict[str, Any]] = []
    roles = fight_rank.get("roles") or {}
    for role_key, label in (("tanks", "Tank"), ("healers", "Healer"), ("dps", "DPS")):
        bucket = roles.get(role_key) or {}
        for char in bucket.get("characters") or []:
            # Defensive copy with the fields we actually render.
            out.append(
                {
                    "id": char.get("id"),
                    "name": char.get("name"),
                    "class": char.get("class"),
                    "spec": char.get("spec"),
                    "role": label,
                    "rankPercent": char.get("rankPercent"),
                    # WCL's "Key %" column — percentile within the keystone-level bracket.
                    "bracketPercent": char.get("bracketPercent"),
                    "amount": char.get("amount"),
                }
            )
    return out


def _merge_table(fight: dict[str, Any], tables: dict[str, Any] | None) -> None:
    """Attach per-player damageDone / healingDone / DPS / HPS to characters."""
    if not tables:
        return
    chars = fight.get("characters") or []
    if not chars:
        return

    duration_ms = fight.get("duration") or 0

    def _table_data(key: str) -> dict[str, Any]:
        table = tables.get(key)
        if not isinstance(table, dict):
            return {}
        return table.get("data") or {}

    def _entries(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        entries = data.get("entries") or []
        return {e.get("name"): e for e in entries if isinstance(e, dict) and e.get("name")}

    dmg_data = _table_data("damage")
    heal_data = _table_data("healing")
    dmg_by_name = _entries(dmg_data)
    heal_by_name = _entries(heal_data)
    # WCL's table DPS/HPS columns are based on table totalTime, not activeTime.
    dmg_total_time = dmg_data.get("totalTime") or duration_ms
    heal_total_time = heal_data.get("totalTime") or duration_ms

    for c in chars:
        name = c.get("name")
        d = dmg_by_name.get(name) or {}
        h = heal_by_name.get(name) or {}
        dmg_total = d.get("total") or 0
        heal_total = h.get("total") or 0
        c["damageDone"] = dmg_total or None
        c["healingDone"] = heal_total or None
        c["dps"] = round(dmg_total / (dmg_total_time / 1000), 1) if dmg_total and dmg_total_time else None
        c["hps"] = round(heal_total / (heal_total_time / 1000), 1) if heal_total and heal_total_time else None


# Specs that determine role without knowing the class.
_TANK_SPECS = {"Blood", "Guardian", "Protection", "Brewmaster", "Vengeance"}
_HEALER_SPECS = {
    "Restoration", "Holy", "Discipline", "Mistweaver", "Preservation",
}


def _role_from_icon(icon: str | None) -> str:
    """Infer role from a WCL ``icon`` string like ``"DeathKnight-Blood"``."""
    if not icon or "-" not in icon:
        return "DPS"
    spec = icon.split("-", 1)[1]
    if spec in _TANK_SPECS:
        return "Tank"
    if spec in _HEALER_SPECS:
        return "Healer"
    return "DPS"


def _characters_from_tables(
    tables: dict[str, Any] | None,
    role_lookup: dict[str, str],
) -> list[dict[str, Any]]:
    """Build a character list from damage/healing tables when rankings are missing.

    Used as a fallback when WCL hasn't yet indexed ranks for a fight.
    No ``rankPercent`` / ``bracketPercent`` — those will be ``None``.
    """
    if not tables:
        return []
    by_name: dict[str, dict[str, Any]] = {}
    for key, role_hint in (("damage", None), ("healing", None)):
        table = tables.get(key) or {}
        data = table.get("data") or {}
        for entry in data.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") in (None, "NPC", "Boss", "Pet"):
                continue
            name = entry.get("name")
            if not name:
                continue
            icon = entry.get("icon") or ""
            spec = icon.split("-", 1)[1] if "-" in icon else None
            role = role_lookup.get(name) or _role_from_icon(icon)
            by_name.setdefault(
                name,
                {
                    "id": None,
                    "name": name,
                    "class": entry.get("type"),
                    "spec": spec,
                    "role": role,
                    "rankPercent": None,
                    "bracketPercent": None,
                    "amount": None,
                },
            )
    return list(by_name.values())


def build_dashboard(
    report: dict[str, Any],
    fight_tables: dict[int, dict[str, Any]] | None = None,
    char_parses: dict[tuple[int, int], dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Shape a raw WCL report into the payload consumed by the frontend.

    Averaging (per-dungeon and session-wide) is intentionally done on the
    client so that role filters (DPS / healer / tank) can be toggled without
    a round-trip to the server.

    ``char_parses`` overrides ``rankPercent`` and ``bracketPercent`` with
    per-character values queried from ``characterData.character`` — the
    session report's own ``rankings`` field returns group-level placeholders
    for M+, not real per-player parses.
    """
    rankings_root = report.get("rankings") or {}
    # `rankings` can be returned as a dict or — for some reports — as None.
    fight_rankings: list[dict[str, Any]] = (rankings_root.get("data") or []) if isinstance(
        rankings_root, dict
    ) else []
    rankings_by_fight = {fr.get("fightID"): fr for fr in fight_rankings}

    # Build name -> role and name -> id maps from whichever fights have
    # ranking data. Fights without rankings can still attribute role and
    # character id correctly by looking up the player's name.
    role_by_name: dict[str, str] = {}
    id_by_name: dict[str, int] = {}
    for fr in fight_rankings:
        roles = fr.get("roles") or {}
        for role_key, label in (("tanks", "Tank"), ("healers", "Healer"), ("dps", "DPS")):
            for ch in (roles.get(role_key) or {}).get("characters") or []:
                name = ch.get("name")
                if name:
                    role_by_name.setdefault(name, label)
                    cid = ch.get("id")
                    if isinstance(cid, int):
                        id_by_name.setdefault(name, cid)

    dungeons: list[dict[str, Any]] = []
    for fight in report.get("fights") or []:
        # Skip wipes — only show completed/timed runs.
        if fight.get("kill") is not True:
            continue
        fight_id = fight.get("id")
        fr = rankings_by_fight.get(fight_id) or {}
        characters = _flatten_characters(fr) if fr else []
        encounter = fr.get("encounter") or {}
        # Fallback: if WCL hasn't indexed rankings for this fight yet, try to
        # populate the player list from the fight's damage/healing tables so
        # the dungeon still shows up with DPS/HPS (just without parse %).
        if not characters and fight_tables and fight_id in fight_tables:
            characters = _characters_from_tables(fight_tables[fight_id], role_by_name)
        # Backfill character id for table-sourced rows using the id map built
        # from any ranked fight in the session.
        for c in characters:
            if c.get("id") is None:
                cid = id_by_name.get(c.get("name"))
                if cid:
                    c["id"] = cid
        dungeon = {
            "fightId": fight_id,
            "name": encounter.get("name") or fight.get("name"),
            "encounterId": encounter.get("id") or fight.get("encounterID"),
            "startTime": fr.get("startTime") or fight.get("startTime"),
            "duration": fr.get("duration")
            or ((fight.get("endTime") or 0) - (fight.get("startTime") or 0)),
            "kill": fight.get("kill"),
            "keystoneLevel": fight.get("keystoneLevel"),
            "keystoneTime": fight.get("keystoneTime"),
            "averageItemLevel": fight.get("averageItemLevel"),
            "characters": characters,
        }
        if fight_tables and fight_id in fight_tables:
            _merge_table(dungeon, fight_tables[fight_id])
        # Fill rankPercent / bracketPercent only when missing in report.rankings.
        # For indexed fights, report.rankings(compare: Rankings, playerMetric: dps)
        # already carries the exact values shown in the WCL damage table.
        if char_parses:
            for c in dungeon["characters"]:
                cid = c.get("id")
                if not isinstance(cid, int):
                    continue
                parses = char_parses.get((cid, fight_id))
                if not parses:
                    continue
                overall = parses.get("overall")
                bracket = parses.get("bracket")
                if overall is not None and c.get("rankPercent") is None:
                    c["rankPercent"] = round(overall, 1)
                if bracket is not None and c.get("bracketPercent") is None:
                    c["bracketPercent"] = round(bracket, 1)
        dungeon["characters"] = sorted(
            dungeon["characters"],
            key=lambda c: (c.get("rankPercent") or -1),
            reverse=True,
        )
        dungeons.append(dungeon)

    # Sort chronologically; latest last.
    dungeons.sort(key=lambda d: d.get("startTime") or 0)

    return {
        "code": report.get("code"),
        "title": report.get("title"),
        "zone": (report.get("zone") or {}).get("name"),
        "owner": (report.get("owner") or {}).get("name"),
        "startTime": report.get("startTime"),
        "endTime": report.get("endTime"),
        "dungeonCount": len(dungeons),
        "dungeons": dungeons,
    }


# --- routes ---------------------------------------------------------------
@app.route("/auth/wcl/start")
def auth_wcl_start() -> Any:
    if not _oauth_enabled():
        return jsonify({"error": "OAuth is not configured"}), 404
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = get_client().build_pkce_challenge(verifier)
    session[_OAUTH_STATE_KEY] = state
    session[_OAUTH_PKCE_VERIFIER_KEY] = verifier
    session.modified = True
    auth_url = get_client().build_authorize_url(
        redirect_uri=_oauth_redirect_uri(),
        state=state,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return redirect(auth_url)


@app.route("/auth/wcl/callback")
def auth_wcl_callback() -> Any:
    if not _oauth_enabled():
        return jsonify({"error": "OAuth is not configured"}), 404
    expected_state = session.get(_OAUTH_STATE_KEY)
    code_verifier = session.get(_OAUTH_PKCE_VERIFIER_KEY)
    returned_state = (request.args.get("state") or "").strip()

    if not expected_state or returned_state != expected_state:
        _clear_oauth_state()
        return jsonify({"error": "OAuth state mismatch"}), 400

    error = (request.args.get("error") or "").strip()
    if error:
        _clear_oauth_state()
        return jsonify({"error": f"OAuth authorization failed: {error}"}), 400

    code = (request.args.get("code") or "").strip()
    if not code:
        _clear_oauth_state()
        return jsonify({"error": "OAuth callback missing code"}), 400

    payload = None
    fallback_attempted = False
    try:
        payload = get_client().exchange_authorization_code(
            code=code,
            redirect_uri=_oauth_redirect_uri(),
        )
    except Exception as exc:
        app.logger.exception("Exchanging OAuth authorization code failed (confidential client flow)")
        msg = str(exc)
        low = msg.lower()
        is_invalid_client = "invalid_client" in low or "client authentication failed" in low
        if is_invalid_client and isinstance(code_verifier, str) and code_verifier:
            fallback_attempted = True
            try:
                payload = get_client().exchange_authorization_code_pkce(
                    code=code,
                    redirect_uri=_oauth_redirect_uri(),
                    code_verifier=code_verifier,
                )
            except Exception as pkce_exc:
                app.logger.exception("Exchanging OAuth authorization code failed (PKCE fallback)")
                _clear_oauth_state()
                pkce_msg = str(pkce_exc)
                return jsonify({
                    "error": "OAuth client authentication failed",
                    "details": pkce_msg,
                    "hint": (
                        "Warcraft Logs rejected both confidential and PKCE token exchange. "
                        "Verify this client allows Authorization Code flow with callback URL "
                        "exactly matching WCL_OAUTH_REDIRECT_URI."
                    ),
                }), 502
        elif is_invalid_client:
            _clear_oauth_state()
            return jsonify({
                "error": "OAuth client authentication failed",
                "details": msg,
                "hint": (
                    "Verify WCL_CLIENT_ID/WCL_CLIENT_SECRET on the server match the exact "
                    "Warcraft Logs API client used for Connect WCL, then restart the app."
                ),
            }), 502
        else:
            _clear_oauth_state()
            return jsonify({"error": str(exc)}), 502

    if not isinstance(payload, dict):
        _clear_oauth_state()
        return jsonify({"error": "OAuth token response was invalid"}), 502

    _store_oauth_token(payload)
    _clear_oauth_state()

    if fallback_attempted:
        app.logger.info("OAuth token exchange succeeded via PKCE fallback")

    return redirect(url_for("index", auth="connected"))


@app.route("/api/auth/status")
def auth_status() -> Any:
    if not _oauth_enabled():
        return jsonify({"enabled": False, "connected": False, "expiresIn": None})
    access = _get_user_access_token(get_client())
    token = _oauth_token_from_session() or {}
    expires_at = token.get("expires_at")
    expires_in = None
    if isinstance(expires_at, (int, float)):
        expires_in = max(0, int(expires_at - time.time()))
    return jsonify({
        "enabled": True,
        "connected": bool(access),
        "expiresIn": expires_in,
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout() -> Any:
    _clear_oauth_token()
    _clear_oauth_state()
    return jsonify({"ok": True})


@app.route("/")
def index() -> Any:
    return render_template("index.html", report_code=_state.get("report_code", ""))


@app.route("/api/session", methods=["GET", "POST"])
def session_endpoint() -> Any:
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        raw = (payload.get("report") or "").strip()
        if not raw:
            # Empty string clears the manual override and resumes auto-tracking.
            _state["report_code"] = ""
            _state["manual_override"] = False
            return jsonify({"report_code": "", "auto": _get_user_id() is not None})
        try:
            code = extract_report_code(raw)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _state["report_code"] = code
        _state["manual_override"] = True
        return jsonify({"report_code": code, "auto": False})
    # GET: surface whatever code the dashboard would currently show.
    current = _state.get("report_code") or ""
    auto = False
    if not _state.get("manual_override") and _get_user_id() is not None:
        try:
            current = _resolve_auto_code(get_client()) or current
            auto = True
        except Exception:
            app.logger.exception("session GET: auto-resolve failed")
    return jsonify({"report_code": current, "auto": auto})


@app.route("/api/dashboard")
def dashboard() -> Any:
    code = (request.args.get("code") or "").strip()
    if not code:
        # The kiosk only ever shows the report the user pasted into the
        # session field. Auto-tracking via the WCL OAuth API is no longer
        # used because the public website scrape always reflects what the
        # user actually sees in their browser.
        code = _state.get("report_code") or ""
    if not code:
        return jsonify({"error": "No report selected"}), 404
    try:
        payload = get_scraper().build_payload(code)
    except Exception as exc:  # surface scrape errors as JSON for the frontend
        app.logger.exception("WCL scrape failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify(payload)


@app.route("/healthz")
def healthz() -> Any:
    return {"ok": True}


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    # debug=False — this is for kiosk use.
    app.run(host=host, port=port, debug=False)
