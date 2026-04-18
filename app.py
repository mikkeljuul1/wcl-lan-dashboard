"""Flask dashboard for Warcraft Logs parses — designed for a Raspberry Pi kiosk.

Shows:
  * the latest dungeon run in the current report
  * the session parse-average across all completed dungeons in the report

The dashboard auto-refreshes so new pulls appear without user interaction.
"""
from __future__ import annotations

import os
from statistics import mean
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
                    "name": char.get("name"),
                    "class": char.get("class"),
                    "spec": char.get("spec"),
                    "role": label,
                    "rankPercent": char.get("rankPercent"),
                    "bracketPercent": char.get("bracketPercent"),
                    "amount": char.get("amount"),
                }
            )
    return out


def _parse_values(characters: list[dict[str, Any]]) -> list[float]:
    return [
        float(c["rankPercent"])
        for c in characters
        if isinstance(c.get("rankPercent"), (int, float))
    ]


def build_dashboard(report: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw WCL report into the payload consumed by the frontend."""
    rankings_root = report.get("rankings") or {}
    # `rankings` can be returned as a dict or — for some reports — as None.
    fight_rankings: list[dict[str, Any]] = (rankings_root.get("data") or []) if isinstance(
        rankings_root, dict
    ) else []

    fights_by_id = {f["id"]: f for f in (report.get("fights") or [])}

    dungeons: list[dict[str, Any]] = []
    for fr in fight_rankings:
        fight_id = fr.get("fightID")
        fight = fights_by_id.get(fight_id, {})
        characters = _flatten_characters(fr)
        parses = _parse_values(characters)
        if not parses:
            # Skip entries with no ranked characters (e.g. trash-only, untimed bugs).
            continue
        encounter = fr.get("encounter") or {}
        dungeons.append(
            {
                "fightId": fight_id,
                "name": encounter.get("name") or fight.get("name"),
                "encounterId": encounter.get("id") or fight.get("encounterID"),
                "startTime": fr.get("startTime") or fight.get("startTime"),
                "duration": fr.get("duration"),
                "kill": fight.get("kill"),
                "keystoneLevel": fight.get("keystoneLevel"),
                "keystoneTime": fight.get("keystoneTime"),
                "averageItemLevel": fight.get("averageItemLevel"),
                "averageParse": round(mean(parses), 1),
                "characters": sorted(
                    characters,
                    key=lambda c: (c.get("rankPercent") or -1),
                    reverse=True,
                ),
            }
        )

    # Sort chronologically; latest last.
    dungeons.sort(key=lambda d: d.get("startTime") or 0)

    latest = dungeons[-1] if dungeons else None

    # Session average = average of per-player parses across every dungeon.
    all_parses: list[float] = []
    for d in dungeons:
        all_parses.extend(
            c["rankPercent"]
            for c in d["characters"]
            if isinstance(c.get("rankPercent"), (int, float))
        )
    session_average = round(mean(all_parses), 1) if all_parses else None

    return {
        "code": report.get("code"),
        "title": report.get("title"),
        "zone": (report.get("zone") or {}).get("name"),
        "owner": (report.get("owner") or {}).get("name"),
        "startTime": report.get("startTime"),
        "endTime": report.get("endTime"),
        "dungeonCount": len(dungeons),
        "sessionAverage": session_average,
        "latest": latest,
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
        report = get_client().get_report(code)
    except Exception as exc:  # surface API errors as JSON for the frontend
        app.logger.exception("WCL fetch failed")
        return jsonify({"error": str(exc)}), 502
    return jsonify(build_dashboard(report))


@app.route("/healthz")
def healthz() -> Any:
    return {"ok": True}


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8080"))
    # debug=False — this is for kiosk use.
    app.run(host=host, port=port, debug=False)
