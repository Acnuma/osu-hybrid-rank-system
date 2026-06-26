# HybridRankSystem

### 🏆 [**View the live leaderboard →**](https://acnuma.github.io/osu-hybrid-rank-system/)

The published board is generated with:

```
python hybrid_rank.py --anchor rankedplay --top 10000 --bws --min-plays 5 --max-score 10000 --out docs/hybrid_leaderboard.csv
```

- `--anchor rankedplay` — player set = the top ranked-play (matchmaking) players
- `--top 10000` — top 10,000 of them
- `--bws` — badge-weighted seeding (tournament players credited); optionally takes a base, e.g. `--bws 0.99`, to tune badge strength (default `0.9937`)
- `--min-plays 5` — drop players with fewer than 5 ranked-play matches (noisy Elo)
- `--max-score 10000` — trim the low-confidence tail (drop rows scoring above 10,000)
- weight `W_PP = 0.35` (default) → `score = 0.35 × bws_pp + 0.65 × elo_rank`
- mode: `osu` standard

A player is skipped if they lack **either** an Elo (ranked-play) rank **or** a PP
rank — both are required to compute the blended score. On top of that the two
filters above remove low-match accounts and the high-score tail, so the live
board currently shows **~4,040 players** (of the 10,000 anchored). Provisional
ratings are **kept** and marked with an asterisk, not dropped. Drop the two
filters (or tune them) to generate a larger board.

---

## Introduction

Builds an osu! **hybrid global leaderboard** that blends two already-normalized
rankings:

| Component | Source | Notes |
|---|---|---|
| **PP global rank** | `osu.ppy.sh/rankings/{mode}/global` (bulk) or profile JSON | 50/page; bulk board capped at top 10k |
| **Ranked-play rank** | `osu.ppy.sh/rankings/ranked-play/{mode}/{pool}` | osu!'s matchmaking system; pool id + last page auto-detected |

### Formula

```
hybrid_score = W_PP * pp_rank + (1 - W_PP) * elo_rank   # lower = better
                                                        # W_PP = 0.35
```

`elo_rank` is osu!'s ranked-play (matchmaking) rank. Sorted ascending by
`hybrid_score`. Ties break deterministically by elo rank, then pp rank, then
user_id.

### Anchor modes

The **anchor** decides which board defines the player set:

- **`--anchor rankedplay` (default)** — take the top-N **ranked-play** players,
  then look up each one's pp rank. PP rank comes from the bulk PP board when the
  player is in the PP top-10k, otherwise from a **per-profile** fetch of
  `statistics.global_rank` (no OAuth; only the extracted number is cached, not
  the page). This is the only way to rank players past PP #10,000, and it never
  skips a ranked-play player who simply has a deep pp rank. Players with **no pp
  global rank** are skipped.
- **`--anchor pp`** — take the top-N **PP** players (hard-capped at 10k, see
  below), blend in ranked-play rank from the bulk ranked-play board. Players
  with **no ranked-play rank** are skipped.

### Badge-weighted seeding (`--bws`)

Optional, opt-in. Accounts for **tournament players** by badge-weighting the pp
axis with osu!'s [BWS formula](https://osu.ppy.sh/wiki/en/Tournaments/Badge-weighted_seeding):

```
bws_pp = pp_rank ^ (BASE ^ tournament_badges²)          # 0 badges -> pp_rank unchanged
hybrid_score = W_PP * bws_pp + (1 - W_PP) * elo_rank    # W_PP = 0.35
```

`--bws` takes an **optional base** (`BASE`, default `0.9937`): a bare `--bws`
uses the standard base, while `--bws 0.99` overrides it. The base sets how hard
each badge pulls the pp rank toward #1 — **lower = stronger** (`1.0` cancels the
effect entirely; the value must be in `(0, 1]`). The chosen base is recorded in
the `.meta.json` sidecar. The same knob is exposed on the website's calculator
tab ("BWS base").

More tournament badges pull a player's effective pp rank toward #1, so a
decorated player with a deep pp rank seeds much higher (e.g. rank 10,000 with 10
badges → effective ~135). Badge count is read from each profile's `user.badges`
and filtered to **tournament badges** by a heuristic: the badge must link to a
tournament wiki page or forum thread, and is excluded if its description names a
non-tournament award (mapping/contest) or a non-playing role
(staff/spectator/commentator). A few older badges carry no URL and are missed.
The heuristic lives in `_is_tournament_badge` / `_NON_TOURNEY_KEYWORDS` — tune
there. BWS mode adds `badges` and `bws_pp_rank` columns to the CSV.

