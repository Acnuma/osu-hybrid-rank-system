#!/usr/bin/env python3
"""
HybridRankSystem
================
Builds an osu! "hybrid" global leaderboard that blends two existing,
already-normalized website rankings -- both pulled in bulk from server-rendered
leaderboard pages (no per-profile fetches, so it scales to 10k+ politely):

  * PP global rank    -> osu.ppy.sh/rankings/{mode}/global
  * Ranked-play rank  -> osu.ppy.sh/rankings/ranked-play/{mode}/{pool}
                         (osu!'s "ranked play" == the matchmaking system)

Hybrid score (lower is better -- it blends two *ranks*):

    hybrid_score = W_PP * pp_rank + W_RP * ranked_play_rank

Players are sorted ascending by hybrid_score to produce a new "hybrid rank".
Ties are broken deterministically by ranked-play rank, then pp rank, then id.
Players with no ranked-play rank are skipped entirely.

Pure standard library -- no pip installs required.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
W_PP = 0.35          # THE single weight knob: how much pp rank counts (0..1).
                     # Elo (ranked-play) gets the rest automatically.
                     # Lower => trust live matchmaking elo more. Kept modest
                     # because tournament badges are a noisy signal (some link
                     # nowhere, no placement/recency info), so pp shouldn't
                     # dominate even when badge-weighted.
W_RP = 1.0 - W_PP    # weight on elo (ranked-play) rank -- derived, don't edit
TOP_N = 1000         # how many top-PP players to pull
MODE = "osu"         # ruleset: osu | taiko | fruits | mania
PER_PAGE = 50        # osu rankings pages return 50 users each
PP_RANK_CAP = 10000  # osu! caps the PUBLIC pp rankings at top 10k (page 200);
                     # pages past 200 just repeat page 200. Beyond this, a
                     # player's pp rank is only obtainable per-profile / via API.

# Badge-weighted seeding (BWS). Official osu! formula:
#     bws_rank = pp_rank ** (BWS_BASE ** (tournament_badges ** 2))
# 0 badges leaves the rank untouched; more badges pull the effective pp rank
# toward #1. See: osu.ppy.sh/wiki/en/Tournaments/Badge-weighted_seeding
BWS_BASE = 0.9937

CONCURRENCY = 5      # parallel page fetches
MIN_INTERVAL = 0.4   # extra global throttle: min seconds between request starts (~2.5 req/s ceiling)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
CACHE_TTL = 24 * 3600  # seconds; ranked-play data shifts slowly, so a day is plenty
ALLOW_STALE = False    # --offline sets this: reuse ANY cached file regardless of age
                       # (pure recompute, e.g. weight tweaks, needs no fresh data)

BASE = "https://osu.ppy.sh"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
PP_CACHE_DIR = os.path.join(CACHE_DIR, "profile_pp")  # tiny per-uid pp-rank cache
PROFILE_CACHE_DIR = os.path.join(CACHE_DIR, "profile")  # per-uid {pp, badges} JSON (BWS mode)
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
_USER_ANCHOR_RE = re.compile(r'data-user-id="(\d+)"[^>]*>(.*?)</a>', re.S)


def _username_from_anchor(inner_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", inner_html)).strip()


def parse_user_rows(body: str) -> list[tuple[int, str]]:
    """Return [(user_id, username), ...] in leaderboard (page) order."""
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for uid_s, inner in _USER_ANCHOR_RE.findall(body):
        uid = int(uid_s)
        if uid in seen:
            continue
        name = _username_from_anchor(inner)
        if name:
            seen.add(uid)
            out.append((uid, name))
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


def get_pp_board(top_n: int, use_cache: bool) -> list[tuple[int, int, str]]:
    """Return [(pp_rank, user_id, username), ...] for the top `top_n` by PP.

    The osu! public pp leaderboard is capped at PP_RANK_CAP (top 10k); requests
    are clamped to it and de-duplicated so the repeated last page can never
    inflate the result."""
    if top_n > PP_RANK_CAP:
        print(f"  ! pp rankings are capped at top {PP_RANK_CAP:,}; clamping "
              f"(requested {top_n:,}). Beyond this, pp rank needs the API.",
              file=sys.stderr)
        top_n = PP_RANK_CAP
    n_pages = (top_n + PER_PAGE - 1) // PER_PAGE
    by_page: dict[int, list[tuple[int, str]]] = {}
    for p, body in fetch_pages(
        lambda p: f"{BASE}/rankings/{MODE}/global?page={p}",
        lambda p: f"pp_{MODE}_p{p}", range(1, n_pages + 1), use_cache, "pp",
    ):
        by_page[p] = parse_user_rows(body)

    out: list[tuple[int, int, str]] = []
    seen: set[int] = set()
    for p in sorted(by_page):
        for i, (uid, name) in enumerate(by_page[p]):
            rank = (p - 1) * PER_PAGE + i + 1
            if rank <= top_n and uid not in seen:   # dedupe guards drift/repeats
                seen.add(uid)
                out.append((rank, uid, name))
    return out


def get_rp_map(pool: int, max_page: int, targets: set[int],
               use_cache: bool, max_pages: int | None) -> dict[int, int]:
    """Build {user_id: ranked_play_rank}. Scans pages until every target id is
    found (early stop) or the board ends. `max_pages` hard-caps the scan."""
    last = min(max_page, max_pages) if max_pages else max_page
    rp: dict[int, int] = {}
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
            for i, (uid, _name) in enumerate(parse_user_rows(body)):
                rp.setdefault(uid, (p - 1) * PER_PAGE + i + 1)
        if targets and targets.issubset(rp.keys()):
            print("      all target players found; stopping early",
                  file=sys.stderr)
            break
    return rp


def get_rp_board(pool: int, top_n: int, use_cache: bool) -> list[tuple[int, int, str]]:
    """Return [(ranked_play_rank, user_id, username), ...] for the top `top_n`
    ranked-play players. Used as the anchor list in ranked-play mode."""
    n_pages = (top_n + PER_PAGE - 1) // PER_PAGE
    by_page: dict[int, list[tuple[int, str]]] = {}
    for p, body in fetch_pages(
        lambda p: f"{BASE}/rankings/ranked-play/{MODE}/{pool}?page={p}",
        lambda p: f"rp_{MODE}_{pool}_p{p}", range(1, n_pages + 1), use_cache, "rp",
    ):
        by_page[p] = parse_user_rows(body)

    out: list[tuple[int, int, str]] = []
    seen: set[int] = set()
    for p in sorted(by_page):
        for i, (uid, name) in enumerate(by_page[p]):
            rank = (p - 1) * PER_PAGE + i + 1
            if rank <= top_n and uid not in seen:
                seen.add(uid)
                out.append((rank, uid, name))
    return out


# --------------------------------------------------------------------------- #
# Per-profile pp rank  (for ranked-play-anchored players outside the PP top-10k)
# --------------------------------------------------------------------------- #
_INITIAL_DATA_RE = re.compile(r'data-initial-data="([^"]*)"')


def _global_rank_from_profile(body: str) -> int | None:
    """Pull user.statistics.global_rank from a profile page's embedded JSON."""
    m = _INITIAL_DATA_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(html.unescape(m.group(1)))
    except ValueError:
        return None
    stats = (data.get("user") or {}).get("statistics") or {}
    rank = stats.get("global_rank")
    return int(rank) if rank else None


