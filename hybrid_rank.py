#!/usr/bin/env python3
"""
HybridRankSystem
================
Builds an osu! "hybrid" global leaderboard that blends three skill signals on a
common, normalized scale -- so it measures *magnitude*, not just ordinal place:

  * PP performance    -> osu.ppy.sh/rankings/{mode}/global        (raw pp value)
  * Elo rating        -> osu.ppy.sh/rankings/ranked-play/{mode}/{pool}
                         (osu!'s "ranked play" matchmaking rating, mu)
  * OTR rating        -> osu! Tournament Rating (otr.stagec.net public API);
                         players with no tournament history get a rank-seeded
                         estimate using OTR's own initial-rating formula.

Each axis is standardized across the board population (z-score; pp is logged
first because it's heavily right-skewed), then blended (higher is better):

    hybrid_score = w_pp * z_pp + w_elo * z_elo + w_otr * z_otr

The weights are *reliability-weighted* per player: a **seeded** axis carries no
independent signal -- a seeded OTR is a deterministic transform of osu! rank
(corr ~0.995 with pp) and a seeded Elo is the PP-prior itself -- so weighting it
like a real measurement just double-counts pp. Any seeded axis is therefore given
**zero** weight. A *real* OTR is further tapered by how much tournament play backs
it (weight *= matches/(matches+OTR_RELIABILITY_K)), since one or two matches barely
move the rating off its rank-seed; a seeded OTR is just the matches=0 limit of that
taper. Each axis's freed share is redistributed to the player's remaining real axes.
With deep real Elo and OTR the weights are exactly the base W_PP / W_ELO / W_OTR.

Players are sorted *descending* by hybrid_score to produce a "hybrid rank".
Ties break deterministically by elo rating, then pp, then user id.

The default "union" anchor takes (PP top-10k) UNION (ranked-play top-10k) and keeps
anyone with at least one real competitive rating (a real Elo OR a real OTR). Low-play
Elos are noisy, so each Elo is *shrunk* toward the rating its pp predicts, weighted by
match count (n/(n+K)); a player with no Elo at all is just the n=0 limit of that same
formula (their Elo equals the pp-prior seed). See the ELO_SHRINK_K note below.

Pure standard library -- no pip installs required.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# The blend has three weights (summing to 1). Edit W_PP and W_ELO; W_OTR is
# derived. PP = raw mechanical performance, ELO = live matchmaking rating, OTR =
# tournament rating. Defaults lean on the two "competitive" axes a touch more
# than raw pp, but all three are easily tunable (the split is openly debatable).
W_PP = 0.30          # weight on pp performance (0..1)
W_ELO = 0.35         # weight on elo (ranked-play) rating (0..1)
W_OTR = 1.0 - W_PP - W_ELO  # weight on OTR tournament rating -- derived (0.35)
TOP_N = 1000         # how many anchor players to pull
MODE = "osu"         # ruleset: osu | taiko | fruits | mania
PER_PAGE = 50        # osu rankings pages return 50 users each
PP_RANK_CAP = 10000  # osu! caps the PUBLIC pp rankings at top 10k (page 200);
                     # pages past 200 just repeat page 200. Beyond this, a
                     # player's pp rank is only obtainable per-profile / via API.
RP_RANK_CAP = 10000  # union caps the ranked-play (Elo) scan at this rank by default
                     # (the rp axis's own "top 10k"). Players past it get a seeded
                     # Elo rather than a scanned one -- prioritizes speed (a full
                     # scan is ~2,000 pages). Override depth with --rp-max-pages.

# Low-play Elos are noisy, so (in the union anchor) every Elo is SHRUNK toward the
# rating its pp predicts, weighted by sample size: elo = prior + (raw - prior)*n/(n+K),
# where prior is a pp->elo fit on stable (>=10-match) players and n is the match
# count. K is the shrinkage half-weight point. K=5 is derived empirically: using OTR
# as an independent skill yardstick, a player's Elo only overtakes a pure pp guess as
# a skill predictor at ~5 matches (below 5 they tie, r~0.56 each, Williams p=0.84;
# at >=5 Elo pulls ahead to r~0.70, Fisher z=8.3, p<1e-15). A player with NO real
# Elo is just the n=0 limit (elo = prior). See README "Low-play Elo shrinkage".
ELO_SHRINK_K = 5.0
ELO_STABLE_PLAYS = 10  # only ratings with >= this many matches fit the pp->elo prior
ELO_SHRINK_MARK = 15.0  # flag a real Elo as "shrunk" once the adjustment is >= this many points

# osu! Tournament Rating (OTR) -- public leaderboard API + initial-rating seed.
# Real ratings come from ONE paginated sweep of the OTR leaderboard (the full
# population with a verified tournament rating), joined to our players by osu id.
# This is a fixed ~267-page cost regardless of board size and carries tournament/
# match counts for free. The seed is a faithful port of otr-processor's
# `mu_from_rank` (OpenSkill PlackettLuce model) for the osu! ruleset: it maps a
# player's osu! global rank to the rating OTR would assign before any tournament
# play, used for everyone absent from the leaderboard. Constants from
# osu-tournament-rating/otr-processor src/model.
OTR_LEADERBOARD = "https://otr.stagec.net/api/leaderboard"
OTR_RULESET = 0       # 0 = osu! standard (matches MODE)
OTR_RANK_CAP = 10000  # union recruits OTR players up to this globalRank -- the tournament
                      # axis's own "top 10k", symmetric with PP_RANK_CAP and the rp top-10k
OTR_PAGE_SIZE = 100   # the leaderboard API caps pageSize at 100
OTR_SEED_MEAN = 9.99      # mean of ln(earliest_global_rank), osu!
OTR_SEED_STD = 1.77       # std  of ln(earliest_global_rank), osu!
OTR_SEED_CENTER = 1200.0  # rating at the population center
OTR_SEED_LEFT = 250.0     # slope for ranks better than center (z < 0)
OTR_SEED_RIGHT = 200.0    # slope for ranks worse than center (z > 0)
OTR_SEED_FLOOR = 500.0    # INITIAL_RATING_FLOOR
OTR_SEED_CEIL = 2000.0    # INITIAL_RATING_CEILING
# Reliability taper for a REAL OTR: weight *= matches / (matches + K). One or two
# tournament matches barely move the rating off its rank-seed (which is ~pp), so a
# thin real OTR re-counts pp and amplifies match-luck if trusted in full. K is the
# match count at which OTR earns half its base weight; a seeded OTR is the
# matches=0 limit (weight 0), so the taper is continuous across the seed boundary.
# At K=5: 1 match -> 17% weight, 3 -> 38%, 5 -> 50%, 22 -> 81%, 58 -> 92%.
OTR_RELIABILITY_K = 5.0
# The OTR API shares ONE token-bucket rate limit (~60 requests / 60s, and it
# sends no Retry-After header) across its endpoints, so a fast leaderboard sweep
# trips HTTP 429. Measured: an exhausted bucket recovers in ~62s. We pace the
# sweep below the refill rate and, on a 429, wait out a full recovery instead of
# the short generic backoff (which gives up well before the bucket refills).
OTR_MIN_INTERVAL = 1.3     # seconds between OTR leaderboard GETs (~0.77 req/s)
OTR_RETRY_429_WAIT = 65.0  # backoff on a 429 w/o Retry-After (recovery ~62s)
OTR_MAX_RETRIES = 6        # extra retries for the rate-limited OTR endpoint

CONCURRENCY = 5      # parallel page fetches (overlaps latency; starts are still paced below)
MIN_INTERVAL = 1.0   # global throttle: min seconds between request STARTS across the whole app
                     # (HTML scraping, the OTR sweep, and the osu! API all share it). Both the
                     # osu! and OTR terms of use ask for <= 60 requests/min (~1 req/s); the
                     # _throttle() lock serializes starts to this interval, so even with
                     # CONCURRENCY workers the start rate never exceeds 1/s.
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
CACHE_TTL = 7 * 24 * 3600  # seconds (1 week); this board is experimental and rarely
                           # refreshed, and the underlying data shifts slowly, so a
                           # week avoids needless re-scrapes. --no-cache forces fresh.
ALLOW_STALE = False    # --offline sets this: reuse ANY cached file regardless of age
                       # (pure recompute, e.g. weight tweaks, needs no fresh data)

BASE = "https://osu.ppy.sh"
# osu! API v2 (client-credentials guest token) -- optional fast path for pp lookups
# of players outside the public pp top-10k. ONE GET /users call returns up to 50
# users with statistics_rulesets (global_rank + pp per ruleset), vs. one huge
# profile-HTML scrape per player. Credentials come from the environment only.
OSU_TOKEN_URL = "https://osu.ppy.sh/oauth/token"
OSU_API_BASE = "https://osu.ppy.sh/api/v2"
OSU_USERS_CHUNK = 50   # GET /users accepts up to 50 ids[] per request
# The osu! API global throttle is 1200 cost-units/minute (config/osu.php
# 'global' => '1200,1,api'), and GET /users charges ONE unit PER id -- so a
# 50-id call costs 50 units, not 1. At 1 req/s that's ~50 units/s and trips a
# 429 within seconds. Pace /users to 50/2.7s ~= 1110 units/min (under 1200), and
# on a 429 wait out a full window instead of the short generic backoff.
OSU_USERS_MIN_INTERVAL = 2.7   # seconds between /users calls (cost-budget paced)
OSU_USERS_RETRY_429_WAIT = 65.0
OSU_API_MAX_RETRIES = 6
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
PROFILE_CACHE_DIR = os.path.join(CACHE_DIR, "profile")  # per-uid {rank, pp} JSON
PP_API_CACHE = os.path.join(CACHE_DIR, "pp_api")  # osu!-API pp board, one JSON per mode
OTR_CACHE_DIR = os.path.join(CACHE_DIR, "otr")  # per-uid OTR rating JSON ({} = none)
OUT_CSV = os.path.join(HERE, "hybrid_leaderboard.csv")


# --------------------------------------------------------------------------- #
# HTTP with retry/backoff, optional global rate limit, and disk cache
# --------------------------------------------------------------------------- #
_rate_lock = threading.Lock()
_last_start = [0.0]


def _throttle() -> None:
    if MIN_INTERVAL <= 0:
        return
    with _rate_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_start[0])
        if wait > 0:
            time.sleep(wait)
        _last_start[0] = time.monotonic()


def _http_get(url: str) -> str:
    if ALLOW_STALE:  # --offline: never touch the network
        raise RuntimeError(f"--offline set but not in cache: {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
    )
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504):  # transient -> back off
                time.sleep((2 ** attempt) + random.uniform(0, 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"GET failed after {MAX_RETRIES} tries: {url} ({last_err})")


def _http_get_json(url: str, headers: dict, max_retries: int = MAX_RETRIES,
                   wait_429: float | None = None) -> object:
    """GET a URL with extra headers (e.g. Authorization) and return parsed JSON.
    Same retry/backoff and global throttle as _http_get. Honors Retry-After on
    429; when the server sends none, a 429 falls back to `wait_429` seconds (for
    endpoints whose bucket needs a long, fixed recovery). The caller's headers
    (e.g. the API key) are never logged."""
    if ALLOW_STALE:  # --offline: never touch the network
        raise RuntimeError(f"--offline set but not in cache: {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                      **headers})
    last_err: Exception | None = None
    for attempt in range(max_retries):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (429, 500, 502, 503, 504):  # not transient -> give up
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
            elif e.code == 429 and wait_429 is not None:  # no header -> wait it out
                delay = wait_429 + random.uniform(0, 2)
            else:
                delay = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"GET failed after {max_retries} tries: {url} ({last_err})")


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".html")


def _cache_fresh(path: str) -> bool:
    """True if the cache file may be used: it exists, and either we're allowing
    stale reads (--offline) or it's within the TTL."""
    return os.path.exists(path) and (
        ALLOW_STALE or time.time() - os.path.getmtime(path) < CACHE_TTL)


