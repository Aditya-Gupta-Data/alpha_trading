"""
src/validation/digest.py — the weekly harness digest (the owner's window)
=========================================================================

Phase 4 of docs/HOLY_GRAIL_PLAN.md (§7.8). The owner chose the weekly
Discord digest (2026-07-11) as the way to see the proving harness: what's
gathering evidence, what validated or died this week, and — the honesty
number — the realized placebo false-discovery rate. A phone-first user
calibrates trust from whatever numbers he sees, so every line leads with a
plain-English verdict and the degradation staircase is shown, not hidden
(the decision-#35 doctrine: confident numbers without their CI train the
human to over-trust).

Read-only over brain_map.db (registry + placebo ledger); composes one
Discord card. Fired weekly (Saturday) via cron. No trades, no writes.

Manual:  python3 -m src.validation.digest
"""

import json
from datetime import date, datetime, timedelta, timezone

from src.validation import placebo as pb
from src.validation import registry as rg
from src.validation import stat_gates as sg


def _wilson_line(stats: dict) -> str:
    real = (stats or {}).get("real") or {}
    n, wins = real.get("n", 0), real.get("wins", 0)
    if not n:
        return "no OOS evidence yet"
    lb = sg.wilson_lower_bound(wins, n)
    return f"{wins}/{n} OOS (win-rate {wins / n:.0%}, 95% LB {lb:.0%})"


def _last_promotion_days(conn, today: date) -> int | None:
    row = conn.execute(
        "SELECT MAX(promoted_at) AS p FROM candidate_patterns "
        "WHERE promoted_at IS NOT NULL").fetchone()
    if not row or not row["p"]:
        return None
    try:
        return (today - date.fromisoformat(row["p"][:10])).days
    except ValueError:
        return None


def _stage_b_block(scoreboard_path=None) -> str:
    """The Stage-B forward-clock section (SB-3), fail-OPEN: a scoreboard read
    error never breaks the pattern digest. Empty string when the clock hasn't
    started (no scoreboard file yet)."""
    try:
        from src.analysis.strategy_scoreboard import digest_lines as _sb
        sb = _sb(scoreboard_path)
    except Exception:
        return ""
    if not sb or sb[0] == "Forward clock: no scoreboard yet.":
        return ""
    return "\n\n**Strategy forward-clock (Stage B):**\n" + "\n".join(sb)


def build_digest(conn, today: date = None, since_days: int = 7,
                 scoreboard_path=None) -> str:
    """Compose the weekly card. Never raises."""
    today = today or date.today()
    rg.ensure_schema(conn)
    counts = {}
    for st in ("CANDIDATE", "TRIAL", "VALIDATED", "LIVE_ADVISORY",
               "INSUFFICIENT_N", "QUARANTINED", "DEAD"):
        counts[st] = len(rg.list_by_status(conn, st))

    since = (today - timedelta(days=since_days)).isoformat()
    events = conn.execute(
        "SELECT pattern_id, to_status, reason, at FROM pattern_audit "
        "WHERE at >= ? ORDER BY at", (since,)).fetchall()
    validated_now = [e for e in events if e["to_status"] == "VALIDATED"]
    killed_now = [e for e in events if e["to_status"] in ("QUARANTINED", "DEAD")]

    total_tracked = sum(counts.values())
    if total_tracked == 0:
        return ("🔬 **Harness digest** — no patterns yet. The miners haven't "
                "run / not enough history. Silence here is correct: nothing "
                "is being surfaced that hasn't earned it."
                + _stage_b_block(scoreboard_path))

    lines = [f"🔬 **Harness digest — week of {today.isoformat()}**"]
    lines.append(
        f"tracked {total_tracked}: {counts['CANDIDATE']} candidate · "
        f"{counts['TRIAL']} in trial · **{counts['VALIDATED'] + counts['LIVE_ADVISORY']} validated** · "
        f"{counts['INSUFFICIENT_N']} gathering · {counts['QUARANTINED']} quarantined · "
        f"{counts['DEAD']} dead")

    if validated_now:
        lines.append("\n**Validated this week:**")
        for e in validated_now[:5]:
            row = rg.get(conn, e["pattern_id"])
            oos = json.loads(row["oos_stats"]) if row and row["oos_stats"] else {}
            lines.append(f"• {rg.mint_tag(e['pattern_id'])} "
                         f"({(row or {}).get('description') or 'pattern'}) — "
                         f"{_wilson_line(oos)}")
    if killed_now:
        lines.append("\n**Killed this week:**")
        for e in killed_now[:5]:
            verb = "☠️ died" if e["to_status"] == "DEAD" else "⚠️ quarantined"
            lines.append(f"• {rg.mint_tag(e['pattern_id'])} {verb}: "
                         f"{(e['reason'] or '')[:80]}")

    # The degradation-staircase honesty line for the current live set.
    live = rg.list_by_status(conn, "VALIDATED") + rg.list_by_status(conn, "LIVE_ADVISORY")
    if live:
        lines.append("\n**Live patterns (validation → now):**")
        for row in live[:5]:
            oos = json.loads(row["oos_stats"]) if row["oos_stats"] else {}
            lines.append(f"• {rg.mint_tag(row['pattern_id'])}: {_wilson_line(oos)}")

    drought = _last_promotion_days(conn, today)
    if drought is None:
        lines.append("\n⏳ Nothing has validated yet — expected while evidence "
                     "accrues (balanced floors).")
    elif drought > 45:
        lines.append(f"\n⏳ {drought} days since the last validation — a "
                     "governed drought, not a bug.")

    fdr = pb.realized_fdr(conn)
    if fdr["state"] == "measured":
        alarm = "  🚨 ABOVE the designed rate — gates may be too loose" if fdr["alarm"] else ""
        lines.append(f"\n🎲 Placebo false-discovery rate: {fdr['rate']:.0%} "
                     f"(LB {fdr['wilson_lb']:.0%} vs designed "
                     f"{fdr['designed_q']:.0%}, n={fdr['n']}){alarm}")
    else:
        lines.append(f"\n🎲 Placebo FDR: {fdr['state']} (n={fdr['n']})")

    return "\n".join(lines) + _stage_b_block(scoreboard_path)


def run(conn=None, db_path=None, today: date = None, notify_fn=None) -> dict:
    """Build + send the digest. Reuses a caller conn or opens its own.
    Returns {sent, card}. Never raises."""
    own = conn is None
    if conn is None:
        from src import brain_map
        conn = brain_map.connect(db_path)
    try:
        card = build_digest(conn, today=today)
    finally:
        if own:
            conn.close()
    sent = False
    if notify_fn is None:
        try:
            from src.notifier import fire_broadcast
            notify_fn = lambda text: fire_broadcast({"text": text})
        except Exception:
            notify_fn = None
    try:
        if notify_fn:
            notify_fn(card)
            sent = True
        else:
            print(card)
    except Exception as exc:
        print(f"  (digest: notify failed [{exc}])")
    return {"sent": sent, "card": card}


if __name__ == "__main__":
    run()