def profile_pp_rank(uid: int, use_cache: bool) -> int | None:
    """pp global rank for one player. Caches only the extracted number (profile
    HTML is huge), with an empty file marking 'no pp rank'."""
    cpath = os.path.join(PP_CACHE_DIR, f"{MODE}_{uid}.txt")
    if use_cache and _cache_fresh(cpath):
        with open(cpath, "r", encoding="utf-8") as fh:
            v = fh.read().strip()
        return int(v) if v else None
    rank = _global_rank_from_profile(_http_get(f"{BASE}/users/{uid}/{MODE}"))
    os.makedirs(PP_CACHE_DIR, exist_ok=True)
    with open(cpath, "w", encoding="utf-8") as fh:
        fh.write("" if rank is None else str(rank))
    return rank


def fetch_profile_pp_ranks(uids: list[int], use_cache: bool) -> dict[int, int | None]:
    """Fetch pp ranks for many players concurrently (rate-limited)."""
    result: dict[int, int | None] = {}
    if not uids:
        return result
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(profile_pp_rank, uid, use_cache): uid for uid in uids}
        done = 0
        for fut in as_completed(futs):
            uid = futs[fut]
            done += 1
            try:
                result[uid] = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! profile {uid} failed: {e}", file=sys.stderr)
                result[uid] = None
            if done % 100 == 0 or done == len(futs):
                print(f"      profiles: {done}/{len(futs)}", file=sys.stderr)
    return result