Cost: BWS needs a profile fetch for **every** anchored player (badges aren't on
any bulk board), so a top-10k `--bws` run fetches ~10k profiles (vs ~5.7k for
plain rankedplay mode). Only `--anchor rankedplay` supports it. Profiles cache
as `{pp, badges}` JSON under `.cache/profile/`.

### Data-quality filters

Three **opt-in** knobs, all **off by default** (a plain run includes every
player who has both ranks — nothing is silently dropped). They act on the
**ranked-play / Elo side** and are independent of `--bws` (which only adjusts the
PP side), so they compose freely.

| Flag | Default | Effect |
|---|---|---|
| `--min-plays N` | `1` (off) | Drop players with fewer than **N** ranked-play matches. An Elo computed from a handful of matches is statistically noise (osu! still publishes a rank as low as ~3 matches), so a small floor removes the least-reliable accounts. |
| `--exclude-provisional` | off | Drop players whose rating osu! flags as **provisional** ("too few recent matches"). Off by default — provisional players are **kept and marked** instead (see below). |
| `--max-score S` | off | Drop rows whose `hybrid_score` exceeds **S**. Because rows are ordered by score, this is identical to keeping the top-K players — a presentation trim of the low-confidence tail, not a re-ranking. |

The ranked-play board exposes each player's **play count** and **provisional
flag** in bulk (no extra fetch), so `--min-plays` and the `provisional` marker
cost nothing. Both new fields are written to every CSV as `plays` and
`provisional` columns regardless of whether you filter on them.

> **Picking `--min-plays`:** the ranked-play feature is young — a recent top-1000
> snapshot had a median of ~12 plays, 58% provisional, and even #1 with only ~22
> plays. So `--min-plays 10` cut ~39% of the board and `15` cut ~59%. A modest
> floor like `--min-plays 5` removes the noisiest 1–4-match accounts (~16%)
> without gutting the board; raise it as play counts grow.

### Usage

```
python hybrid_rank.py --anchor rankedplay --top 10000   # ranked-play top 10k (default mode)
python hybrid_rank.py --anchor rankedplay --top 10000 --bws  # tournament-aware (badge-weighted)
python hybrid_rank.py --anchor rankedplay --top 50      # quick sample
python hybrid_rank.py --anchor pp --top 10000           # PP top 10k (the PP max -- see cap)
python hybrid_rank.py --no-cache                        # force a fresh pull
python hybrid_rank.py --bws --offline                   # pure recompute (weight tweaks); reuse cache, no network
python hybrid_rank.py --bws --offline --w-pp 0.4        # try a different pp weight (elo gets 1 - w_pp)
python hybrid_rank.py --bws 0.99 --offline              # tune BWS base (badge strength; lower = stronger)
python hybrid_rank.py --bws --min-plays 5               # opt-in: drop <5-match (noisy-Elo) players
python hybrid_rank.py --bws --exclude-provisional       # opt-in: drop osu!-flagged provisional ratings
python hybrid_rank.py --bws --offline --max-score 2000  # opt-in: trim the low-confidence tail (score > 2000)
python hybrid_rank.py --show 50                         # print more rows
```

