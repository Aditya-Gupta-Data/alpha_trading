"""
src/analysis/macro_fingerprints.py — M2: fingerprints, DTW, archetypes
======================================================================

The Phase Engine's discovery half (docs/macro_regime_engine_spec.md §2):
episodes (config/macro_episodes.yaml) -> fingerprint channel matrices
(through macro_features.trajectory — the ONE featurizer) -> multivariate
DTW distances -> average-linkage agglomerative clustering, HARD-CAPPED
at K_MAX archetypes -> data/macro_templates.json.

Honesty machinery:
  * A channel missing at an offset (hole/insufficient history) is simply
    absent for that offset; the local DTW cost uses the channels BOTH
    rows share, and every pair records its channel coverage fraction. A
    dark cell (no shared channels) is crossable at a documented penalty
    — one dark stretch can't sever two long fingerprints, but it never
    pretends to be similarity. A pair with NO shared observation
    anywhere gets distance None: "not comparable" is an answer, never a
    fake number, and a None pair can never merge into an archetype.
  * K_MAX = 4 (spec §2: ~17 episodes cannot honestly support more).
  * Deterministic end to end — no randomness, stable tie-breaks, so the
    artifact only changes when the data or the catalog change.

Pure compute + one artifact write (atomic tmp+rename). Reads the macro
lake and the episode catalog; touches no trade state.

CLI: python3 -m src.analysis.macro_fingerprints [--dry-run]
"""
import json
import math
from datetime import datetime
from pathlib import Path

from src.analysis import macro_features as MF

ROOT = Path(__file__).resolve().parents[2]
EPISODES_PATH = ROOT / "config" / "macro_episodes.yaml"
TEMPLATES_PATH = ROOT / "data" / "macro_templates.json"

K_MAX = 4                  # the taxonomy may not outgrow the evidence
DTW_BAND = 20              # Sakoe-Chiba: phases may stretch, not teleport
T_MINUS, T_PLUS = 20, 120
UNOBSERVED_PENALTY = 3.0   # ~a 3-sigma disagreement; documented, versioned
_INF = float("inf")

# The comparable channels of a fingerprint row (order fixed = vector shape).
CHANNELS = tuple(f"{s}:z20" for s in MF.SERIES) + ("dxy_brent_corr60",)


def load_episodes(path=None):
    """config/macro_episodes.yaml -> list of {anchor, name, class, why}.
    Anchors normalized to ISO strings; a malformed row is REFUSED loudly
    (a curated catalog with a bad row is a catalog bug, not a hole)."""
    import yaml
    doc = yaml.safe_load(Path(path or EPISODES_PATH).read_text()) or {}
    out = []
    for row in doc.get("episodes") or []:
        anchor = str((row or {}).get("anchor") or "")[:10]
        name = (row or {}).get("name")
        if not name or len(anchor) != 10:
            raise ValueError(f"malformed episode row: {row!r}")
        out.append({"anchor": anchor, "name": name,
                    "class": row.get("class"), "why": row.get("why")})
    return out


def channel_rows(traj):
    """trajectory() output -> one dict per offset holding ONLY the
    genuinely observed channels ({} for a None vector)."""
    rows = []
    for r in traj["rows"]:
        vec, out = r.get("vector"), {}
        if vec:
            for s in MF.SERIES:
                z = (vec["series"].get(s) or {}).get("z20")
                if z is not None:
                    out[f"{s}:z20"] = z
            c = vec["pairs"].get("dxy_brent_corr60")
            if c is not None:
                out["dxy_brent_corr60"] = c
        rows.append(out)
    return rows


def _local_cost(row_a, row_b):
    """Mean |Δ| over the channels BOTH rows observe -> (cost, shared_n).
    (None, 0) when nothing is shared — the caller decides the penalty."""
    shared = [k for k in row_a if k in row_b]
    if not shared:
        return None, 0
    return (math.fsum(abs(row_a[k] - row_b[k]) for k in shared)
            / len(shared), len(shared))


