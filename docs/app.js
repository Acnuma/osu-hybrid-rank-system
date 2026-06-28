"use strict";

// The leaderboard CSV lives next to this file (GitHub Pages serves /docs as the
// site root, so the data must sit inside it). Regenerate with:
//   python hybrid_rank.py --anchor union --otr <key> --osu-api --out docs/hybrid_leaderboard.csv
const CSV_URL = "hybrid_leaderboard.csv";
// Sidecar written by hybrid_rank.py at generation time. We read the date from
// HERE (not the HTTP Last-Modified header) so the "updated" stamp reflects only
// LEADERBOARD DATA refreshes, never website/code deploys. It also carries the
// weights + per-axis normalization params the calculator reuses.
const META_URL = "hybrid_leaderboard.meta.json";

// Default weights, used only if the meta sidecar is missing them.
const DEFAULT_WEIGHTS = { pp: 0.30, elo: 0.35, otr: 0.35 };

// column key -> numeric? Order here is irrelevant; the <thead> drives layout.
const NUMERIC = new Set([
  "hybrid_rank", "pp_rank", "pp", "elo_rank", "elo_rating", "elo_raw",
  "otr_rank", "otr_rating", "tournaments_played", "plays", "hybrid_score",
  "elo_delta",  // derived (elo_rank - hybrid_rank); null when Elo is seeded
  "pp_delta",   // TEMP derived (pp_rank - hybrid_rank) for testing the vs-pp column
  "otr_delta",  // TEMP derived (otr_rank - hybrid_rank); null when OTR is seeded
]);

let ROWS = [];                       // parsed objects
// hybrid_score is higher-is-better now, so the board opens sorted by it descending.
let sortKey = "hybrid_score";
let sortDir = -1;                    // 1 = ascending, -1 = descending

const $ = (sel) => document.querySelector(sel);

// --- minimal CSV parse (osu usernames contain no commas, so split is safe) ---
function parseCSV(text) {
  const lines = text.trim().split(/\r?\n/);
  const header = lines[0].split(",");
  const out = new Array(lines.length - 1);
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(",");
    const obj = {};
    for (let c = 0; c < header.length; c++) {
      const key = header[c];
      const raw = cells[c];
      obj[key] = NUMERIC.has(key) ? parseFloat(raw) : raw;
    }
    // how many places the player ranks higher (+) or lower (-) than Elo alone;
    // null when the Elo is seeded (no real Elo rank to compare against)
    obj.elo_delta = Number.isNaN(obj.elo_rank) ? null : obj.elo_rank - obj.hybrid_rank;
    // TEMP: places gained/lost versus PP rank alone (pp_rank − hybrid_rank).
    obj.pp_delta = Number.isNaN(obj.pp_rank) ? null : obj.pp_rank - obj.hybrid_rank;
    // TEMP: versus OTR global rank (otr_rank − hybrid_rank); null when OTR seeded.
    obj.otr_delta = Number.isNaN(obj.otr_rank) ? null : obj.otr_rank - obj.hybrid_rank;
    // "yes"/"" flags from the CSV -> booleans. Absent on older CSVs -> false.
    obj.provisional = obj.provisional === "yes";
    obj.otr_estimated = obj.otr_estimated === "yes";
    obj.elo_estimated = obj.elo_estimated === "yes";
    obj.elo_shrunk = obj.elo_shrunk === "yes";
    out[i - 1] = obj;
  }
  return out;
}

function fmt(key, val) {
  if (key === "hybrid_score") return val.toFixed(3);
  if (NUMERIC.has(key)) return val.toLocaleString("en-US");
  return val;
}

function deltaCell(d) {
  if (d === null || d === undefined) return `<td class="delta zero">—</td>`;
  if (d > 0) return `<td class="delta up">▴ +${d.toLocaleString("en-US")}</td>`;
  if (d < 0) return `<td class="delta down">▾ ${d.toLocaleString("en-US")}</td>`;
  return `<td class="delta zero">0</td>`;
}

