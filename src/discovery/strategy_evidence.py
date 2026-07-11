"""
src/discovery/strategy_evidence.py — the pattern×strategy evidence view
=======================================================================

Phase 5 of docs/HOLY_GRAIL_PLAN.md (§8.6, the middle rung). The owner's
"check WHICH strategy" question, answered honestly: for a given pattern
(a set of tags), which defined-risk STRUCTURE has actually earned evidence
— iron condor vs bull-call vs bear-put — and by how much, with the
degradation the harness insists on shown, never a bare point estimate.

This is the DESCRIPTIVE substrate the champion/challenger duel later
consumes; it is NOT the duel. The plan gates the duel on a validated
pattern + counterfactual pricing + a ≥30-day disagreement floor, none of
which exist yet — and a selector that auto-picks a structure is explicitly
human-gated (#49) and never auto-applied. So this module only READS and
RENDERS. It recommends a structure only as far as the evidence has earned,
and ABSTAINS loudly the moment two structures are within noise of each
other or none clears the floor.

The honesty rails (delegated to stat_gates; nothing inline):
  * REAL and SIMULATED resolutions are split and NEVER pooled. A structure
    that only "wins" in simulator replays is shown as such and can never be
    preferred (sim supports, never solely justifies — the locked policy).
  * A structure row is not even RENDERED below MIN_VIEW_RESOLUTIONS real
    resolutions (default 5) — a 2-real comparison is noise wearing a
    preference.
  * Every win-rate carries its one-sided 95% Wilson LOWER bound, and the
    preference is decided on the LOWER bound, not the headline rate. A
    6/7 (86%) structure whose LB is 47% does not out-rank a 12/15 (80%,
    LB 58%) one — the honest number wins.

Manual:  python3 -m src.discovery.strategy_evidence golden_cross fii_selling
"""

import json

from src.validation import stat_gates as sg


def _corpus(journal_ref: str) -> str:
    """Which corpus a resolution belongs to: 'real' unless its ref is one
    of the system's own hypotheses/replays (sim:/shadow:/trial:/placebo:)."""
    return "real" if sg.is_learnable_ref(journal_ref) else "sim"


def _stat_block(rows: list) -> dict:
    """{n, wins, losses, scratch, win_rate, wilson_lb, avg_r} over a group.
    Scratches count against the win-rate (the review.py convention)."""
    n = len(rows)
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    r_vals = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    return {"n": n, "wins": wins, "losses": losses, "scratch": n - wins - losses,
            "win_rate": round(wins / n, 4) if n else None,
            "wilson_lb": round(sg.wilson_lower_bound(wins, n), 4) if n else 0.0,
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else None}


def strategy_breakdown(conn, tags, min_real: int = None) -> dict:
    """For every outcome linked to an event carrying any of `tags`, break
    the evidence down BY STRATEGY (archetype), real and sim strata kept
    apart. A strategy is `renderable` only once its REAL resolutions clear
    the view floor. Returns the ranked view; never raises."""
    if isinstance(tags, str):
        tags = [tags]
    tags = [t for t in (tags or [])]
    floor = (sg.configured_floors()["min_view_resolutions"]
             if min_real is None else min_real)
    empty = {"tags": tags, "floor": floor, "strategies": [],
             "total_real": 0, "total_sim": 0}
    if not tags:
        return empty

    placeholders = ", ".join("?" for _ in tags)
    rows = conn.execute(
        f"""
        SELECT o.id, o.journal_ref, o.archetype, o.result, o.r_multiple
        FROM outcomes o
        JOIN event_outcome_link l ON l.outcome_id = o.id
        JOIN events e ON e.id = l.event_id
        WHERE e.tag IN ({placeholders})
        GROUP BY o.id
        """, tags).fetchall()
    if not rows:
        return empty

    groups = {}   # archetype -> {"real": [...], "sim": [...]}
    for r in rows:
        strat = r["archetype"] or "unknown"
        bucket = groups.setdefault(strat, {"real": [], "sim": []})
        bucket[_corpus(r["journal_ref"])].append(r)

    strategies = []
    for strat, buckets in groups.items():
        real, sim = _stat_block(buckets["real"]), _stat_block(buckets["sim"])
        renderable = real["n"] >= floor
        strategies.append({
            "strategy": strat, "real": real, "sim": sim,
            "renderable": renderable,
            "reason": ("" if renderable
                       else f"{real['n']}/{floor} real resolutions — "
                            "gathering evidence")})
    # Rank renderable-first, then by the HONEST number (real Wilson LB).
    strategies.sort(key=lambda s: (s["renderable"], s["real"]["wilson_lb"]),
                    reverse=True)
    return {"tags": tags, "floor": floor, "strategies": strategies,
            "total_real": sum(s["real"]["n"] for s in strategies),
            "total_sim": sum(s["sim"]["n"] for s in strategies)}