# --------------------------------------------------------------------------- #
# Tournament badges  (for badge-weighted seeding, --bws)
# --------------------------------------------------------------------------- #
# Heuristic: a badge counts as a *tournament* badge if it links to a tournament
# wiki page or a forum thread (where tournament results live) AND its
# description isn't a clearly non-tournament award (mapping/contest) or a
# non-playing role (staff/spectator/commentator), which official BWS excludes.
# The osu! API exposes no "is tournament" flag, so this trades a little accuracy
# for zero maintenance. A handful of older badges carry no url and are missed.
# Refine the keyword lists / url test here.
_NON_TOURNEY_KEYWORDS = (
    # not tournaments at all
    "mapping", "mapper", "contest", "aspire", "exemplary", "contributor",
    "fan art", "fanart", "spotlight", "community choice", "beatmap",
    "labour of love", "monthly", "pending cup",
    # tournament *roles* that don't count toward BWS (non-playing)
    "spectator", "broadcast", "commentator", "commentary", "staff",
    "referee", "streamer", "graphic", "statistician", "host of",
)


def _is_tournament_badge(badge: dict) -> bool:
    url = (badge.get("url") or "").lower()
    desc = (badge.get("description") or "").lower()
    is_tourney_link = "/community/forums/" in url or (
        "/wiki/" in url and "tournaments" in url)
    if not is_tourney_link:
        return False
    return not any(k in desc for k in _NON_TOURNEY_KEYWORDS)


def _profile_fields(body: str) -> tuple[int | None, int]:
    """From a profile page, return (pp_global_rank, tournament_badge_count)."""
    m = _INITIAL_DATA_RE.search(body)
    if not m:
        return None, 0
    try:
        data = json.loads(html.unescape(m.group(1)))
    except ValueError:
        return None, 0
    user = data.get("user") or {}
    rank = (user.get("statistics") or {}).get("global_rank")
    pp = int(rank) if rank else None
    badges = sum(1 for b in (user.get("badges") or []) if _is_tournament_badge(b))
    return pp, badges


