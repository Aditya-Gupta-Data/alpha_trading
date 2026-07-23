"""
src/analysis/macro_fingerprints.py — M2: fingerprints, DTW, archetypes
======================================================================

The Phase Engine's discovery half (docs/macro_regime_engine_spec.md §2):
episodes (config/macro_episodes.yaml) -> fingerprint channel matrices
(through macro_features.trajectory — the ONE featurizer) -> multivariate
DTW distances -> average-linkage agglomerative clustering, HARD-CAPPED
at K_MAX archetypes -> data/macro_templates.json.

M2.1 (the clustering-report addendum's fix): the TAXONOMY layer runs on
CORE_CHANNELS — the global set every episode can in principle show — so
pairwise distances stay commensurable across the catalog; the India
channels (observable for 2021+ episodes only) live in the artifact's
`india_view` as a within-family refinement and never steer clustering.

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
# The static episode fingerprints, persisted at (rare) rebuild time so the
# nightly declare() on the e2-micro READS them instead of recomputing 20+
# episode trajectories every run. Stamped with the templates' built_at so a
# consumer can verify the cache matches the live templates (else recompute).
FINGERPRINT_CACHE_PATH = ROOT / "data" / "macro_fingerprints_cache.json"

K_MAX = 4                  # the taxonomy may not outgrow the evidence
DTW_BAND = 20              # Sakoe-Chiba: phases may stretch, not teleport
T_MINUS, T_PLUS = 20, 120
UNOBSERVED_PENALTY = 3.0   # ~a 3-sigma disagreement; documented, versioned
_INF = float("inf")

# HORIZON CLASSES (owner directive 2026-07-23: slow-burn cycles). A war
# and a weather cycle are different species: each horizon gets its own
# window geometry, z-channel tempo (z20 for shocks, z60 for multi-month
# cycles), DTW band (elasticity scales with the window) and k-cap
# (3 slow-burn episodes cannot support more than 2 families). Horizons
# NEVER cross-compare — the commensurability law, extended.
HORIZONS = {
    "shock": {"t_minus": T_MINUS, "t_plus": T_PLUS, "band": DTW_BAND,
              "k_max": K_MAX, "z_window": "z20", "id_prefix": "A"},
    "slow_burn": {"t_minus": 60, "t_plus": 500, "band": 75,
                  "k_max": 2, "z_window": "z60", "id_prefix": "S"},
}

# The comparable channels of a fingerprint row (order fixed = vector shape).
CHANNELS = (tuple(f"{s}:z20" for s in MF.SERIES)
            + tuple(f"{s}:z60" for s in MF.SERIES)
            + ("dxy_brent_corr60",))

# M2.1 (2026-07-23, the addendum's fix): ARCHETYPES cluster on the core
# channels every episode can in principle show — the global set. India
# channels (indices history starts 2019-10 + the 252-session z baseline
# ⇒ they speak only for 2021+ episodes) would make pairwise distances
# incommensurable across the catalog and reshuffle the taxonomy by
# coverage, not by behavior — verified live in the 05:00 rebuild. They
# stay a WITHIN-family refinement (the artifact's `india_view`), never
# a clustering input. Each horizon has its own tempo's core set.
_GLOBALS = ("BRENT", "DXY", "USDINR", "US10Y")
CORE_CHANNELS = tuple(f"{s}:z20" for s in _GLOBALS) + ("dxy_brent_corr60",)
CORE_CHANNELS_SLOW = (tuple(f"{s}:z60" for s in _GLOBALS)
                      + ("dxy_brent_corr60",))
INDIA_CHANNELS = ("INDIAVIX:z20", "NIFTY:z20", "INDIAVIX:z60", "NIFTY:z60")


def core_channels_for(horizon: str):
    return CORE_CHANNELS_SLOW if horizon == "slow_burn" else CORE_CHANNELS


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
        horizon = (row or {}).get("horizon") or "shock"
        if not name or len(anchor) != 10 or horizon not in HORIZONS:
            raise ValueError(f"malformed episode row: {row!r}")
        out.append({"anchor": anchor, "name": name, "horizon": horizon,
                    "class": row.get("class"), "why": row.get("why")})
    return out


def channel_rows(traj, channels=None):
    """trajectory() output -> one dict per offset holding ONLY the
    genuinely observed channels ({} for a None vector). `channels`
    filters to a subset (M2.1: CORE_CHANNELS for the clustering layer);
    None keeps every channel."""
    keep = set(channels) if channels else None
    rows = []
    for r in traj["rows"]:
        vec, out = r.get("vector"), {}
        if vec:
            for s in MF.SERIES:
                block = vec["series"].get(s) or {}
                for zw in ("z20", "z60"):
                    z = block.get(zw)
                    if z is not None:
                        out[f"{s}:{zw}"] = z
            c = vec["pairs"].get("dxy_brent_corr60")
            if c is not None:
                out["dxy_brent_corr60"] = c
        if keep is not None:
            out = {k: v for k, v in out.items() if k in keep}
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


def distance_matrix(fingerprints, band=DTW_BAND):
    """{name: rows} -> ({(a, b): (dist|None, coverage)}, sorted names)."""
    names = sorted(fingerprints)
    out = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            out[(a, b)] = dtw_distance(fingerprints[a], fingerprints[b],
                                       band=band)
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
                    dry_run=False, k_max=K_MAX, cache_path=None):
    """The M2 artifact: every episode's fingerprint -> distances ->
    archetypes -> data/macro_templates.json (atomic). Episodes whose
    whole window is unobservable are EXCLUDED and named — the artifact
    records what it could not see. Also persists the static CORE-channel
    episode fingerprints to `cache_path` so the nightly declare() reads
    them (the e2-micro reliability fix)."""
    episodes = load_episodes(episodes_path)
    horizons_out = {}
    cache_horizons = {}
    for hz, cfg in HORIZONS.items():
        hz_eps = [e for e in episodes if e["horizon"] == hz]
        if not hz_eps:
            continue
        core_set = core_channels_for(hz)
        hz_k_max = cfg["k_max"] if k_max == K_MAX else k_max
        core_fp, full_fp, excluded = {}, {}, []
        for ep in hz_eps:
            traj = MF.trajectory(ep["anchor"], cfg["t_minus"],
                                 cfg["t_plus"], lake_dir)
            core = channel_rows(traj, channels=core_set)
            if not any(core):
                excluded.append({"name": ep["name"],
                                 "why": "no observable core channels "
                                        "in window"})
                continue
            core_fp[ep["name"]] = core
            full_fp[ep["name"]] = channel_rows(traj)
        # M2.1: the taxonomy layer sees this horizon's CORE channels
        # only (commensurable across its whole catalog) …
        dist, names = distance_matrix(core_fp, band=cfg["band"])
        archetypes = cluster(dist, names, k_max=hz_k_max)
        # … and the India channels become the WITHIN-family refinement.
        india_view = []
        for i, arch in enumerate(archetypes):
            members = arch["members"]
            pair_block = {}
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    a, b = members[x], members[y]
                    d, cov = dtw_distance(full_fp[a], full_fp[b],
                                          band=cfg["band"])
                    pair_block[f"{a}|{b}"] = {"dtw": d, "coverage": cov}
            has_india = sorted(
                m for m in members
                if any(any(ch in row for ch in INDIA_CHANNELS)
                       for row in full_fp[m]))
            india_view.append({"id": f"{cfg['id_prefix']}{i + 1}",
                               "members_with_india_channels": has_india,
                               "full_channel_distances": pair_block})
        horizons_out[hz] = {
            "params": {"k_max": hz_k_max, "dtw_band": cfg["band"],
                       "window": [cfg["t_minus"], cfg["t_plus"]],
                       "clustering_channels": list(core_set)},
            "episodes": [{**ep, "included": ep["name"] in core_fp}
                         for ep in hz_eps],
            "excluded": excluded,
            "distances": {f"{a}|{b}": {"dtw": v[0], "coverage": v[1]}
                          for (a, b), v in sorted(dist.items())},
            "archetypes": [{"id": f"{cfg['id_prefix']}{i + 1}", **c}
                           for i, c in enumerate(archetypes)],
            "india_view": india_view,
        }
        # the static core fingerprints for this horizon — what the nightly
        # declare() would otherwise recompute every run
        cache_horizons[hz] = core_fp
    included = set()
    for h in horizons_out.values():
        for arch in h["archetypes"]:
            included.update(arch["members"])
    doc = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "params": {"unobserved_penalty": UNOBSERVED_PENALTY,
                   "refinement_channels": list(INDIA_CHANNELS),
                   "channels": list(CHANNELS),
                   "horizons": {h: dict(c) for h, c in HORIZONS.items()}},
        "episodes": [{**ep, "included": ep["name"] in included}
                     for ep in episodes],
        "horizons": horizons_out,
    }
    cache = {"built_at": doc["built_at"], "horizons": cache_horizons}
    path = Path(out_path or TEMPLATES_PATH)
    # THE cache follows the templates (2026-07-23 leak fix): a redirected
    # build (tests -> tmp) writes its cache BESIDE its templates, so a
    # test can NEVER clobber the production cache. Only an explicit
    # cache_path overrides; a bare production build uses both defaults.
    if cache_path is not None:
        cpath = Path(cache_path)
    elif out_path is not None:
        cpath = path.parent / FINGERPRINT_CACHE_PATH.name
    else:
        cpath = FINGERPRINT_CACHE_PATH
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(path)
        ctmp = cpath.with_suffix(".tmp")
        ctmp.write_text(json.dumps(cache, default=str))
        ctmp.replace(cpath)
    doc["_cache"] = cache            # expose for callers/tests (not persisted in the template file)
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
        "horizons": {h: {"excluded": blk["excluded"],
                         "archetypes": blk["archetypes"]}
                     for h, blk in result["horizons"].items()},
    }, indent=2, default=str))