def dtw_distance(rows_a, rows_b, band=DTW_BAND):
    """Banded multivariate DTW, normalized by path length.

    Returns (distance|None, coverage): coverage is the fraction of the
    warping path that crossed genuinely shared observation. None when
    the fingerprints share no observation at all (not comparable)."""
    n, m = len(rows_a), len(rows_b)
    if n == 0 or m == 0:
        return None, 0.0
    # dp cell = (accumulated cost, path length, covered-cells count)
    dp = [[(_INF, 0, 0)] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = (0.0, 0, 0)
    for i in range(1, n + 1):
        lo, hi = max(1, i - band), min(m, i + band)
        for j in range(lo, hi + 1):
            cost, shared_n = _local_cost(rows_a[i - 1], rows_b[j - 1])
            covered = 1 if shared_n else 0
            if cost is None:
                cost = UNOBSERVED_PENALTY
            best = min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1],
                       key=lambda t: t[0])
            if best[0] < _INF:
                dp[i][j] = (best[0] + cost, best[1] + 1, best[2] + covered)
    total, length, covered = dp[n][m]
    if total == _INF or length == 0:
        return None, 0.0
    coverage = covered / length
    if coverage == 0.0:
        return None, 0.0
    return total / length, round(coverage, 4)


def distance_matrix(fingerprints):
    """{name: rows} -> ({(a, b): (dist|None, coverage)}, sorted names)."""
    names = sorted(fingerprints)
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            out[(a, b)] = dtw_distance(fingerprints[a], fingerprints[b])
    return out, names


def cluster(dist, names, k_max=K_MAX):
    """Average-linkage agglomerative, deterministic, k_max-capped.

    A pair whose distance is None is NOT comparable and never merges;
    clustering stops early when nothing comparable remains, so the
    result can hold MORE than k_max clusters only in that honest case.
    Returns [{"members": [...], "medoid": name}], largest first."""
    clusters = [[n] for n in names]

    def d(a, b):
        v = dist.get((a, b)) or dist.get((b, a))
        return None if v is None or v[0] is None else v[0]

    def linkage(ca, cb):
        vals = [d(a, b) for a in ca for b in cb]
        vals = [v for v in vals if v is not None]
        return (math.fsum(vals) / len(vals)) if vals else None

    while len(clusters) > k_max:
        best, pair = None, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                link = linkage(clusters[i], clusters[j])
                if link is not None and (best is None or link < best):
                    best, pair = link, (i, j)
        if pair is None:              # nothing comparable left to merge
            break
        i, j = pair
        clusters[i] = sorted(clusters[i] + clusters[j])
        del clusters[j]

    out = []
    for members in clusters:
        med, med_score = members[0], _INF
        for cand in members:
            vals = [v for v in (d(cand, o) for o in members if o != cand)
                    if v is not None]
            score = math.fsum(vals) / len(vals) if vals else _INF
            if score < med_score:
                med, med_score = cand, score
        out.append({"members": members, "medoid": med})
    return sorted(out, key=lambda c: (-len(c["members"]), c["members"][0]))


def build_templates(lake_dir=None, episodes_path=None, out_path=None,
                    dry_run=False, k_max=K_MAX):
    """The M2 artifact: every episode's fingerprint -> distances ->
    archetypes -> data/macro_templates.json (atomic). Episodes whose
    whole window is unobservable are EXCLUDED and named — the artifact
    records what it could not see."""
    episodes = load_episodes(episodes_path)
    fingerprints, excluded = {}, []
    for ep in episodes:
        rows = channel_rows(
            MF.trajectory(ep["anchor"], T_MINUS, T_PLUS, lake_dir))
        if not any(rows):
            excluded.append({"name": ep["name"],
                             "why": "no observable channels in window"})
            continue
        fingerprints[ep["name"]] = rows
    dist, names = distance_matrix(fingerprints)
    archetypes = cluster(dist, names, k_max=k_max)
    doc = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"k_max": k_max, "dtw_band": DTW_BAND,
                   "window": [T_MINUS, T_PLUS],
                   "unobserved_penalty": UNOBSERVED_PENALTY,
                   "channels": list(CHANNELS)},
        "episodes": [{**ep, "included": ep["name"] in fingerprints}
                     for ep in episodes],
        "excluded": excluded,
        "distances": {f"{a}|{b}": {"dtw": v[0], "coverage": v[1]}
                      for (a, b), v in sorted(dist.items())},
        "archetypes": [{"id": f"A{i + 1}", **c}
                       for i, c in enumerate(archetypes)],
    }
    if not dry_run:
        path = Path(out_path or TEMPLATES_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(path)
    return doc


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print, write no artifact")
    args = ap.parse_args()
    result = build_templates(dry_run=args.dry_run)
    print(json.dumps({
        "built_at": result["built_at"],
        "included": sum(1 for e in result["episodes"] if e["included"]),
        "excluded": result["excluded"],
        "archetypes": result["archetypes"],
    }, indent=2, default=str))
