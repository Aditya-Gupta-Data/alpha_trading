"""
src/discovery/sequence_miner.py — the lagged "A-then-B" miner
=============================================================

Phase 5 of docs/HOLY_GRAIL_PLAN.md (§8.2). The co-occurrence miner
(cooccurrence_miner.py) asks "what market state SITS WITH a good outcome."
This miner asks the harder, more valuable question the owner's theses turn
on — "what market state PRECEDES it." Both H1 (a leader going extended +
high-vol PRECEDES the laggard's breakout) and H2 (smart-money distribution
PRECEDES the sector's drawdown) are lagged-antecedent claims: an early tell
that leads the move. This miner is how the brain can ever discover one on
its own.

The construction is the co-occurrence miner lifted onto a time axis, so it
inherits every honesty rail unchanged:

  * Each resolved outcome's transaction carries LAGGED antecedent tags —
    `lag{k}:<ctx-tag>` = "that market-frame tag was active k trading-days
    BEFORE this trade's entry." Lags default to 1/2/3/5. Same-day state is
    deliberately excluded (that is co-occurrence, not sequence).
  * NO LOOK-AHEAD, structurally. The antecedent frame for lag k is the
    frame k positions earlier than the as-of frame (most recent frame
    dated <= entry) in the daily_context series — always STRICTLY before
    entry. Adding future-dated frames cannot change any transaction
    (proved by a timelock test), honoring decision #50.
  * Entry is anchored on the trade's own ENTRY date — the min date of its
    linked entry-context events — never the exit date, so the lag really
    measures a lead.
  * The pure mining core (Apriori + STRATIFIED base rate + support floor +
    exact-binomial + batch-wide Benjamini-Hochberg + directional filter)
    is cooccurrence_miner.mine, reused verbatim. Real and simulated
    corpora are mined SEPARATELY.

A survivor `lag2:ctx:affinity:distribution` reads "distribution two days
before entry preceded an outcome that beat its stratum base rate"; a
multi-item survivor is a genuine multi-step sequence. Every survivor is a
CANDIDATE that still owes the proving harness its rent. Not wired into the
nightly sleep phase yet (panel: defer until the daily_context series is
long enough for lags to have support). Near-zero survivors on a short
series is the CORRECT output.

Manual:  python3 -m src.discovery.sequence_miner        (real corpus)
         python3 -m src.discovery.sequence_miner sim    (sim corpus)
"""

import bisect
import json
from datetime import date

from src import daily_context as dc
from src.discovery import cooccurrence_miner as cm
from src.validation import registry as rg
from src.validation import stat_gates as sg

LAGS = (1, 2, 3, 5)


# --------------------------------------------------- lagged antecedents

def lagged_antecedent_tags(entry_date: str, ctx_dates: list, frames: dict,
                           lags=LAGS) -> set:
    """The `lag{k}:`-prefixed ctx tags for the frames k trading-days before
    `entry_date`. Pure. The as-of frame is the most recent frame dated
    <= entry_date; lag k reads k positions earlier in the ordered series,
    which is ALWAYS strictly before entry (no look-ahead). Absent frames
    (short history / gaps) simply contribute nothing — NULL-honest."""
    if not ctx_dates:
        return set()
    # Index of the most recent frame dated <= entry_date (as-of).
    as_of = bisect.bisect_right(ctx_dates, entry_date) - 1
    if as_of < 0:
        return set()
    tags = set()
    for k in lags:
        idx = as_of - k
        if idx < 0:
            continue
        frame = frames.get(ctx_dates[idx])
        for t in cm.context_tags(frame):
            tags.add(f"lag{k}:{t}")
    return tags


# --------------------------------------------------- transactions

