"""
src/validation/monitor.py — validation is a lease, not a diploma
================================================================

Phase 4 of docs/HOLY_GRAIL_PLAN.md (§7.6). Markets are non-stationary; a
harness that only gates ENTRY ratifies stale edges forever. This closes
the loop with two mechanisms, both no-LLM and VM-safe (a Sleep-Phase
task):

  DRIFT (CUSUM) — every resolved shadow outcome for a VALIDATED /
                  LIVE_ADVISORY pattern updates a one-sided CUSUM against
                  its validated win-rate. A breach means the edge is
                  bleeding: the pattern AUTO-QUARANTINES (owner's choice
                  2026-07-11 — protective demotion is automatic) and a
                  Discord card fires stating what died and why.
  EXPIRY        — VALIDATED status carries an adaptive lease:
                  max(90 days, time to accrue K new matching resolutions).
                  An expired pattern silently demotes to CANDIDATE and must
                  re-clear the trial on evidence that now includes the
                  period it was live (re-trial on a GROWING sample).

A faster, cruder tripwire rides alongside CUSUM: the Wilson lower bound of
the pattern's LIVE win-rate crossing back below the structural breakeven
also quarantines (CUSUM can lag at a few matches/month). Quarantine +
re-trial is governed by the registry, so a second quarantine is DEAD
(the lineage rule). Nothing here writes journal/portfolio.
"""

import json
from datetime import date, datetime, timedelta, timezone

from src.validation import registry as rg
from src.validation import stat_gates as sg
from src.validation import trial as tr

# CUSUM tuned for a ~10% false-alarm rate over a validity window at a few
# matches/month: slack = half the edge we expect to keep; alarm at h * slack.
CUSUM_H = 5.0
VALIDATION_LEASE_DAYS = 90
LEASE_RENEW_MATCHES = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cusum_breach(outcomes: list, validated_rate: float,
                 h: float = CUSUM_H) -> dict:
    """One-sided downward CUSUM on a win(1)/loss(0) sequence against the
    validated win-rate. Each step: S = max(0, S + (target - k - x)), where
    k (slack) = half the margin the validated rate held over breakeven-ish
    0.5. Breach when S exceeds h. Returns {breached, peak, at_index}."""
    if not outcomes:
        return {"breached": False, "peak": 0.0, "at_index": None}
    slack = max(0.02, (validated_rate - 0.5) / 2.0) if validated_rate > 0.5 \
        else 0.05
    s, peak, at = 0.0, 0.0, None
    for i, x in enumerate(outcomes):
        s = max(0.0, s + (validated_rate - slack - (1 if x else 0)))
        if s > peak:
            peak = s
        if s > h and at is None:
            at = i
    return {"breached": at is not None, "peak": round(peak, 3), "at_index": at}


def live_outcomes(conn, pattern_id: str, since: str = None) -> list:
    """Resolved shadow outcomes for a pattern in fire-date order (1=win,
    0=else), optionally only on/after `since` (the promotion date — the
    LIVE period). Read-only."""
    tr.ensure_schema(conn)
    rows = conn.execute(
        "SELECT fire_date, result FROM shadow_trades WHERE pattern_id = ? "
        "AND resolved = 1 ORDER BY fire_date, journal_ref", (pattern_id,)
    ).fetchall()
    out = []
    for r in rows:
        if since and r["fire_date"] and r["fire_date"][:10] < since[:10]:
            continue
        out.append(1 if r["result"] == "win" else 0)
    return out


def _validated_rate(row: dict) -> float:
    """The win-rate the pattern was validated at, from its oos_stats."""
    try:
        stats = json.loads(row["oos_stats"]) if row["oos_stats"] else {}
        real = stats.get("real") or {}
        n, wins = real.get("n", 0), real.get("wins", 0)
        return wins / n if n else 0.6
    except (ValueError, TypeError):
        return 0.6


