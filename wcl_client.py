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
from urllib.parse import urlencode

import requests

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
AUTHORIZE_URL = "https://www.warcraftlogs.com/oauth/authorize"
API_URL = "https://www.warcraftlogs.com/api/v2/client"
USER_API_URL = "https://www.warcraftlogs.com/api/v2/user"

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
    def _query_endpoint(
        self,
        endpoint: str,
        token: str,
        gql: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = requests.post(
            endpoint,
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

    def query(self, gql: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._get_token()
        return self._query_endpoint(API_URL, token, gql, variables)

    def query_user(
        self,
        access_token: str,
        gql: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not access_token:
            raise RuntimeError("Missing user access token")
        return self._query_endpoint(USER_API_URL, access_token, gql, variables)

    def build_authorize_url(self, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_authorization_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(self._client_id, self._client_secret),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("access_token"):
            raise RuntimeError("WCL OAuth response missing access_token")
        return payload

    def refresh_user_access_token(self, refresh_token: str) -> dict[str, Any]:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(self._client_id, self._client_secret),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("access_token"):
            raise RuntimeError("WCL OAuth refresh response missing access_token")
        return payload

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
          rankings(compare: Rankings, playerMetric: dps)
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

    def get_report_rankings_user(self, code: str, access_token: str) -> dict[str, Any]:
        """Fetch report rankings from the user-authenticated API endpoint."""
        code = extract_report_code(code)
        gql = """
        query($code: String!) {
            reportData {
                report(code: $code) {
                    rankings(compare: Rankings, playerMetric: dps)
                }
            }
        }
        """
        data = self.query_user(access_token, gql, {"code": code})
        report = (data.get("reportData") or {}).get("report") or {}
        rankings = report.get("rankings")
        return rankings if isinstance(rankings, dict) else {"data": []}

    def get_latest_user_report_code(self, user_id: int) -> str | None:
        """Return the newest public report code uploaded by ``user_id``.

        WCL orders ``reportData.reports`` by ``startTime`` descending, so the
        first entry is the most recent upload. Returns ``None`` if the user has
        no accessible reports.
        """
        gql = """
        query($uid: Int!) {
          reportData {
            reports(userID: $uid, limit: 1) {
              data { code startTime }
            }
          }
        }
        """
        data = self.query(gql, {"uid": int(user_id)})
        reports = (
            ((data.get("reportData") or {}).get("reports") or {}).get("data") or []
        )
        if not reports:
            return None
        return reports[0].get("code")

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
    ) -> dict[tuple[int, int], dict[str, float]]:
        """Return per-character, per-fight overall + bracket (Key %) parses.

        ``lookups`` items are dicts with keys:
          * ``characterId`` (int) - WCL character id
          * ``encounterId`` (int)
          * ``fightId``     (int) - fight id within the *session* report
          * ``absStartTime``(int) - absolute epoch ms when the fight began
          * ``reportCode``  (str, optional) - when provided, only rank rows
            from this report code are eligible matches
          * ``metric``      (str) - "dps" | "hps"

        Returns ``{(characterId, fightId): {"overall": float, "bracket": float}}``.
        Matching is done by absolute ``startTime`` (+/- 60 s). If ``reportCode``
        is present in a lookup, rank rows from other report codes are ignored.
        """
        # Group by (character_id, metric) -> set of encounter_ids
        grouped: dict[tuple[int, str], set[int]] = {}
        per_fight: dict[tuple[int, int], dict[str, Any]] = {}
        # (character_id, fight_id) -> {absStartTime, reportCode}
        for lk in lookups:
            cid = lk.get("characterId")
            eid = lk.get("encounterId")
            fid = lk.get("fightId")
            abs_start = lk.get("absStartTime")
            report_code = lk.get("reportCode")
            metric = lk.get("metric") or "dps"
            if not all(isinstance(v, int) for v in (cid, eid, fid, abs_start)):
                continue
            if report_code is not None and not isinstance(report_code, str):
                report_code = None
            grouped.setdefault((cid, metric), set()).add(eid)
            per_fight[(cid, fid)] = {
                "absStartTime": abs_start,
                "reportCode": (report_code or "").strip(),
            }

        out: dict[tuple[int, int], dict[str, float]] = {}
        for (cid, metric), enc_ids in grouped.items():
            field_lines: list[str] = []
            for eid in enc_ids:
                field_lines.append(
                    f"o{eid}: encounterRankings("
                    f"encounterID: {eid}, byBracket: false, "
                    f"metric: {metric}, includePrivateLogs: true)"
                )
                field_lines.append(
                    f"b{eid}: encounterRankings("
                    f"encounterID: {eid}, byBracket: true, "
                    f"metric: {metric}, includePrivateLogs: true)"
                )
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
                continue
            char = ((data.get("characterData") or {}).get("character")) or {}
            wanted = {
                fid: meta
                for (c, fid), meta in per_fight.items()
                if c == cid
            }
            best_score: dict[tuple[int, str], float] = {}
            for eid in enc_ids:
                for prefix, key in (("o", "overall"), ("b", "bracket")):
                    er = char.get(f"{prefix}{eid}") or {}
                    best_for_fight: dict[int, tuple[float, float]] = {}
                    for rk in (er.get("ranks") or []):
                        rk_start = rk.get("startTime")
                        if not isinstance(rk_start, (int, float)):
                            continue
                        pct = rk.get("rankPercent")
                        if not isinstance(pct, (int, float)):
                            continue
                        rk_report = (rk.get("report") or {}).get("code")
                        rk_code = rk_report.strip() if isinstance(rk_report, str) else ""
                        for fid, meta in wanted.items():
                            abs_start = meta.get("absStartTime")
                            if not isinstance(abs_start, (int, float)):
                                continue
                            diff = abs(rk_start - abs_start)
                            if diff > 60_000:
                                continue
                            target_code = meta.get("reportCode") or ""
                            if target_code:
                                if not rk_code or rk_code != target_code:
                                    continue
                            prev = best_for_fight.get(fid)
                            if prev is None or diff < prev[0]:
                                best_for_fight[fid] = (float(diff), float(pct))

                    for fid, (diff, pct_value) in best_for_fight.items():
                        score_key = (fid, key)
                        prev_best = best_score.get(score_key)
                        if prev_best is not None and diff >= prev_best:
                            continue
                        best_score[score_key] = diff
                        slot = out.setdefault((cid, fid), {})
                        slot[key] = pct_value
        return out