def fetch_profile(uid: int, use_cache: bool) -> tuple[int | None, int]:
    """pp rank + tournament badge count for one player, cached as small JSON."""
    cpath = os.path.join(PROFILE_CACHE_DIR, f"{MODE}_{uid}.json")
    if use_cache and _cache_fresh(cpath):
        try:
            with open(cpath, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            return d.get("pp"), int(d.get("badges") or 0)
        except (ValueError, OSError):
            pass  # corrupt cache -> refetch
    pp, badges = _profile_fields(_http_get(f"{BASE}/users/{uid}/{MODE}"))
    os.makedirs(PROFILE_CACHE_DIR, exist_ok=True)
    with open(cpath, "w", encoding="utf-8") as fh:
        json.dump({"pp": pp, "badges": badges}, fh)
    return pp, badges


def fetch_profiles(uids: list[int], use_cache: bool) -> dict[int, tuple[int | None, int]]:
    """Fetch (pp_rank, badges) for many players concurrently (rate-limited)."""
    result: dict[int, tuple[int | None, int]] = {}
    if not uids:
        return result
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futs = {pool.submit(fetch_profile, uid, use_cache): uid for uid in uids}
        done = 0
        for fut in as_completed(futs):
            uid = futs[fut]
            done += 1
            try:
                result[uid] = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! profile {uid} failed: {e}", file=sys.stderr)
                result[uid] = (None, 0)
            if done % 100 == 0 or done == len(futs):
                print(f"      profiles: {done}/{len(futs)}", file=sys.stderr)
    return result


def bws_rank(pp_rank: int, badges: int) -> float:
    """Badge-weighted seed: pp_rank ** (BWS_BASE ** badges**2). 0 badges -> pp."""
    if badges <= 0:
        return float(pp_rank)
    return float(pp_rank) ** (BWS_BASE ** (badges * badges))


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@dataclass
class Row:
    hybrid_rank: int
    user_id: int
    username: str
    pp_rank: int
    rp_rank: int
    hybrid_score: float
    badges: int = 0          # tournament badge count (BWS mode only)
    bws_pp: float = 0.0      # badge-weighted pp rank (BWS mode only)


def build(top_n: int, use_cache: bool, rp_max_pages: int | None) -> list[Row]:
    print(f"[1/3] PP board: top {top_n} ...", file=sys.stderr)
    pp = get_pp_board(top_n, use_cache)
    print(f"      got {len(pp)} players", file=sys.stderr)

    print("[2/3] Ranked-play board ...", file=sys.stderr)
    pool, max_page = detect_pool(use_cache)
    targets = {uid for _, uid, _ in pp}
    rp = get_rp_map(pool, max_page, targets, use_cache, rp_max_pages)
    print(f"      ranked-play entries collected: {len(rp)}", file=sys.stderr)

    print("[3/3] Blending and ranking ...", file=sys.stderr)
    rows: list[Row] = []
    skipped = 0
    for pp_rank, uid, name in pp:
        rp_rank = rp.get(uid)
        if rp_rank is None:                       # no ranked-play rank -> skip
            skipped += 1
            continue
        score = W_PP * pp_rank + W_RP * rp_rank
        rows.append(Row(0, uid, name, pp_rank, rp_rank, score))

    rows.sort(key=lambda r: (r.hybrid_score, r.rp_rank, r.pp_rank, r.user_id))
    for i, r in enumerate(rows, 1):
        r.hybrid_rank = i

    print(f"      ranked {len(rows)} | skipped {skipped} (no ranked play)",
          file=sys.stderr)
    return rows


def build_rankedplay(top_n: int, use_cache: bool, bws: bool = False) -> list[Row]:
    """Ranked-play-anchored: take the top `top_n` ranked-play players and blend
    in each one's pp rank. Players with no pp global rank are skipped.

    With `bws`, the pp axis is badge-weighted (pp_rank ** BWS_BASE**badges**2)
    before blending, which seeds tournament-decorated players higher. That needs
    a profile fetch for *every* anchored player (badges aren't on any bulk
    board), so it costs more than the plain mode's pp-only profile fetches."""
    print(f"[1/3] Ranked-play board: top {top_n} ...", file=sys.stderr)
    pool, _max_page = detect_pool(use_cache)
    rp = get_rp_board(pool, top_n, use_cache)
    print(f"      got {len(rp)} ranked-play players (pool {pool})", file=sys.stderr)

    if bws:
        print("[2/3] PP ranks + tournament badges (per profile) ...", file=sys.stderr)
        # Bulk pp board is still the authoritative pp rank for the top-10k; use
        # it where present and fall back to the profile's pp for the rest. But
        # badges only come from the profile, so we fetch one for everyone.
        bulk_pp = {uid: r for r, uid, _ in get_pp_board(PP_RANK_CAP, use_cache)}
        print(f"      fetching {len(rp)} profiles for badges", file=sys.stderr)
        prof = fetch_profiles([uid for _, uid, _ in rp], use_cache)
    else:
        print("[2/3] PP ranks for those players ...", file=sys.stderr)
        # Fast path: the bulk pp board already gives pp rank for anyone in the
        # PP top-10k, so we only pay a per-profile fetch for those outside it.
        pp_map = {uid: r for r, uid, _ in get_pp_board(PP_RANK_CAP, use_cache)}
        need = [uid for _, uid, _ in rp if uid not in pp_map]
        print(f"      {len(rp) - len(need)} found on bulk pp board; "
              f"fetching {len(need)} profiles for the rest", file=sys.stderr)
        for uid, r in fetch_profile_pp_ranks(need, use_cache).items():
            if r is not None:
                pp_map[uid] = r

    print("[3/3] Blending and ranking ...", file=sys.stderr)
    rows: list[Row] = []
    skipped = 0
    for rp_rank, uid, name in rp:
        if bws:
            prof_pp, badges = prof.get(uid, (None, 0))
            pp_rank = bulk_pp.get(uid, prof_pp)
        else:
            pp_rank, badges = pp_map.get(uid), 0
        if pp_rank is None:                       # no pp global rank -> skip
            skipped += 1
            continue
        bws_pp = bws_rank(pp_rank, badges) if bws else float(pp_rank)
        score = W_PP * bws_pp + W_RP * rp_rank
        rows.append(Row(0, uid, name, pp_rank, rp_rank, score, badges, bws_pp))

    rows.sort(key=lambda r: (r.hybrid_score, r.rp_rank, r.pp_rank, r.user_id))
    for i, r in enumerate(rows, 1):
        r.hybrid_rank = i

    if bws:
        with_badges = sum(1 for r in rows if r.badges)
        print(f"      ranked {len(rows)} | skipped {skipped} (no pp rank) | "
              f"{with_badges} with tournament badges", file=sys.stderr)
    else:
        print(f"      ranked {len(rows)} | skipped {skipped} (no pp rank)",
              file=sys.stderr)
    return rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_csv(rows: list[Row], path: str, bws: bool = False) -> str:
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
        if bws:
            w.writerow(["hybrid_rank", "user_id", "username", "pp_rank",
                        "badges", "bws_pp_rank", "elo_rank",
                        "hybrid_score"])
            for r in rows:
                w.writerow([r.hybrid_rank, r.user_id, r.username, r.pp_rank,
                            r.badges, f"{r.bws_pp:.1f}", r.rp_rank,
                            f"{r.hybrid_score:.2f}"])
        else:
            w.writerow(["hybrid_rank", "user_id", "username", "pp_rank",
                        "elo_rank", "hybrid_score"])
            for r in rows:
                w.writerow([r.hybrid_rank, r.user_id, r.username, r.pp_rank,
                            r.rp_rank, f"{r.hybrid_score:.2f}"])
    return path


def print_table(rows: list[Row], limit: int, bws: bool = False) -> None:
    if bws:
        print(f"\n{'#':>5}  {'user':<18} {'pp':>6} {'bdg':>4} {'bws':>8} "
              f"{'elo':>6} {'score':>9}")
        print("-" * 64)
        for r in rows[:limit]:
            print(f"{r.hybrid_rank:>5}  {r.username[:18]:<18} {r.pp_rank:>6} "
                  f"{r.badges:>4} {r.bws_pp:>8.0f} {r.rp_rank:>6} "
                  f"{r.hybrid_score:>9.1f}")
    else:
        print(f"\n{'#':>5}  {'user':<18} {'pp':>6} {'elo':>6} {'score':>9}")
        print("-" * 50)
        for r in rows[:limit]:
            print(f"{r.hybrid_rank:>5}  {r.username[:18]:<18} {r.pp_rank:>6} "
                  f"{r.rp_rank:>6} {r.hybrid_score:>9.1f}")


# --------------------------------------------------------------------------- #
def main() -> None:
    global W_PP, W_RP, ALLOW_STALE
    ap = argparse.ArgumentParser(description="osu! hybrid PP / ranked-play leaderboard")
    ap.add_argument("--anchor", choices=("rankedplay", "pp"), default="rankedplay",
                    help="which board defines the player set: 'rankedplay' (top-N "
                         "ranked-play players, pp rank fetched per profile; default) "
                         "or 'pp' (top-N PP players, capped at 10k)")
    ap.add_argument("-n", "--top", type=int, default=TOP_N,
                    help=f"how many anchor players to pull (default {TOP_N})")
    ap.add_argument("--bws", action="store_true",
                    help="badge-weighted seeding: weight the pp axis by tournament "
                         "badge count (rankedplay anchor only). Fetches a profile "
                         "for every anchored player to read badges.")
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
    ap.add_argument("--w-pp", type=float, default=W_PP, metavar="0..1",
                    help=f"weight on pp rank; elo gets 1 - w_pp (default {W_PP}). "
                         "The single weight knob.")
    ap.add_argument("--show", type=int, default=30,
                    help="rows to print to console (default 30)")
    ap.add_argument("--out", default=OUT_CSV, help="output CSV path")
    args = ap.parse_args()

    if not 0.0 <= args.w_pp <= 1.0:
        ap.error(f"--w-pp must be between 0 and 1 (got {args.w_pp})")
    W_PP = args.w_pp
    W_RP = 1.0 - W_PP

    if args.offline:
        ALLOW_STALE = True
        print("  (offline: reusing cached data regardless of age)", file=sys.stderr)

    if args.bws and args.anchor != "rankedplay":
        print("  ! --bws is only wired for --anchor rankedplay; ignoring it",
              file=sys.stderr)
        args.bws = False

    t0 = time.time()
    if args.anchor == "rankedplay":
        rows = build_rankedplay(args.top, use_cache=not args.no_cache,
                                bws=args.bws)
    else:
        rows = build(args.top, use_cache=not args.no_cache,
                     rp_max_pages=args.rp_max_pages)
    out_path = write_csv(rows, args.out, bws=args.bws)
    print_table(rows, args.show, bws=args.bws)
    print(f"\nWrote {len(rows)} rows -> {out_path}  ({time.time()-t0:.0f}s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
