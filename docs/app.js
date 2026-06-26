"use strict";

// The leaderboard CSV lives next to this file (GitHub Pages serves /docs as the
// site root, so the data must sit inside it). Regenerate with:
//   python hybrid_rank.py --anchor rankedplay --top 10000 --bws --min-plays 5 --max-score 10000 --out docs/hybrid_leaderboard.csv
const CSV_URL = "hybrid_leaderboard.csv";
// Sidecar written by hybrid_rank.py at generation time. We read the date from
// HERE (not the HTTP Last-Modified header) so the "updated" stamp reflects only
// LEADERBOARD DATA refreshes, never website/code deploys.
const META_URL = "hybrid_leaderboard.meta.json";

// column key -> {label, numeric}. Order here is irrelevant; the <thead> drives layout.
const NUMERIC = new Set([
  "hybrid_rank", "pp_rank", "badges", "bws_pp_rank", "elo_rank", "hybrid_score",
  "pp_delta",   // derived (pp_rank - hybrid_rank), not a CSV column
]);

let ROWS = [];                       // parsed objects
let sortKey = "hybrid_rank";
let sortDir = 1;                     // 1 = ascending, -1 = descending

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
    // how many places the player ranks higher (+) or lower (-) than PP alone
    obj.pp_delta = obj.pp_rank - obj.hybrid_rank;
    // osu! flags ratings with too few recent matches as provisional ("yes" in
    // the CSV); coerce to a boolean. Absent on older CSVs -> false.
    obj.provisional = obj.provisional === "yes";
    out[i - 1] = obj;
  }
  return out;
}

function fmt(key, val) {
  if (key === "bws_pp_rank") return val % 1 === 0 ? val.toFixed(0) : val.toFixed(1);
  if (key === "hybrid_score") return val.toFixed(2);
  if (NUMERIC.has(key)) return val.toLocaleString("en-US");
  return val;
}

function deltaCell(d) {
  if (d > 0) return `<td class="delta up">▴ +${d.toLocaleString("en-US")}</td>`;
  if (d < 0) return `<td class="delta down">▾ ${d.toLocaleString("en-US")}</td>`;
  return `<td class="delta zero">0</td>`;
}

function rowHTML(r) {
  const badgeCls = r.badges > 0 ? "badge has" : "badge";
  const url = `https://osu.ppy.sh/users/${r.user_id}/osu`;
  return (
    `<tr>` +
    `<td class="rank">${r.hybrid_rank.toLocaleString("en-US")}</td>` +
    `<td class="left"><a class="user" href="${url}" target="_blank" rel="noopener">${escapeHTML(r.username)}</a></td>` +
    `<td>${fmt("pp_rank", r.pp_rank)}</td>` +
    deltaCell(r.pp_delta) +
    `<td class="${badgeCls}">${r.badges}</td>` +
    `<td>${fmt("bws_pp_rank", r.bws_pp_rank)}</td>` +
    `<td>${fmt("elo_rank", r.elo_rank)}${r.provisional ? `<abbr class="prov" title="Provisional rating — too few recent ranked-play matches for a stable Elo.">*</abbr>` : ""}</td>` +
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

function onHeaderClick(th) {
  const key = th.dataset.key;
  if (key === sortKey) {
    sortDir *= -1;
  } else {
    sortKey = key;
    // ranks/score read better ascending (1 = best); badges & vs-pp gain descending
    sortDir = (key === "badges" || key === "pp_delta") ? -1 : 1;
  }
  render();
}

// Show when the leaderboard DATA was generated (from the meta sidecar, in UTC).
async function loadUpdated() {
  const el = document.getElementById("updated-date");
  if (!el) return;
  try {
    const res = await fetch(META_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error();
    const meta = await res.json();
    if (!meta.generated_utc) throw new Error();
    const d = new Date(meta.generated_utc);
    const date = d.toLocaleDateString("en-GB",
      { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" });
    const time = d.toLocaleTimeString("en-GB",
      { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
    el.textContent = `${date}, ${time} UTC`;
  } catch {
    el.textContent = "unknown";
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

// --- calculator: same formula the board uses, evaluated live in the browser ---
//   bws_pp = pp_rank ^ ( base ^ badges^2 )    (base = 0.9937 on the board)
//   score  = weight * bws_pp + (1 - weight) * elo_rank   (lower = better)
function initCalc() {
  const pp = document.getElementById("c-pp");
  const elo = document.getElementById("c-elo");
  const badgesEl = document.getElementById("c-badges");
  const baseEl = document.getElementById("c-bwsbase");
  const weightEl = document.getElementById("c-weight");
  const bwsOut = document.getElementById("c-bws");
  const scoreOut = document.getElementById("c-score");
  const formulaOut = document.getElementById("c-formula");
  const errOut = document.getElementById("c-err");
  if (!pp || !elo || !badgesEl || !baseEl || !weightEl) return;

  const num = (v, d) => v.toLocaleString("en-US", { maximumFractionDigits: d });

  function update() {
    const ppRank = parseFloat(pp.value);
    const eloRank = parseFloat(elo.value);
    const badges = parseInt(badgesEl.value, 10);
    const base = parseFloat(baseEl.value);
    const weight = parseFloat(weightEl.value);

    let problem = "";
    if (!(ppRank >= 1)) problem = "PP rank must be 1 or greater.";
    else if (!(eloRank >= 1)) problem = "Elo rank must be 1 or greater.";
    else if (!(badges >= 0)) problem = "Badges must be 0 or greater.";
    else if (!(base > 0 && base <= 1)) problem = "BWS base must be greater than 0 and at most 1.";
    else if (!(weight >= 0 && weight <= 1)) problem = "Weight must be between 0 and 1.";

    if (problem) {
      bwsOut.textContent = "—";
      scoreOut.textContent = "—";
      formulaOut.textContent = "";
      errOut.textContent = problem;
      errOut.hidden = false;
      return;
    }
    errOut.hidden = true;

    const bws = Math.pow(ppRank, Math.pow(base, badges * badges));
    const score = weight * bws + (1 - weight) * eloRank;
    const rest = 1 - weight;

    bwsOut.textContent = num(bws, 1);
    scoreOut.textContent = num(score, 2);
    formulaOut.textContent =
      `score = ${weight} × ( ${num(ppRank, 0)} ^ ( ${base} ^ ( ${badges} ^ 2 ) ) ) + ${rest} × ${num(eloRank, 0)}\n` +
      `      = ${weight} × ${num(bws, 2)} + ${rest} × ${num(eloRank, 0)}\n` +
      `      = ${num(score, 2)}`;
  }

  [pp, elo, badgesEl, baseEl, weightEl].forEach((el) =>
    el.addEventListener("input", update));
  update();
}

async function init() {
  initTabs();
  initCalc();
  loadUpdated();
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
