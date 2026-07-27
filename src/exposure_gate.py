"""
src/exposure_gate.py — structural exposure cap + trend-flip exit advisory
=========================================================================

Decision #68. Born from the 2026-07-13 observation: the paper book had
accumulated NINE near-identical bear put spreads over three sessions —
one bearish view expressed nine times (~Rs.49k correlated max loss) —
because nothing between the 2h cooldown and the margin gate ever looked
at what was already open. HANDOVER flagged exactly this gap when
PAPER_AUTO_APPROVE went live: duplicate-exposure judgment used to be the
human tap's job, and nothing replaced it.

Two features, one open-positions vocabulary:

  gate_entry(proposal)        ONE open spread per underlying+direction.
                              Called by options_proposer.run_headless
                              BEFORE the margin gate (a blocked duplicate
                              must never lock margin) and only for the
                              real paper book — injected sandbox books
                              are their own worlds, same exemption as the
                              margin gate. Blocks are logged to
                              logs/exposure_blocks.jsonl and announced on
                              Discord at most once per (ticker,
                              direction) per IST day — the ledger IS the
                              once-per-day persistence (Issue-8 pattern,
                              no new state file).

  trend_flip_advisory(...)    When the binary trend read (the same
                              SMA50-vs-SMA200 boolean every proposal was
                              born from) flips AGAINST open directional
                              positions, fire ONE advisory Discord card.
                              Advisory only by hard rule (#11/#41):
                              nothing here settles, journals, or touches
                              trade state. Neutral structures (condors/
                              butterflies) are trend-agnostic and never
                              fire; bullish->neutral does not fire either
                              (neutral is the resting state of an uptrend
                              in market_view — the SMA cross is the
                              honest contradiction, not an RSI wobble).

DOCTRINE: both features are binary verdicts (block / advise), never
scores (#63 composition law). Both fail OPEN — an unreadable journal, a
dead quote feed, a broken ledger write can only ever mean "behave as if
this module didn't exist", never "block a proposal" or "kill a cycle".
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "exposure_blocks.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

# Fallback vocabulary for entries that predate strategy.py's stamped
# spread["direction"]. The stamp (StrategyConstructor._package) is the
# authoritative source; tests/test_exposure_gate.py asserts this map
# agrees with every constructor so the two can never drift.
DIRECTION_BY_STRATEGY = {
    "bull_call_spread": "bullish",
    "bear_put_spread":  "bearish",
    "iron_condor":      "neutral",
    "iron_butterfly":   "neutral",
}

_DIRECTIONS = ("bullish", "bearish", "neutral")


def direction_of(spread_or_position: dict) -> str | None:
    """Directional bias of a spread block or a positions.py row: the
    stamped `direction` first (structural truth), the strategy-name map
    as fallback, None when unclassifiable — and an unclassifiable
    position never blocks anything."""
    if not isinstance(spread_or_position, dict):
        return None
    d = spread_or_position.get("direction")
    if d in _DIRECTIONS:
        return d
    return DIRECTION_BY_STRATEGY.get(spread_or_position.get("strategy"))


# --------------------------------------------------- feature 1: entry gate

def conflicting_positions(ticker: str, direction: str,
                          entries: list = None, today: date = None) -> list:
    """Open spread positions (positions.active_positions — the tracker's
    own predicates, approved + unresolved only) on `ticker` whose
    direction matches. Unclassifiable directions are skipped."""
    from src import positions
    return [p for p in positions.active_positions(entries, today)
            if p.get("kind") == "spread"
            and p.get("ticker") == ticker
            and direction_of(p) == direction]


def gate_entry(proposal: dict, entries: list = None,
               today: date = None, notify_fn=None, record_fn=None) -> tuple:
    """(allowed, reason). ONE open position per underlying+direction —
    bullish, bearish and neutral each get one slot per index. Fail-OPEN
    by hard rule: ANY failure returns (True, ...) with a printed note,
    the margin gate's exact contract."""
    try:
        spread = proposal.get("spread") or {}
        direction = direction_of(spread)
        if direction is None and proposal.get("view") in _DIRECTIONS:
            direction = proposal["view"]
        ticker = proposal.get("ticker")
        if not ticker or direction is None:
            return True, "allowed (unclassifiable proposal — gate skipped)"

        conflicts = conflicting_positions(ticker, direction, entries, today)
        if not conflicts:
            return True, "allowed"

        ids = [str(p.get("trade_id") or "?") for p in conflicts]
        _log_block(ticker, direction, spread.get("strategy"),
                   ids, proposal.get("view"), today=today,
                   notify_fn=notify_fn, conflicts=conflicts)
        _record_opportunity_cost(ticker, direction, conflicts, today=today,
                                 record_fn=record_fn)
        return False, (
            f"exposure gate: {len(conflicts)} open {direction} "
            f"position(s) on {ticker} already (`{'`, `'.join(ids)}`) — "
            "max one per underlying+direction (decision #68)")
    except Exception as e:
        print(f"  (exposure gate unavailable — failing open: {e})")
        return True, f"exposure gate unavailable ({e})"


