// Client-side logic for the WCL LAN dashboard.
// Polls /api/dashboard every 10s. Role filter (DPS/Healer/Tank) is applied
// locally so toggling doesn't require a round-trip.

const REFRESH_MS = 10_000;
const ROLE_PREF_KEY = "wcl.roleFilter";
const BRACKET_PREF_KEY = "wcl.useBracket";
const EXPANDED_PREF_KEY = "wcl.expanded";
const ROLES = ["DPS", "Healer", "Tank"];

const els = {
  form:          document.getElementById("report-form"),
  input:         document.getElementById("report-input"),
  refreshBtn:    document.getElementById("refresh-btn"),
  status:        document.getElementById("status"),
  reportMeta:    document.getElementById("report-meta"),
  sessionAvg:    document.getElementById("session-average"),
  sessionSub:    document.getElementById("session-sub"),
  latestTitle:   document.getElementById("latest-title"),
  latestAverage: document.getElementById("latest-average"),
  latestPlayers: document.getElementById("latest-players"),
  dungeonList:   document.getElementById("dungeon-list"),
  roleToggles:   document.querySelectorAll(".role-toggle"),
  bracketToggle: document.getElementById("bracket-toggle"),
};

let refreshTimer = null;

// Which dungeons are currently expanded. Keyed by fightId.
const expanded = new Set();
try {
  const saved = JSON.parse(sessionStorage.getItem(EXPANDED_PREF_KEY) || "[]");
  if (Array.isArray(saved)) saved.forEach((id) => expanded.add(id));
} catch { /* ignore */ }

function saveExpanded() {
  try {
    sessionStorage.setItem(EXPANDED_PREF_KEY, JSON.stringify([...expanded]));
  } catch { /* ignore */ }
}

// Whether to show ilvl-bracket parses instead of overall parses.
let useBracket = (() => {
  try { return localStorage.getItem(BRACKET_PREF_KEY) === "1"; } catch { return false; }
})();
els.bracketToggle.checked = useBracket;
els.bracketToggle.addEventListener("change", () => {
  useBracket = els.bracketToggle.checked;
  try { localStorage.setItem(BRACKET_PREF_KEY, useBracket ? "1" : "0"); } catch { /* ignore */ }
  if (lastData) render(lastData);
});
let lastData = null; // raw payload from the server

// --- role filter persistence ---------------------------------------------
function loadRolePref() {
  try {
    const saved = JSON.parse(localStorage.getItem(ROLE_PREF_KEY) || "null");
    if (Array.isArray(saved) && saved.every((r) => ROLES.includes(r))) return new Set(saved);
  } catch { /* ignore */ }
  return new Set(ROLES); // default: all roles included
}

function saveRolePref(set) {
  try { localStorage.setItem(ROLE_PREF_KEY, JSON.stringify([...set])); } catch { /* ignore */ }
}

let selectedRoles = loadRolePref();

// Initialise checkboxes from stored pref.
for (const cb of els.roleToggles) {
  cb.checked = selectedRoles.has(cb.value);
  cb.addEventListener("change", () => {
    if (cb.checked) selectedRoles.add(cb.value);
    else selectedRoles.delete(cb.value);
    saveRolePref(selectedRoles);
    if (lastData) render(lastData);
  });
}

// --- formatting helpers --------------------------------------------------
function parseTier(pct) {
  if (pct == null || Number.isNaN(pct)) return "";
  if (pct >= 99) return "t-legendary";
  if (pct >= 95) return "t-artifact";
  if (pct >= 75) return "t-epic";
  if (pct >= 50) return "t-rare";
  if (pct >= 25) return "t-uncommon";
  return "";
}

function fmtParse(pct) {
  if (pct == null || Number.isNaN(pct)) return "—";
  return Number(pct).toFixed(1);
}

