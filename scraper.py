"""Scrape Warcraft Logs report pages directly to mirror the WCL website data.

We avoid the v2 GraphQL API (which exposes group placeholders for M+ Key %)
and instead hit the same JSON / HTML endpoints the SPA uses internally:

  * ``/reports/fights-and-participants/<code>/0?lang=en`` — JSON: fights list
  * ``/reports/table/<kind>/<code>/<fightId>/<startMs>/<endMs>/source/...`` — HTML

The HTML is the exact ``#main-table-0`` rendered on the WCL website, so the
columns we extract (Parse %, Key %, DPS, ilvl, …) are the same numbers a
human would copy from the page.

curl_cffi impersonates a real Chrome TLS fingerprint, which is what lets
these endpoints respond with data instead of "Use the API at /v1/docs".
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

from curl_cffi import requests as cffi


# Specs that determine role unambiguously.
_TANK_SPECS = {"Blood", "Guardian", "Protection", "Brewmaster", "Vengeance"}
_HEALER_SPECS = {"Restoration", "Holy", "Discipline", "Mistweaver", "Preservation"}


def _role_from_spec(class_name: str | None, spec: str | None) -> str:
    if not spec:
        return "DPS"
    if spec in _TANK_SPECS:
        return "Tank"
    if spec in _HEALER_SPECS:
        return "Healer"
    return "DPS"


def _strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def _parse_amount(raw: str) -> tuple[float | None, str | None]:
    """Return (absolute_amount, percent_text) parsed from the Amount cell HTML."""
    pct_m = re.search(r"report-amount-percent[^>]*>\s*([\d.,]+)%", raw)
    abs_m = re.search(r"report-amount-total[^>]*>\s*([\d.,]+)\s*([kKmMbB]?)", raw)
    abs_val: float | None = None
    if abs_m:
        try:
            n = float(abs_m.group(1).replace(",", ""))
            unit = abs_m.group(2).lower()
            mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(unit, 1)
            abs_val = n * mult
        except ValueError:
            abs_val = None
    pct_text = pct_m.group(1) + "%" if pct_m else None
    return abs_val, pct_text


def _parse_number(text: str) -> float | None:
    text = text.strip().replace(",", "")
    if not text or text in {"-", "—"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# A row starts at ``<tr ... id="main-table-row-..."`` and ends at the next
# row start or </tbody>. Inside a row the top-level cells are identified by
# their distinctive ``class="main-table-…"`` markers (the inner name cell
# contains its own nested <table>, so naive <td> splitting doesn't work).
_ROW_START_RE = re.compile(r'<tr[^>]*id="(main-table-row-[^"]+)"[^>]*>', re.S)
_SPRITE_RE = re.compile(r"actor-sprite-([A-Za-z]+)(?:-([A-Za-z]+))?")

# Each entry: (key, regex matching the cell open tag). We slice from the
# opening tag's end to the start of the next known top-level cell.
_CELL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("parse",   re.compile(r'<td\b[^>]*class="main-table-performance[^"]*"[^>]*>', re.S)),
    ("name",    re.compile(r'<td\b[^>]*class="main-table-name[^"]*"[^>]*>', re.S)),
    ("amount",  re.compile(r'<td\b[^>]*class="[^"]*main-table-amount[^"]*"[^>]*>', re.S)),
    ("ilvl",    re.compile(r'<td\b[^>]*class="[^"]*main-table-ilvl[^"]*"[^>]*>', re.S)),
    ("key",     re.compile(r'<td\b[^>]*class="main-table-ilvl-performance[^"]*"[^>]*>', re.S)),
    ("active",  re.compile(r'<td\b[^>]*class="[^"]*main-table-active[^"]*"[^>]*>', re.S)),
    ("rate",    re.compile(r'<td\b[^>]*class="[^"]*main-per-second-amount[^"]*"[^>]*>', re.S)),
    ("pin",     re.compile(r'<td\b[^>]*class="pin-select-cell[^"]*"[^>]*>', re.S)),
]


def _extract_cells(row_html: str) -> dict[str, str]:
    """Return a dict mapping cell key → raw inner HTML for that cell."""
    # Find each known cell's opening tag position.
    found: list[tuple[int, int, str]] = []  # (start, end_of_open_tag, key)
    for key, pat in _CELL_PATTERNS:
        m = pat.search(row_html)
        if m:
            found.append((m.start(), m.end(), key))
    found.sort(key=lambda t: t[0])
    cells: dict[str, str] = {}
    for i, (_start, open_end, key) in enumerate(found):
        next_start = found[i + 1][0] if i + 1 < len(found) else len(row_html)
        cells[key] = row_html[open_end:next_start]
    return cells


def _parse_table_rows(html_doc: str, kind: str) -> list[dict[str, Any]]:
    """Extract player rows from the table HTML returned by WCL.

    ``kind`` is ``"damage"`` or ``"healing"`` and only affects the throughput
    field name (``dps`` vs ``hps``).
    """
    out: list[dict[str, Any]] = []
    throughput_key = "hps" if kind == "healing" else "dps"
    starts = [(m.start(), m.end(), m.group(1)) for m in _ROW_START_RE.finditer(html_doc)]
    if not starts:
        return out
    # Append a sentinel end position so the last real row gets a slice.
    end_marker = html_doc.find("</tbody>")
    if end_marker == -1:
        end_marker = len(html_doc)
    for i, (_, body_start, row_id) in enumerate(starts):
        if "main-table-row-totals" in row_id:
            continue
        body_end = starts[i + 1][0] if i + 1 < len(starts) else end_marker
        body = html_doc[body_start:body_end]
        cells = _extract_cells(body)
        if "name" not in cells or "parse" not in cells:
            continue
        parse_pct = _parse_number(_strip_html(cells["parse"]))
        sprite = _SPRITE_RE.search(cells["name"])
        class_name = sprite.group(1) if sprite else None
        spec = sprite.group(2) if sprite else None
        anchors = re.findall(r">\s*([A-Za-z0-9_\- ]+?)\s*</a>", cells["name"], re.S)
        # Last anchor in the name cell holds the player display name.
        name = anchors[-1].strip() if anchors else "Unknown"

        amount_abs, _amt_pct = _parse_amount(cells.get("amount", ""))
        ilvl = _parse_number(_strip_html(cells.get("ilvl", "")))
        key_pct = _parse_number(_strip_html(cells.get("key", "")))
        active_pct = _parse_number(_strip_html(cells.get("active", "")).rstrip("%"))
        throughput = _parse_number(_strip_html(cells.get("rate", "")))

        # actorId is the middle segment of the row id (main-table-row-<actor>-<row>-<idx>)
        actor_id = None
        m = re.match(r"main-table-row-(\d+)-", row_id)
        if m:
            try:
                actor_id = int(m.group(1))
            except ValueError:
                actor_id = None

        out.append({
            "id": actor_id,
            "name": name,
            "class": class_name,
            "spec": spec,
            "role": _role_from_spec(class_name, spec),
            "rankPercent": parse_pct,    # WCL "Parse %"
            "bracketPercent": key_pct,    # WCL "Key %"
            "amount": amount_abs,
            "itemLevel": ilvl,
            "activePercent": active_pct,
            throughput_key: throughput,
        })
    return out


class WCLScraper:
    """Lightweight client for WCL's internal report endpoints."""

    BASE = "https://www.warcraftlogs.com"

    def __init__(self) -> None:
        self._session = cffi.Session()
        self._warmed_for: str | None = None

    def _warm(self, code: str) -> None:
        # WCL requires a real browser TLS fingerprint AND a Referer matching
        # the report page on subsequent table requests. Hitting the report
        # page once primes the cookie jar and also lets us reuse the session.
        if self._warmed_for == code:
            return
        self._session.get(
            f"{self.BASE}/reports/{code}",
            impersonate="chrome",
            timeout=30,
        )
        self._warmed_for = code

    def _headers(self, code: str) -> dict[str, str]:
        return {
            "Referer": f"{self.BASE}/reports/{code}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

    def get_fights(self, code: str) -> dict[str, Any]:
        self._warm(code)
        r = self._session.get(
            f"{self.BASE}/reports/fights-and-participants/{code}/0?lang=en",
            impersonate="chrome",
            headers=self._headers(code),
            timeout=30,
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception as exc:
            raise RuntimeError(
                f"WCL did not return JSON for fights of {code}: {r.text[:200]}"
            ) from exc

    def get_table_html(
        self,
        code: str,
        kind: str,
        fight_id: int,
        start_ms: int,
        end_ms: int,
    ) -> str:
        """Fetch the rendered ``#main-table-0`` HTML for a fight + kind."""
        if kind not in {"damage-done", "healing"}:
            raise ValueError(f"unsupported kind {kind!r}")
        self._warm(code)
        # The trailing path segments are filter slots used by WCL's UI; the
        # exact values are unused by the data fetch (any int works).
        url = (
            f"{self.BASE}/reports/table/{kind}/{code}/{fight_id}/{start_ms}/{end_ms}"
            "/source/0/0/0/0/0/0/-1.0.-1.-1/0/Any/Any/0/0"
        )
        r = self._session.get(
            url,
            impersonate="chrome",
            headers=self._headers(code),
            timeout=30,
        )
        r.raise_for_status()
        return r.text

    # --- high-level helpers ------------------------------------------------
    def latest_completed_fight(self, fights: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        completed = [f for f in fights if f.get("kill")]
        if not completed:
            return None
        return max(completed, key=lambda f: f.get("end_time") or 0)

    def scrape_fight(
        self,
        code: str,
        fight: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return merged player rows (damage + healing) for a single fight."""
        fid = fight["id"]
        start = fight["start_time"]
        end = fight["end_time"]
        dmg_html = self.get_table_html(code, "damage-done", fid, start, end)
        heal_html = self.get_table_html(code, "healing", fid, start, end)
        dmg_rows = _parse_table_rows(dmg_html, "damage")
        heal_rows = _parse_table_rows(heal_html, "healing")

        # Merge by name. Healing-table role overrides damage-table guess for
        # players whose damage row inferred the wrong role (very rare since
        # actor-sprite carries the spec).
        by_name: dict[str, dict[str, Any]] = {}
        for row in dmg_rows:
            by_name[row["name"]] = row
        for hrow in heal_rows:
            slot = by_name.setdefault(hrow["name"], hrow)
            if slot is not hrow:
                slot["hps"] = hrow.get("hps")
            # If the healing table reports a healer spec, trust it.
            if hrow.get("role") == "Healer":
                slot["role"] = "Healer"
                slot.setdefault("class", hrow.get("class"))
                slot.setdefault("spec", hrow.get("spec"))
        return list(by_name.values())

    def build_payload(self, code: str) -> dict[str, Any]:
        """Return a dashboard-shaped payload for the latest completed fight."""
        fights_doc = self.get_fights(code)
        fights = fights_doc.get("fights") or []
        latest = self.latest_completed_fight(fights)
        dungeons: list[dict[str, Any]] = []
        if latest is not None:
            characters = self.scrape_fight(code, latest)
            characters.sort(key=lambda c: (c.get("rankPercent") or -1), reverse=True)
            dungeons.append({
                "fightId": latest.get("id"),
                "name": latest.get("name") or latest.get("zoneName"),
                "encounterId": latest.get("boss"),
                "startTime": latest.get("start_time"),
                "duration": (latest.get("end_time") or 0) - (latest.get("start_time") or 0),
                "kill": bool(latest.get("kill")),
                "keystoneLevel": latest.get("keystoneLevel"),
                "characters": characters,
            })
        return {
            "code": code,
            "title": fights_doc.get("title") or "",
            "zone": (latest or {}).get("zoneName") if latest else None,
            "owner": "",
            "startTime": (latest or {}).get("start_time"),
            "endTime": (latest or {}).get("end_time"),
            "dungeonCount": len(dungeons),
            "dungeons": dungeons,
        }


def extract_report_code(value: str) -> str:
    """Pull the report code out of a WCL URL or return ``value`` if it already
    looks like a code."""
    s = (value or "").strip()
    if not s:
        raise ValueError("empty report value")
    m = re.search(r"reports/(?:a:)?([a-zA-Z0-9]{12,24})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{12,24}", s):
        return s
    raise ValueError(f"could not extract report code from {value!r}")
