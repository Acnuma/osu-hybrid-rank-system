"use strict";

// The leaderboard CSV lives next to this file (GitHub Pages serves /docs as the
// site root, so the data must sit inside it). Regenerate with:
//   python hybrid_rank.py --bws --offline --out docs/hybrid_leaderboard.csv
const CSV_URL = "hybrid_leaderboard.csv";

// column key -> {label, numeric}. Order here is irrelevant; the <thead> drives layout.
const NUMERIC = new Set([
  "hybrid_rank", "pp_rank", "badges", "bws_pp_rank", "elo_rank", "hybrid_score",
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

function rowHTML(r) {
  const badgeCls = r.badges > 0 ? "badge has" : "badge";
  const url = `https://osu.ppy.sh/users/${r.user_id}/osu`;
  return (
    `<tr>` +
    `<td class="rank">${r.hybrid_rank.toLocaleString("en-US")}</td>` +
    `<td class="left"><a class="user" href="${url}" target="_blank" rel="noopener">${escapeHTML(r.username)}</a></td>` +
    `<td>${fmt("pp_rank", r.pp_rank)}</td>` +
    `<td class="${badgeCls}">${r.badges}</td>` +
    `<td>${fmt("bws_pp_rank", r.bws_pp_rank)}</td>` +
    `<td>${fmt("elo_rank", r.elo_rank)}</td>` +
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
    // ranks/score read better ascending (1 = best); badges descending (more = notable)
    sortDir = (key === "badges") ? -1 : 1;
  }
  render();
}

async function init() {
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
    $("#status").textContent =
      `Could not load ${CSV_URL} (${err.message}). ` +
      `Make sure the CSV sits next to index.html.`;
  }
}

init();
