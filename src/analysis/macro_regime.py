"""
src/analysis/macro_regime.py — M4: the nightly declaration tracker
==================================================================

Spec §3: every evening, answer "which archetype does TODAY resemble,
where in that pattern's life are we — or honestly, no confident match"
and put the answer ON THE RECORD before the outcome is knowable.

Mechanics (every piece reused, none invented):
  * The current state = the trailing CURRENT_LEN sessions through THE
    featurizer (macro_features.trajectory anchored at the latest lake
    session) — the same code path that built the historical templates.
  * Matching runs on CORE channels only (M2.1 commensurability law):
    the current window slides across each episode fingerprint
    (subsequence DTW, step SLIDE_STEP); the best window's center offset
    is the "days since pattern anchor" estimate, which maps into the
    playbook phases (P1/P2/P3).
  * similarity = 1 / (1 + dtw). DECLARED only when the best episode's
    similarity >= SIM_FLOOR AND >= MIN_ANALOGS episodes OF THE SAME
    ARCHETYPE clear ANALOG_FLOOR. The runner-up archetype is ALWAYS
    reported — a regime call that hides its ambiguity lies by omission.
  * Every run appends ONE line to the immutable declaration ledger
    (logs/macro_regime_declarations.jsonl) — Dept 5's scoring reads the
    ledger, never a backtest of ourselves. `data/macro_regime.json` is
    the current-state artifact (atomic).
  * Discord: ONE card on a FAMILY TRANSITION only (declared archetype/
    phase changes, declare<->undeclare). Sameness is silent; the ledger
    is the memory. Card failure never blocks the write (fail-open).

Authority: NONE. This module writes artifacts and speaks on Discord.
No proposal, sizing, treasury or desk path reads it until Dept 5's
stat_gates pass its ledger (spec §3-4 graduation).

CLI: python3 -m src.analysis.macro_regime [--dry-run]
"""
import json
from datetime import datetime
from pathlib import Path

from src.analysis import macro_features as MF
from src.analysis import macro_fingerprints as FP

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_PATH = FP.TEMPLATES_PATH
PLAYBOOKS_PATH = ROOT / "data" / "macro_playbooks.json"
STATE_PATH = ROOT / "data" / "macro_regime.json"
LEDGER_PATH = ROOT / "logs" / "macro_regime_declarations.jsonl"

CURRENT_LEN = 60          # trailing sessions in the shock window
SLIDE_STEP = 5            # subsequence stride across episode fingerprints
SIM_FLOOR = 0.70          # best-episode similarity to declare
ANALOG_FLOOR = 0.60       # an "analog" clears this similarity
MIN_ANALOGS = 3           # same-archetype analogs required to declare

# Per-horizon matching geometry (owner directive 2026-07-23): a
# slow-burn cycle is tracked with a YEAR of trailing context and a
# proportionally elastic stride/band; MIN_ANALOGS drops to 2 there
# because the whole catalog holds 3 slow-burn episodes — demanding 3
# would make declaration structurally impossible, which is not
# abstention but a dead switch.
HORIZON_MATCH = {
    "shock": {"current_len": CURRENT_LEN, "step": SLIDE_STEP,
              "min_analogs": MIN_ANALOGS},
    "slow_burn": {"current_len": 250, "step": 25, "min_analogs": 2},
}


def _similarity(dtw):
    return None if dtw is None else 1.0 / (1.0 + dtw)


def _phase_for(offset, horizon="shock"):
    from src.analysis.macro_playbooks import PHASES_BY_HORIZON
    for name, lo, hi in PHASES_BY_HORIZON[horizon]:
        if lo <= offset <= hi:
            return name
    return None


def current_rows(lake_dir=None, length=CURRENT_LEN, horizon="shock"):
    """The trailing `length`-session core-channel window for one
    horizon, through the ONE featurizer. Returns (as_of_date|None,
    rows) — (None, []) when the lake is empty (honest, never
    synthetic)."""
    raw = {name: MF.read_series(name, lake_dir) for name in MF.SERIES}
    calendar = sorted({d for rows in raw.values() for d, _ in rows})
    if not calendar:
        return None, []
    as_of = calendar[-1]
    traj = MF.trajectory(as_of, t_minus=length - 1, t_plus=0,
                         lake_dir=lake_dir)
    return as_of, FP.channel_rows(traj,
                                  channels=FP.core_channels_for(horizon))