def http_get_cached(url: str, key: str, use_cache: bool) -> str:
    path = _cache_path(key)
    if use_cache and _cache_fresh(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    body = _http_get(url)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return body


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _username_from_anchor(inner_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", inner_html)).strip()


# The PP board renders each row as a table: name link, then dimmed columns
# (Accuracy, Play Count, Ranked Score), then the *non-dimmed* Performance (pp)
# column, then dimmed SS/S/A counts. Targeting `col">` (no `--dimmed` suffix)
# right after the link uniquely catches the pp cell.
_PP_ROW_RE = re.compile(
    r'ranking-page-table-main__link js-usercard'
    r'[^>]*?data-user-id="(\d+)"'                 # user id
    r'.*?>(.*?)</a>'                              # link inner html -> username
    r'.*?ranking-page-table__column">\s*([\d,]+)',  # first non-dimmed col = pp
    re.S,
)


def parse_pp_rows(body: str) -> list[tuple[int, str, float]]:
    """Return [(user_id, username, pp), ...] in leaderboard (page) order."""
    out: list[tuple[int, str, float]] = []
    seen: set[int] = set()
    for uid_s, inner, pp_s in _PP_ROW_RE.findall(body):
        uid = int(uid_s)
        if uid in seen:
            continue
        name = _username_from_anchor(inner)
        if name:
            seen.add(uid)
            out.append((uid, name, float(pp_s.replace(",", ""))))
    return out


# The ranked-play board renders each row as: name link, then Wins / Plays /
# Rating columns (Rating is `--number-focus`, a "N" or "N*" number, with a
# "Provisional rating ..." title when osu! considers it not yet stable). The PP
# board has a different column layout, so this parser is ranked-play only.
_RP_ROW_RE = re.compile(
    r'ranking-page-table-main__link js-usercard'
    r'[^>]*?data-user-id="(\d+)"'              # user id
    r'.*?>(.*?)</a>'                           # link inner html -> username
    r'.*?col--number">\s*([\d,]+)'             # Wins
    r'.*?col--number">\s*([\d,]+)'             # Plays
    r'.*?col--number-focus">(.*?)</div>',      # Rating cell (value + provisional)
    re.S,
)
_RP_RATING_NUM_RE = re.compile(r'([\d,]+)')


@dataclass
class RpRow:
    user_id: int
    username: str
    plays: int
    provisional: bool
    rating: float      # ranked-play elo rating (mu)


def parse_rp_rows(body: str) -> list[RpRow]:
    """Return per-row ranked-play data in page order: id, name, play count, the
    elo rating (mu), and whether the rating is provisional (osu!'s own "too few
    recent matches" flag, shown as a "*" on the board)."""
    out: list[RpRow] = []
    seen: set[int] = set()
    for uid_s, inner, _wins, plays_s, focus in _RP_ROW_RE.findall(body):
        uid = int(uid_s)
        if uid in seen:
            continue
        name = _username_from_anchor(inner)
        if not name:
            continue
        m = _RP_RATING_NUM_RE.search(re.sub(r"<[^>]+>", "", focus))
        if not m:                                 # no parseable rating -> skip
            continue
        seen.add(uid)
        plays = int(plays_s.replace(",", ""))
        provisional = "*" in focus or "Provisional" in focus
        rating = float(m.group(1).replace(",", ""))
        out.append(RpRow(uid, name, plays, provisional, rating))
    return out


# --------------------------------------------------------------------------- #
# Boards
# --------------------------------------------------------------------------- #
def detect_pool(use_cache: bool) -> tuple[int, int]:
    """Detect the current ranked-play pool id and its last page number."""
    body = http_get_cached(
        f"{BASE}/rankings/ranked-play/{MODE}", f"rp_{MODE}_index", use_cache
    )
    pools = [int(x) for x in re.findall(rf"ranked-play/{MODE}/(\d+)", body)]
    pages = [int(x) for x in re.findall(r"[?&]page=(\d+)", body)]
    if not pools:
        raise RuntimeError("could not detect ranked-play pool id")
    return max(pools), (max(pages) if pages else 1)


def fetch_pages(url_for, key_for, pages, use_cache, label):
    """Fetch a range of pages concurrently; yield (page, body) as they finish."""
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(http_get_cached, url_for(p), key_for(p), use_cache): p
                for p in pages}
        done = 0
        for fut in as_completed(futs):
            p = futs[fut]
            done += 1
            try:
                yield p, fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! {label} page {p} failed: {e}", file=sys.stderr)
            if done % 25 == 0 or done == len(futs):
                print(f"      {label}: {done}/{len(futs)} pages", file=sys.stderr)


def get_pp_board_api(top_n: int, osu_creds: tuple[str, str], use_cache: bool
                     ) -> list[tuple[int, int, str, float]]:
    """PP board via the osu! API v2 rankings endpoint (GET /rankings/{mode}/
    performance) instead of scraping HTML -- structured JSON, no brittle parsing.
    Same top-10k cap and 50/page as the web board. Cached as one JSON per mode."""
    cpath = os.path.join(PP_API_CACHE, f"{MODE}.json")
    if use_cache and _cache_fresh(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as fh:
                return [tuple(r) for r in json.load(fh)]
        except (ValueError, OSError):
            pass  # corrupt cache -> refetch
    token = osu_token(*osu_creds)
    headers = {"Authorization": f"Bearer {token}"}
    n_pages = (top_n + PER_PAGE - 1) // PER_PAGE
    out: list[tuple[int, int, str, float]] = []
    seen: set[int] = set()
    for p in range(1, n_pages + 1):
        data = _http_get_json(
            f"{OSU_API_BASE}/rankings/{MODE}/performance?page={p}", headers)
        ranking = (data or {}).get("ranking") or []
        for i, entry in enumerate(ranking):
            u = entry.get("user") or {}
            uid, name, pp = u.get("id"), u.get("username"), entry.get("pp")
            rank = (p - 1) * PER_PAGE + i + 1
            if uid and pp is not None and rank <= top_n and int(uid) not in seen:
                seen.add(int(uid))
                out.append((rank, int(uid), name or str(uid), float(pp)))
        if not ranking:        # ran off the end of the board
            break
        if p % 25 == 0 or p == n_pages:
            print(f"      pp (API): {p}/{n_pages} pages, {len(out)} players",
                  file=sys.stderr)
    os.makedirs(PP_API_CACHE, exist_ok=True)
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


def get_pp_board(top_n: int, use_cache: bool,
                 osu_creds: tuple[str, str] | None = None
                 ) -> list[tuple[int, int, str, float]]:
    """Return [(pp_rank, user_id, username, pp), ...] for the top `top_n` by PP.

    The osu! public pp leaderboard is capped at PP_RANK_CAP (top 10k); requests
    are clamped to it and de-duplicated so the repeated last page can never
    inflate the result. With osu! API credentials this uses the rankings API
    (clean JSON); otherwise it scrapes the HTML board."""
    if top_n > PP_RANK_CAP:
        print(f"  ! pp rankings are capped at top {PP_RANK_CAP:,}; clamping "
              f"(requested {top_n:,}). Beyond this, pp rank needs the API.",
              file=sys.stderr)
        top_n = PP_RANK_CAP
    if osu_creds:
        return get_pp_board_api(top_n, osu_creds, use_cache)
    n_pages = (top_n + PER_PAGE - 1) // PER_PAGE
    by_page: dict[int, list[tuple[int, str, float]]] = {}
    for p, body in fetch_pages(
        lambda p: f"{BASE}/rankings/{MODE}/global?page={p}",
        lambda p: f"pp_{MODE}_p{p}", range(1, n_pages + 1), use_cache, "pp",
    ):
        by_page[p] = parse_pp_rows(body)

    out: list[tuple[int, int, str, float]] = []
    seen: set[int] = set()
    for p in sorted(by_page):
        for i, (uid, name, pp) in enumerate(by_page[p]):
            rank = (p - 1) * PER_PAGE + i + 1
            if rank <= top_n and uid not in seen:   # dedupe guards drift/repeats
                seen.add(uid)
                out.append((rank, uid, name, pp))
    return out


def get_rp_map(pool: int, max_page: int, targets: set[int],
               use_cache: bool, max_pages: int | None
               ) -> dict[int, tuple[int, int, bool, float]]:
    """Build {user_id: (ranked_play_rank, plays, provisional, rating)}. Scans
    pages until every target id is found (early stop) or the board ends.
    `max_pages` hard-caps the scan."""
    last = min(max_page, max_pages) if max_pages else max_page
    rp: dict[int, tuple[int, int, bool, float]] = {}
    pages = list(range(1, last + 1))
    print(f"      scanning up to {last} ranked-play pages (pool {pool})",
          file=sys.stderr)
    # Fetch in page order in chunks so early-stop can kick in.
    chunk = max(CONCURRENCY * 4, 40)
    for start in range(0, len(pages), chunk):
        block = pages[start:start + chunk]
        for p, body in fetch_pages(
            lambda p: f"{BASE}/rankings/ranked-play/{MODE}/{pool}?page={p}",
            lambda p: f"rp_{MODE}_{pool}_p{p}", block, use_cache, "rp",
        ):
            for i, row in enumerate(parse_rp_rows(body)):
                rp.setdefault(row.user_id,
                              ((p - 1) * PER_PAGE + i + 1, row.plays,
                               row.provisional, row.rating))
        if targets and targets.issubset(rp.keys()):
            print("      all target players found; stopping early",
                  file=sys.stderr)
            break
    return rp


def get_rp_board(pool: int, top_n: int, use_cache: bool
                 ) -> list[tuple[int, int, str, int, bool, float]]:
    """Return [(ranked_play_rank, user_id, username, plays, provisional, rating),
    ...] for the top `top_n` ranked-play players. The anchor list in ranked-play
    mode."""
    n_pages = (top_n + PER_PAGE - 1) // PER_PAGE
    by_page: dict[int, list[RpRow]] = {}
    for p, body in fetch_pages(
        lambda p: f"{BASE}/rankings/ranked-play/{MODE}/{pool}?page={p}",
        lambda p: f"rp_{MODE}_{pool}_p{p}", range(1, n_pages + 1), use_cache, "rp",
    ):
        by_page[p] = parse_rp_rows(body)

    out: list[tuple[int, int, str, int, bool, float]] = []
    seen: set[int] = set()
    for p in sorted(by_page):
        for i, row in enumerate(by_page[p]):
            rank = (p - 1) * PER_PAGE + i + 1
            if rank <= top_n and row.user_id not in seen:
                seen.add(row.user_id)
                out.append((rank, row.user_id, row.username, row.plays,
                            row.provisional, row.rating))
    return out


# --------------------------------------------------------------------------- #
# Per-profile pp rank + pp value
# (for ranked-play-anchored players outside the PP top-10k bulk board)
# --------------------------------------------------------------------------- #
_INITIAL_DATA_RE = re.compile(r'data-initial-data="([^"]*)"')


def _rank_and_pp_from_profile(body: str) -> tuple[int | None, float | None]:
    """Pull (global_rank, pp) from a profile page's embedded JSON."""
    m = _INITIAL_DATA_RE.search(body)
    if not m:
        return None, None
    try:
        data = json.loads(html.unescape(m.group(1)))
    except ValueError:
        return None, None
    stats = (data.get("user") or {}).get("statistics") or {}
    rank = stats.get("global_rank")
    pp = stats.get("pp")
    return (int(rank) if rank else None,
            float(pp) if pp is not None else None)


def profile_pp(uid: int, use_cache: bool) -> tuple[int | None, float | None]:
    """(pp_global_rank, pp_value) for one player, cached as a small JSON value
    (the profile HTML is huge). An empty rank marks 'no pp rank'."""
    cpath = os.path.join(PROFILE_CACHE_DIR, f"{MODE}_{uid}.json")
    if use_cache and _cache_fresh(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return d.get("rank"), d.get("pp")
        except (ValueError, OSError):
            pass  # corrupt cache -> refetch
    rank, pp = _rank_and_pp_from_profile(_http_get(f"{BASE}/users/{uid}/{MODE}"))
    os.makedirs(PROFILE_CACHE_DIR, exist_ok=True)
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump({"rank": rank, "pp": pp}, fh)
    return rank, pp


def fetch_profile_pp(uids: list[int], use_cache: bool
                     ) -> dict[int, tuple[int | None, float | None]]:
    """Fetch (pp_rank, pp) for many players concurrently (rate-limited)."""
    result: dict[int, tuple[int | None, float | None]] = {}
    if not uids:
        return result
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(profile_pp, uid, use_cache): uid for uid in uids}
        done = 0
        for fut in as_completed(futs):
            uid = futs[fut]
            done += 1
            try:
                result[uid] = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! profile {uid} failed: {e}", file=sys.stderr)
                result[uid] = (None, None)
            if done % 100 == 0 or done == len(futs):
                print(f"      profiles: {done}/{len(futs)}", file=sys.stderr)
    return result


# --------------------------------------------------------------------------- #
# osu! API v2 (optional): fast batched pp lookups via a client-credentials token
# --------------------------------------------------------------------------- #
def osu_token(client_id: str, client_secret: str) -> str:
    """Get a client-credentials ('guest') access token for the osu! API v2, scope
    `public`. The secret is read from the environment by the caller and sent ONCE
    here; neither it nor the returned token is ever written to disk or logged."""
    if ALLOW_STALE:  # --offline: never touch the network
        raise RuntimeError("--offline set but osu! API token needs the network")
    body = json.dumps({"client_id": int(client_id), "client_secret": client_secret,
                       "grant_type": "client_credentials", "scope": "public"}).encode()
    req = urllib.request.Request(
        OSU_TOKEN_URL, data=body, method="POST",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                 "Content-Type": "application/json"})
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                tok = json.loads(r.read().decode("utf-8", "replace")).get("access_token")
            if not tok:
                raise RuntimeError("osu! token response carried no access_token")
            return tok
        except urllib.error.HTTPError as e:  # 401 here = bad client id/secret
            last_err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"osu! token request failed: HTTP {e.code} "
                                   "(check OSU_CLIENT_ID / OSU_CLIENT_SECRET)")
            time.sleep((2 ** attempt) + random.uniform(0, 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep((2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"osu! token request failed after {MAX_RETRIES} tries ({last_err})")


def fetch_pp_api(uids: list[int], osu_creds: tuple[str, str], use_cache: bool
                 ) -> dict[int, tuple[int | None, float | None]]:
    """(pp_rank, pp) for many players via the osu! API v2 batch /users endpoint
    (up to 50 ids/request). Shares the same per-uid cache as the HTML path, so the
    two sources are interchangeable. A token is fetched only if there are misses."""
    result: dict[int, tuple[int | None, float | None]] = {}
    misses: list[int] = []
    for uid in uids:
        cpath = os.path.join(PROFILE_CACHE_DIR, f"{MODE}_{uid}.json")
        if use_cache and _cache_fresh(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                result[uid] = (d.get("rank"), d.get("pp"))
                continue
            except (ValueError, OSError):
                pass  # corrupt cache -> refetch
        misses.append(uid)
    if not misses:
        return result
    token = osu_token(*osu_creds)
    headers = {"Authorization": f"Bearer {token}"}
    os.makedirs(PROFILE_CACHE_DIR, exist_ok=True)
    done = 0
    last_get = 0.0
    for i in range(0, len(misses), OSU_USERS_CHUNK):
        chunk = misses[i:i + OSU_USERS_CHUNK]
        qs = "&".join(f"ids[]={u}" for u in chunk)
        wait = OSU_USERS_MIN_INTERVAL - (time.monotonic() - last_get)  # cost-budget pace
        if wait > 0:
            time.sleep(wait)
        last_get = time.monotonic()
        data = _http_get_json(f"{OSU_API_BASE}/users?{qs}", headers,
                              max_retries=OSU_API_MAX_RETRIES,
                              wait_429=OSU_USERS_RETRY_429_WAIT)
        got: dict[int, tuple[int | None, float | None]] = {}
        for u in (data or {}).get("users") or []:
            stats = ((u.get("statistics_rulesets") or {}).get(MODE)) or {}
            rank, pp = stats.get("global_rank"), stats.get("pp")
            got[int(u["id"])] = (int(rank) if rank else None,
                                 float(pp) if pp is not None else None)
        for u in chunk:  # cache every requested id, incl. misses (banned/restricted)
            rp = got.get(u, (None, None))
            result[u] = rp
            with open(os.path.join(PROFILE_CACHE_DIR, f"{MODE}_{u}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"rank": rp[0], "pp": rp[1]}, fh)
        done += len(chunk)
        if done % 500 == 0 or done >= len(misses):
            print(f"      osu! API pp: {done}/{len(misses)}", file=sys.stderr)
    return result


def fetch_pp(uids: list[int], use_cache: bool,
             osu_creds: tuple[str, str] | None = None
             ) -> dict[int, tuple[int | None, float | None]]:
    """Resolve (pp_rank, pp) for players outside the bulk pp board. Prefers the
    osu! API v2 batch endpoint when credentials are supplied (fast); otherwise
    falls back to per-profile HTML scraping. Both share one cache."""
    if not uids:
        return {}
    if osu_creds:
        return fetch_pp_api(uids, osu_creds, use_cache)
    return fetch_profile_pp(uids, use_cache)


# --------------------------------------------------------------------------- #
# OTR (osu! Tournament Rating): real ratings via API + rank-seeded fallback
# --------------------------------------------------------------------------- #
def otr_seed_from_rank(rank: int) -> float:
    """Estimate a player's OTR rating from their osu! global rank, using OTR's
    own initial-rating formula (otr-processor `mu_from_rank`, osu! ruleset).
    This is the rating OTR would assign before any tournament play."""
    z = (math.log(rank) - OTR_SEED_MEAN) / OTR_SEED_STD
    slope = OTR_SEED_LEFT if z > 0 else OTR_SEED_RIGHT
    mu = OTR_SEED_CENTER - slope * z
    return min(max(mu, OTR_SEED_FLOOR), OTR_SEED_CEIL)


def fetch_otr_leaderboard(api_key: str, use_cache: bool) -> dict[int, dict]:
    """Return {osu_id: entry} for every player on the OTR leaderboard -- the full
    population that holds a real (verified-tournament) rating. One paginated GET
    sweep at OTR_PAGE_SIZE, cached as a single JSON map under OTR_CACHE_DIR at
    CACHE_TTL. The API key is sent as a Bearer header and never written to disk.
    Players absent from this map have no OTR rating and are rank-seeded instead."""
    cpath = os.path.join(OTR_CACHE_DIR, f"leaderboard_{OTR_RULESET}.json")
    if use_cache and _cache_fresh(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as fh:
                return {int(k): v for k, v in json.load(fh).items()}
        except (ValueError, OSError):
            pass  # corrupt cache -> refetch
    headers = {"Authorization": f"Bearer {api_key}"}
    out: dict[int, dict] = {}
    page, pages = 1, 1
    last_get = 0.0
    while page <= pages:
        wait = OTR_MIN_INTERVAL - (time.monotonic() - last_get)  # stay under limit
        if wait > 0:
            time.sleep(wait)
        last_get = time.monotonic()
        data = _http_get_json(
            f"{OTR_LEADERBOARD}?page={page}&pageSize={OTR_PAGE_SIZE}"
            f"&ruleset={OTR_RULESET}", headers,
            max_retries=OTR_MAX_RETRIES, wait_429=OTR_RETRY_429_WAIT)
        pages = int((data or {}).get("pages") or 1)
        for e in (data or {}).get("leaderboard") or []:
            osu_id = (e.get("player") or {}).get("osuId")
            if osu_id is not None:
                out[int(osu_id)] = e
        if page % 25 == 0 or page >= pages:
            print(f"      OTR leaderboard: {page}/{pages} pages, "
                  f"{len(out)} players", file=sys.stderr)
        page += 1
    os.makedirs(OTR_CACHE_DIR, exist_ok=True)
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    hybrid_rank: int
    user_id: int
    username: str
    pp_rank: int
    pp: float                # raw pp performance value
    rp_rank: int | None      # ranked-play (elo) rank; None when the Elo is seeded
    elo_rating: float        # elo used in scoring (shrunk toward the PP-prior; see below)
    otr_rating: float        # OTR tournament rating (real or rank-seeded)
    otr_estimated: bool      # True == rank-seeded estimate, not a real OTR entry
    tournaments_played: int  # OTR verified tournaments (0 when estimated)
    plays: int = 0           # ranked-play match count
    provisional: bool = False  # osu!'s "rating not yet stable" flag
    elo_raw: float | None = None  # pre-shrink osu! elo (mu); None when there is no real elo
    elo_estimated: bool = False   # True == no real elo: elo_rating IS the PP-prior seed
    elo_shrunk: bool = False      # True == real elo materially pulled toward the prior
    otr_rank: int | None = None   # OTR global rank (real OTR entries only)
    matches_played: int = 0  # OTR verified matches (0 when seeded); drives the OTR reliability taper
    z_pp: float = 0.0        # standardized log(pp)
    z_elo: float = 0.0       # standardized elo rating
    z_otr: float = 0.0       # standardized OTR rating
    # Effective per-player blend weights. Equal to the base W_PP/W_ELO/W_OTR for a
    # player with deep real axes, but a *seeded* axis (no real elo / no real otr --
    # its value is just a PP-derived prior, not independent evidence) is given zero
    # weight, and a real OTR is tapered by its match count; freed weight is
    # redistributed to the player's other real axes. See _effective_weights.
    w_pp: float = W_PP
    w_elo: float = W_ELO
    w_otr: float = W_OTR
    hybrid_score: float = 0.0  # weighted blend of the z-scores (higher = better)


@dataclass
class Filters:
    """Data-quality / presentation knobs applied when assembling the board."""
    min_plays: int = 1            # drop players with fewer ranked-play matches
    exclude_provisional: bool = False  # drop players osu! flags as provisional
    top_k: int | None = None      # keep only the best-K rows after scoring
    min_otr_matches: int = 0      # >0: keep only players with a REAL OTR of >= N matches

    def keep_player(self, plays: int, provisional: bool) -> bool:
        if plays < self.min_plays:
            return False
        if self.exclude_provisional and provisional:
            return False
        return True


def _standardize(values: list[float]) -> tuple[float, float]:
    """(mean, std) for z-scoring. A zero/degenerate std becomes 1 so a constant
    axis contributes 0 rather than blowing up."""
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    return mean, (sd if sd > 1e-9 else 1.0)


def _effective_weights(r: Row) -> tuple[float, float, float]:
    """Reliability-weighted blend weights for one player, summing to 1.

    A *seeded* axis is not independent evidence -- a seeded OTR is a deterministic
    transform of osu! rank (corr ~0.995 with pp), and a seeded Elo is the PP-prior
    itself -- so weighting it like a real measurement double-counts pp. We therefore
    give any seeded axis **zero** weight.

    A *real* OTR is additionally tapered by how much tournament play backs it:
    weight *= matches / (matches + OTR_RELIABILITY_K). One or two matches barely move
    the rating off its rank-seed (~pp), so trusting such a thin rating in full re-counts
    pp and amplifies match-luck; the taper trusts OTR only as a real résumé accumulates.
    A seeded OTR is just the matches=0 limit (taper 0), so the rule is continuous across
    the seed boundary. Elo needs no weight taper of its own -- its thin-sample noise is
    already handled upstream by shrinking the *value* toward the PP-prior (n/(n+K)).

    The freed share of every zeroed/tapered axis is redistributed proportionally (the
    sum-to-1 renormalization below). PP is always real and fully weighted, so the total
    is always >= W_PP > 0 and the weights never blow up. A player with deep real Elo and
    OTR keeps the base weights.
    """
    w_pp = W_PP
    w_elo = 0.0 if r.elo_estimated else W_ELO
    if r.otr_estimated:
        w_otr = 0.0
    else:
        w_otr = W_OTR * r.matches_played / (r.matches_played + OTR_RELIABILITY_K)
    total = w_pp + w_elo + w_otr
    return w_pp / total, w_elo / total, w_otr / total


def _apply_otr_floor(rows: list[Row], filt: Filters) -> list[Row]:
    """Opt-in tournament floor (--min-otr-matches): keep only players whose OTR is
    real and backed by >= filt.min_otr_matches matches. Applied BEFORE normalization
    so the surviving cohort is scored against itself (symmetric with --min-plays).
    A no-op at the default 0, so the standard board is unaffected."""
    n = filt.min_otr_matches
    if n <= 0:
        return rows
    before = len(rows)
    rows = [r for r in rows if not r.otr_estimated and r.matches_played >= n]
    print(f"      --min-otr-matches {n}: kept {len(rows)} of {before} "
          f"(dropped {before - len(rows)} seeded or < {n} OTR matches)",
          file=sys.stderr)
    return rows


def normalize_and_score(rows: list[Row]) -> dict:
    """Standardize each axis across the population and write z-scores + the
    weighted hybrid score onto every row. Returns the normalization parameters
    (per-axis mean/std) so the site can reproduce the score for any input."""
    if not rows:
        return {}
    log_pp = [math.log(max(r.pp, 1.0)) for r in rows]
    m_pp, s_pp = _standardize(log_pp)
    m_elo, s_elo = _standardize([r.elo_rating for r in rows])
    m_otr, s_otr = _standardize([r.otr_rating for r in rows])
    for r, lp in zip(rows, log_pp):
        r.z_pp = (lp - m_pp) / s_pp
        r.z_elo = (r.elo_rating - m_elo) / s_elo
        r.z_otr = (r.otr_rating - m_otr) / s_otr
        # Reliability weighting (see _effective_weights): seeded axes get zero weight
        # and a thin real OTR is tapered by its match count; the freed share is
        # redistributed to the player's other real axes.
        r.w_pp, r.w_elo, r.w_otr = _effective_weights(r)
        r.hybrid_score = r.w_pp * r.z_pp + r.w_elo * r.z_elo + r.w_otr * r.z_otr
    return {
        "pp": {"mean": round(m_pp, 6), "std": round(s_pp, 6), "log": True},
        "elo": {"mean": round(m_elo, 6), "std": round(s_elo, 6), "log": False},
        "otr": {"mean": round(m_otr, 6), "std": round(s_otr, 6), "log": False},
    }


def _finalize(rows: list[Row], filt: Filters) -> tuple[list[Row], int]:
    """Sort by hybrid score (descending -- higher is better now), apply the
    top-K presentation trim, and assign hybrid ranks. Returns (rows, n_trimmed).
    Ties break by elo rating, then pp, then user id -- all deterministic."""
    rows.sort(key=lambda r: (-r.hybrid_score, -r.elo_rating, -r.pp, r.user_id))
    trimmed = 0
    if filt.top_k is not None and len(rows) > filt.top_k:
        trimmed = len(rows) - filt.top_k
        rows = rows[:filt.top_k]
    for i, r in enumerate(rows, 1):
        r.hybrid_rank = i
    return rows, trimmed


# A candidate is everything gathered before OTR/normalization:
#   (rp_rank, uid, name, plays, provisional, elo_rating, pp_rank, pp)
Candidate = tuple[int, int, str, int, bool, float, int, float]


def _assemble(cand: list[Candidate], otr_key: str | None, use_cache: bool,
              filt: Filters) -> tuple[list[Row], dict, int]:
    """Attach OTR ratings (real where available, rank-seeded otherwise), build
    rows, normalize the three axes, blend, and finalize. Shared by both anchor
    modes. Returns (rows, norm_params, n_trimmed)."""
    if otr_key:
        otr = fetch_otr_leaderboard(otr_key, use_cache)
        have = sum(1 for c in cand if c[1] in otr)
        print(f"      {have} of {len(cand)} are on the OTR leaderboard (real "
              f"rating); the rest are seeded from osu! rank", file=sys.stderr)
    else:
        otr = {}
        print("      no OTR key -> every OTR rating is seeded from osu! rank "
              "(pass --otr <key> for real tournament ratings)", file=sys.stderr)

    rows: list[Row] = []
    for rp_rank, uid, name, plays, prov, elo, pp_rank, pp in cand:
        entry = otr.get(uid)
        if entry:
            otr_rating = float(entry["rating"])
            tp = int(entry.get("tournamentsPlayed") or 0)
            mp = int(entry.get("matchesPlayed") or 0)
            otr_rank = int(entry["globalRank"]) if entry.get("globalRank") else None
            estimated = False
        else:
            otr_rating = otr_seed_from_rank(pp_rank)
            tp = mp = 0
            otr_rank = None
            estimated = True
        # These anchors only keep players with a real Elo, so no shrink/seed here.
        rows.append(Row(0, uid, name, pp_rank, pp, rp_rank, elo, otr_rating,
                        estimated, tp, plays, prov, elo_raw=elo, otr_rank=otr_rank,
                        matches_played=mp))

    rows = _apply_otr_floor(rows, filt)
    norm = normalize_and_score(rows)
    rows, trimmed = _finalize(rows, filt)
    return rows, norm, trimmed


def build(top_n: int, use_cache: bool, rp_max_pages: int | None,
          filt: Filters, otr_key: str | None = None,
          osu_creds: tuple[str, str] | None = None) -> tuple[list[Row], dict]:
    """PP-anchored: take the top `top_n` PP players and blend in each one's elo
    rating and OTR rating. Players with no elo rating are skipped."""
    print(f"[1/4] PP board: top {top_n} ...", file=sys.stderr)
    pp = get_pp_board(top_n, use_cache, osu_creds)
    print(f"      got {len(pp)} players", file=sys.stderr)

    print("[2/4] Ranked-play board ...", file=sys.stderr)
    pool, max_page = detect_pool(use_cache)
    targets = {uid for _, uid, _, _ in pp}
    rp = get_rp_map(pool, max_page, targets, use_cache, rp_max_pages)
    print(f"      ranked-play entries collected: {len(rp)}", file=sys.stderr)

    cand: list[Candidate] = []
    skipped = filtered = 0
    for pp_rank, uid, name, ppv in pp:
        entry = rp.get(uid)
        if entry is None:                         # no elo rating -> skip
            skipped += 1
            continue
        rp_rank, plays, provisional, rating = entry
        if not filt.keep_player(plays, provisional):
            filtered += 1
            continue
        cand.append((rp_rank, uid, name, plays, provisional, rating, pp_rank, ppv))

    print("[3/4] OTR ratings + [4/4] normalizing, blending, ranking ...",
          file=sys.stderr)
    rows, norm, trimmed = _assemble(cand, otr_key, use_cache, filt)
    real = sum(1 for r in rows if not r.otr_estimated)
    print(f"      ranked {len(rows)} | skipped {skipped} (no elo) | "
          f"filtered {filtered} (min-plays/provisional) | trimmed {trimmed} "
          f"(top-k) | {real} with real OTR", file=sys.stderr)
    return rows, norm


def build_rankedplay(top_n: int, use_cache: bool, filt: Filters,
                     otr_key: str | None = None,
                     osu_creds: tuple[str, str] | None = None
                     ) -> tuple[list[Row], dict]:
    """Ranked-play-anchored (default): take the top `top_n` ranked-play players
    and blend in each one's pp performance and OTR rating. Players with no pp
    value are skipped. This is the only mode that reaches past PP #10k (pp comes
    from the bulk board where present, else a per-profile fetch)."""
    print(f"[1/4] Ranked-play board: top {top_n} ...", file=sys.stderr)
    pool, _max_page = detect_pool(use_cache)
    rp = get_rp_board(pool, top_n, use_cache)
    print(f"      got {len(rp)} ranked-play players (pool {pool})", file=sys.stderr)

    # Apply the elo-axis filters first so we don't fetch pp/OTR for dropped rows.
    anchor = [t for t in rp if filt.keep_player(t[3], t[4])]
    filtered = len(rp) - len(anchor)

    print("[2/4] PP performance for those players ...", file=sys.stderr)
    # Bulk pp board gives (rank, pp) for anyone in the PP top-10k; only the rest
    # need a per-profile fetch.
    bulk = {uid: (rank, pp) for rank, uid, _n, pp in get_pp_board(PP_RANK_CAP, use_cache, osu_creds)}
    need = [uid for _r, uid, _n, _p, _pr, _rt in anchor if uid not in bulk]
    print(f"      {len(anchor) - len(need)} on bulk pp board; "
          f"fetching {len(need)} profiles for the rest", file=sys.stderr)
    for uid, (rank, pp) in fetch_pp(need, use_cache, osu_creds).items():
        if rank is not None and pp is not None:
            bulk[uid] = (rank, pp)

    cand: list[Candidate] = []
    skipped = 0
    for rp_rank, uid, name, plays, provisional, rating in anchor:
        pr = bulk.get(uid)
        if pr is None or pr[0] is None or pr[1] is None:   # no pp -> skip
            skipped += 1
            continue
        cand.append((rp_rank, uid, name, plays, provisional, rating, pr[0], pr[1]))

    print("[3/4] OTR ratings + [4/4] normalizing, blending, ranking ...",
          file=sys.stderr)
    rows, norm, trimmed = _assemble(cand, otr_key, use_cache, filt)
    real = sum(1 for r in rows if not r.otr_estimated)
    print(f"      ranked {len(rows)} | skipped {skipped} (no pp) | "
          f"filtered {filtered} (min-plays/provisional) | trimmed {trimmed} "
          f"(top-k) | {real} with real OTR", file=sys.stderr)
    return rows, norm


def build_union(use_cache: bool, rp_max_pages: int | None, filt: Filters,
                otr_key: str | None = None,
                osu_creds: tuple[str, str] | None = None
                ) -> tuple[list[Row], dict, dict]:
    """Union-anchored (default): the player set is (PP top-10k) UNION (ranked-play
    top-10k) UNION (OTR top-10k) -- the most complete board, with each of the three
    skill axes contributing its own elite pool. A player is kept if they carry at
    least one real competitive rating (a real Elo OR a real OTR); pure-PP accounts
    with neither are dropped (they'd collapse the blend to raw pp). Elo is then
    handled three ways by one formula: a real Elo is SHRUNK toward its pp-predicted
    value by n/(n+K); a player with no Elo is the n=0 limit (the pp-prior seed); the
    rest are unchanged. OTR-recruited players (outside the pp/rp top-10k) get their
    pp via the per-profile path -- fast when osu! API credentials are supplied.
    Returns (rows, norm, extra_meta) where extra_meta carries the pp->elo prior.
    """
    src_pp = "API" if osu_creds else "HTML"
    print(f"[1/5] PP board: top 10k ({src_pp}) ...", file=sys.stderr)
    pp_board = get_pp_board(PP_RANK_CAP, use_cache, osu_creds)
    pp_val: dict[int, tuple[int, float]] = {uid: (rank, pp) for rank, uid, _n, pp in pp_board}
    names: dict[int, str] = {uid: nm for _r, uid, nm, _p in pp_board}
    print(f"      got {len(pp_board)} PP players", file=sys.stderr)

    # Ranked-play scan: capped at RP top-10k by default (speed); --rp-max-pages overrides.
    rp_scan = rp_max_pages or math.ceil(RP_RANK_CAP / PER_PAGE)
    print(f"[2/5] Ranked-play board (scan for Elo, <= {rp_scan} pages) ...",
          file=sys.stderr)
    pool, max_page = detect_pool(use_cache)
    rp = get_rp_map(pool, max_page, set(), use_cache, rp_scan)
    for _rk, uid, uname, *_ in get_rp_board(pool, PP_RANK_CAP, use_cache):
        names.setdefault(uid, uname)   # RP-top-10k usernames (don't fall back to id)
    print(f"      ranked-play entries collected: {len(rp)}", file=sys.stderr)

    if otr_key:
        otr = fetch_otr_leaderboard(otr_key, use_cache)
        # Hard-cap the OTR axis to its own top-10k: only players within OTR_RANK_CAP
        # keep a real rating; the rest fall through to the rank-seed below.
        before = len(otr)
        otr = {uid: e for uid, e in otr.items()
               if (e.get("globalRank") or 10**9) <= OTR_RANK_CAP}
        print(f"      OTR leaderboard: {before} players -> {len(otr)} kept "
              f"(top {OTR_RANK_CAP:,} by OTR rank)", file=sys.stderr)
        for uid, e in otr.items():
            nm = (e.get("player") or {}).get("username")
            if nm:
                names.setdefault(int(uid), nm)  # OTR usernames as a last resort
    else:
        otr = {}
        print("      no OTR key -> OTR ratings seeded from osu! rank "
              "(pass --otr <key> for real ratings)", file=sys.stderr)

    # Union of the three elite pools: PP top-10k, ranked-play top-10k, OTR top-10k.
    # Keep rule: a member with at least one real competitive rating (Elo or OTR);
    # OTR recruits all carry a real OTR rating, so they pass.
    rp10k = {uid for uid, v in rp.items() if v[0] <= PP_RANK_CAP}
    otr10k = set(otr)                          # already filtered to OTR top-10k above
    recruited = otr10k - set(pp_val) - rp10k   # OTR-only members no other pool surfaced
    union = set(pp_val) | rp10k | otr10k
    kept = [u for u in union if (u in otr) or (u in rp)]
    print(f"[3/5] Union {len(union)} (pp {len(pp_val)} | rp10k {len(rp10k)} | "
          f"otr10k {len(otr10k)}; {len(recruited)} OTR-only recruits) -> kept "
          f"{len(kept)} (have a real Elo or OTR)", file=sys.stderr)

    # Resolve pp for kept players outside the bulk top-10k. Uses the osu! API batch
    # endpoint when credentials are supplied, else per-profile HTML scraping.
    need = [u for u in kept if u not in pp_val]
    src = "osu! API" if osu_creds else "per-profile HTML"
    print(f"[4/5] PP for {len(need)} players outside the top-10k ({src}) ...",
          file=sys.stderr)
    for uid, (rank, pp) in fetch_pp(need, use_cache, osu_creds).items():
        if rank is not None and pp is not None:
            pp_val[uid] = (rank, pp)

    # pp->elo prior, fit on STABLE (>= ELO_STABLE_PLAYS) players so it isn't itself
    # polluted by small-sample noise. Doubles as the seed for players with no Elo.
    fx, fy = [], []
    for u in kept:
        if u in rp and rp[u][1] >= ELO_STABLE_PLAYS and u in pp_val:
            fx.append(math.log(pp_val[u][1]))
            fy.append(rp[u][3])
    fit = statistics.linear_regression(fx, fy)
    prior_elo = lambda ppv: fit.intercept + fit.slope * math.log(ppv)
    print(f"      pp->elo prior (n={len(fx)} stable): "
          f"elo = {fit.intercept:.0f} + {fit.slope:.1f}*ln(pp), K={ELO_SHRINK_K:g}",
          file=sys.stderr)

    print("[5/5] OTR + shrinkage + normalizing, blending, ranking ...", file=sys.stderr)
    rows: list[Row] = []
    no_pp = 0
    for u in kept:
        pr = pp_val.get(u)
        if pr is None or pr[1] is None:        # no pp value -> can't place
            no_pp += 1
            continue
        pp_rank, ppv = pr
        mu = prior_elo(ppv)                     # pp-expected Elo (the prior)
        if u in rp:                             # real Elo -> shrink toward the prior
            rp_rank, plays, prov, elo_raw = rp[u]
            w = plays / (plays + ELO_SHRINK_K)
            elo = mu + (elo_raw - mu) * w
            elo_est = False
            elo_shrunk = abs(elo - elo_raw) >= ELO_SHRINK_MARK
        else:                                   # no Elo -> the n=0 limit (seed)
            rp_rank, plays, prov, elo_raw = None, 0, False, None
            elo, elo_est, elo_shrunk = mu, True, False
        entry = otr.get(u)
        if entry:
            otr_rating = float(entry["rating"])
            tp = int(entry.get("tournamentsPlayed") or 0)
            mp = int(entry.get("matchesPlayed") or 0)
            otr_rank = int(entry["globalRank"]) if entry.get("globalRank") else None
            otr_est = False
        else:
            otr_rating = otr_seed_from_rank(pp_rank)
            tp, mp, otr_rank, otr_est = 0, 0, None, True
        rows.append(Row(0, u, names.get(u, str(u)), pp_rank, ppv, rp_rank, elo,
                        otr_rating, otr_est, tp, plays, prov, elo_raw=elo_raw,
                        elo_estimated=elo_est, elo_shrunk=elo_shrunk, otr_rank=otr_rank,
                        matches_played=mp))

    rows = _apply_otr_floor(rows, filt)
    norm = normalize_and_score(rows)
    rows, trimmed = _finalize(rows, filt)
    real_otr = sum(1 for r in rows if not r.otr_estimated)
    seed_elo = sum(1 for r in rows if r.elo_estimated)
    shrunk = sum(1 for r in rows if r.elo_shrunk)
    print(f"      ranked {len(rows)} | skipped {no_pp} (no pp) | trimmed {trimmed} "
          f"(top-k) | {real_otr} real OTR | {seed_elo} seeded Elo | {shrunk} shrunk Elo",
          file=sys.stderr)
    on_board_recruits = sum(1 for r in rows if r.user_id in recruited)
    extra = {"anchor": "union",
             "pp_source": "api" if osu_creds else "html",
             "otr_rank_cap": OTR_RANK_CAP,
             "rp_scan_pages": rp_scan,
             "otr_recruited": len(recruited),
             "otr_recruited_on_board": on_board_recruits,
             "elo_prior": {"intercept": round(fit.intercept, 6),
                           "slope": round(fit.slope, 6), "k": ELO_SHRINK_K}}
    return rows, norm, extra


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _write_meta(csv_path: str, rows: list[Row], norm: dict,
                filt: Filters | None = None, extra: dict | None = None) -> None:
    """Write a sidecar `<name>.meta.json` stamped with the generation time.

    The website reads this (not the HTTP Last-Modified header) to show when the
    leaderboard DATA was last refreshed, so the stamp tracks data regenerations
    only -- never website/code deploys. It also carries the weights and per-axis
    normalization params so the site calculator can reproduce any score.
    """
    meta_path = os.path.splitext(csv_path)[0] + ".meta.json"
    otr_estimated = sum(1 for r in rows if r.otr_estimated)
    elo_estimated = sum(1 for r in rows if r.elo_estimated)
    elo_shrunk = sum(1 for r in rows if r.elo_shrunk)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "players": len(rows),
        "mode": MODE,
        "weight_pp": round(W_PP, 4),
        "weight_elo": round(W_ELO, 4),
        "weight_otr": round(W_OTR, 4),
        # Per-player reliability weighting: a seeded axis (estimated Elo or OTR) gets
        # zero weight, and a real OTR is tapered by its match count
        # (weight *= matches/(matches+otr_reliability_k)); freed weight is redistributed
        # to the player's other real axes, so the base weights above apply only to deep
        # all-real résumés.
        "reliability_weighting": "seeded axes (estimated elo/otr) get zero weight; a "
                                 "real OTR is tapered by matches/(matches+k_otr); freed "
                                 "weight redistributed to the player's other real axes",
        "otr_reliability_k": OTR_RELIABILITY_K,
        "norm": norm,
        "otr_real": len(rows) - otr_estimated,
        "otr_estimated": otr_estimated,
        "elo_real": len(rows) - elo_estimated,
        "elo_estimated": elo_estimated,
        "elo_shrunk": elo_shrunk,
    }
    if filt is not None:
        payload["min_plays"] = filt.min_plays
        payload["exclude_provisional"] = filt.exclude_provisional
        payload["top_k"] = filt.top_k
        payload["min_otr_matches"] = filt.min_otr_matches
    if extra:
        payload.update(extra)   # anchor, elo_prior, ...
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def write_csv(rows: list[Row], path: str, norm: dict,
              filt: Filters | None = None, extra: dict | None = None) -> str:
    try:
        fh = open(path, "w", newline="", encoding="utf-8")
    except PermissionError:
        base, ext = os.path.splitext(path)
        alt = next(f"{base}_{i}{ext}" for i in range(1, 1000)
                   if not os.path.exists(f"{base}_{i}{ext}"))
        print(f"  ! {os.path.basename(path)} is locked (open in Excel?); "
              f"writing {os.path.basename(alt)} instead", file=sys.stderr)
        path = alt
        fh = open(path, "w", newline="", encoding="utf-8")
    with fh:
        w = csv.writer(fh)
        w.writerow(["hybrid_rank", "user_id", "username", "pp_rank", "pp",
                    "elo_rank", "elo_rating", "elo_raw", "elo_estimated", "elo_shrunk",
                    "otr_rank", "otr_rating", "otr_estimated",
                    "tournaments_played", "matches_played", "plays", "provisional",
                    "hybrid_score"])
        for r in rows:
            w.writerow([r.hybrid_rank, r.user_id, r.username, r.pp_rank,
                        f"{r.pp:.0f}",
                        "" if r.rp_rank is None else r.rp_rank, f"{r.elo_rating:.0f}",
                        "" if r.elo_raw is None else f"{r.elo_raw:.0f}",
                        "yes" if r.elo_estimated else "",
                        "yes" if r.elo_shrunk else "",
                        "" if r.otr_rank is None else r.otr_rank,
                        f"{r.otr_rating:.0f}", "yes" if r.otr_estimated else "",
                        r.tournaments_played, r.matches_played, r.plays,
                        "yes" if r.provisional else "", repr(r.hybrid_score)])
    _write_meta(path, rows, norm, filt, extra)
    return path


def print_table(rows: list[Row], limit: int) -> None:
    print(f"\n{'#':>5}  {'user':<18} {'pp':>7} {'elo':>6} {'otr':>6} "
          f"{'play':>5} {'score':>8}")
    print("-" * 64)
    for r in rows[:limit]:
        prov = "*" if r.provisional else " "
        est = "~" if r.otr_estimated else " "
        emark = "^" if r.elo_estimated else "°" if r.elo_shrunk else " "
        print(f"{r.hybrid_rank:>5}  {r.username[:18]:<18} {r.pp:>7.0f} "
              f"{r.elo_rating:>6.0f}{emark}{r.otr_rating:>5.0f}{est} "
              f"{r.plays:>4}{prov} {r.hybrid_score:>8.3f}")


# --------------------------------------------------------------------------- #
def main() -> None:
    global W_PP, W_ELO, W_OTR, ALLOW_STALE
    ap = argparse.ArgumentParser(
        description="osu! hybrid leaderboard: normalized PP + elo + OTR blend")
    ap.add_argument("--anchor", choices=("union", "rankedplay", "pp"),
                    default="union",
                    help="which board defines the player set: 'union' (default; the "
                         "union of the PP top-10k and ranked-play top-10k, with Elo "
                         "shrinkage + seeding -- the most complete board), 'rankedplay' "
                         "(top-N ranked-play players only) or 'pp' (top-N PP players, "
                         "<=10k). union ignores --top; cap its size with --top-k.")
    ap.add_argument("-n", "--top", type=int, default=TOP_N,
                    help=f"how many anchor players to pull for the pp/rankedplay "
                         f"anchors (default {TOP_N}); ignored by the union anchor")
    ap.add_argument("--otr", nargs="?", const="", default=None, metavar="KEY",
                    help="fetch real OTR tournament ratings from the otr.stagec.net "
                         "API. Pass your API key (--otr <key>) or set OTR_API_KEY in "
                         "the environment and use a bare --otr. Without this, every "
                         "OTR rating is seeded from osu! rank (no real tournament "
                         "data). Get a key by signing in at otr.stagec.net; never "
                         "commit it.")
    ap.add_argument("--osu-api", action="store_true",
                    help="use the osu! API v2 (client-credentials) for fast BATCHED pp "
                         "lookups (50 ids/request) of players outside the pp top-10k, "
                         "instead of slow per-profile HTML scraping. Needed to keep the "
                         "OTR-recruited tail cheap. Reads OSU_CLIENT_ID and "
                         "OSU_CLIENT_SECRET from the environment (register an app at "
                         "osu.ppy.sh/home/account/edit; never commit the secret). "
                         "Without this, pp falls back to HTML scraping.")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore disk cache, always re-fetch")
    ap.add_argument("--offline", action="store_true",
                    help="reuse ANY cached file regardless of age (no network). "
                         "Use for pure recomputes like weight tweaks. Errors if a "
                         "needed file isn't cached.")
    ap.add_argument("--rp-max-pages", type=int, default=None,
                    help="cap ranked-play pages scanned (players beyond the cap "
                         "are treated as unranked). Default: scan until all "
                         "target players are found or the board ends")
    ap.add_argument("--min-plays", type=int, default=1, metavar="N",
                    help="(pp/rankedplay anchors only) drop players with fewer than N "
                         "ranked-play matches. Default 1 (off). The union anchor does "
                         "NOT hard-cut on play count -- it shrinks noisy low-play Elos "
                         "toward their pp-predicted value instead (n/(n+5)).")
    ap.add_argument("--exclude-provisional", action="store_true",
                    help="drop players osu! flags as provisional (rating not yet "
                         "stable). Off by default: provisional players are kept "
                         "and marked in the 'provisional' CSV column instead.")
    ap.add_argument("--top-k", type=int, default=None, metavar="K",
                    help="presentation cap: after scoring, keep only the best K "
                         "players (drops the low-confidence tail).")
    ap.add_argument("--min-otr-matches", type=int, default=0, metavar="N",
                    help="tournament floor (all anchors): keep only players with a "
                         "REAL OTR rating backed by >= N tournament matches, dropping "
                         "seeded and thin-OTR players. Applied BEFORE normalization, so "
                         "survivors are scored against this cohort. Default 0 (off).")
    ap.add_argument("--w-pp", type=float, default=W_PP, metavar="0..1",
                    help=f"weight on pp performance (default {W_PP})")
    ap.add_argument("--w-elo", type=float, default=W_ELO, metavar="0..1",
                    help=f"weight on elo rating (default {W_ELO}); OTR gets the "
                         "remainder, 1 - w_pp - w_elo")
    ap.add_argument("--show", type=int, default=30,
                    help="rows to print to console (default 30)")
    ap.add_argument("--out", default=OUT_CSV, help="output CSV path")
    args = ap.parse_args()

    if not 0.0 <= args.w_pp <= 1.0 or not 0.0 <= args.w_elo <= 1.0:
        ap.error("--w-pp and --w-elo must each be between 0 and 1")
    if args.w_pp + args.w_elo > 1.0 + 1e-9:
        ap.error(f"--w-pp + --w-elo must be <= 1 (got {args.w_pp + args.w_elo})")
    W_PP = args.w_pp
    W_ELO = args.w_elo
    W_OTR = 1.0 - W_PP - W_ELO

    if args.min_plays < 1:
        ap.error(f"--min-plays must be >= 1 (got {args.min_plays})")
    if args.top_k is not None and args.top_k < 1:
        ap.error(f"--top-k must be >= 1 (got {args.top_k})")
    if args.min_otr_matches < 0:
        ap.error(f"--min-otr-matches must be >= 0 (got {args.min_otr_matches})")
    # The union board scores the full ~13k union then shows the best 10k by default
    # (osu! only ranks the top 10k anyway); override with an explicit --top-k.
    top_k = args.top_k
    if args.anchor == "union" and top_k is None:
        top_k = PP_RANK_CAP
    filt = Filters(min_plays=args.min_plays,
                   exclude_provisional=args.exclude_provisional,
                   top_k=top_k,
                   min_otr_matches=args.min_otr_matches)

    # --otr: absent -> seed-only; bare -> key from OTR_API_KEY env; with a value
    # -> that key. The key is never printed.
    otr_key: str | None = None
    if args.otr is not None:
        otr_key = args.otr or os.environ.get("OTR_API_KEY", "")
        if not otr_key:
            ap.error("--otr needs an API key: pass --otr <key> or set OTR_API_KEY")

    # --osu-api: client id + secret from the env only. The secret is never printed
    # (only its length) nor written anywhere.
    osu_creds: tuple[str, str] | None = None
    if args.osu_api:
        cid = os.environ.get("OSU_CLIENT_ID", "")
        csec = os.environ.get("OSU_CLIENT_SECRET", "")
        if not cid or not csec:
            ap.error("--osu-api needs OSU_CLIENT_ID and OSU_CLIENT_SECRET in the "
                     "environment")
        osu_creds = (cid, csec)
        print(f"  osu! API: client {cid} (secret {len(csec)} chars) -> batched pp",
              file=sys.stderr)

    if args.offline:
        ALLOW_STALE = True
        print("  (offline: reusing cached data regardless of age)", file=sys.stderr)

    print(f"  weights: pp={W_PP:.2f} elo={W_ELO:.2f} otr={W_OTR:.2f}",
          file=sys.stderr)

    t0 = time.time()
    extra: dict = {"anchor": args.anchor}
    if args.anchor == "union":
        rows, norm, extra = build_union(use_cache=not args.no_cache,
                                        rp_max_pages=args.rp_max_pages, filt=filt,
                                        otr_key=otr_key, osu_creds=osu_creds)
    elif args.anchor == "rankedplay":
        rows, norm = build_rankedplay(args.top, use_cache=not args.no_cache,
                                      filt=filt, otr_key=otr_key, osu_creds=osu_creds)
    else:
        rows, norm = build(args.top, use_cache=not args.no_cache,
                           rp_max_pages=args.rp_max_pages, filt=filt,
                           otr_key=otr_key, osu_creds=osu_creds)
    out_path = write_csv(rows, args.out, norm, filt=filt, extra=extra)
    print_table(rows, args.show)
    print(f"\nWrote {len(rows)} rows -> {out_path}  ({time.time()-t0:.0f}s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