function rowHTML(r) {
  const url = `https://osu.ppy.sh/users/${r.user_id}/osu`;
  const prov = r.provisional
    ? `<abbr class="prov" title="Provisional rating — too few recent ranked-play matches for a stable Elo.">*</abbr>`
    : "";
  const est = r.otr_estimated
    ? `<abbr class="prov" title="Estimated from osu! rank — this player has no verified tournament play, so OTR's starting prior is used.">~</abbr>`
    : "";
  // Shrinkage applies to EVERY real Elo (continuously), so a per-row "shrunk"
  // symbol would just be threshold noise. Instead the Elo NUMBER itself is the
  // hover target: mousing over it reveals the raw rating + match count. Only the
  // genuinely categorical states keep a visible mark — `*` provisional (osu!'s own
  // "not yet stable" flag) and `^` seeded (no real Elo at all; a PP estimate).
  let eloCell;
  if (r.elo_estimated) {
    eloCell = `<abbr class="prov" title="Estimated from PP — this player has never queued ranked play, so an Elo is inferred from their pp (the w=0 limit of the shrinkage below).">${fmt("elo_rating", r.elo_rating)}^</abbr>`;
  } else {
    const rawTxt = Number.isNaN(r.elo_raw) ? "?" : r.elo_raw.toLocaleString("en-US");
    const matches = `${r.plays} ranked ${r.plays === 1 ? "match" : "matches"}`;
    const title = `Ranked Play Elo. The shown value is sample-size adjusted toward its PP-expected value (weight n/(n+5)); raw Elo ${rawTxt} over ${matches}.`;
    eloCell = `<abbr class="elo-num" title="${title}">${fmt("elo_rating", r.elo_rating)}</abbr>${r.provisional ? prov : ""}`;
  }
  return (
    `<tr>` +
    `<td class="rank">${r.hybrid_rank.toLocaleString("en-US")}</td>` +
    `<td class="left"><a class="user" href="${url}" target="_blank" rel="noopener">${escapeHTML(r.username)}</a></td>` +
    deltaCell(r.pp_delta) +
    deltaCell(r.elo_delta) +
    deltaCell(r.otr_delta) +
    `<td>${fmt("pp", r.pp)}</td>` +
    `<td>${eloCell}</td>` +
    `<td>${fmt("otr_rating", r.otr_rating)}${est}</td>` +
    `<td class="score">${fmt("hybrid_score", r.hybrid_score)}</td>` +
    `</tr>`
  );
}