class CacheUnavailable(Exception):
    """Raised when the fingerprint cache is stale/absent AND the caller
    demanded it (require_cache). The e2-micro executor must NEVER fall
    back to the 30-minute recompute — it aborts and abstains instead."""

    def __init__(self, horizon, status):
        self.horizon, self.status = horizon, status
        super().__init__(f"fingerprint cache {status} for {horizon}")


def _load_fingerprint_cache(templates, horizon, cache_path=None):
    """(rows, status) for a horizon's static fingerprints from the cache.
    status ∈ 'hit' | 'miss_stale' (stamp ≠ live templates) | 'miss_absent'
    (no file / horizon not present). rows is None on any miss."""
    p = Path(cache_path) if cache_path else FP.FINGERPRINT_CACHE_PATH
    try:
        cache = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None, "miss_absent"
    if cache.get("built_at") != templates.get("built_at"):
        return None, "miss_stale"         # templates rebuilt since
    rows = (cache.get("horizons") or {}).get(horizon)
    return (rows, "hit") if rows else (None, "miss_absent")


def episode_fingerprints(templates, lake_dir=None, horizon="shock",
                         cache_path=None, require_cache=False):
    """{episode: core-channel fingerprint rows} for one horizon. Reads
    the fingerprint CACHE when it matches the live templates (the
    e2-micro fix — no nightly recompute). `require_cache=True` (the VM
    executor) turns a cache miss into a RAISE (CacheUnavailable) rather
    than the 30-min recompute; the default recomputes as a
    correctness-preserving fallback (the Mac rebuild lane)."""
    cached, status = _load_fingerprint_cache(templates, horizon, cache_path)
    if cached is not None:
        return cached                     # ultra-light: no featurizer pass
    if require_cache:
        raise CacheUnavailable(horizon, status)

    cfg = FP.HORIZONS[horizon]
    block = templates["horizons"].get(horizon) or {"episodes": []}
    out = {}
    for ep in block["episodes"]:
        if not ep.get("included"):
            continue
        traj = MF.trajectory(ep["anchor"], cfg["t_minus"], cfg["t_plus"],
                             lake_dir)
        out[ep["name"]] = FP.channel_rows(
            traj, channels=FP.core_channels_for(horizon))
    return out


def best_window_match(current, episode_rows, step=SLIDE_STEP,
                      t_minus=FP.T_MINUS, band=FP.DTW_BAND):
    """Slide the current window across one episode fingerprint; return
    (best_dtw|None, best_center_offset|None, coverage). Offsets are
    fingerprint positions minus the horizon's t_minus, i.e. sessions
    since anchor."""
    n, m = len(episode_rows), len(current)
    if n == 0 or m == 0 or n < m:
        return None, None, 0.0
    best = (None, None, 0.0)
    for start in range(0, n - m + 1, step):
        seg = episode_rows[start:start + m]
        d, cov = FP.dtw_distance(current, seg, band=band)
        if d is not None and (best[0] is None or d < best[0]):
            center = start + m // 2 - t_minus
            best = (d, center, cov)
    return best


def evaluate(current, fingerprints, archetype_members, horizon="shock",
             min_analogs=None):
    """Pure decision core: match the current window against every
    episode, aggregate per archetype, apply the declaration floors.
    Returns the full verdict dict (no I/O — the testable heart)."""
    cfg = FP.HORIZONS[horizon]
    match_cfg = HORIZON_MATCH[horizon]
    if min_analogs is None:
        min_analogs = match_cfg["min_analogs"]
    per_episode = {}
    for name, rows in fingerprints.items():
        d, center, cov = best_window_match(
            current, rows, step=match_cfg["step"],
            t_minus=cfg["t_minus"], band=cfg["band"])
        per_episode[name] = {"dtw": d, "similarity": _similarity(d),
                             "offset_estimate": center, "coverage": cov}

    ranked = []
    for aid, members in archetype_members.items():
        scored = [(per_episode[m]["similarity"], m) for m in members
                  if per_episode.get(m, {}).get("similarity") is not None]
        if not scored:
            continue
        scored.sort(reverse=True)
        best_sim, best_ep = scored[0]
        analogs = [m for s, m in scored if s >= ANALOG_FLOOR]
        ranked.append({"archetype": aid, "best_episode": best_ep,
                       "similarity": round(best_sim, 4),
                       "offset_estimate":
                           per_episode[best_ep]["offset_estimate"],
                       "analogs": analogs,
                       "analog_count": len(analogs)})
    ranked.sort(key=lambda r: -r["similarity"])

    best = ranked[0] if ranked else None
    declared = bool(best and best["similarity"] >= SIM_FLOOR
                    and best["analog_count"] >= min_analogs)
    reason = ("declared" if declared else
              "no_comparable_match" if not best else
              f"best {best['similarity']:.2f} < floor {SIM_FLOOR}"
              if best["similarity"] < SIM_FLOOR else
              f"analogs {best['analog_count']} < {min_analogs}")
    phase = (_phase_for(best["offset_estimate"], horizon)
             if declared and best["offset_estimate"] is not None else None)
    return {"declared": declared, "reason": reason, "phase": phase,
            "horizon": horizon,
            "best": best, "runner_up": ranked[1] if len(ranked) > 1 else None,
            "per_episode": per_episode}