def preferred_structure(breakdown: dict) -> dict:
    """The descriptive verdict over a breakdown: PREFER one structure only
    when its REAL Wilson lower bound clears the runner-up's HEADLINE rate
    (a conservative bound beating an optimistic point = genuine, not-noise
    separation). Otherwise ABSTAIN — either nothing clears the floor, or
    the top structures overlap. Never fabricates a preference."""
    renderable = [s for s in breakdown.get("strategies", []) if s["renderable"]]
    if not renderable:
        return {"verdict": "ABSTAIN",
                "reason": f"no structure has ≥{breakdown.get('floor')} real "
                          "resolutions for this pattern yet",
                "structure": None}
    top = renderable[0]
    if len(renderable) == 1:
        return {"verdict": "PREFER", "structure": top["strategy"],
                "wilson_lb": top["real"]["wilson_lb"],
                "reason": (f"{top['strategy']}: {top['real']['wins']}/"
                           f"{top['real']['n']} real (95% LB "
                           f"{top['real']['wilson_lb']:.0%}); no other "
                           "structure has cleared the floor")}
    rival = renderable[1]
    clear = top["real"]["wilson_lb"] > (rival["real"]["win_rate"] or 0.0)
    if clear:
        return {"verdict": "PREFER", "structure": top["strategy"],
                "wilson_lb": top["real"]["wilson_lb"],
                "runner_up": rival["strategy"],
                "reason": (f"{top['strategy']} LB {top['real']['wilson_lb']:.0%} "
                           f"beats {rival['strategy']}'s headline "
                           f"{(rival['real']['win_rate'] or 0):.0%} — real "
                           "separation")}
    return {"verdict": "ABSTAIN", "structure": None,
            "reason": (f"{top['strategy']} and {rival['strategy']} are within "
                       "noise of each other (overlapping evidence) — no "
                       "honest preference")}


def summarize(breakdown: dict, verdict: dict = None) -> str:
    """A phone-first card. Descriptive evidence, not a blended score —
    obeys the composition law (#63): it emits PREFER/ABSTAIN as ONE context
    verdict, never points added into someone else's headline."""
    verdict = verdict or preferred_structure(breakdown)
    tags = " + ".join(breakdown.get("tags", [])) or "(no tags)"
    if not breakdown.get("strategies"):
        return f"🧭 Strategy evidence for [{tags}]: no resolved trades yet."
    lines = [f"🧭 **Strategy evidence — [{tags}]**",
             f"{breakdown['total_real']} real · {breakdown['total_sim']} sim "
             f"resolutions (floor {breakdown['floor']} real to render)"]
    for s in breakdown["strategies"]:
        r = s["real"]
        if s["renderable"]:
            avg = f", avg {r['avg_r']:+.2f}R" if r["avg_r"] is not None else ""
            lines.append(f"• **{s['strategy']}** — {r['wins']}/{r['n']} real "
                         f"({(r['win_rate'] or 0):.0%}, LB "
                         f"{r['wilson_lb']:.0%}{avg})")
        else:
            lines.append(f"• {s['strategy']} — {s['reason']} (not rendered)")
    if verdict["verdict"] == "PREFER":
        lines.append(f"\n➡️ **PREFER {verdict['structure']}** — {verdict['reason']}")
    else:
        lines.append(f"\n➡️ **ABSTAIN** — {verdict['reason']}")
    return "\n".join(lines)


def view(conn, tags, min_real: int = None) -> dict:
    """Breakdown + verdict + card in one call. Read-only, never raises."""
    breakdown = strategy_breakdown(conn, tags, min_real=min_real)
    verdict = preferred_structure(breakdown)
    return {"breakdown": breakdown, "verdict": verdict,
            "card": summarize(breakdown, verdict)}


if __name__ == "__main__":
    import sys
    from src import brain_map
    conn = brain_map.connect()
    try:
        out = view(conn, sys.argv[1:])
        print(out["card"])
        print("\n" + json.dumps({"verdict": out["verdict"]}, indent=2))
    finally:
        conn.close()