`--offline` reuses any cached file regardless of age and never hits the network
(errors if something needed isn't cached) — so changing `W_PP`/`W_RP`, the BWS
base, or the BWS formula re-ranks in seconds instead of re-scraping. The normal
cache TTL is 24h.

Ranked-play mode cost: 200 ranked-play pages + 200 PP pages + one profile fetch
per top-10k ranked-play player **outside** the PP top-10k (~5–6k of the 10k, so
~40 min at the polite default rate). Re-runs are near-instant from cache.

Output: `hybrid_leaderboard.csv` (hybrid_rank, user_id, username, pp_rank,
elo_rank, **plays**, **provisional**, hybrid_score; `--bws` also adds badges +
bws_pp_rank). `provisional` is `yes` when osu! flags the rating as not yet
stable, else blank. A sidecar `<name>.meta.json` records the generation time,
weights, the active filter settings, and (when `--bws` is on) the BWS base. If
the CSV is open in Excel a numbered sibling is written instead.

### Website (GitHub Pages)

The repo ships a dependency-free static site in [`docs/`](docs/) — an
`index.html` + `app.js` that fetch the committed CSV and render a **searchable,
sortable** table (search by username, click any column header to sort by pp /
elo / bws / hybrid score). No backend, no build step, no tracking. Provisional
players are marked with an **asterisk (`*`) on their Elo rank** (mirroring osu!'s
own convention); the `plays` column is read but not displayed. A second
**Calculator** tab lets anyone compute a hybrid score from a PP rank, Elo rank,
badge count, BWS base, and weight — the same formula the board uses, evaluated
live in the browser.

**Enable it:** push the repo to GitHub → *Settings → Pages → Build from a
branch* → branch `main`, folder `/docs`. The board goes live at
`https://<user>.github.io/<repo>/`.

**Refresh the published data** (manual — you control the scrape rate):

```
python hybrid_rank.py --anchor rankedplay --top 10000 --bws --min-plays 5 --max-score 10000 --out docs/hybrid_leaderboard.csv  # full scrape (~40 min first time)
git add docs/hybrid_leaderboard.csv docs/hybrid_leaderboard.meta.json && git commit -m "refresh leaderboard" && git push
```

The site reads `docs/hybrid_leaderboard.csv`, so the CSV must live **inside**
`docs/` (Pages only serves the publish folder). Root-level
`hybrid_leaderboard*.csv` outputs are git-ignored so throwaway runs don't clutter
the repo.

### Hard cap: top 10,000

osu!'s **public PP leaderboard is capped at the top 10,000** (page 200); any
deeper page just repeats page 200. So the PP-anchored hybrid maxes out at
`--top 10000` — the tool clamps anything larger and warns. To rank players
beyond PP #10,000 you must fetch each one's pp rank individually from the osu!
API (`statistics.global_rank`), since there is no bulk source past 10k. (The
ranked-play board has no such cap — it pages fully to ~98k.)

### Scale & politeness

- Cost is `ceil(top/50)` PP pages + the ranked-play scan. The ranked-play
  board is ~1964 pages (~98k players); the scan stops early once every target
  player is found, otherwise it covers the whole board (needed so a top-PP
  player who simply has a deep ranked-play rank isn't wrongly skipped).
- So **top 10k ≈ 200 + up to ~1964 ≈ ~2200 small page fetches**, not 10k.
- `CONCURRENCY` (default 5) and `MIN_INTERVAL` (global min seconds between
  requests, default 0.4) at the top of the script tune throughput vs politeness.
  No Cloudflare/anti-bot block was observed at these settings.
- Pages are cached under `.cache/` for 24h, so re-runs are near-instant.

### Notes
- Pure standard library — no `pip install`.
- The ranked-play (matchmaking) rating *is* exposed by the osu! API v2 as the
  `matchmaking_stats` include on the user object, but the **bulk web
  leaderboard above is more efficient** for large pulls and needs no OAuth.
- Tune the **single weight knob `W_PP`** (0..1; elo gets `1 - W_PP`
  automatically), plus `TOP_N`, `MODE` at the top of `hybrid_rank.py`.

---

## Limitations

### Biggest limitations

- **Elo is still inaccurate because too few players queue.** For Elo to be
  meaningful, players — especially those at the top — need to play ranked
  matches relatively frequently. (This assumes the Elo system itself is sound;
  it is brand new and still under active development.)
- **Most players haven't queued even once,** so they never appear on the
  leaderboard at all.
- **Tournament badges can't be counted reliably.** A profile only exposes *how
  many* badges a player has — not whether a given badge is for a tournament, how
  old it is, or the skill level of the event. Some badges note the placement
  achieved, many don't, and the tooltip wording is inconsistent and often in
  different languages, so the data can't be trusted. This tool approximates it by
  counting only badges that link to a tournament forum thread or wiki page — but
  some tournament badges link to nothing at all (e.g. **Corsace**), so they are
  missed entirely.
- **The 0.35 weight (the BWS ↔ Elo skew) is debatable.** The board leans toward
  Elo because the PP side is already lifted by BWS, and Elo carries a recency
  bias that should, in theory, make it a better gauge of a player's *current*
  skill relative to others. Even so, a different weight may be equally valid — or
  better.

### Does a hybrid leaderboard even need to exist?

If Elo were already an accurate representation of how players' skill levels
compare, a hybrid leaderboard might not be needed at all. The two strongest
arguments for its existence are:

1. **PP is a good gauge of raw mechanical skill,** which is important in osu! and
   deserves to be accounted for.
2. **osu! is primarily a single-player experience.** Even in tournaments,
   players never interact during gameplay — each one plays alone, and the winner
   is decided by comparing scores. By
   [Chris Crawford](https://en.wikipedia.org/wiki/Chris_Crawford_(game_designer))'s
   definition, that makes a tournament match a *competition* rather than a *game*
   (though it is, of course, still a video game). Since results come from each
   player's *own* performance and not from direct play against an opponent, a
   purely head-to-head rating like Elo can't tell the whole story by itself — so
   blending in PP, a measure of that individual performance, is justified. One
   could object that players *do* interact: they ban and pick beatmaps against
   their opponent in tournaments and ranked play. But that's a meta-game layered
   on top — the core gameplay loop, clicking circles to the beat for a high
   score, plays out in isolation and is unaffected by it.