def _record_opportunity_cost(ticker, direction, conflicts, *, today=None,
                             record_fn=None) -> None:
    """Directive 1 (docs/opportunity_cost_design.md): route this block into
    the EXISTING shadow_trades table, host-linked to the position that
    caused it — so the existing Sleep-Phase sweep resolves it the night
    that position resolves, and we can finally answer whether this gate
    saves or costs money.

    THE HOST IS THE POINT: `positions.trade_id` IS `outcomes.journal_ref`
    (both the journal short_id), so no new resolver is needed. The
    inherited outcome is an honest PROXY — same underlying, same
    direction, overlapping window, different strikes.

    Fail-open and verdict-neutral by the gate's hard rule: every failure
    here is swallowed. Bookkeeping never changes whether a trade is
    blocked."""
    try:
        host = next((str(p.get("trade_id")) for p in (conflicts or [])
                     if p.get("trade_id")), None)
        if not host:
            return          # no host -> unresolvable -> do not record a ghost
        day = (today or datetime.now(IST).date()).isoformat()
        if record_fn is not None:
            record_fn(gate="exposure_gate", fire_date=day, ticker=ticker,
                      direction=direction, host_ref=host)
            return
        # THE MUZZLE — added the same hour this seam shipped, because it
        # was already wrong: under pytest this path opened the REAL
        # brain_map.db and wrote fixture rows into it (4 of them, host_ref
        # 'ab12cd34'), and `python3 -m src.opportunity_cost` then reported
        # "the exposure gate has refused 4 duplicate trade(s)" — a
        # fabricated number in a risk report. The gate's own tests sandbox
        # LEDGER_PATH but had no way to sandbox a DB connection this seam
        # opened itself. Same doctrine as `notifier.webhooks_muzzled()`:
        # everything under pytest is muzzled automatically, so no future
        # test can re-poison the live record by forgetting a fixture.
        # Tests exercise this seam through `record_fn=` (or their own conn).
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        from src import brain_map
        from src.validation import trial
        conn = brain_map.connect()
        try:
            trial.record_block(conn, gate="exposure_gate", fire_date=day,
                               ticker=ticker, direction=direction,
                               host_ref=host)
        finally:
            conn.close()
    except Exception as e:
        print(f"  (opportunity-cost row skipped: {e})")


def _log_block(ticker, direction, strategy, blocked_by, view, *,
               today=None, notify_fn=None, conflicts=None) -> None:
    """Ledger line for every block + at most ONE Discord note per
    (ticker, direction) per IST day. The ledger doubles as the
    once-per-day memory, so a restart can't re-announce. Every failure
    in here is swallowed — bookkeeping never changes the verdict."""
    day = (today or datetime.now(IST).date()).isoformat()
    noted_today = False
    try:
        if LEDGER_PATH.exists():
            for line in LEDGER_PATH.read_text().splitlines():
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if (rec.get("ticker") == ticker
                        and rec.get("direction") == direction
                        and str(rec.get("ts", "")).startswith(day)):
                    noted_today = True
                    break
    except OSError:
        pass
    try:
        LEDGER_PATH.parent.mkdir(exist_ok=True)
        with open(LEDGER_PATH, "a") as f:
            f.write(json.dumps({
                # Stamp with the gate's evaluation day so injected `today`
                # (tests, replays) and the once-per-day scan above agree.
                "ts": f"{day}T{datetime.now(IST).time().isoformat(timespec='seconds')}",
                "ticker": ticker, "direction": direction,
                "strategy": strategy, "blocked_by": blocked_by,
                "view": view}) + "\n")
    except OSError:
        pass
    if notify_fn and not noted_today:
        rep = (conflicts or [{}])[0]
        try:
            notify_fn(
                f"🧱 **Exposure gate — {ticker}**: suppressed a duplicate "
                f"{direction} spread proposal.\n"
                f"{len(blocked_by)} open {direction} position(s) already "
                f"(`{blocked_by[0]}`, "
                f"{(rep.get('strategy') or 'spread').replace('_', ' ')}, "
                f"exp {rep.get('expiry') or '?'}).\n"
                f"Further {direction} {ticker} duplicates today are "
                "blocked silently (decision #68).")
        except Exception:
            pass


