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
- base weights `W_PP = 0.33`, `W_ELO = 0.34`, `W_OTR = 0.33` (defaults) → `score = 0.33·z(log pp) + 0.34·z(elo) + 0.33·z(otr)` for all-real players; a **seeded** axis (estimated Elo/OTR) is dropped to zero weight and its share redistributed to the player's real axes, and a real OTR is further tapered by its tournament-match count ([reliability weighting](#formula))
- mode: `osu` standard

A player appears if they carry at least one **real competitive rating** — a real
Elo (they've queued ranked play) **or** a real OTR (they've played a verified
tournament). Pure-PP accounts with neither are dropped (they'd collapse the blend
to raw PP). The union is ~20k players; the board scores all of them and shows the
**best 10,000**. There is **no hard min-plays cutoff**: low-play Elos are
*statistically shrunk* toward their PP-predicted value instead of discarded (see
[Low-play Elo shrinkage](#low-play-elo-shrinkage)). Provisional ratings are
**kept** and marked, not dropped.

---

## Introduction

Builds an osu! **hybrid global leaderboard** that blends three skill signals on a
single, **normalized** scale — so it measures *magnitude*, not just ordinal place:

| Component | Source | Notes |
|---|---|---|
| **PP performance** | osu! API v2 `rankings`/`users` with `--osu-api`, else `osu.ppy.sh/rankings/{mode}/global` (bulk) or profile HTML | raw pp value; bulk board capped at top 10k |
| **Elo rating** | `osu.ppy.sh/rankings/ranked-play/{mode}/{pool}` | osu!'s matchmaking rating (mu); pool id + last page auto-detected |
| **OTR rating** | [otr.stagec.net](https://otr.stagec.net) public API | osu! Tournament Rating; rank-seeded estimate when a player has no tournament history |

### Formula

Each axis is **standardized** across the board population (z-score), then blended:

```
z(x)         = (x - mean) / std        # mean/std measured over the whole board
hybrid_score = w_pp·z(log pp) + w_elo·z(elo) + w_otr·z(otr)   # higher = better
               base weights: W_PP = 0.33, W_ELO = 0.34, W_OTR = 0.33
```

PP is **logged** before standardizing because it is heavily right-skewed. Sorted
**descending** by `hybrid_score`. Ties break deterministically by elo rating,
then pp, then user_id. The per-axis `mean`/`std` are recorded in the `.meta.json`
sidecar so the website calculator can reproduce any score exactly. The
normalization population is the **whole board, seeded placeholders included**: a
seeded Elo/OTR is zero-weighted in its *own* player's blend (above) but still
contributes to that axis's `mean`/`std`, so it helps define the scale every real
rating is standardized against. Seeds are therefore kept in the data rather than
blanked; dropping them would shift each axis's baseline and re-rank the board.

**Reliability weighting (per player).** The weights `w_pp/w_elo/w_otr` equal the
base `W_PP/W_ELO/W_OTR` only when all three axes are *real*. A **seeded** axis
carries no independent signal — a seeded OTR is a deterministic transform of osu!
rank (corr ≈ 0.995 with pp) and a seeded Elo *is* the PP-prior — so weighting it
like a real measurement just double-counts pp. Any seeded axis is therefore given
**zero** weight and its share is redistributed proportionally to the player's real
axes (e.g. a player with a real Elo but a seeded OTR is scored
`0.49·z(log pp) + 0.51·z(elo)`). Every board player has at least one real
competitive axis, so the real weights never sum to zero. A **real** OTR is
additionally tapered by how much tournament play backs it — its weight scales as
`matches / (matches + 5)`, so a one- or two-match rating (barely nudged off its
rank-seed, which is ≈ pp) leans mostly on pp and Elo, while a deep tournament
record earns close to its full share. A seeded OTR is just the matches = 0 limit
of that taper, so the rule is continuous across the seed boundary. (Elo needs no
weight taper — its thin-sample noise is already handled by shrinking the *value*
toward the PP-prior; see below.) Without this, ~⅓ of the board (the seeded-OTR
players) effectively had pp counted ~twice, and a single tournament match flipped a
near-seed rating to full weight; the fix leaves the top of the board virtually
unchanged while correcting the seeded and thin-record mid-board.

**Why not hand-pick a two-axis split?** Renormalizing the base weights is
deliberately the *only* rule for a player missing an axis: it keeps one formula
for every case and leaves the taper intact. Hard-coding a separate split (say
forcing `0.4 / 0.6`) would re-open the thin-OTR loophole the taper just closed,
since a one- or two-match rating would snap back to a large fixed share; it would
also lean *harder* on a lone competitive axis that, having no second axis to
corroborate it, warrants more caution, not less.

### Anchor modes

The **anchor** decides which board defines the player set:

- **`--anchor union` (default)** — the player set is (**PP top-10k**) ∪
  (**Ranked Play top-10k**) ∪ (**OTR top-10k**), the most complete board: each of
  the three skill axes contributes its own elite pool, so a player strong on *any*
  one of them is surfaced (the OTR pool adds tournament players who don't grind PP
  or queue ranked play). A player is kept if they carry at least one *real*
  competitive rating (a real Elo **or** a real OTR); pure-PP accounts with neither
  are dropped. Every kept player then gets all three axes: PP from the bulk board
  or a per-player lookup (fast via `--osu-api`, else a per-profile HTML fetch), Elo
  **shrunk** toward its PP-prior (or seeded from PP when absent — see below), and
  OTR (real or rank-seeded). The full union (~20k) is scored, then the best
  **10,000** are shown (override with `--top-k`). `--top` is ignored in this mode.
- **`--anchor rankedplay`** — take the top-N **ranked-play** players, then look up
  each one's pp value (bulk PP board, else a **per-profile** fetch of
  `statistics.global_rank` + `statistics.pp`). Players with **no pp value** are
  skipped. No shrinkage/seeding; obeys `--min-plays`.
- **`--anchor pp`** — take the top-N **PP** players (hard-capped at 10k, see
  below), blend in elo rating from the bulk ranked-play board. Players with **no
  elo rating** are skipped. No shrinkage/seeding; obeys `--min-plays`.

The `rankedplay`/`pp` anchors are simpler, single-pool boards retained for
comparison; the **union** anchor is what the published site uses.

### OTR tournament rating (`--otr`)

The third axis is **OTR** (osu! Tournament Rating), an OpenSkill / Plackett-Luce
rating built from verified tournament results — a real measure of tournament
performance, replacing the old badge-count heuristic.

```
--otr <key>     # fetch real OTR ratings (API key required)
--otr           # bare form: read the key from the OTR_API_KEY environment variable
(omitted)       # no API call — every OTR rating is seeded from osu! rank
```

**Getting a key:** sign in at [otr.stagec.net](https://otr.stagec.net) with your
osu! account and create an API key (up to 3). Pass it as `--otr <key>` or export
it as `OTR_API_KEY` and use a bare `--otr`. **The key is sent only as a Bearer
header and is never written to disk or the CSV — keep it out of git.**

Real ratings come from a single paginated sweep of the public **OTR leaderboard**
(`GET /api/leaderboard`, ~267 pages / ~27k players), joined to our players by osu!
id — a fixed cost regardless of board size. The sweep is cached for 1 week; the OTR
API shares one rate limit across endpoints, so it is paced and self-heals on 429.

**Coverage & the rank-seeded fallback.** OTR only rates players who have competed
in verified tournaments — about **two-thirds** of this board (the rest of osu! has
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
   ~2.7 s apart (`OSU_USERS_MIN_INTERVAL`) to stay under budget — ~10 min for the
   full ~10k, still far better than hours of HTML scraping.

```
--osu-api       # PP board + pp lookups via the osu! API; needs OSU_CLIENT_ID + OSU_CLIENT_SECRET
(omitted)       # falls back to HTML scraping (bulk pp pages + one profile per recruit; slow, no key)
```

(The ranked-play **Elo** board has no API equivalent — its matchmaking rating isn't
exposed anywhere in the osu! API — so it is always HTML-scraped.)

**Getting credentials:** at [osu.ppy.sh/home/account/edit](https://osu.ppy.sh/home/account/edit)
→ **OAuth** → *New OAuth Application* (callback URL can be blank). Export the pair:

```powershell
[Environment]::SetEnvironmentVariable('OSU_CLIENT_ID','<id>','User')
[Environment]::SetEnvironmentVariable('OSU_CLIENT_SECRET','<secret>','User')
```

A `client_credentials` ("guest") token with `scope=public` is fetched at runtime.
**The secret is read from the environment only — never written to disk, the CSV,
or git, and never logged (only its length is printed).** Cached pp values are
shared with the HTML path, so the two are interchangeable.

### Low-play Elo shrinkage

A ranked-play Elo built on a handful of matches is noisy. The **union** anchor does
not discard those players (a hard min-plays cutoff throws away real signal) nor
trust them blindly. Instead it **shrinks** every Elo toward the rating its PP
predicts, weighted by how many matches back it:

```
prior = a + b·ln(pp)            # PP→Elo fit on STABLE (≥10-match) players only
elo    = prior + (raw_elo − prior) · n / (n + K)      # K = 5, n = match count
```

With one match the Elo is pulled most of the way to its PP-expected value; by ~25
matches it is almost entirely the player's own. A player with **no Elo at all** is
simply the `n = 0` limit of the same formula (their Elo equals the prior) — that is
how union members who qualify on OTR alone get an Elo. So one expression covers all
three cases: real-and-trusted, real-but-noisy, and absent. The shrunk value is what
feeds the score and is shown in the **Elo** column; the raw osu! rating is kept in
the CSV (`elo_raw`) and the site tooltip. CSV flags: `elo_estimated=yes` (no real
Elo, value is the seed), `elo_shrunk=yes` (real Elo adjusted by ≥15 points). The
prior's coefficients and `K` are recorded in the meta sidecar (`elo_prior`).

**Why K = 5 (and why 5 is statistically meaningful).** Using **OTR as an
independent yardstick** for competitive skill (it shares no inputs with Elo or PP),
on the ~5,400 players who have both a real Elo and a real OTR:

- Grid-searching the shrinkage weight `n/(n+K)` to best predict OTR peaks flat at
  **K ≈ 4–5**. The blended Elo (corr **0.62** with OTR for low-play players) beats
  *both* the raw low-play Elo (0.56) and a pure PP guess (0.56) — combining the two
  complementary signals recovers more skill than either alone.
- Below **5 matches**, a real Elo predicts OTR **no better than PP does**
  (r = 0.559 vs 0.563 — a statistical dead heat; Williams dependent-correlation
  test **p = 0.84**). At **≥5 matches** it pulls clearly ahead (r = 0.704; Fisher
  r-to-z **z = 8.3, p < 10⁻¹⁵**).
- So 5 is the match count at which one match's worth of evidence equals the PP
  prior — exactly the right half-weight point for the shrinkage.

This was also checked *per* play-count bucket: shrinkage improves the Elo↔OTR
correlation at **every** level (1–4: +0.056 … 50+: +0.004), so it is applied
continuously to all players rather than gated at a threshold — its effect simply
fades as match counts grow, so well-measured ratings move only slightly.

**OTR is deliberately *not* shrunk this way — it doesn't need it.** Unlike osu!'s
raw Ranked-Play Elo, OTR is already a Bayesian rating seeded from osu! rank and
tempered by its own volatility, so a low-tournament rating is *already* shrunk
toward that prior internally. Pulling it toward PP on top of that would
double-count the prior and bleed the tournament axis into PP, so the OTR rating is
used as-is (real value, or our rank-seed when the player has no real OTR). This is a
different lever from the **reliability taper** above, and the two are not in tension:
*shrinkage* adjusts a rating's **value** (a low-play Elo's number is pulled toward its
PP estimate), whereas the taper adjusts a rating's **weight** (a thin OTR keeps its
exact number but counts for less in the blend). So a thin OTR is *down-weighted, never
re-valued* — its number is left untouched; only its share of the score scales with
match count.

### Data-quality filters

The **union** anchor handles Elo noise with shrinkage (above), not a cutoff, so it
takes the cap and provisional knobs but ignores `--min-plays`. The two legacy
anchors (`pp`/`rankedplay`) honor all three. All are **off by default**.

| Flag | Default | Effect |
|---|---|---|
| `--top-k K` | union: `10000`, else off | After scoring, keep only the best **K** players — a presentation trim, not a re-ranking. The union anchor defaults this to 10,000 (osu! only ranks the top 10k anyway). |
| `--exclude-provisional` | off | Drop players whose rating osu! flags as **provisional** ("too few recent matches"). Off by default — provisional players are **kept and marked** instead. |
| `--min-plays N` | `1` (off) | **(pp/rankedplay only)** Drop players with fewer than **N** ranked-play matches. The union anchor shrinks low-play Elos instead, so this does nothing there. |
| `--min-otr-matches N` | `0` (off) | **(all anchors)** Keep only players with a **real OTR** rating backed by **≥ N** tournament matches — drops seeded and thin-OTR players for a tournament-focused board. Applied **before** normalization, so survivors are scored against this cohort, not the full board. (The taper already down-weights thin OTR; this hard-excludes it.) |

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
python hybrid_rank.py --anchor rankedplay --top 10000 --otr <key># legacy: ranked-play-only board
python hybrid_rank.py --anchor pp --top 10000                    # legacy: PP-only board (the PP max -- see cap)
python hybrid_rank.py --no-cache                                 # force a fresh pull
python hybrid_rank.py --show 50                                  # print more rows
```

`--offline` reuses any cached file regardless of age and never hits the network
(errors if something needed isn't cached) — so changing the weights or the score
formula re-ranks in seconds instead of re-scraping. The normal cache TTL is 1 week.
(Real OTR ratings still require a network fetch the first time; once cached they
recompute offline too.)

Union-mode cost (cold), all three axes capped at **top-10k**: ~200 ranked-play
pages (RP top-10k) + 200 PP pages + a pp lookup per kept player **outside** the PP
top-10k (~10k, incl. OTR recruits) + the ~267-page OTR sweep. With `--osu-api` the
PP board and the pp lookups both use the osu! API (the lookups batched 50-at-a-time
→ ~200 calls); without it both are HTML-scraped. At the polite **1 request/second**
cap (osu! & OTR both ask for ≤60/min) — plus the `/users` calls paced to ~2.7 s for
the osu! API cost budget — that's roughly **~15–20 min** cold. Re-runs are
near-instant from cache; weight/formula tweaks use `--offline`.

> **Speed vs. completeness.** The RP scan is capped at RP top-10k by default
> (`RP_RANK_CAP`); a player ranked beyond that gets a *seeded* Elo instead of their
> scanned one. Pass `--rp-max-pages <N>` to scan deeper (the full board is ~2,000
> pages / ~33 min) if you want real Elos for lower-ranked pp/OTR players.

Output: `hybrid_leaderboard.csv` with columns `hybrid_rank, user_id, username,
pp_rank, pp, elo_rank, elo_rating, elo_raw, elo_estimated, elo_shrunk, otr_rank,
otr_rating, otr_estimated, tournaments_played, matches_played, plays, provisional,
hybrid_score`. `elo_rating` is the **shrunk** value used in scoring; `elo_raw` is
the player's pre-shrink osu! Elo (blank when seeded); `elo_rank` is blank when the
Elo is seeded. `matches_played` is the verified OTR match count (0 when seeded) and
sets the OTR reliability weight. The `*_estimated`/`elo_shrunk`/`provisional` flags
are `yes` or blank. A sidecar `<name>.meta.json` records the generation time, the
three weights, the per-axis normalization params, the OTR reliability constant
(`otr_reliability_k`), the real-vs-estimated OTR/Elo counts, the shrinkage prior
(`elo_prior`), the anchor, and the active filters. If the CSV is open in
Excel a numbered sibling is written.

### Reading the deltas: a big `vs pp` jump is signal, not noise

The three **delta** columns (`vs pp`, `vs elo`, `vs otr`) show how many places a
player's hybrid rank beats (green ▴) or trails (red ▾) that one axis's rank alone. A
large `vs pp` value can look alarming — **+100,000 or more** — but it is the board
working as designed, not a low-confidence artifact.

Because the **union anchor** recruits players by their *competitive* standing (the
ranked-play/Elo top-10k and the OTR leaderboard), a strong tournament or matchmaking
player who simply doesn't farm PP is pulled onto the board despite a PP rank in the six
figures. Their `vs pp` is then enormous — and that gap *is* the signal: PP badly
understates them, which is the whole reason the board exists.

Crucially, **the biggest jumps belong to the most-confident competitive players, not the
tail.** The largest `vs pp` values consistently come from players with a *deep* verified
tournament record — dozens of OTR matches — rather than thin, single-axis entries. They
need no special protection: even a strict tournament-match floor that drops most of the
board still keeps these top jumps. The genuinely low-confidence players — a single thin
axis, two or three matches — sit near the **bottom** of the board with *much smaller*
deltas, already pulled toward PP by Elo shrinkage and the OTR reliability taper.

So read a large `vs pp` as "PP badly understates this player," not as an error; trimming
those rows away would delete the board's most distinctive output. If you specifically want
a board without the low-PP tournament crowd, that is exactly what `--min-otr-matches` is
for.

### Website (GitHub Pages)

The repo ships a dependency-free static site in [`docs/`](docs/) — an
`index.html` + `app.js` that fetch the committed CSV and render a **searchable,
sortable** table (search by username, click any column header to sort). Three
**delta** columns show how a player's hybrid rank compares to each axis alone:
`vs pp`, `vs elo`, `vs otr` (green ▴ gained, red ▾ lost; `—` when that axis is
seeded). No backend, no build step, no tracking. Every real Elo is sample-size
adjusted toward its PP-prior, so rather than a per-row symbol the **Elo number is
itself a hover target** (shows the raw rating + match count). Only the categorical
states carry a mark: **`*`** provisional (osu!'s own flag) and **`^`** no real Elo
(the value is the PP seed). OTR estimates from rank are marked **`~`**. A second **Calculator** tab computes a hybrid score
from a raw PP, Elo, and OTR — it pulls the published board's per-axis mean/std from the
meta sidecar, so with the default weights it reproduces exactly what the board
computed. The three weights are pre-filled with the board's split but **editable**,
so you can see how a different PP/Elo/OTR balance would score a player (with a
one-click reset back to the board weights).

**Enable it:** push the repo to GitHub → *Settings → Pages → Build from a
branch* → branch `main`, folder `/docs`. The board goes live at
`https://<user>.github.io/<repo>/`.

**Refresh the published data** (manual — you control the scrape rate):

```
python hybrid_rank.py --anchor union --otr <key> --osu-api --out docs/hybrid_leaderboard.csv  # ~15 min first time (10k caps)
git add docs/hybrid_leaderboard.csv docs/hybrid_leaderboard.meta.json && git commit -m "refresh leaderboard" && git push
```

The site reads `docs/hybrid_leaderboard.csv`, so the CSV must live **inside**
`docs/` (Pages only serves the publish folder). Root-level
`hybrid_leaderboard*.csv` outputs are git-ignored so throwaway runs don't clutter
the repo.

### Hard cap: top 10,000

osu!'s **public PP leaderboard is capped at the top 10,000** (page 200); deeper
pages just repeat page 200. The **union** anchor draws from three 10k pools — the
PP top-10k, the ranked-play top-10k, **and the OTR top-10k** — so a player must sit
inside at least one of the three to be considered (PP for a player outside the bulk
board is then fetched via the osu! API batch endpoint with `--osu-api`, else
per-profile via `statistics.global_rank` / `statistics.pp`). A player ranked
outside **all three** pools never appears, even if their hybrid score would place
them — so every hybrid rank is a standing *within this union sample*, not a true
global one. (Adding the OTR pool closes the old blind spot where a tournament-only
player who didn't grind PP or queue ranked play couldn't appear at all.)

### Scale & politeness

- Union cost (all three axes capped at top-10k): ~200 ranked-play pages + the PP
  top-10k board + pp lookups for kept players outside it (`--osu-api` batches the
  lookups and serves the PP board from the rankings API; else both are HTML) + the
  ~267-page OTR sweep. `RP_RANK_CAP` / `OTR_RANK_CAP` / `PP_RANK_CAP` set the caps;
  `--rp-max-pages` scans the RP board deeper at the cost of time.
- `MIN_INTERVAL` (default **1.0 s** — the global minimum seconds between request
  *starts*) caps the whole app at **≤60 requests/min**, honoring both the osu! and
  OTR terms of use (~1 req/s). `CONCURRENCY` (default 5) only overlaps latency; the
  shared throttle still paces starts to `MIN_INTERVAL`, so the rate never exceeds
  1/s. Both are at the top of the script.
- Pages are cached under `.cache/` for 1 week, so re-runs are near-instant.

### Notes
- Pure standard library — no `pip install`.
- Tune the weights **`W_PP`** and **`W_ELO`** (`W_OTR = 1 - W_PP - W_ELO` is
  derived), the shrinkage **`ELO_SHRINK_K`**, plus `MODE` at the top of
  `hybrid_rank.py` — or pass `--w-pp` / `--w-elo` on the command line.
- The OTR rating model + constants are documented inline where `otr_seed_from_rank`
  / `fetch_otr_leaderboard` are defined; they mirror `osu-tournament-rating/otr-processor`.

---

## Limitations

### Biggest limitations

- **Elo is still inaccurate because too few players queue.** For Elo to be
  meaningful, players — especially those at the top — need to play ranked
  matches relatively frequently. (This assumes the Elo system itself is reliable
  — it is brand new and still under active development.) The low-play shrinkage
  softens this, but it cannot manufacture data that isn't there.
- **The pools it draws from stop at 10,000.** The board is the union of the PP
  top-10k, the Ranked Play top-10k, and the OTR top-10k — each leaderboard is
  capped at 10k — so a player outside *all three* never appears even if their
  hybrid score would place them. Every hybrid rank is a standing *within this union
  sample* rather than a true global one. (The OTR pool now covers tournament-only
  players, but OTR itself rates only ~27k players total, so its bar is far looser
  than the PP top-10k — a known asymmetry in how selective each pool is.)
- **Shrinkage leans on an imperfect PP→Elo prior.** Pulling a noisy Elo toward its
  PP-expected value assumes PP is a decent guess of competitive skill — but that
  fit is loose (R² ≈ 0.35, it explains only about a third of the variance in Elo).
  So a genuine over-performer who has played very few matches gets tugged toward
  the crowd until they rack up games. Shrinkage trades a little of that edge-case
  fairness for a lot less small-sample noise (validated: it improves agreement with
  independent OTR ratings at every play-count level).
- **About a third of this board's OTR ratings are estimates, not real ones.**
  [OTR](https://otr.stagec.net/leaderboard) only rates players who have competed in
  verified tournaments — about two-thirds of the board. Everyone else gets a rating
  *seeded from their osu! rank* (marked `~`), which is just OTR's starting prior,
  not evidence of actual tournament results. These seeded values are **kept out of a player's own weighted blend** (zero weight — see [reliability weighting](#formula)) precisely because
  they'd otherwise just re-count pp; they're still shown for context and still feed the per-axis
  normalization (each axis's mean/std is computed over the full board, seeds
  included), so they aren't idle: the zero weight applies only to their own
  player's blend. A *real* OTR backed by only a
  handful of tournament matches sits just off that same seed, so it is *partially*
  down-weighted too — its share scales as `matches / (matches + 5)`, and only a
  deep tournament record earns its full weight.
- **OTR itself is a moving, partial target.** It updates on a weekly cadence and
  decays after about six months of tournament inactivity, so a player's tournament
  axis can lag their current form. It also only counts *approved* matches —
  qualifiers, scrims and unverified events don't register.
- **The three-way weight split is debatable.** The board uses base weights of
  **0.33 PP / 0.34 Elo / 0.33 OTR** — the three axes weighted near-equally. That
  balance is a judgement call — a different split may be equally valid, or better.
  (Reliability weighting means a player missing a real axis is scored on the other
  two at the same relative ratio, rather than having a pp-derived placeholder
  diluting the blend.)
- **Normalization is relative to whoever is on the board.** Each axis is
  standardized against this population, so a player's score shifts a little
  whenever the board's makeup changes. That is the trade for measuring magnitude
  on a common scale rather than blending raw, incomparable numbers — but it means
  scores are standings within *this* sample, not absolute values.

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

---

*Parts of this app were vibe-coded or edited with the help of AI.*