def build_lagged_transactions(conn, corpus: str = "real", lags=LAGS) -> list:
    """One transaction per resolved outcome: its items are the LAGGED
    antecedent ctx tags leading up to its ENTRY date; win + stratum as in
    the co-occurrence miner. Corpus split is the same is_learnable_ref
    guard. Read-only."""
    dc.ensure_schema(conn)
    frames = {r["date"]: r for r in conn.execute(
        "SELECT * FROM daily_context")}
    ctx_dates = sorted(frames)

    rows = conn.execute(
        """
        SELECT o.id, o.journal_ref, o.date, o.ticker, o.result, o.regime_vix,
               MIN(e.date) AS entry_date
        FROM outcomes o
        LEFT JOIN event_outcome_link l ON l.outcome_id = o.id
        LEFT JOIN events e ON e.id = l.event_id
        GROUP BY o.id
        """
    ).fetchall()

    txns = []
    for r in rows:
        ref = r["journal_ref"]
        learnable = sg.is_learnable_ref(ref)
        if corpus == "real" and not learnable:
            continue
        if corpus == "sim" and learnable:
            continue
        entry_date = r["entry_date"] or r["date"]
        items = lagged_antecedent_tags(entry_date, ctx_dates, frames, lags)
        if not items:
            continue
        # Stratum vix-band = the as-of (entry-day) frame's band, else the
        # trade's own recorded regime band.
        as_of = bisect.bisect_right(ctx_dates, entry_date) - 1
        band = None
        if as_of >= 0:
            f = frames.get(ctx_dates[as_of])
            band = f["vix_band"] if f else None
        band = band or r["regime_vix"]
        txns.append({"items": frozenset(items),
                     "win": r["result"] == "win",
                     "stratum": (r["ticker"], band)})
    return txns


# --------------------------------------------------- registration

def _definition(cand: dict) -> dict:
    """Frozen predicate. `kind:"sequence"` keeps it distinct from a
    co-occurrence pattern even if two tag-sets ever coincided; the lag
    prefixes already namespace the tags."""
    return {"kind": "sequence", "tags": cand["tags"]}


def register_survivors(conn, survivors: list, corpus: str,
                       run_id: str = "", window: str = "") -> list:
    """Register each surviving lagged sequence as a CANDIDATE. Idempotent
    on the frozen definition. Returns the register() results."""
    results = []
    for cand in survivors:
        desc = (f"[{corpus}] SEQ " + " → ".join(cand["tags"]) +
                f"  ({cand['wins']}/{cand['n']} = {cand['win_rate']:.0%} vs "
                f"base {cand['expected_rate']:.0%})")
        res = rg.register(
            conn, "sequence", _definition(cand), description=desc,
            mining_run=run_id or f"sequence:{corpus}",
            discovery_window=window, support_n=cand["support"],
            fdr_q=sg.configured_floors()["fdr_q"],
            insample_stats={"corpus": corpus, "n": cand["n"],
                            "wins": cand["wins"],
                            "win_rate": round(cand["win_rate"], 4),
                            "expected_rate": cand["expected_rate"],
                            "p_value": cand["p_value"],
                            "lift": round(cand["lift"], 4)})
        results.append({**res, "tags": cand["tags"]})
    return results


def run(conn=None, db_path=None, corpus: str = "real",
        today: date = None, lags=LAGS) -> dict:
    """Mine one corpus for lagged sequences and register survivors. Reuses
    a caller conn or opens its own. Never raises."""
    own = conn is None
    if conn is None:
        from src import brain_map
        conn = brain_map.connect(db_path)
    today = today or date.today()
    try:
        txns = build_lagged_transactions(conn, corpus=corpus, lags=lags)
        survivors = cm.mine(txns)
        registered = register_survivors(
            conn, survivors, corpus,
            run_id=f"sequence:{corpus}:{today.isoformat()}",
            window=f"..{today.isoformat()}")
        new = sum(1 for r in registered if r.get("created"))
    finally:
        if own:
            conn.close()
    summary = {"corpus": corpus, "transactions": len(txns),
               "survivors": len(survivors), "newly_registered": new,
               "registered": registered}
    print(f"  (sequence miner [{corpus}]: {len(txns)} txns -> "
          f"{len(survivors)} survivor(s), {new} newly registered)")
    return summary


if __name__ == "__main__":
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else "real"
    print(json.dumps(run(corpus=which), indent=2, default=str))