def _playbook_slice(playbooks, archetype, phase):
    if not (playbooks and archetype and phase):
        return None
    return (playbooks.get("table", {}).get(archetype, {})
            .get(phase)) or None


def _strategy_slice(archetype, phase, registry_path=None, require_cache=False):
    """The Strategy Registry's ranked recipes for a DECLARED (archetype,
    phase) cell — spec §7. Fail-OPEN: any error (missing artifact, bad
    shape) yields None and NEVER blocks the declaration/ledger write; the
    scoring clock must tick even if the registry is absent. top_strategies
    itself already returns an honest 'unavailable' status rather than
    raising — the try/except is belt-and-suspenders."""
    if not (archetype and phase):
        return None
    try:
        from src.analysis.strategy_registry import top_strategies
        return top_strategies(archetype, phase, registry_path=registry_path,
                              require_cache=require_cache)
    except Exception:
        return None


def _last_ledger_line(ledger_path):
    try:
        lines = Path(ledger_path).read_text().strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, json.JSONDecodeError, IndexError):
        return None


def _transition(prev, now):
    """A card-worthy change: any horizon's declare<->undeclare, or a
    declared horizon's archetype/phase moving."""
    def key(doc):
        if not doc:
            return ("nothing",)
        hz_block = doc.get("horizons") or {}
        parts = []
        for hz in sorted(hz_block):
            v = hz_block[hz]
            if v.get("declared"):
                # the state doc nests archetype under "best"; a ledger
                # line stores it flat — one detector reads both shapes
                arch = ((v.get("best") or {}).get("archetype")
                        or v.get("archetype"))
                parts.append((hz, arch, v.get("phase")))
            else:
                parts.append((hz, None, None))
        return tuple(parts) or ("undeclared",)
    return key(prev) != key(now)