# ------------------------------------------- feature 2: trend-flip advisory

class TrendFlipRegistry:
    """Per-ticker last-observed uptrend bool. observe() returns True when
    the value CHANGED — or on the first observation, so a daemon starting
    into an already-contradicted book alerts once at session start
    instead of never. Process-lifetime state, same semantics as
    live_bridge.AlertRegistry (a mid-session restart re-fires at most
    once)."""

    def __init__(self):
        self._last: dict = {}

    def observe(self, ticker: str, uptrend: bool) -> bool:
        changed = self._last.get(ticker, object()) != uptrend
        self._last[ticker] = uptrend
        return changed


# Daily closes are static intraday: ONE fetch per (ticker, session date),
# then every 60s cycle re-runs the pure math on cached bars + live spot.
_CLOSES_CACHE: dict = {}


def trend_flip_advisory(ticker: str, spot: float, *,
                        registry: TrendFlipRegistry,
                        entries: list = None, closes_fn=None,
                        analysis_fn=None, today: date = None) -> dict | None:
    """None, or {"ticker", "uptrend", "affected", "card"} when the binary
    trend read flipped and >=1 open directional position on `ticker` is
    contradicted. Read-only; fail-open: any error returns None quietly."""
    try:
        from src import positions
        directional = [
            p for p in positions.active_positions(entries, today)
            if p.get("kind") == "spread" and p.get("ticker") == ticker
            and direction_of(p) in ("bullish", "bearish")]
        if not directional:
            return None

        day = (today or datetime.now(IST).date()).isoformat()
        key = (ticker, day)
        if key not in _CLOSES_CACHE:
            if closes_fn is None:
                from src.dhan_client import get_daily_closes
                closes_fn = get_daily_closes
            _CLOSES_CACHE[key] = list(closes_fn(ticker) or [])
        closes = _CLOSES_CACHE[key]
        if not closes:
            return None

        if analysis_fn is None:
            from src.simulator import analysis_from_closes
            analysis_fn = analysis_from_closes
        analysis = analysis_fn(ticker, closes + [float(spot)])
        if not analysis:
            return None   # thin history — absence of evidence, no verdict
        uptrend = bool(analysis["uptrend"])

        if not registry.observe(ticker, uptrend):
            return None
        against = "bearish" if uptrend else "bullish"
        affected = [p for p in directional if direction_of(p) == against]
        if not affected:
            return None

        read = "bullish" if uptrend else "bearish"
        cross = ("SMA50 crossed above SMA200" if uptrend
                 else "SMA50 crossed below SMA200")
        lines = [f"🔄 **TREND FLIP — {ticker} now reads {read}** "
                 f"({cross} on the live read). {len(affected)} open "
                 f"{against} spread(s) no longer trend-supported:"]
        for p in affected:
            lines.append(
                f"• `{p.get('trade_id')}` "
                f"{(p.get('strategy') or 'spread').replace('_', ' ')} — "
                f"exp {p.get('expiry') or '?'}, "
                f"{p.get('days_in_trade') if p.get('days_in_trade') is not None else '?'}d in trade, "
                f"max loss Rs.{(p.get('max_loss_rs') or 0):,.0f}")
        lines.append("Consider exit. Advisory only — nothing settles "
                     "here; the tracker manages exits at the daily close "
                     "(decision #41).")
        return {"ticker": ticker, "uptrend": uptrend,
                "affected": affected, "card": "\n".join(lines)}
    except Exception as e:
        print(f"  (trend-flip advisory skipped for {ticker}: {e})")
        return None
