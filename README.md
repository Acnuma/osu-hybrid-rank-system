# HybridRankSystem

### 🏆 [**View the leaderboard →**](https://acnuma.github.io/osu-hybrid-rank-system/)

*Not real-time — the board is a periodically-refreshed snapshot (manually regenerated, data cached ~1 week). The site shows when the data was last updated.*

The published board is generated with:

```
python hybrid_rank.py --anchor union --otr <key> --osu-api --out docs/hybrid_leaderboard.csv
```

- `--anchor union` — player set = (**PP top-10k**) ∪ (**Ranked Play top-10k**) ∪ (**OTR top-10k**), the most complete pool (default)
- `--otr <key>` — fetch real **OTR** tournament ratings from the otr.stagec.net API (key required; see below)
- `--osu-api` — use the osu! API v2 for fast **batched** pp lookups of players outside the PP top-10k (50/request, vs. one HTML scrape each); reads `OSU_CLIENT_ID`/`OSU_CLIENT_SECRET` from the env (see below)
- base weights `W_PP = W_ELO = W_OTR = 1/3` (equal thirds) → `score = (z(log pp) + z(elo) + z(otr)) / 3` for all-real players; a **seeded** axis (estimated Elo/OTR) is dropped to zero weight and its share redistributed, and each **real** axis is tapered by its own evidence count — Elo by ranked matches, OTR by tournament matches ([reliability weighting](#formula))
- mode: `osu` standard

A player appears if they carry at least one **real competitive rating**: a real
Elo (they've queued ranked play) **or** a real OTR (they've played a verified
tournament). Pure-PP accounts with neither are dropped (they'd collapse the blend
to raw PP). The union is ~20k players; the board scores all of them and shows the
**best 10,000**. There is **no hard min-plays cutoff**: a low-play Elo is
*weight-tapered* by its match count instead of discarded (see
[Reliability weighting](#reliability-weighting-elo--otr)). Provisional ratings are
**kept** and marked, not dropped.

---

## Introduction

Builds an osu! **hybrid global leaderboard** that blends three skill signals on a
single, **normalized** scale, so it measures *magnitude* rather than ordinal place:

| Component | Source | Notes |
|---|---|---|
| **PP performance** | osu! API v2 `rankings`/`users` with `--osu-api`, else `osu.ppy.sh/rankings/{mode}/global` (bulk) or profile HTML | raw pp value; bulk board capped at top 10k |
| **Elo rating** | `osu.ppy.sh/rankings/ranked-play/{mode}/{pool}` | osu!'s Ranked-Play rating (mu) — an OpenSkill (Plackett-Luce) posterior seeded from a PP estimate; pool id + last page auto-detected |
| **OTR rating** | [otr.stagec.net](https://otr.stagec.net) public API | osu! Tournament Rating; rank-seeded estimate when a player has no tournament history |

### Formula

Each axis is **standardized** across the board population (z-score), then blended:

```
z(x)         = (x - mean) / std        # mean/std measured over the whole board
hybrid_score = w_pp·z(log pp) + w_elo·z(elo) + w_otr·z(otr)   # higher = better
               base weights: W_PP = W_ELO = W_OTR = 1/3 (equal thirds)
```

PP is **logged** before standardizing because it is heavily right-skewed. Sorted
**descending** by `hybrid_score`. Ties break deterministically by elo rating,
then pp, then user_id. The per-axis `mean`/`std` are recorded in the `.meta.json`
sidecar so the website calculator can reproduce any score exactly. The
normalization population is the **whole board, seeded placeholders included**: a
seeded Elo/OTR is zero-weighted in its *own* player's blend (above) but still
contributes to that axis's `mean`/`std`, so it helps define the scale every real
rating is standardized against. Seeds are therefore kept in the data rather than
blanked. Dropping them would shift each axis's baseline and re-rank the board.

**Reliability weighting (per player).** The weights `w_pp/w_elo/w_otr` equal the
base `W_PP/W_ELO/W_OTR` only when all three axes are *real*. Elo and OTR are the same
kind of object: OpenSkill (Plackett-Luce) posteriors seeded from a prior (Elo from a
pp estimate, OTR from osu! rank) that washes out as evidence accrues. So both are
handled identically: the rating **value** is used as-is (never edited) and reliability
lives entirely in the **weight**. A **seeded** axis carries no independent signal: its
value is just the prior, which for both axes is ≈ pp, so weighting it like a real
measurement double-counts pp. Any seeded axis is therefore given **zero** weight and
its share is redistributed proportionally to the player's real axes (e.g. a player with
a real Elo but a seeded OTR is scored on roughly `½·z(log pp) + ½·z(elo)`). A **real**
axis is tapered by its own evidence count — Elo by `plays / (plays + 5)`, OTR by
`matches / (matches + 5)` — so a thin rating (barely off its ≈ pp seed) leans on the
other axes, while a deep one earns close to its full share. A seeded axis is just the
evidence = 0 limit of that taper, so the rule is continuous across the seed boundary.
Every board player has at least one real competitive axis, so the real weights never
sum to zero.

On the published board **full weight is the most accurate choice** for both
axes: a real Elo/OTR and pp are complementary, so down-weighting a thin rating just
leans on pp, which the seed already duplicates. Measured against OTR, tapering only
*lowers* agreement. The taper is therefore a conventional reliability
hedge: it damps small-sample luck and gives a smooth ramp off the zero-weighted seed,
rather than optimizing accuracy. `K = 5` is a shared constant rather than a per-axis
fitted threshold. Without *any* reliability handling, ~⅓ of the board (seeded-OTR
players) effectively had pp counted ~twice, and a single match flipped a near-seed
rating to full weight. The weighting leaves the top of the board virtually unchanged
while correcting the seeded and thin-record mid-board.

**Why not hand-pick a two-axis split?** Renormalizing the base weights is
the *only* rule for a player missing an axis: it keeps one formula
for every case and leaves the taper intact. Hard-coding a separate split (say
forcing `0.4 / 0.6`) would re-open the thin-rating loophole the taper just closed,
since a one- or two-match rating would snap back to a large fixed share. It would
also lean *harder* on a lone competitive axis that, having no second axis to
corroborate it, warrants more caution, not less.

### Anchor modes

The **anchor** decides which board defines the player set:

- **`--anchor union` (default)** — the player set is (**PP top-10k**) ∪
  (**Ranked Play top-10k**) ∪ (**OTR top-10k**), the most complete board: each of
  the three skill axes contributes its own elite pool, so a player strong on *any*
  one of them is surfaced (the OTR pool adds tournament players who don't grind PP
  or queue ranked play). A player is kept if they carry at least one *real*
  competitive rating (a real Elo **or** a real OTR). Pure-PP accounts with neither
  are dropped. Every kept player then gets all three axes: PP from the bulk board
  or a per-player lookup (fast via `--osu-api`, else a per-profile HTML fetch), Elo
  used at its own posterior value (or a zero-weighted PP-seed when absent, see
  below), and OTR (real or rank-seeded). The full union (~20k) is scored, then the best
  **10,000** are shown (override with `--top-k`). `--top` is ignored in this mode.
- **`--anchor rankedplay`** — take the top-N **ranked-play** players, then look up
  each one's pp value (bulk PP board, else a **per-profile** fetch of
  `statistics.global_rank` + `statistics.pp`). Players with **no pp value** are
  skipped. No seeding. Obeys `--min-plays`.
- **`--anchor pp`** — take the top-N **PP** players (hard-capped at 10k, see
  below), blend in elo rating from the bulk ranked-play board. Players with **no
  elo rating** are skipped. No seeding. Obeys `--min-plays`.

The `rankedplay`/`pp` anchors are simpler, single-pool boards retained for
comparison. The **union** anchor is what the published site uses.

### OTR tournament rating (`--otr`)

The third axis is **OTR** (osu! Tournament Rating), an OpenSkill / Plackett-Luce
rating built from verified tournament results, a real measure of tournament
performance, replacing the old badge-count heuristic.

```
--otr <key>     # fetch real OTR ratings (API key required)
--otr           # bare form: read the key from the OTR_API_KEY environment variable
(omitted)       # no API call — every OTR rating is seeded from osu! rank
```

**Getting a key:** sign in at [otr.stagec.net](https://otr.stagec.net) with your
osu! account and create an API key (up to 3). Pass it as `--otr <key>` or export
it as `OTR_API_KEY` and use a bare `--otr`. **The key is sent only as a Bearer
header and is never written to disk or the CSV. Keep it out of git.**

Real ratings come from a single paginated sweep of the public **OTR leaderboard**
(`GET /api/leaderboard`, ~267 pages / ~27k players), joined to our players by osu!
id, a fixed cost regardless of board size. The sweep is cached for 1 week. The OTR
API shares one rate limit across endpoints, so it is paced and self-heals on 429.

**Coverage & the rank-seeded fallback.** OTR only rates players who have competed
in verified tournaments, about **two-thirds** of this board (the rest of osu! has
none). Everyone else gets an OTR rating **seeded from their osu! rank** using OTR's
own initial-rating formula (`otr-processor`'s `mu_from_rank`, osu! ruleset):

```
z  = (ln(rank) - 9.99) / 1.77
mu = 1200 - (z>0 ? 250 : 200)·z          # clamped to [500, 2000]
```

This is the rating OTR would assign *before any tournament play*. Seeded players
are marked `otr_estimated=yes` in the CSV and with a `~` on the website, and
their `tournaments_played` is 0. Note: a seeded OTR is a deterministic function
of rank, so for those players the OTR axis adds little beyond PP. Real entries
also carry the player's OTR **global rank** (`otr_rank`), used for the site's
`vs otr` column. The whole sweep caches under `.cache/otr/` for 1 week.

### Fast pp lookups via the osu! API (`--osu-api`)

`--osu-api` routes the two pp data needs through the official osu! API v2 instead
of HTML scraping:

1. **The bulk PP top-10k board** — `GET /api/v2/rankings/{mode}/performance`
   (structured JSON, no brittle HTML parsing). Same top-10k cap and 50/page; cached
   as one file under `.cache/pp_api/`.
2. **pp for the ~10k players outside that board** (rp/OTR recruits) — the **batch**
   endpoint `GET /api/v2/users?ids[]=…`, up to **50 players per request** with
   `statistics_rulesets` (`global_rank` + `pp`) — turning ~10k profile scrapes into
   ~200 calls. Note: the osu! API throttle is **1,200 cost-units/min** and `/users`
   charges **one unit per id** (a 50-id call costs 50), so these calls are paced to
   ~2.7 s apart (`OSU_USERS_MIN_INTERVAL`) to stay under budget (~10 min for the
   full ~10k), still far better than hours of HTML scraping.

```
--osu-api       # PP board + pp lookups via the osu! API; needs OSU_CLIENT_ID + OSU_CLIENT_SECRET
(omitted)       # falls back to HTML scraping (bulk pp pages + one profile per recruit; slow, no key)
```

(The ranked-play **Elo** board has no API equivalent: its matchmaking rating isn't
exposed anywhere in the osu! API, so it is always HTML-scraped.)

**Getting credentials:** at [osu.ppy.sh/home/account/edit](https://osu.ppy.sh/home/account/edit)
→ **OAuth** → *New OAuth Application* (callback URL can be blank). Export the pair:

```powershell
[Environment]::SetEnvironmentVariable('OSU_CLIENT_ID','<id>','User')
[Environment]::SetEnvironmentVariable('OSU_CLIENT_SECRET','<secret>','User')
```

A `client_credentials` ("guest") token with `scope=public` is fetched at runtime.
**The secret is read from the environment only: never written to disk, the CSV,
or git, and never logged (only its length is printed).** Cached pp values are
shared with the HTML path, so the two are interchangeable.

### Reliability weighting (Elo & OTR)

osu!'s Ranked-Play "Elo" is not a raw number waiting to be corrected. It is an **OpenSkill
(Plackett-Luce) posterior seeded from a PP estimate** at account creation, then
Bayesian-updated per match. So a low-match Elo already sits near its PP seed and drifts
to the player's own level as games accrue, the same kind of object as OTR (seeded
from rank). The **union** anchor therefore does not shrink or discard it. It **uses the
Elo at its own posterior value** and puts all the reliability handling in the *weight*:

```
prior = a + b·ln(pp)     # PP→Elo fit on STABLE (≥10-match) players; used only to SEED
                         # an absent Elo (a player who qualifies on OTR alone)
w_elo = W_ELO · plays / (plays + K)      # K = 5; the real Elo's weight, tapered by matches
```

> **Source — osu!'s own matchmaking code, verified 2026-07-02.** Every claim above is
> in [`ppy/osu-server-spectator`](https://github.com/ppy/osu-server-spectator) (`master`):
> - *OpenSkill engine (not Elo-MMR)* — [`osu.Server.Spectator.csproj` L18](https://github.com/ppy/osu-server-spectator/blob/master/osu.Server.Spectator/osu.Server.Spectator.csproj#L18): `<PackageReference Include="OpenSkillSharp" Version="1.1.0" />`.
> - *PP seed (the prior μ)* — [`MatchmakingQueueBackgroundService.cs` L527–538](https://github.com/ppy/osu-server-spectator/blob/master/osu.Server.Spectator/Hubs/Multiplayer/Matchmaking/Queue/MatchmakingQueueBackgroundService.cs#L527-L538): a first-time queuer's `InitialRating` **and** live `Rating` are set to `eloEstimate = -4000 + 600·ln(pp + 4000)`.
> - *Per-match Bayesian update* — [`RankedPlayMatchController.cs` L387–439](https://github.com/ppy/osu-server-spectator/blob/master/osu.Server.Spectator/Hubs/Multiplayer/Matchmaking/RankedPlay/RankedPlayMatchController.cs#L387-L439): each ranked match feeds every player's stored μ/σ into `PlackettLuce.Rate(...)` and writes back the resulting posterior.
>
> These link `master`, so **line numbers may drift** and osu! may change the model — treat
> this as osu!'s ranked-play rating **as it stood on 2026-07-02**.

A real Elo is used at its reported value. A player with **no real Elo** gets the
`prior` as a **zero-weighted** seed value (it only feeds that axis's normalization,
never the player's own blend). Its weight ramps from ~0 at the seed up to nearly full
as matches accrue, so one rule covers all cases: real-and-deep, real-but-thin, and
absent. CSV flag: `elo_estimated=yes` (no real Elo, the value is the seed). The seed
prior's coefficients live in the meta sidecar (`elo_prior`), and the taper constant is
`elo_reliability_k`.

**Does Elo carry real skill signal, and why K = 5?** Using **OTR as an independent
yardstick** (it shares no data with Ranked Play or PP), on the players who carry both a
real Elo and a real OTR:

- A real Elo's agreement with OTR **climbs with match count**: a thin Elo (1–4 matches)
  predicts OTR **no better than a pure PP guess** (a statistical dead heat, with the
  Williams test nowhere near significant), but by **≥5 matches** it pulls clearly ahead (the
  Elo↔OTR correlation rises from about **0.5** to about **0.7**, Fisher `p < 10⁻¹⁰`).
  So Elo earns its place as an independent axis.
- But `K = 5` is a **conventional reliability constant rather than a fitted threshold.** In the
  actual 3-axis blend, **full weight is the most accurate choice**: a real Elo and PP
  are complementary, so down-weighting a thin Elo just leans on PP (which the seed
  already duplicates), and grid-searching the *weight* taper to best predict OTR peaks
  at `K → 0` (no taper), declining monotonically as `K` grows. The taper is a
  conventional hedge (small-sample luck plus a smooth ramp off the
  zero-weighted seed), costing a hair of aggregate accuracy by choice.

**Reproduce it yourself** (no OTR key or network needed):

```
python analysis/elo_reliability.py
```

This recomputes the bucketed Elo↔OTR correlations, the Williams/Fisher tests and the `K`
grid search from a **frozen, timestamped snapshot** in `analysis/snapshots/` rather than the
live `docs/` board (which is overwritten on every weekly refresh), so the proof always
reproduces the exact dataset it was written against. Figures shift slightly between
snapshots, but the qualitative result (Elo's signal grows with matches) is stable. After a
refresh you can freeze a fresh
snapshot by copying `docs/hybrid_leaderboard.csv` and its `.meta.json` into
`analysis/snapshots/` with the generation date in the filename (the script then picks
the newest automatically).

**Elo and OTR are treated identically, because they are the same kind of thing.** Both
are OpenSkill posteriors seeded from a prior (Elo from PP, OTR from rank) that washes out
with evidence, so neither rating's **value** is ever edited and all reliability handling
lives in the **weight**: a thin rating keeps its exact number but *counts for less* — Elo
by `plays/(plays+5)`, OTR by `matches/(matches+5)` — until enough play backs it. There is
no "raw Elo vs Bayesian OTR" asymmetry to justify, because *both* are already Bayesian,
seeded posteriors. One caveat: `5` is a shared, conventional constant rather than a
per-axis fitted value. The accuracy-optimal weight is close to *full* for both, and
there is no non-circular way to fit a separate reliability point per axis, so
`evidence/(evidence+5)` is a reasonable, cautious default rather than a measured threshold.

> **Open question.** On the current snapshot OTR's own rank-seed out-predicts a *raw
> thin* OTR out to ~20+ tournament matches, so in isolation OTR's reliability half-point
> looks far higher than 5. In the blend that is a red herring: the seed ≈ PP (already an
> axis), so what the OTR axis uniquely adds is its raw value at near-full weight, which
> is why `K_OTR = 5` is retained. Whether OTR deserves a heavier taper than Elo is left
> as a separate, unresolved tuning question.

### Data-quality filters

All filters below are **off by default**. When set, they apply to **every anchor**
(including the default `union` board) and run **before** scoring, so the surviving
players are normalized and ranked against each other. Left unset, the union anchor
leans on reliability weighting rather than cutting.

| Flag | Default | Effect |
|---|---|---|
| `--top-k K` | union: `10000`, else off | After scoring, keep only the best **K** players (a presentation trim, applied after everything else). The union anchor defaults this to 10,000 (osu! only ranks the top 10k anyway). |
| `--exclude-provisional` | off | **(all anchors)** Drop players whose rating osu! flags as **provisional** ("too few recent matches"). Off by default, so provisional players are otherwise **kept and marked**. |
| `--min-plays N` | `0` (off) | **(all anchors)** Drop players with fewer than **N** ranked-play matches. A seeded Elo has 0 plays, so on the union board any **N ≥ 1** also drops OTR-only players. Left unset, the union anchor weight-tapers low-play Elos instead of cutting them. |
| `--min-otr-matches N` | `0` (off) | **(all anchors)** Keep only players with a **real OTR** rating backed by **≥ N** tournament matches, dropping seeded and thin-OTR players for a tournament-focused board. Applied **before** normalization, so survivors are scored against this cohort. (The taper already down-weights thin OTR; this hard-excludes it.) |
| `--exclude-seeded` | off | **(all anchors)** Keep only players whose **Elo and OTR are both real**, dropping any pp-seeded Elo or rank-seeded OTR (pp is always real). Applied **before** normalization, so survivors are scored against this fully-backed cohort. |

The ranked-play board exposes each player's **play count**, **provisional flag**,
and **elo rating** in bulk (no extra fetch), so these cost nothing. `plays` and
`provisional` are written to every CSV regardless of whether you filter on them.

### Usage

```
python hybrid_rank.py --otr <key> --osu-api                      # the published board (union, real OTR, fast pp)
python hybrid_rank.py                                            # union, OTR all seeded (no key)
python hybrid_rank.py --offline                                  # pure recompute (weight tweaks); reuse cache, no network
python hybrid_rank.py --offline --w-pp 0.4 --w-elo 0.3           # try different weights (OTR gets the remainder)
python hybrid_rank.py --offline --top-k 1000                     # show only the best 1000
python hybrid_rank.py --offline --min-otr-matches 5             # tournament-only: real OTR with >=5 matches
python hybrid_rank.py --offline --exclude-seeded                # only players with all three axes real (pp+elo+otr)
python hybrid_rank.py --offline --min-plays 20                  # require >=20 real ranked-play matches
python hybrid_rank.py --offline --exclude-seeded --min-plays 20 --min-otr-matches 10 --exclude-provisional  # strict, fully-backed board
python hybrid_rank.py --anchor rankedplay --top 10000 --otr <key># legacy: ranked-play-only board
python hybrid_rank.py --anchor pp --top 10000                    # legacy: PP-only board (the PP max -- see cap)
python hybrid_rank.py --no-cache                                 # force a fresh pull
python hybrid_rank.py --show 50                                  # print more rows
```

`--offline` reuses any cached file regardless of age and never hits the network
(errors if something needed isn't cached), so changing the weights or the score
formula re-ranks in seconds instead of re-scraping. The normal cache TTL is 1 week.
(Real OTR ratings still require a network fetch the first time; once cached they
recompute offline too.)

Union-mode cost (cold), all three axes capped at **top-10k**: ~200 ranked-play
pages (RP top-10k) + 200 PP pages + a pp lookup per kept player **outside** the PP
top-10k (~10k, incl. OTR recruits) + the ~267-page OTR sweep. With `--osu-api` the
PP board and the pp lookups both use the osu! API (the lookups batched 50-at-a-time
→ ~200 calls). Without it both are HTML-scraped. At the polite **1 request/second**
cap (osu! & OTR both ask for ≤60/min), plus the `/users` calls paced to ~2.7 s for
the osu! API cost budget, that's roughly **~15–20 min** cold. Re-runs are
near-instant from cache, and weight/formula tweaks use `--offline`.

> **Speed vs. completeness.** The RP scan is capped at RP top-10k by default
> (`RP_RANK_CAP`); a player ranked beyond that gets a *seeded* Elo instead of their
> scanned one. Pass `--rp-max-pages <N>` to scan deeper (the full board is ~2,000
> pages / ~33 min) if you want real Elos for lower-ranked pp/OTR players.

Output: `hybrid_leaderboard.csv` with columns `hybrid_rank, user_id, username,
pp_rank, pp, elo_rank, elo_rating, elo_estimated, otr_rank,
otr_rating, otr_estimated, tournaments_played, matches_played, plays, provisional,
hybrid_score`. `elo_rating` is the player's own Ranked-Play posterior (its value is
used as-is); when a player has no real Elo it holds the zero-weighted PP-seed and
`elo_estimated=yes` (and `elo_rank` is blank). `plays` is the ranked-match count that
sets the Elo reliability weight. `matches_played` is the verified OTR match count
(0 when seeded) that sets the OTR reliability weight. The `*_estimated`/`provisional`
flags are `yes` or blank. A sidecar `<name>.meta.json` records the generation time, the
three weights, the per-axis normalization params, the reliability constants
(`elo_reliability_k`, `otr_reliability_k`), the real-vs-estimated OTR/Elo counts, the
Elo seed prior (`elo_prior`), the anchor, and the active filters. If the CSV is open in
Excel a numbered sibling is written.

### Reading the deltas: what a big `vs pp` jump means

The three **delta** columns (`vs pp`, `vs elo`, `vs otr`) show how many places a
player's hybrid rank beats (green ▴) or trails (red ▾) that one axis's rank alone. A
large `vs pp` value can look alarming (**+100,000 or more**), but it is the board
working as designed rather than a low-confidence artifact.

Because the **union anchor** recruits players by their *competitive* standing (the
ranked-play/Elo top-10k and the OTR leaderboard), a strong tournament or matchmaking
player who simply doesn't farm PP is pulled onto the board despite a PP rank in the six
figures. Their `vs pp` is then enormous, and that gap *is* the signal: PP badly
understates them, which is the whole reason the board exists.

**The biggest jumps belong to the most-confident competitive players, not the
tail.** The largest `vs pp` values consistently come from players with a *deep* verified
tournament record (dozens of OTR matches) rather than thin, single-axis entries. They
need no special protection: even a strict tournament-match floor that drops most of the
board still keeps these top jumps. The low-confidence players (a single thin
axis, two or three matches) sit near the **bottom** of the board with *much smaller*
deltas, since a thin axis is down-weighted and their score leans on PP.

So read a large `vs pp` as "PP badly understates this player," not as an error. Trimming
those rows away would delete the board's most distinctive output. If you want
a board without the low-PP tournament crowd, that is what `--min-otr-matches` is
for.

### Website (GitHub Pages)

The repo ships a dependency-free static site in [`docs/`](docs/): an
`index.html` + `app.js` that fetch the committed CSV and render a **searchable,
sortable** table (search by username, click any column header to sort). Three
**delta** columns show how a player's hybrid rank compares to each axis alone:
`vs pp`, `vs elo`, `vs otr` (green ▴ gained, red ▾ lost; `—` when that axis is
seeded). No backend, no build step, no tracking. Every real Elo is weight-tapered by
its match count (its value shown as-is), so rather than a per-row symbol the **Elo
number is itself a hover target** (shows the match count behind it). Only the
categorical states carry a mark: **`*`** provisional (osu!'s own flag) and **`^`** no
real Elo (the value is the PP seed). OTR estimates from rank are marked **`~`**. A second **Calculator** tab computes a hybrid score
from a raw PP, Elo, and OTR. It pulls the published board's per-axis mean/std from the
meta sidecar, so with the default weights it reproduces exactly what the board
computed. The three weights are pre-filled with the board's split but **editable**,
so you can see how a different PP/Elo/OTR balance would score a player (with a
one-click reset back to the board weights).

**Enable it:** push the repo to GitHub → *Settings → Pages → Build from a
branch* → branch `main`, folder `/docs`. The board goes live at
`https://<user>.github.io/<repo>/`.

**Refresh the published data** (manual, you control the scrape rate):

```
python hybrid_rank.py --anchor union --otr <key> --osu-api --out docs/hybrid_leaderboard.csv  # ~15 min first time (10k caps)
git add docs/hybrid_leaderboard.csv docs/hybrid_leaderboard.meta.json && git commit -m "refresh leaderboard" && git push
```

The site reads `docs/hybrid_leaderboard.csv`, so the CSV must live **inside**
`docs/` (Pages only serves the publish folder). Root-level
`hybrid_leaderboard*.csv` outputs are git-ignored so throwaway runs don't clutter
the repo.

### Hard cap: top 10,000

osu!'s **public PP leaderboard is capped at the top 10,000** (page 200). Deeper
pages just repeat page 200. The **union** anchor draws from three 10k pools (the
PP top-10k, the ranked-play top-10k, **and the OTR top-10k**), so a player must sit
inside at least one of the three to be considered (PP for a player outside the bulk
board is then fetched via the osu! API batch endpoint with `--osu-api`, else
per-profile via `statistics.global_rank` / `statistics.pp`). A player ranked
outside **all three** pools never appears, even if their hybrid score would place
them, so every hybrid rank is a standing *within this union sample* rather than a true
global one. (Adding the OTR pool closes the old blind spot where a tournament-only
player who didn't grind PP or queue ranked play couldn't appear at all.)

### Scale & politeness

- Union cost (all three axes capped at top-10k): ~200 ranked-play pages + the PP
  top-10k board + pp lookups for kept players outside it (`--osu-api` batches the
  lookups and serves the PP board from the rankings API; else both are HTML) + the
  ~267-page OTR sweep. `RP_RANK_CAP` / `OTR_RANK_CAP` / `PP_RANK_CAP` set the caps;
  `--rp-max-pages` scans the RP board deeper at the cost of time.
- `MIN_INTERVAL` (default **1.0 s**, the global minimum seconds between request
  *starts*) caps the whole app at **≤60 requests/min**, honoring both the osu! and
  OTR terms of use (~1 req/s). `CONCURRENCY` (default 5) only overlaps latency. The
  shared throttle still paces starts to `MIN_INTERVAL`, so the rate never exceeds
  1/s. Both are at the top of the script.
- Pages are cached under `.cache/` for 1 week, so re-runs are near-instant.

### Notes
- Pure standard library — no `pip install`.
- Tune the weights **`W_PP`** and **`W_ELO`** (`W_OTR = 1 - W_PP - W_ELO` is
  derived), the reliability constants **`ELO_RELIABILITY_K`** / **`OTR_RELIABILITY_K`**,
  plus `MODE` at the top of `hybrid_rank.py`, or pass `--w-pp` / `--w-elo` on the
  command line.
- The OTR rating model + constants are documented inline where `otr_seed_from_rank`
  / `fetch_otr_leaderboard` are defined. They mirror `osu-tournament-rating/otr-processor`.

---

*Parts of this app were vibe-coded or edited with the help of AI.*