def declare(lake_dir=None, templates_path=None, playbooks_path=None,
            state_path=None, ledger_path=None, broadcast_fn=None,
            clock=None, dry_run=False, require_cache=False,
            cache_path=None, strategies_path=None):
    """The nightly run: read lake -> evaluate -> write state artifact +
    ledger line -> ONE Discord card iff the family changed. Fail-open
    on the card; dry_run performs NO writes and NO card.

    `require_cache=True` (the VM executor, via macro_nightly) FORBIDS the
    30-minute recompute: a stale/absent fingerprint cache makes that
    horizon ABSTAIN with a named `cache_miss_*` reason rather than grind
    the e2-micro. Every horizon stamps its `cache_status` (hit/miss_*/
    recomputed) — a fallback is never silent again (the silence ban)."""
    templates = json.loads(
        Path(templates_path or TEMPLATES_PATH).read_text())
    try:
        playbooks = json.loads(
            Path(playbooks_path or PLAYBOOKS_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        playbooks = None                          # honest: slice = None

    as_of, horizons_out = None, {}
    for hz in templates.get("horizons", {}):
        match_cfg = HORIZON_MATCH[hz]
        hz_as_of, current = current_rows(
            lake_dir, length=match_cfg["current_len"], horizon=hz)
        as_of = as_of or hz_as_of
        members = {a["id"]: a["members"]
                   for a in templates["horizons"][hz]["archetypes"]}
        _, cstatus = _load_fingerprint_cache(templates, hz, cache_path)
        if not current or not any(current):
            verdict = {"declared": False,
                       "reason": "empty_current_window", "phase": None,
                       "horizon": hz, "best": None, "runner_up": None,
                       "per_episode": {}}
        else:
            try:
                fps = episode_fingerprints(templates, lake_dir, horizon=hz,
                                           cache_path=cache_path,
                                           require_cache=require_cache)
                if cstatus != "hit":
                    cstatus = "recomputed"
            except CacheUnavailable as exc:
                # THE fail-fast: abstain, do NOT recompute on the e2-micro
                verdict = {"declared": False,
                           "reason": f"cache_{exc.status}_aborted",
                           "phase": None, "horizon": hz, "best": None,
                           "runner_up": None, "per_episode": {}}
                verdict["playbook_slice"] = None
                verdict["cache_status"] = exc.status
                horizons_out[hz] = verdict
                continue
            verdict = evaluate(current, fps, members, horizon=hz)
        verdict["cache_status"] = cstatus
        declared_archetype = (verdict["best"]["archetype"]
                              if verdict["declared"] else None)
        verdict["playbook_slice"] = _playbook_slice(
            playbooks, declared_archetype, verdict["phase"])
        verdict["strategy_slice"] = _strategy_slice(
            declared_archetype, verdict["phase"],
            registry_path=strategies_path, require_cache=require_cache)
        horizons_out[hz] = verdict

    declared_any = any(v["declared"] for v in horizons_out.values())
    now = (clock or datetime.now)().isoformat(timespec="seconds")
    doc = {
        "as_of_session": as_of, "run_at": now,
        "params": {"sim_floor": SIM_FLOOR, "analog_floor": ANALOG_FLOOR,
                   "horizon_match": HORIZON_MATCH,
                   "templates_built_at": templates["built_at"]},
        "declared": declared_any,
        "horizons": {hz: {k: v.get(k) for k in
                          ("declared", "reason", "phase", "best",
                           "runner_up", "playbook_slice", "strategy_slice",
                           "cache_status")}
                     for hz, v in horizons_out.items()},
        "advisory_note": ("public forecast on the record; zero execution "
                          "authority until Dept-5 scoring passes"),
    }

    prev = _last_ledger_line(ledger_path or LEDGER_PATH)
    if not dry_run:
        state = Path(state_path or STATE_PATH)
        state.parent.mkdir(parents=True, exist_ok=True)
        tmp = state.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str))
        tmp.replace(state)
        ledger = Path(ledger_path or LEDGER_PATH)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger_line = {"as_of_session": doc["as_of_session"],
                       "run_at": doc["run_at"],
                       "declared": doc["declared"],
                       "horizons": {}}
        for hz, v in doc["horizons"].items():
            b = v.get("best") or {}
            ss = v.get("strategy_slice") or {}
            ledger_line["horizons"][hz] = {
                "declared": v["declared"], "reason": v["reason"],
                "phase": v["phase"],
                "archetype": b.get("archetype"),
                "episode": b.get("best_episode"),
                "similarity": b.get("similarity"),
                # the strategy calls put ON the record for Dept-5 scoring:
                # the compact top-3, so the immutable ledger stays lean
                "strategy_verdict": ss.get("verdict"),
                "strategy_status": ss.get("status"),
                "top_strategies": [
                    {"name": s.get("name"), "ev": s.get("ev"),
                     "hit_rate": s.get("hit_rate"),
                     "wilson_lb": s.get("wilson_lb"),
                     "significant": s.get("significant")}
                    for s in (ss.get("strategies") or [])[:3]]}
        with ledger.open("a") as fh:
            fh.write(json.dumps(ledger_line, default=str) + "\n")
        if _transition(prev, doc):
            try:
                fn = broadcast_fn
                if fn is None:
                    from src.notifier import fire_broadcast as fn
                lines = []
                for hz, v in sorted(doc["horizons"].items()):
                    if v["declared"]:
                        b = v.get("best") or {}
                        lines.append(
                            f"{hz}: {b.get('archetype')} "
                            f"{v.get('phase') or ''} (analog "
                            f"{b.get('best_episode')}, sim "
                            f"{b.get('similarity')})")
                        ss = v.get("strategy_slice") or {}
                        tops = ss.get("strategies") or []
                        if tops:
                            t = tops[0]
                            lines.append(
                                f"   ↳ {ss.get('verdict')}: {t['name']} "
                                f"EV {t['ev']:+.1%} (hit {t['hit_rate']:.0%}, "
                                f"LB {t['wilson_lb']:.0%})")
                    else:
                        lines.append(f"{hz}: — ({v['reason']})")
                fn({"event": "macro_regime_transition",
                    "title": "🧭 Macro Regime "
                             + ("DECLARED" if doc["declared"]
                                else "cleared"),
                    "description": ("; ".join(lines)
                                    + f" — as of {as_of}")})
            except Exception:
                pass                              # fail-open, never block
    return doc


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = declare(dry_run=args.dry_run)
    for v in out.get("horizons", {}).values():
        v.pop("playbook_slice", None)
    print(json.dumps(out, indent=2, default=str))
