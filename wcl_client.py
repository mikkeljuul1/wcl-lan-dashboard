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
          rankings
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
