"""Warcraft Logs API v2 (GraphQL) client.

Uses the OAuth2 client credentials flow to authenticate, then issues GraphQL
queries against the public API. Tokens are cached in-memory until expiry.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

import requests

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

# Warcraft Logs report codes in URLs look like /reports/<code>[/...]
# Codes are alphanumeric, length ~16. Accept the code itself or a full URL.
_REPORT_CODE_RE = re.compile(r"([a-zA-Z0-9]{8,})")


def extract_report_code(value: str) -> str:
    """Return the report code given either a code or a full report URL."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Empty report code")
    if "warcraftlogs.com" in value:
        # e.g. https://www.warcraftlogs.com/reports/ba8GyNv4nCKqgt7V#...
        parts = value.split("/reports/", 1)
        if len(parts) == 2:
            tail = parts[1].split("#", 1)[0].split("?", 1)[0].split("/", 1)[0]
            if tail:
                return tail
    match = _REPORT_CODE_RE.search(value)
    if not match:
        raise ValueError(f"Could not find a report code in: {value!r}")
    return match.group(1)


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # epoch seconds


class WCLClient:
    """Minimal Warcraft Logs v2 GraphQL client."""

    def __init__(self, client_id: str, client_secret: str, timeout: float = 20.0):
        if not client_id or not client_secret:
            raise RuntimeError(
                "Missing WCL_CLIENT_ID / WCL_CLIENT_SECRET. "
                "Create a client at https://www.warcraftlogs.com/api/clients/"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token: _CachedToken | None = None
        self._lock = Lock()

    # ---------------------------------------------------------------- auth
    def _get_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and self._token.expires_at - 30 > now:
                return self._token.access_token

            resp = requests.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            self._token = _CachedToken(
                access_token=payload["access_token"],
                expires_at=now + float(payload.get("expires_in", 3600)),
            )
            return self._token.access_token

    # ------------------------------------------------------------- queries
    def query(self, gql: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._get_token()
        resp = requests.post(
            API_URL,
            json={"query": gql, "variables": variables or {}},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            messages = "; ".join(e.get("message", "unknown") for e in data["errors"])
            raise RuntimeError(f"WCL GraphQL error: {messages}")
        return data.get("data", {})

    # Single combined query: metadata, fights and rankings in one round-trip.
    _REPORT_QUERY = """
    query($code: String!) {
      reportData {
        report(code: $code) {
          title
          startTime
          endTime
          owner { name }
          zone { name }
          fights(killType: Encounters) {
            id
            name
            difficulty
            kill
            startTime
            endTime
            encounterID
            keystoneLevel
            keystoneTime
            keystoneAffixes
            averageItemLevel
          }
          rankings(compare: Parses, playerMetric: default)
        }
      }
    }
    """

    def get_report(self, code: str) -> dict[str, Any]:
        code = extract_report_code(code)
        data = self.query(self._REPORT_QUERY, {"code": code})
        report = (data.get("reportData") or {}).get("report")
        if not report:
            raise RuntimeError(f"Report not found or not accessible: {code}")
        report["code"] = code
        return report

    def get_fight_tables(
        self, code: str, fight_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Fetch DamageDone + Healing tables for the given fight IDs.

        Uses GraphQL field aliases to fetch everything in a single request.
        Returns a dict keyed by fight id: ``{fight_id: {"damage": {...}, "healing": {...}}}``.
        Fights that error out are silently omitted.
        """
        if not fight_ids:
            return {}
        code = extract_report_code(code)
        # Build aliased fields: d123: table(fightIDs: [123], dataType: DamageDone) ...
        field_lines: list[str] = []
        for fid in fight_ids:
            field_lines.append(
                f"d{fid}: table(fightIDs: [{fid}], dataType: DamageDone)"
            )
            field_lines.append(
                f"h{fid}: table(fightIDs: [{fid}], dataType: Healing)"
            )
        gql = (
            "query($code: String!) {\n"
            "  reportData {\n"
            "    report(code: $code) {\n"
            + "\n".join("      " + line for line in field_lines)
            + "\n    }\n"
            "  }\n"
            "}\n"
        )
        data = self.query(gql, {"code": code})
        report = (data.get("reportData") or {}).get("report") or {}
        out: dict[int, dict[str, Any]] = {}
        for fid in fight_ids:
            out[fid] = {
                "damage": report.get(f"d{fid}"),
                "healing": report.get(f"h{fid}"),
            }
        return out

    def get_ilvl_bracket_parses(
        self,
        lookups: list[dict[str, Any]],
    ) -> dict[tuple[int, int], float]:
        """Return ilvl-bracket percentile for each (characterId, fightId).

        ``lookups`` items are dicts with keys:
          * ``characterId`` (int) — WCL character id
          * ``encounterId`` (int)
          * ``fightId``     (int) — the fight id within the *session* report
          * ``absStartTime``(int) — absolute epoch ms when the fight began
          * ``metric``      (str) — "dps" | "hps"

        One GraphQL request is issued per unique (characterId, metric) pair,
        aliasing one ``encounterRankings`` field per encounter the character
        played. We match by absolute ``startTime`` (±60 s tolerance) because
        each player uploads their own WCL report for the same M+ run, so the
        ``report.code`` on their personal rank entries will differ from the
        session report's code.
        """
        # Group by (character_id, metric) -> set of encounter_ids
        grouped: dict[tuple[int, str], set[int]] = {}
        per_fight: dict[tuple[int, int], int] = {}  # (character_id, fight_id) -> abs start ms
        for lk in lookups:
            cid = lk.get("characterId")
            eid = lk.get("encounterId")
            fid = lk.get("fightId")
            abs_start = lk.get("absStartTime")
            metric = lk.get("metric") or "dps"
            if not all(isinstance(v, int) for v in (cid, eid, fid, abs_start)):
                continue
            grouped.setdefault((cid, metric), set()).add(eid)
            per_fight[(cid, fid)] = abs_start

        out: dict[tuple[int, int], float] = {}
        for (cid, metric), enc_ids in grouped.items():
            field_lines = [
                (
                    f"e{eid}: encounterRankings("
                    f"encounterID: {eid}, byBracket: true, "
                    f"metric: {metric}, includePrivateLogs: true)"
                )
                for eid in enc_ids
            ]
            gql = (
                "query($id: Int!) {\n"
                "  characterData {\n"
                "    character(id: $id) {\n"
                + "\n".join("      " + line for line in field_lines)
                + "\n    }\n"
                "  }\n"
                "}\n"
            )
            try:
                data = self.query(gql, {"id": cid})
            except Exception:
                # One bad character shouldn't break the whole dashboard.
                continue
            char = ((data.get("characterData") or {}).get("character")) or {}
            # Build list of (fight_id, abs_start) expected for this character.
            wanted = [
                (fid, abs_start)
                for (c, fid), abs_start in per_fight.items()
                if c == cid
            ]
            for eid in enc_ids:
                er = char.get(f"e{eid}") or {}
                for rk in (er.get("ranks") or []):
                    rk_start = rk.get("startTime")
                    if not isinstance(rk_start, (int, float)):
                        continue
                    for fid, abs_start in wanted:
                        # 60 s tolerance — logs from different players can
                        # differ by a few hundred ms (combat start timestamp).
                        if abs(rk_start - abs_start) <= 60_000:
                            pct = rk.get("rankPercent")
                            if isinstance(pct, (int, float)):
                                prev = out.get((cid, fid))
                                if prev is None or pct > prev:
                                    out[(cid, fid)] = float(pct)
                            break
        return out
