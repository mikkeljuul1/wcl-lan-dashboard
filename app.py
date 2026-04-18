"""Flask dashboard for Warcraft Logs parses — designed for a Raspberry Pi kiosk.

Shows:
  * the latest dungeon run in the current report
  * the session parse-average across all completed dungeons in the report

The dashboard auto-refreshes so new pulls appear without user interaction.
"""
from __future__ import annotations

import os
import time
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from wcl_client import WCLClient, extract_report_code

load_dotenv()

app = Flask(__name__)

# --- singletons -----------------------------------------------------------
_client: WCLClient | None = None


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

    def _entries(key: str) -> dict[str, dict[str, Any]]:
        table = tables.get(key)
        if not isinstance(table, dict):
            return {}
        data = table.get("data") or {}
        entries = data.get("entries") or []
        return {e.get("name"): e for e in entries if isinstance(e, dict) and e.get("name")}

    dmg_by_name = _entries("damage")
    heal_by_name = _entries("healing")

    for c in chars:
        name = c.get("name")
        d = dmg_by_name.get(name) or {}
        h = heal_by_name.get(name) or {}
        dmg_total = d.get("total") or 0
        heal_total = h.get("total") or 0
        active_dmg = d.get("activeTime") or duration_ms
        active_heal = h.get("activeTime") or duration_ms
        c["damageDone"] = dmg_total or None
        c["healingDone"] = heal_total or None
        c["dps"] = round(dmg_total / (active_dmg / 1000), 1) if dmg_total and active_dmg else None
        c["hps"] = round(heal_total / (active_heal / 1000), 1) if heal_total and active_heal else None


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
    client = get_client()
    code = (request.args.get("code") or "").strip()
    if not code:
        # Prefer the manually selected code; otherwise fall back to the newest
        # report from the configured WCL_USER_ID.
        if _state.get("manual_override") and _state.get("report_code"):
            code = _state["report_code"]
        else:
            code = _resolve_auto_code(client) or _state.get("report_code") or ""
            if code:
                _state["report_code"] = code
    if not code:
        return jsonify({"error": "No report selected"}), 404
    try:
        report = client.get_report(code)
        # Fetch damage/healing tables for every completed (kill) fight, so the
        # dashboard can populate player rows even when WCL hasn't yet indexed
        # rankings for the most recent run.
        kill_fight_ids: list[int] = []
        for fight in report.get("fights") or []:
            if fight.get("kill") is True and isinstance(fight.get("id"), int):
                kill_fight_ids.append(fight["id"])
        fight_tables: dict[int, dict[str, Any]] = {}
        if kill_fight_ids:
            try:
                fight_tables = client.get_fight_tables(code, kill_fight_ids)
            except Exception:
                # Non-fatal: fall back to parse-only display.
                app.logger.exception("Fetching fight tables failed")

        # Build per-character parse lookups. WCL's report.rankings field only
        # returns group-level placeholders for M+, so we query each character's
        # personal encounterRankings and match by absolute fight start time.
        report_start = report.get("startTime") or 0
        fight_by_id = {
            f.get("id"): f for f in (report.get("fights") or []) if f.get("kill") is True
        }
        fight_rankings = (
            (report.get("rankings") or {}).get("data") or []
            if isinstance(report.get("rankings"), dict) else []
        )
        # name -> (id, encounterId, metric_role)
        player_info: dict[str, dict[str, Any]] = {}
        for fr in fight_rankings:
            enc_id = (fr.get("encounter") or {}).get("id")
            # Use dps for all roles to mirror the WCL Damage Done table.
            for role_key, metric in (("tanks", "dps"), ("dps", "dps"), ("healers", "dps")):
                for ch in ((fr.get("roles") or {}).get(role_key) or {}).get("characters") or []:
                    name = ch.get("name")
                    cid = ch.get("id")
                    if name and isinstance(cid, int) and isinstance(enc_id, int):
                        player_info.setdefault(
                            name, {"id": cid, "encounterId": enc_id, "metric": metric}
                        )
        # Build lookups: one (character, fight) pair per completed fight per player.
        lookups: list[dict[str, Any]] = []
        for fid, fight in fight_by_id.items():
            abs_start = (fight.get("startTime") or 0) + report_start
            enc_id = fight.get("encounterID")
            for name, info in player_info.items():
                eid = enc_id if isinstance(enc_id, int) else info["encounterId"]
                lookups.append({
                    "characterId": info["id"],
                    "encounterId": eid,
                    "fightId": fid,
                    "absStartTime": abs_start,
                    "metric": info["metric"],
                })
        char_parses: dict[tuple[int, int], dict[str, float]] = {}
        if lookups:
            try:
                char_parses = client.get_ilvl_bracket_parses(lookups)
            except Exception:
                app.logger.exception("Fetching per-character parses failed")
    except Exception as exc:  # surface API errors as JSON for the frontend
        app.logger.exception("WCL fetch failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify(build_dashboard(report, fight_tables, char_parses))


@app.route("/healthz")
def healthz() -> Any:
    return {"ok": True}


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    # debug=False — this is for kiosk use.
    app.run(host=host, port=port, debug=False)