function fmtDuration(ms) {
  if (!ms || ms < 0) return "";
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtTime(epochMs) {
  if (!epochMs) return "";
  try {
    return new Date(epochMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
}

function setStatus(msg, { error = false } = {}) {
  els.status.textContent = msg || "";
  els.status.classList.toggle("error", Boolean(error));
}

// Value used for parse display/averaging — depends on bracket toggle.
function parseValue(c) {
  if (!c) return null;
  const v = useBracket ? c.bracketPercent : c.rankPercent;
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}

// Format DPS/HPS "amount" as 1.23M / 456k.
function fmtAmount(n) {
  if (typeof n !== "number" || !Number.isFinite(n) || n <= 0) return "";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

// --- averaging (filtered by selectedRoles) -------------------------------
function filterChars(characters) {
  return (characters || []).filter((c) => selectedRoles.has(c.role));
}

function averageFor(characters) {
  const vals = filterChars(characters)
    .map(parseValue)
    .filter((v) => v != null);
  if (!vals.length) return null;
  const sum = vals.reduce((a, b) => a + b, 0);
  return Math.round((sum / vals.length) * 10) / 10;
}

// --- rendering -----------------------------------------------------------
function renderPlayer(p) {
  const node = document.createElement("div");
  node.className = "player";
  node.setAttribute("role", "listitem");

  const left = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = p.name || "Unknown";
  const role = document.createElement("div");
  role.className = "role";
  role.textContent = [p.role, p.spec, p.class].filter(Boolean).join(" · ");
  left.append(name, role);

  const pct = parseValue(p);
  const parse = document.createElement("div");
  parse.className = `parse ${parseTier(pct)}`;
  parse.setAttribute(
    "aria-label",
    `${useBracket ? "Bracket parse" : "Parse"} ${fmtParse(pct)} percent`,
  );
  parse.textContent = fmtParse(pct);

  node.append(left, parse);

  const amount = fmtAmount(p.amount);
  if (d.fightId != null && expanded.has(d.fightId)) li.setAttribute("open", "");

  const button = document.createElement("button");
  button.type = "button";
  button.className = "dungeon-summary";
  const isOpen = d.fightId != null && expanded.has(d.fightId);
  button.setAttribute("aria-expanded", isOpen ? "true" : "false");

  const chev = document.createElement("span");
  chev.className = "chev";
  chev.setAttribute("aria-hidden", "true");
  chev.textContent = "▶";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = d.name || "Dungeon";

  if (d.keystoneLevel) {
    const badge = document.createElement("span");
    badge.className = "badge key";
    badge.textContent = `+${d.keystoneLevel}`;
    title.appendChild(badge);
  }
  if (d.kill === true) {
    const badge = document.createElement("span");
    badge.className = "badge kill";
    badge.textContent = "Completed";
    title.appendChild(badge);
  } else if (d.kill === false) {
    const badge = document.createElement("span");
    badge.className = "badge wipe";
    badge.textContent = "Wipe";
    title.appendChild(badge);
  }

  const avgValue = averageFor(d.characters);
  const avg = document.createElement("div");
  avg.className = `avg parse ${parseTier(avgValue)}`;
  avg.textContent = fmtParse(avgValue);

  button.append(chev, title, avg);
  li.appendChild(button);

  const when = document.createElement("div");
  when.className = "when";
  const startedAt = fmtTime(d.startTime);
  const dur = fmtDuration(d.duration);
  when.textContent = [startedAt && `Started ${startedAt}`, dur && `Duration ${dur}`]
    .filter(Boolean)
    .join(" · ");
  li.appendChild(when);

  const details = document.createElement("div");
  details.className = "dungeon-details";
  if (!isOpen) details.setAttribute("hidden", "");
  details.appendChild(
    renderPlayerGrid(d.characters, {
      emptyMessage:
        d.kill === false
          ? "No parse data for this run (wipe)."
          : "No players match the current role filter.",
    }),
  );
  li.appendChild(details);

  button.addEventListener("click", () => {
    const open = !li.hasAttribute("open");
    if (open) {
      li.setAttribute("open", "");
      details.removeAttribute("hidden");
      if (d.fightId != null) expanded.add(d.fightId);
    } else {
      li.removeAttribute("open");
      details.setAttribute("hidden", "");
      if (d.fightId != null) expanded.delete(d.fightId);
    }
    button.setAttribute("aria-expanded", open ? "true" : "false");
    saveExpanded();
  });
ey";
    badge.textContent = `+${d.keystoneLevel}`;
    title.appendChild(badge);
  }
  if (d.kill === true) {
    const badge = document.createElement("span");
    badge.className = "badge kill";
    badge.textContent = "Completed";
    title.appendChild(badge);
  } else if (d.kill === false) {
    const badge = document.createElement("span");
    badge.className = "badge wipe";
    badge.textContent = "Wipe";
    title.appendChild(badge);
  }

  const avgValue = averageFor(d.characters);
  const avg = document.createElement("div");
  avg.className = `avg parse ${parseTier(avgValue)}`;
  avg.textContent = fmtParse(avgValue);

  const when = document.createElement("div");
  when.className = "when";
  const startedAt = fmtTime(d.startTime);
  const dur = fmtDuration(d.duration);
  when.textContent = [startedAt && `Started ${startedAt}`, dur && `Duration ${dur}`]
    .filter(Boolean)
    .join(" · ");

  li.append(title, avg, when);
  return li;
}

function render(data) {
  // Header meta
  const metaParts = [data.title, data.zone, data.owner && `by ${data.owner}`].filter(Boolean);
  els.reportMeta.textContent = metaParts.length ? metaParts.join(" · ") : "Report loaded";

  const dungeons = data.dungeons || [];

  // Session average across all dungeons, filtered by selected roles.
  const allChars = dungeons.flatMap((d) => d.characters || []);
  const sessionAvg = averageFor(allChars);
  els.sessionAvg.textContent = fmtParse(sessionAvg);
  els.sessionAvg.className = `big-number parse ${parseTier(sessionAvg)}`;
  els.sessionSub.textContent =
    dungeons.length === 1 ? "Across 1 dungeon" : `Across ${dungeons.length} dungeons`;

  // Latest dungeon = most recent by startTime.
  const latest = dungeons.length
    ? [...dungeons].sort((a, b) => (b.startTime || 0) - (a.startTime || 0))[0]
    : null;

  els.latestPlayers.replaceChildren();
  if (latest) {
    const bits = [latest.name];
    if (latest.keystoneLevel) bits.push(`+${latest.keystoneLevel}`);
    if (latest.duration) bits.push(fmtDuration(latest.duration));
    if (latest.kill === false) bits.push("Wipe");
    els.latestTitle.textContent = bits.join(" · ");

    const latestAvg = averageFor(latest.characters);
    els.latestAverage.textContent = fmtParse(latestAvg);
    els.latestAverage.className = `big-number parse ${parseTier(latestAvg)}`;

    els.latestPlayers.appendChild(
      renderPlayerGrid(latest.characters, {
        emptyMessage:
          latest.kill === false
            ? "No parse data for this run (wipe)."
            : "No players match the current role filter.",
      }),
    );
  } else {
    els.latestTitle.textContent = "No dungeons yet";
    els.latestAverage.textContent = "—";
    els.latestAverage.className = "big-number";
  }

  // Dungeon list — newest first
  els.dungeonList.replaceChildren();
  const sorted = [...dungeons].sort((a, b) => (b.startTime || 0) - (a.startTime || 0));
  for (const d of sorted) els.dungeonList.appendChild(renderDungeon(d));
}

// --- data loading --------------------------------------------------------
async function loadDashboard({ silent = false } = {}) {
  try {
    if (!silent) setStatus("Refreshing…");
    const res = await fetch("/api/dashboard", { cache: "no-store" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      setStatus(body.error || `Failed to load (${res.status})`, { error: true });
      return;
    }
    lastData = body;
    render(body);
    setStatus(`Updated ${new Date().toLocaleTimeString()}`);
  } catch (err) {
    setStatus(String(err.message || err), { error: true });
  }
}

async function setReport(value) {
  setStatus("Setting report…");
  const res = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ report: value }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    setStatus(body.error || `Failed (${res.status})`, { error: true });
    return;
  }
  els.input.value = body.report_code;
  await loadDashboard();
}

els.form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const value = els.input.value.trim();
  if (value) setReport(value);
});

els.refreshBtn.addEventListener("click", () => loadDashboard());

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => loadDashboard({ silent: true }), REFRESH_MS);
}

// Initial load.
if (els.input.value.trim()) {
  loadDashboard();
} else {
  setStatus("Paste a Warcraft Logs report URL or code to begin.");
}
startAutoRefresh();