function escapeHTML(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function render() {
  const q = $("#search").value.trim().toLowerCase();
  let view = ROWS;
  if (q) view = ROWS.filter((r) => r.username.toLowerCase().includes(q));

  const dir = sortDir;
  const key = sortKey;
  const numeric = NUMERIC.has(key);
  view = view.slice().sort((a, b) => {
    let av = a[key], bv = b[key];
    if (!numeric) { av = av.toLowerCase(); bv = bv.toLowerCase(); }
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return a.hybrid_rank - b.hybrid_rank;     // stable tie-break by hybrid rank
  });

  $("#rows").innerHTML = view.map(rowHTML).join("");
  $("#board").hidden = view.length === 0;
  $("#status").hidden = view.length !== 0 || ROWS.length === 0;
  if (view.length === 0 && ROWS.length > 0) {
    $("#status").textContent = `No player matching “${q}”.`;
    $("#status").hidden = false;
  }
  $("#meta").textContent =
    `${view.length.toLocaleString("en-US")}` +
    (q ? ` of ${ROWS.length.toLocaleString("en-US")}` : "") + " players";

  // header sort indicators
  document.querySelectorAll("thead th").forEach((th) => {
    const isSorted = th.dataset.key === key;
    th.classList.toggle("sorted", isSorted);
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.textContent = isSorted ? (dir === 1 ? "▲" : "▼") : "";
  });
}

// ranks read better ascending (1 = best); pp, ratings, score and vs-Elo gain descending
const ASC_KEYS = new Set(["hybrid_rank", "username"]);

function onHeaderClick(th) {
  const key = th.dataset.key;
  if (key === sortKey) {
    sortDir *= -1;
  } else {
    sortKey = key;
    sortDir = ASC_KEYS.has(key) ? 1 : -1;
  }
  render();
}

// Show when the leaderboard DATA was generated (from the meta sidecar, in UTC).
function showUpdated(meta) {
  const el = document.getElementById("updated-date");
  if (!el) return;
  if (!meta || !meta.generated_utc) { el.textContent = "unknown"; return; }
  const d = new Date(meta.generated_utc);
  const date = d.toLocaleDateString("en-GB",
    { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });
  const time = d.toLocaleTimeString("en-GB",
    { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  el.textContent = `${date}, ${time} UTC`;
}

async function loadMeta() {
  try {
    const res = await fetch(META_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error();
    return await res.json();
  } catch {
    return null;
  }
}

// --- tabs: switch between the leaderboard and the calculator views ---
function initTabs() {
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => tab.addEventListener("click", () => {
    document.body.dataset.view = tab.dataset.view;
    tabs.forEach((t) => t.classList.toggle("is-active", t === tab));
  }));
}

// --- calculator: same normalized blend the board uses, evaluated in-browser.
//   z(x)  = (transform(x) - mean) / std        (transform = log for pp)
//   score = w_pp*z_pp + w_elo*z_elo + w_otr*z_otr   (higher = better)
// The per-axis mean/std come from the meta sidecar (the live board population),
// so the calculator reproduces exactly what the board computed.
function initCalc(meta) {
  const ppEl = document.getElementById("c-pp");
  const eloEl = document.getElementById("c-elo");
  const otrEl = document.getElementById("c-otr");
  const wppEl = document.getElementById("c-wpp");
  const weloEl = document.getElementById("c-welo");
  const wotrEl = document.getElementById("c-wotr");
  const wsumOut = document.getElementById("c-wsum");
  const wResetBtn = document.getElementById("c-w-reset");
  const zppOut = document.getElementById("c-zpp");
  const zeloOut = document.getElementById("c-zelo");
  const zotrOut = document.getElementById("c-zotr");
  const scoreOut = document.getElementById("c-score");
  const formulaOut = document.getElementById("c-formula");
  const errOut = document.getElementById("c-err");
  if (!ppEl || !eloEl || !otrEl) return;

  const norm = meta && meta.norm;
  // The board's own weights — used to seed the inputs and as the "reset" target.
  const boardW = {
    pp: meta && typeof meta.weight_pp === "number" ? meta.weight_pp : DEFAULT_WEIGHTS.pp,
    elo: meta && typeof meta.weight_elo === "number" ? meta.weight_elo : DEFAULT_WEIGHTS.elo,
    otr: meta && typeof meta.weight_otr === "number" ? meta.weight_otr : DEFAULT_WEIGHTS.otr,
  };
  const label = document.getElementById("c-w-label");
  if (label) label.textContent =
    `${boardW.pp} PP / ${boardW.elo} Elo / ${boardW.otr} OTR`;

  // Seed the weight inputs with the live board weights (the static HTML values are
  // only a fallback for the published default).
  const seedWeights = () => {
    if (wppEl) wppEl.value = boardW.pp;
    if (weloEl) weloEl.value = boardW.elo;
    if (wotrEl) wotrEl.value = boardW.otr;
  };
  seedWeights();
  if (wResetBtn) wResetBtn.textContent =
    `Reset to board weights (${boardW.pp} / ${boardW.elo} / ${boardW.otr})`;

  // Without normalization params (e.g. an old meta file) the calculator can't
  // reproduce the board's z-scores, so explain rather than mislead.
  if (!norm || !norm.pp || !norm.elo || !norm.otr) {
    if (errOut) {
      errOut.textContent =
        "Calculator unavailable: the leaderboard data is missing its " +
        "normalization parameters. Regenerate the board to enable it.";
      errOut.hidden = false;
    }
    return;
  }

  const z = (val, axis) => {
    const x = axis.log ? Math.log(Math.max(val, 1)) : val;
    return (x - axis.mean) / axis.std;
  };
  const num = (v, d) => v.toLocaleString("en-US", { maximumFractionDigits: d });

  function update() {
    const pp = parseFloat(ppEl.value);
    const elo = parseFloat(eloEl.value);
    const otr = parseFloat(otrEl.value);
    const wpp = parseFloat(wppEl.value);
    const welo = parseFloat(weloEl.value);
    const wotr = parseFloat(wotrEl.value);

    let problem = "";
    if (!(pp >= 1)) problem = "PP must be 1 or greater.";
    else if (!(elo >= 1)) problem = "Elo rating must be 1 or greater.";
    else if (!(otr >= 1)) problem = "OTR rating must be 1 or greater.";
    else if (!(wpp >= 0) || !(welo >= 0) || !(wotr >= 0))
      problem = "Weights must be 0 or greater.";

    if (problem) {
      zppOut.textContent = zeloOut.textContent = zotrOut.textContent = "—";
      scoreOut.textContent = "—";
      formulaOut.textContent = "";
      if (wsumOut) wsumOut.textContent = "";
      errOut.textContent = problem;
      errOut.hidden = false;
      return;
    }
    errOut.hidden = true;

    const zpp = z(pp, norm.pp), zelo = z(elo, norm.elo), zotr = z(otr, norm.otr);
    const score = wpp * zpp + welo * zelo + wotr * zotr;

    zppOut.textContent = (zpp >= 0 ? "+" : "") + num(zpp, 2);
    zeloOut.textContent = (zelo >= 0 ? "+" : "") + num(zelo, 2);
    zotrOut.textContent = (zotr >= 0 ? "+" : "") + num(zotr, 2);
    scoreOut.textContent = (score >= 0 ? "+" : "") + num(score, 3);
    formulaOut.textContent =
      `score = ${num(wpp, 2)} × z(log ${num(pp, 0)}) + ${num(welo, 2)} × z(${num(elo, 0)}) + ${num(wotr, 2)} × z(${num(otr, 0)})\n` +
      `      = ${num(wpp, 2)} × ${num(zpp, 2)} + ${num(welo, 2)} × ${num(zelo, 2)} + ${num(wotr, 2)} × ${num(zotr, 2)}\n` +
      `      = ${num(score, 3)}`;

    // The board normalizes weights to sum to 1; flag when a custom split doesn't,
    // since it rescales every score (ordering is unaffected, magnitude isn't).
    if (wsumOut) {
      const wsum = wpp + welo + wotr;
      const off = Math.abs(wsum - 1) > 0.001;
      wsumOut.textContent = off
        ? `Weights sum to ${num(wsum, 2)} — the board uses a split that sums to 1, so scores here are on a different scale (ranking order is unchanged).`
        : `Weights sum to ${num(wsum, 2)}.`;
      wsumOut.classList.toggle("warn", off);
    }
  }

  [ppEl, eloEl, otrEl, wppEl, weloEl, wotrEl].forEach(
    (el) => el && el.addEventListener("input", update));
  if (wResetBtn) wResetBtn.addEventListener("click", () => { seedWeights(); update(); });
  update();
}

async function init() {
  initTabs();
  const meta = await loadMeta();
  showUpdated(meta);
  initCalc(meta);
  document.querySelectorAll("thead th").forEach((th) =>
    th.addEventListener("click", () => onHeaderClick(th)));

  let debounce;
  $("#search").addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(render, 120);
  });

  try {
    const res = await fetch(CSV_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    ROWS = parseCSV(await res.text());
    render();
  } catch (err) {
    if (location.protocol === "file:") {
      $("#status").innerHTML =
        "This page was opened directly from disk (<code>file://</code>), so the " +
        "browser blocks loading the leaderboard file. Serve the folder over HTTP " +
        "instead — e.g. run <code>python -m http.server</code> inside " +
        "<code>docs/</code> and open <code>http://localhost:8000</code>. " +
        "(On the live site this works automatically.)";
    } else {
      $("#status").textContent =
        `Could not load ${CSV_URL} (${err.message}). ` +
        `Make sure the CSV sits next to index.html.`;
    }
  }
}

init();
