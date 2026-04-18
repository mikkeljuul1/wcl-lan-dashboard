// Client-side logic for the WCL LAN dashboard.
// Polls /api/dashboard at a regular interval so new pulls appear automatically.

const REFRESH_MS = 60_000; // 1 minute

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
};

let refreshTimer = null;

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

  const parse = document.createElement("div");
  parse.className = `parse ${parseTier(p.rankPercent)}`;
  parse.setAttribute("aria-label", `Parse ${fmtParse(p.rankPercent)} percent`);
  parse.textContent = fmtParse(p.rankPercent);

  node.append(left, parse);
  return node;
}

function renderDungeon(d) {
  const li = document.createElement("li");
  li.className = "dungeon";

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

  const avg = document.createElement("div");
  avg.className = `avg ${parseTier(d.averageParse)}`;
  avg.textContent = fmtParse(d.averageParse);

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

  // Session summary
  els.sessionAvg.textContent = fmtParse(data.sessionAverage);
  els.sessionAvg.className = `big-number parse ${parseTier(data.sessionAverage)}`;
  els.sessionSub.textContent =
    data.dungeonCount === 1
      ? "Across 1 dungeon"
      : `Across ${data.dungeonCount} dungeons`;

  // Latest dungeon
  els.latestPlayers.replaceChildren();
  if (data.latest) {
    const bits = [data.latest.name];
    if (data.latest.keystoneLevel) bits.push(`+${data.latest.keystoneLevel}`);
    if (data.latest.duration) bits.push(fmtDuration(data.latest.duration));
    els.latestTitle.textContent = bits.join(" · ");

    els.latestAverage.textContent = fmtParse(data.latest.averageParse);
    els.latestAverage.className = `big-number parse ${parseTier(data.latest.averageParse)}`;

    for (const p of data.latest.characters || []) {
      els.latestPlayers.appendChild(renderPlayer(p));
    }
  } else {
    els.latestTitle.textContent = "No dungeons yet";
    els.latestAverage.textContent = "—";
    els.latestAverage.className = "big-number";
  }

  // Dungeon list — newest first
  els.dungeonList.replaceChildren();
  const sorted = [...(data.dungeons || [])].sort(
    (a, b) => (b.startTime || 0) - (a.startTime || 0),
  );
  for (const d of sorted) els.dungeonList.appendChild(renderDungeon(d));
}

async function loadDashboard() {
  try {
    setStatus("Refreshing…");
    const res = await fetch("/api/dashboard", { cache: "no-store" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      setStatus(body.error || `Failed to load (${res.status})`, { error: true });
      return;
    }
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
  refreshTimer = setInterval(loadDashboard, REFRESH_MS);
}

// Initial load.
if (els.input.value.trim()) {
  loadDashboard();
} else {
  setStatus("Paste a Warcraft Logs report URL or code to begin.");
}
startAutoRefresh();
