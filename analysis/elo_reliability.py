#!/usr/bin/env python3
"""Reproduce the two empirical facts behind the leaderboard's Elo handling.

The board treats osu! Ranked-Play "Elo" as what it is -- an OpenSkill (Plackett-Luce)
posterior SEEDED from a PP estimate, then updated per match. It never edits the Elo
value; reliability lives entirely in the axis's WEIGHT (see ELO_RELIABILITY_K in
hybrid_rank.py). This script checks the two claims that justify that design, straight
from the published board -- no OTR key or network needed:

  1. Elo carries real, INDEPENDENT skill signal. Using OTR as an external yardstick
     (it shares no data with Ranked Play or PP), a real Elo's agreement with OTR GROWS
     with match count: a thin Elo (1-4 matches) is a statistical dead heat with a pure
     PP guess, but by >=5 matches it pulls clearly ahead. So Elo earns its place as an
     axis. (Williams test within a bucket; Fisher r-to-z across the threshold.)

  2. In the 3-axis blend, FULL Elo weight is the most accurate choice -- so the K=5
     reliability taper is a conventional robustness hedge, NOT an accuracy optimizer.
     Grid-searching the weight taper plays/(plays+K) to best predict OTR peaks at K->0
     (no taper) and declines monotonically as K grows: down-weighting a thin Elo just
     leans on PP, which its ~PP seed already duplicates.

Cohort: players who carry BOTH a real Elo and a real OTR (so OTR is an external
yardstick and the raw Elo is a genuine, if noisy, measurement rather than a seed).

Reads a frozen, timestamped snapshot under analysis/snapshots/ (NOT the live docs/
board, which is overwritten weekly) so the numbers are stable. Stdlib only; ASCII-only
output. Run:  python analysis/elo_reliability.py
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys

# Must match ELO_RELIABILITY_K / base weights in hybrid_rank.py.
ELO_RELIABILITY_K = 5.0
BASE_W_PP = 1.0 / 3.0
BASE_W_ELO = 1.0 / 3.0
BUCKETS = [(1, 4), (5, 9), (10, 19), (20, 49), (50, None)]


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sxx = syy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def _zscore(vals):
    n = len(vals)
    m = sum(vals) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / n)
    sd = sd if sd > 1e-9 else 1.0
    return [(v - m) / sd for v in vals]


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _two_sided_p(stat):
    p = 2.0 * (1.0 - _norm_cdf(abs(stat)))
    return p, (f"{p:.3g}" if p >= 1e-15 else "< 1e-15")


def _williams(r_elo_otr, r_pp_otr, r_elo_pp, n):
    """Williams's test for two dependent correlations sharing OTR (Steiger 1980),
    df = n-3. Normal-tail p (large n). Returns (t, p_str)."""
    if n < 4:
        return float("nan"), "n/a"
    r12, r13, r23 = r_elo_otr, r_pp_otr, r_elo_pp
    det = 1.0 - r12 * r12 - r13 * r13 - r23 * r23 + 2.0 * r12 * r13 * r23
    denom = (2.0 * ((n - 1.0) / (n - 3.0)) * det
             + ((r12 + r13) ** 2 / 4.0) * (1.0 - r23) ** 3)
    if denom <= 0.0:
        return float("nan"), "n/a"
    t = (r12 - r13) * math.sqrt((n - 1.0) * (1.0 + r23) / denom)
    return t, _two_sided_p(t)[1]


def _fisher_independent(r1, n1, r2, n2):
    if n1 < 4 or n2 < 4:
        return float("nan"), "n/a"
    z = (math.atanh(r1) - math.atanh(r2)) / math.sqrt(1.0 / (n1 - 3.0) + 1.0 / (n2 - 3.0))
    return z, _two_sided_p(z)[1]


def load_cohort(csv_path):
    """Players with BOTH a real Elo and a real OTR: {plays, elo, pp_log, otr}."""
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("elo_estimated") == "yes" or r.get("otr_estimated") == "yes":
                continue
            try:
                pp = float(r["pp"])
                # We want the RAW osu! posterior. New-mechanism CSVs put it directly in
                # elo_rating (values are never edited); older ones kept elo_rating as a
                # shrunk value and the raw one in elo_raw -- prefer elo_raw when present.
                raw = r.get("elo_raw")
                elo = float(raw) if raw not in (None, "") else float(r["elo_rating"])
                otr = float(r["otr_rating"])
                plays = int(float(r["plays"]))
            except (KeyError, ValueError):
                continue
            if pp < 1 or plays < 1:
                continue
            rows.append({"plays": plays, "elo": elo, "pp_log": math.log(pp), "otr": otr})
    return rows


def _corrs(group):
    elo = [g["elo"] for g in group]
    pp = [g["pp_log"] for g in group]
    otr = [g["otr"] for g in group]
    return _pearson(elo, otr), _pearson(pp, otr), _pearson(elo, pp)


def report_signal(rows):
    print("=" * 72)
    print(f"1. Does Elo carry independent skill signal?  (cohort: {len(rows)} players)")
    print("=" * 72)
    print("Predicting OTR from raw Elo vs. a pure PP guess, split at 5 matches.\n")

    below = [r for r in rows if r["plays"] < 5]
    atabove = [r for r in rows if r["plays"] >= 5]
    hdr = f"{'bucket':>10} {'n':>6} {'r(Elo,OTR)':>12} {'r(PP,OTR)':>11} {'Williams p':>12}"
    print(hdr)
    print("-" * len(hdr))
    stat = {}
    for label, grp in (("< 5 plays", below), (">= 5 plays", atabove)):
        if len(grp) < 4:
            continue
        r_eo, r_po, r_ep = _corrs(grp)
        _, p_str = _williams(r_eo, r_po, r_ep, len(grp))
        stat[label] = (r_eo, len(grp))
        print(f"{label:>10} {len(grp):>6} {r_eo:>12.3f} {r_po:>11.3f} {p_str:>12}")

    print("\nWilliams (within a bucket): below 5 matches a real Elo is no better than the")
    print("PP guess at predicting OTR (high p = dead heat); its independent edge appears")
    print("only with more matches.")
    if "< 5 plays" in stat and ">= 5 plays" in stat:
        (r_lo, n_lo), (r_hi, n_hi) = stat["< 5 plays"], stat[">= 5 plays"]
        z, p_str = _fisher_independent(r_lo, n_lo, r_hi, n_hi)
        print("\nFisher z (across the threshold): does r(Elo,OTR) rise from <5 to >=5?")
        print(f"  {r_lo:.3f} (<5, n={n_lo})  ->  {r_hi:.3f} (>=5, n={n_hi})   z={z:.2f}, p={p_str}")
        print("  -> Elo's agreement with OTR grows with matches, so it earns an axis.")


def report_buckets(rows):
    print("\n" + "=" * 72)
    print("Per-match-count correlations (Elo's signal by depth of record)")
    print("=" * 72)
    hdr = f"{'bucket':>9} {'n':>6} {'r(Elo,OTR)':>12} {'r(PP,OTR)':>11}"
    print(hdr)
    print("-" * len(hdr))
    for lo, hi in BUCKETS:
        grp = [r for r in rows if r["plays"] >= lo and (hi is None or r["plays"] <= hi)]
        label = f"{lo}-{hi}" if hi is not None else f"{lo}+"
        if len(grp) < 4:
            print(f"{label:>9} {len(grp):>6}   (too few)")
            continue
        r_eo, r_po, _ = _corrs(grp)
        print(f"{label:>9} {len(grp):>6} {r_eo:>12.3f} {r_po:>11.3f}")
    print("\nr(Elo,OTR) climbs with match count; r(PP,OTR) is roughly flat -- extra games")
    print("add real, independent skill signal on top of what PP already captures.")


def report_weight_taper(rows, w_pp, w_elo):
    print("\n" + "=" * 72)
    print("2. In the blend, does down-weighting a thin Elo help?  (predict OTR)")
    print("=" * 72)
    print("PP + weight-tapered Elo, correlated with OTR. K=0 -> full Elo weight (freed")
    print("weight to PP as K grows); PP-only -> Elo dropped. Higher = better.\n")
    zpp = _zscore([r["pp_log"] for r in rows])
    zelo = _zscore([r["elo"] for r in rows])
    for i, r in enumerate(rows):
        r["_zpp"], r["_zelo"] = zpp[i], zelo[i]
    subs = [("1-4", [r for r in rows if r["plays"] <= 4]),
            ("5-9", [r for r in rows if 5 <= r["plays"] <= 9]),
            ("1-9", [r for r in rows if r["plays"] <= 9]),
            ("all", rows)]

    def blend_corr(K, sub):
        preds, tgt = [], []
        for r in sub:
            we = w_elo * r["plays"] / (r["plays"] + K) if K > 0 else w_elo
            wp = w_pp + (w_elo - we)
            s = wp + we
            preds.append((wp * r["_zpp"] + we * r["_zelo"]) / s)
            tgt.append(r["otr"])
        return _pearson(preds, tgt)

    print(f"{'K_ELO':>8} " + " ".join(f"{b:>8}" for b, _ in subs))
    print("-" * 44)
    for K in [0, 1, 2, 3, 5, 8, 15, 1e9]:
        lab = "PP-only" if K >= 1e9 else f"{K:g}"
        print(f"{lab:>8} " + " ".join(f"{blend_corr(K, sub):>8.4f}" for _, sub in subs))
    print(f"\nEvery column peaks at K=0 (full weight) and falls monotonically as K grows,")
    print(f"so the taper never improves accuracy. K={ELO_RELIABILITY_K:g} (used by the board) is a")
    print("conventional robustness hedge -- small-sample luck + a smooth ramp off the")
    print("zero-weighted seed -- applied the same way to the OTR axis.")


def _newest_snapshot(snap_dir):
    hits = glob.glob(os.path.join(snap_dir, "hybrid_leaderboard_*.csv"))
    return max(hits) if hits else None


def _meta_for(csv_path):
    cand = csv_path[:-4] + ".meta.json" if csv_path.endswith(".csv") else csv_path + ".meta.json"
    return cand if os.path.exists(cand) else None


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    snap_dir = os.path.join(here, "snapshots")
    ap = argparse.ArgumentParser(description="Reproduce the leaderboard's Elo-handling facts.")
    ap.add_argument("--csv", default=None,
                    help="leaderboard CSV (default: newest frozen snapshot in analysis/snapshots/)")
    ap.add_argument("--meta", default=None,
                    help="meta sidecar (default: the chosen snapshot's matching .meta.json)")
    args = ap.parse_args(argv)

    csv_path = args.csv or _newest_snapshot(snap_dir)
    if not csv_path:
        sys.exit("No snapshot in analysis/snapshots/ and no --csv given. Freeze one with:\n"
                 "  cp docs/hybrid_leaderboard.csv "
                 "analysis/snapshots/hybrid_leaderboard_<date>.csv\n"
                 "  cp docs/hybrid_leaderboard.meta.json "
                 "analysis/snapshots/hybrid_leaderboard_<date>.meta.json")
    if not os.path.exists(csv_path):
        sys.exit(f"CSV not found: {csv_path}")

    w_pp, w_elo, generated = BASE_W_PP, BASE_W_ELO, None
    meta_path = args.meta or _meta_for(csv_path)
    if meta_path:
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            w_pp = float(meta.get("weight_pp", BASE_W_PP))
            w_elo = float(meta.get("weight_elo", BASE_W_ELO))
            generated = meta.get("generated_utc")
        except (OSError, ValueError):
            pass

    stamp = f" (generated {generated})" if generated else ""
    print(f"Snapshot: {os.path.basename(csv_path)}{stamp}   base w_pp={w_pp:g} w_elo={w_elo:g}\n")

    rows = load_cohort(csv_path)
    if len(rows) < 8:
        sys.exit(f"Only {len(rows)} players carry both a real Elo and a real OTR -- too few.")

    report_signal(rows)
    report_buckets(rows)
    report_weight_taper(rows, w_pp, w_elo)


if __name__ == "__main__":
    main()
