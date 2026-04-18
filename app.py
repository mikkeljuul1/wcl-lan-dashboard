"""Flask dashboard for Warcraft Logs parses — designed for a Raspberry Pi kiosk.

Shows:
  * the latest dungeon run in the current report
  * the session parse-average across all completed dungeons in the report

The dashboard auto-refreshes so new pulls appear without user interaction.
"""
from __future__ import annotations

import os
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


# Very small in-memory "session" — which report code is currently tracked.
_state: dict[str, str] = {
    "report_code": os.environ.get("WCL_REPORT_CODE", "").strip(),
}


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
                    "bracketPercent": char.get("bracketPercent"),
                    "ilvlBracketPercent": None,
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


def build_dashboard(
    report: dict[str, Any],
    fight_tables: dict[int, dict[str, Any]] | None = None,
    ilvl_bracket: dict[tuple[int, int], float] | None = None,
) -> dict[str, Any]:
    """Shape a raw WCL report into the payload consumed by the frontend.

    Averaging (per-dungeon and session-wide) is intentionally done on the
    client so that role filters (DPS / healer / tank) can be toggled without
    a round-trip to the server.
    """
    rankings_root = report.get("rankings") or {}
    # `rankings` can be returned as a dict or — for some reports — as None.
    fight_rankings: list[dict[str, Any]] = (rankings_root.get("data") or []) if isinstance(
        rankings_root, dict
    ) else []
    rankings_by_fight = {fr.get("fightID"): fr for fr in fight_rankings}

    dungeons: list[dict[str, Any]] = []
    for fight in report.get("fights") or []:
        # Skip wipes — only show completed/timed runs.
        if fight.get("kill") is not True:
            continue
        fight_id = fight.get("id")
        fr = rankings_by_fight.get(fight_id) or {}
        characters = _flatten_characters(fr) if fr else []
        encounter = fr.get("encounter") or {}
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
        if ilvl_bracket:
            for c in dungeon["characters"]:
                cid = c.get("id")
                if isinstance(cid, int):
                    pct = ilvl_bracket.get((cid, fight_id))
                    if pct is not None:
                        c["ilvlBracketPercent"] = round(pct, 1)
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
            return jsonify({"error": "Missing 'report' value"}), 400
        try:
            code = extract_report_code(raw)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _state["report_code"] = code
        return jsonify({"report_code": code})
    return jsonify({"report_code": _state.get("report_code", "")})


@app.route("/api/dashboard")
def dashboard() -> Any:
    code = (request.args.get("code") or _state.get("report_code") or "").strip()
    if not code:
        return jsonify({"error": "No report selected"}), 404
    try:
        client = get_client()
        report = client.get_report(code)
        # Fetch damage/healing tables only for fights that have ranked characters.
        rankings = (report.get("rankings") or {}).get("data") or []
        ranked_fight_ids: list[int] = []
        for fr in rankings:
            roles = fr.get("roles") or {}
            if any(
                (roles.get(r) or {}).get("characters")
                for r in ("tanks", "healers", "dps")
            ):
                fid = fr.get("fightID")
                if isinstance(fid, int):
                    ranked_fight_ids.append(fid)
        fight_tables: dict[int, dict[str, Any]] = {}
        if ranked_fight_ids:
            try:
                fight_tables = client.get_fight_tables(code, ranked_fight_ids)
            except Exception:
                # Non-fatal: fall back to parse-only display.
                app.logger.exception("Fetching fight tables failed")

        # Per-character ilvl-bracket parses (completed/timed keys only).
        # Build lookup list: one entry per (character, completed fight).
        # Match is by absolute start time because each player uploads their
        # own WCL report for the same M+ run.
        report_start = report.get("startTime") or 0
        fights_by_id = {f.get("id"): f for f in (report.get("fights") or [])}
        lookups: list[dict[str, Any]] = []
        for fr in rankings:
            fid = fr.get("fightID")
            if not isinstance(fid, int):
                continue
            fight = fights_by_id.get(fid) or {}
            if fight.get("kill") is not True:
                continue  # WCL only ranks completed/timed runs
            encounter_id = (fr.get("encounter") or {}).get("id")
            if not isinstance(encounter_id, int):
                continue
            # Prefer absolute time from the fight-rankings payload; fall back
            # to report_start + fight.startTime (which is relative).
            abs_start = fr.get("startTime")
            if not isinstance(abs_start, int):
                rel = fight.get("startTime") or 0
                abs_start = int(report_start) + int(rel)
            roles = fr.get("roles") or {}
            for role_key, metric in (("dps", "dps"), ("tanks", "dps"), ("healers", "hps")):
                for ch in (roles.get(role_key) or {}).get("characters") or []:
                    cid = ch.get("id")
                    if isinstance(cid, int):
                        lookups.append(
                            {
                                "characterId": cid,
                                "encounterId": encounter_id,
                                "fightId": fid,
                                "absStartTime": int(abs_start),
                                "metric": metric,
                            }
                        )
        ilvl_bracket: dict[tuple[int, int], float] = {}
        if lookups:
            try:
                ilvl_bracket = client.get_ilvl_bracket_parses(lookups)
            except Exception:
                app.logger.exception("Fetching ilvl-bracket parses failed")
    except Exception as exc:  # surface API errors as JSON for the frontend
        app.logger.exception("WCL fetch failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify(build_dashboard(report, fight_tables, ilvl_bracket))


@app.route("/healthz")
def healthz() -> Any:
    return {"ok": True}


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    # debug=False — this is for kiosk use.
    app.run(host=host, port=port, debug=False)