def check_pattern(conn, pattern_id: str, today: date = None,
                  avg_win_r: float = 1.5, avg_loss_r: float = 1.0) -> dict:
    """Assess ONE validated/live pattern for drift or lease expiry, and
    ACT on the registry. Returns {action, reason, card|None}. Actions:
    quarantined | expired_to_candidate | held."""
    today = today or date.today()
    row = rg.get(conn, pattern_id)
    if row is None or row["status"] not in ("VALIDATED", "LIVE_ADVISORY"):
        return {"action": "held", "reason": "not a live pattern", "card": None}

    since = (row["promoted_at"] or "")[:10] or None
    outs = live_outcomes(conn, pattern_id, since=since)
    vrate = _validated_rate(row)

    # 1) CUSUM drift OR the cruder Wilson-crossing tripwire.
    breach = cusum_breach(outs, vrate)
    null_rate = sg.breakeven_win_rate(avg_win_r, avg_loss_r)
    live_wins = sum(outs)
    wilson = sg.wilson_lower_bound(live_wins, len(outs)) if outs else 1.0
    wilson_cross = len(outs) >= sg.configured_floors()["min_resolutions"] \
        and wilson < null_rate
    if breach["breached"] or wilson_cross:
        why = ("CUSUM drift" if breach["breached"]
               else f"live Wilson LB {wilson:.2f} fell below breakeven "
                    f"{null_rate:.2f}")
        res = rg.transition(conn, pattern_id, "QUARANTINED", why)
        dead = res["status"] == "DEAD"
        card = quarantine_card(pattern_id, row, vrate, live_wins, len(outs),
                               why, dead)
        return {"action": "dead" if dead else "quarantined",
                "reason": why, "card": card}

    # 2) Adaptive lease expiry: past 90 days AND enough live matches to
    #    re-earn it -> demote to CANDIDATE for a re-trial on a bigger sample.
    if since:
        try:
            live_days = (today - date.fromisoformat(since)).days
        except ValueError:
            live_days = 0
        if live_days >= VALIDATION_LEASE_DAYS and len(outs) >= LEASE_RENEW_MATCHES:
            rg.transition(conn, pattern_id, "CANDIDATE",
                          f"lease expired ({live_days}d, {len(outs)} live "
                          "matches) — re-trial on the grown sample")
            return {"action": "expired_to_candidate",
                    "reason": f"lease expired after {live_days}d", "card": None}

    return {"action": "held", "reason": "within lease, no drift", "card": None}


def quarantine_card(pattern_id, row, vrate, live_wins, live_n, why,
                    dead: bool) -> str:
    desc = row.get("description") or row.get("kind") or "pattern"
    tag = rg.mint_tag(pattern_id)
    head = ("💀 **PATTERN DIED**" if dead
            else "⚠️ **PATTERN QUARANTINED**")
    return (
        f"{head} — {tag} ({desc})\n"
        f"validated at {vrate:.0%} win-rate; live it went "
        f"{live_wins}/{live_n}. {why}. "
        + ("Second quarantine — retired permanently."
           if dead else "Stopped being cited; may re-trial if it recovers.")
    )


def run_sweep(conn, today: date = None, notify_fn=None) -> dict:
    """Sweep every live pattern (Sleep-Phase task). Auto-acts per pattern
    and fires ONE Discord card per quarantine/death (owner's choice:
    auto-quarantine + notify). Returns a summary. Never raises."""
    today = today or date.today()
    live = (rg.list_by_status(conn, "VALIDATED")
            + rg.list_by_status(conn, "LIVE_ADVISORY"))
    summary = {"checked": len(live), "quarantined": 0, "dead": 0,
               "expired": 0, "held": 0}
    cards = []
    for row in live:
        try:
            res = check_pattern(conn, row["pattern_id"], today=today)
        except Exception as exc:
            print(f"  (monitor: {row['pattern_id']} check failed [{exc}])")
            continue
        action = res["action"]
        if action == "quarantined":
            summary["quarantined"] += 1
        elif action == "dead":
            summary["dead"] += 1
        elif action == "expired_to_candidate":
            summary["expired"] += 1
        else:
            summary["held"] += 1
        if res.get("card"):
            cards.append(res["card"])
    if cards and notify_fn is None:
        try:
            from src.notifier import fire_broadcast
            notify_fn = lambda text: fire_broadcast({"text": text})
        except Exception:
            notify_fn = None
    for card in cards:
        try:
            if notify_fn:
                notify_fn(card)
            else:
                print(card)
        except Exception:
            pass
    return summary
