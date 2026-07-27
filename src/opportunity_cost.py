"""
src/opportunity_cost.py — did our risk gates save us money, or cost us?
(Directive 1, docs/opportunity_cost_design.md)

READ-ONLY over the opportunity-cost rows that `exposure_gate` routes into
the EXISTING `shadow_trades` table (mode = BLOCKED_BY_RISK). No new
database, no new resolver: those rows are host-linked to the real position
that caused the block, and the existing Sleep-Phase Task I sweep
(`discovery.shadow_runner.resolve_from_outcomes`) fills their outcome from
that host's.

HOW TO READ THE NUMBER — the honest framing, carried onto every render:
the blocked duplicate inherits the BLOCKING position's outcome. Same
underlying, same direction, overlapping window, but a different strike
structure — so the R is a PROXY, not the refused trade's true R.

  hosts mostly WON   -> the gate refused trades that would likely have won
                        -> the gate is COSTING us second winners
  hosts mostly LOST  -> the gate refused trades that would likely have lost
                        -> the gate is SAVING us second losers

WHAT IT DOES NOT MEASURE: decision #68's purpose was structural — capping
concentration so two correlated losers cannot compound into one drawdown.
A gate can cost expectancy and still be right on variance. This is one
input to that judgement, never the verdict.

Only the exposure gate appears here. Halt/margin/sizing-veto blocks have no
host trade, and inventing one would require the synthetic-chain model whose
known ~10x generosity (HANDOVER open item 5) would make every risk gate look
more expensive than it is. Those blocks stay in their own ledgers, counted
and honest, without a fabricated P&L.

    python3 -m src.opportunity_cost
"""
from src.validation import trial

MIN_RESOLVED_FOR_A_VERDICT = 5     # below this, report the count and abstain


def collect(conn=None, db_path=None) -> dict:
    """Roll up opportunity-cost rows. Returns
    {available, blocked_total, resolved, wins, losses, scratches,
     sum_r, avg_r, verdict, by_gate}."""
    owns = conn is None
    try:
        if conn is None:
            from src import brain_map
            conn = brain_map.connect(db_path)
        trial.ensure_schema(conn)
        rows = conn.execute(
            "SELECT pattern_id, resolved, result, r_multiple "
            "FROM shadow_trades WHERE mode = ?",
            (trial.BLOCKED_MODE,)).fetchall()
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    by_gate, wins, losses, scratches, rs = {}, 0, 0, 0, []
    for r in rows:
        gate = str(r["pattern_id"] or "").replace("blocked:", "") or "unknown"
        g = by_gate.setdefault(gate, {"blocked": 0, "resolved": 0})
        g["blocked"] += 1
        if not r["resolved"]:
            continue
        g["resolved"] += 1
        if r["result"] == "win":
            wins += 1
        elif r["result"] == "loss":
            losses += 1
        else:
            scratches += 1
        if r["r_multiple"] is not None:
            rs.append(float(r["r_multiple"]))

    resolved = wins + losses + scratches
    sum_r = round(sum(rs), 3) if rs else None
    return {
        "available": True,
        "blocked_total": len(rows),
        "resolved": resolved,
        "wins": wins, "losses": losses, "scratches": scratches,
        "sum_r": sum_r,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "verdict": _verdict(resolved, wins, losses),
        "by_gate": by_gate,
    }


def _verdict(resolved, wins, losses) -> str:
    """ACCUMULATING until there is enough to say anything — the house
    discipline (#50): a verdict on 2 blocks is theatre."""
    if resolved < MIN_RESOLVED_FOR_A_VERDICT:
        return "ACCUMULATING"
    if wins > losses:
        return "COSTING"
    if losses > wins:
        return "SAVING"
    return "INCONCLUSIVE"


def render_lines(stats: dict = None, **kwargs) -> list:
    """Plain-English lines for a digest/CLI. Honest when there is nothing
    to say yet — never a fabricated verdict."""
    stats = stats if stats is not None else collect(**kwargs)
    if not stats.get("available"):
        return [f"Opportunity cost: unavailable ({stats.get('reason')})."]
    if not stats["blocked_total"]:
        return ["Opportunity cost: no trades have been blocked yet — "
                "nothing to weigh."]

    lines = [f"The exposure gate has refused **{stats['blocked_total']}** "
             f"duplicate trade(s); **{stats['resolved']}** of those can now "
             "be judged (the position that caused the block has resolved)."]
    if stats["verdict"] == "ACCUMULATING":
        lines.append(
            f"Too few resolved to call it — a verdict needs "
            f"{MIN_RESOLVED_FOR_A_VERDICT}. Accumulating honestly.")
        return lines

    lines.append(f"Of those, the exposure we already held won "
                 f"{stats['wins']} and lost {stats['losses']}"
                 + (f" (scratch {stats['scratches']})"
                    if stats["scratches"] else "") + ".")
    if stats["verdict"] == "COSTING":
        lines.append("➡️ On this record the gate is **costing** us — the "
                     "duplicates it refused would likely have won too.")
    elif stats["verdict"] == "SAVING":
        lines.append("➡️ On this record the gate is **saving** us — the "
                     "duplicates it refused would likely have lost too.")
    else:
        lines.append("➡️ No edge either way on this record.")
    if stats["avg_r"] is not None:
        lines.append(f"Average outcome of the blocking position: "
                     f"{stats['avg_r']:+.2f}R — a PROXY for the refused "
                     "trade (same underlying and direction, different "
                     "strikes), never its true R.")
    lines.append("_Measures P&L only. The gate's actual job (#68) is capping "
                 "concentration so two correlated losers can't compound — a "
                 "gate can cost expectancy and still be right on variance._")
    return lines


def main() -> int:
    stats = collect()
    print("Opportunity Cost — what our risk gates refused\n")
    for line in render_lines(stats):
        print("  " + line.replace("**", "").replace("_", ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
