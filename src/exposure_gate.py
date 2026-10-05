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

  gate_entry(proposal)        ONE open spread per underlying+direction,
                              FIRM-WIDE (Architect ruling 2, 2026-10-05,
                              audit F09): a position held by ANY paper
                              account — PAPER_10L, PAPER_2L, PAPER_2L_ROT,
                              PAPER_2L_LIVE — fills the slot, not only the
                              primary journal's. Called by
                              options_proposer.run_headless
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
(An unreadable brain_map.db removes only the firm-wide part of the slot:
the gate then judges on the primary journal alone, as before 10-05.)
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "exposure_blocks.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

# Fallback vocabulary for entries that predate strategy.py's stamped
# spread["direction"]. The stamp (StrategyConstructor._package) is the
# authoritative source; tests/test_exposure_gate.py asserts this map
# agrees with every constructor so the two can never drift.
# decision #100 (2026-09-19): ONE routing table. This name is kept for
# every existing reader; the values come from strategy_router.
from src.strategy_router import ROUTING_TABLE as _ROUTING
DIRECTION_BY_STRATEGY = {k: v["direction"] for k, v in _ROUTING.items()}

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

# Architect ruling 2 (2026-10-05, audit F09): the slot is FIRM-WIDE. Until
# then the gate read only the primary journal's approved + unresolved rows,
# and two kinds of position outlive that row:
#   * a PAPER_2L_LIVE position settles itself on live quotes (#120), so
#     release_shadow_locks keeps its lock while live_pricer.has_open_position
#     — the moment the primary settled, the slot looked empty and the next
#     same-direction signal stacked a second position on one thesis in LIVE;
#   * any shadow lock whose release failed (margin_release_error) stays
#     ACTIVE until reconcile_orphan_locks retries it on the hour.
# Both are read here, from ONE read-only snapshot of brain_map.db.
_FIRM_TABLES = ("paper_live_positions", "paper_margin_locks")


def _firm_holdings(conn=None) -> tuple:
    """([holding, ...], unavailable) — what the SHADOW side of the firm
    still holds, in one read-only snapshot:

      source "live"  every paper_live_positions row that is not CLOSED
                     (open or exiting: live_pricer.has_open_position's own
                     predicate), in any LIVE account — it carries its own
                     ticker / strategy / direction / expiry;
      source "lock"  every ACTIVE (released_at IS NULL) paper_margin_locks
                     row, in any shadow account — the caller maps its
                     journal_ref to a ticker/direction through the journal.

    `conn` is injectable (tests). By default brain_map.db is opened
    `mode=ro`: this gate never writes a money table. A missing file or a
    missing table is a FACT — that book never held anything — so it reads
    as empty. Any other failure (locked past the timeout, corrupt,
    unreadable) returns ([], reason): the caller fails OPEN on it, exactly
    as the gate does for an unreadable journal."""
    from src.execution.live_pricer import STATE_CLOSED
    own = conn is None
    try:
        if own:
            from src import brain_map
            path = Path(brain_map.DEFAULT_DB_PATH)
            if not path.exists():
                return [], None
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN (?, ?)", _FIRM_TABLES)}
        parts, args = [], []
        if "paper_live_positions" in present:
            parts.append("SELECT 'live', account_id, journal_ref, ticker, "
                         "strategy, direction, expiry "
                         "FROM paper_live_positions WHERE state != ?")
            args.append(STATE_CLOSED)
        if "paper_margin_locks" in present:
            parts.append("SELECT 'lock', account_id, journal_ref, NULL, NULL, "
                         "NULL, NULL "
                         "FROM paper_margin_locks WHERE released_at IS NULL")
        if not parts:
            return [], None
        cols = ("source", "account_id", "journal_ref", "ticker", "strategy",
                "direction", "expiry")
        rows = conn.execute(" UNION ALL ".join(parts), args).fetchall()
        return [dict(zip(cols, tuple(r))) for r in rows], None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _firm_conflicts(ticker: str, direction: str, entries: list = None,
                    today: date = None, conn=None) -> tuple:
    """(conflicts, unavailable). One dict per conflicting journal ref, each
    with `held_by` = the account(s) holding it (PAPER_10L first, then the
    shadow accounts in portfolio_manager.PAPER_ACCOUNTS order):

      (a) the primary's open spreads — positions.active_positions, the
          tracker's own predicates (approved + unresolved), as before;
      (b) every LIVE row still open/exiting on `ticker` whose direction
          matches;
      (c) every ACTIVE shadow lock whose journal row is APPROVED and on
          `ticker` + `direction`.

    The journal is read ONCE (here, when not injected) and serves both
    (a) and the ref -> ticker/direction mapping of (c). A ref held by the
    primary and by shadows is ONE position with several holders, never
    several positions. `unavailable` is the firm-wide read's failure
    reason (None when it read cleanly)."""
    from src import journal, positions
    from src import portfolio_manager as pm
    if entries is None:
        entries = journal.read_all()
    primary = pm.ACCOUNT_PAPER_10L
    conflicts, by_ref = [], {}
    for p in positions.active_positions(entries, today):
        if (p.get("kind") == "spread" and p.get("ticker") == ticker
                and direction_of(p) == direction):
            c = dict(p, held_by=[primary])
            conflicts.append(c)
            if p.get("trade_id"):
                by_ref[str(p["trade_id"])] = c

    holdings, unavailable = _firm_holdings(conn)
    if holdings:
        rows = {journal.row_key(e): e for e in entries if isinstance(e, dict)}
        for h in holdings:
            ref = str(h.get("journal_ref") or "")
            if h["source"] == "live":
                pos = {k: h.get(k) for k in
                       ("ticker", "strategy", "direction", "expiry")}
            else:
                e = rows.get(ref)
                # A shadow lock is a POSITION only once its trade was
                # approved. A pending entry's proposal-time lock is a
                # reservation (the gate has never counted pending rows),
                # and a rejected entry never opened anything. A ref with no
                # journal row (an eqd: lock, a lost line) cannot be
                # classified — and an unclassifiable position never blocks.
                if e is None or e.get("decision") != "approved":
                    continue
                s = e.get("spread") or {}
                pos = {"ticker": e.get("ticker"), "strategy": s.get("strategy"),
                       "direction": s.get("direction"), "expiry": s.get("expiry")}
            if pos["ticker"] != ticker or direction_of(pos) != direction:
                continue
            c = by_ref.get(ref)
            if c is None:
                c = dict(pos, kind="spread", trade_id=ref, direction=direction,
                         held_by=[])
                conflicts.append(c)
                by_ref[ref] = c
            if h["account_id"] not in c["held_by"]:
                c["held_by"].append(h["account_id"])
        order = (primary,) + tuple(pm.PAPER_ACCOUNTS)
        for c in conflicts:
            c["held_by"].sort(key=lambda a: (order.index(a) if a in order
                                             else len(order), str(a)))
    return conflicts, unavailable


def conflicting_positions(ticker: str, direction: str, entries: list = None,
                          today: date = None, conn=None) -> list:
    """Every open position on `ticker` whose direction matches, FIRM-WIDE
    (Architect ruling 2) — see _firm_conflicts. Unclassifiable directions
    are skipped."""
    return _firm_conflicts(ticker, direction, entries, today, conn)[0]


def gate_entry(proposal: dict, entries: list = None,
               today: date = None, notify_fn=None, record_fn=None,
               conn=None) -> tuple:
    """(allowed, reason). ONE open position per underlying+direction —
    bullish, bearish and neutral each get one slot per index, and the slot
    is FIRM-WIDE: a position any paper account still holds fills it
    (Architect ruling 2, 2026-10-05). Fail-OPEN by hard rule: ANY failure
    returns (True, ...) with a printed note, the margin gate's exact
    contract — and an unreadable firm-wide read (brain_map.db) fails open
    the same way an unreadable journal does: it adds no conflict, the
    primary journal's own conflicts still count, and the note names it."""
    try:
        spread = proposal.get("spread") or {}
        direction = direction_of(spread)
        if direction is None and proposal.get("view") in _DIRECTIONS:
            direction = proposal["view"]
        ticker = proposal.get("ticker")
        if not ticker or direction is None:
            return True, "allowed (unclassifiable proposal — gate skipped)"

        conflicts, firm_unavailable = _firm_conflicts(
            ticker, direction, entries, today, conn)
        if firm_unavailable:
            print(f"  (exposure gate: firm-wide view unavailable — judged on "
                  f"the primary journal only: {firm_unavailable})")
        if not conflicts:
            # (The #109 book-wide correlation cap lived here for one day and
            # was withdrawn by #110: entries are not throttled by thesis.)
            if firm_unavailable:
                return True, ("allowed (primary journal only — firm-wide "
                              f"view unavailable: {firm_unavailable})")
            return True, "allowed"

        ids = [str(p.get("trade_id") or "?") for p in conflicts]
        held = "; ".join(f"`{i}` {', '.join(p.get('held_by') or ['?'])}"
                         for i, p in zip(ids, conflicts))
        _log_block(ticker, direction, spread.get("strategy"),
                   ids, proposal.get("view"), today=today,
                   notify_fn=notify_fn, conflicts=conflicts,
                   firm_unavailable=firm_unavailable)
        # The opportunity-cost row inherits its HOST's outcome, so only a
        # trade the PRIMARY still holds can host it: a primary row that
        # already resolved (the LIVE / orphan-lock case) closed before this
        # block, and its outcome would answer a different window. No such
        # host = no row (the "no ghost" rule below).
        from src import portfolio_manager as pm
        hosts = [p for p in conflicts
                 if pm.ACCOUNT_PAPER_10L in (p.get("held_by") or [])]
        _record_opportunity_cost(ticker, direction, hosts, today=today,
                                 record_fn=record_fn)
        return False, (
            f"exposure gate: {len(conflicts)} open {direction} "
            f"position(s) on {ticker} already ({held}) — "
            "max one per underlying+direction, firm-wide (decision #68)")
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
               today=None, notify_fn=None, conflicts=None,
               firm_unavailable=None) -> None:
    """Ledger line for every block + at most ONE Discord note per
    (ticker, direction) per IST day. The ledger doubles as the
    once-per-day memory, so a restart can't re-announce. Every failure
    in here is swallowed — bookkeeping never changes the verdict.

    Since the slot went firm-wide (Architect ruling 2) a block can come
    from an account the primary journal no longer shows, so both the line
    (`held_by`: ref -> accounts) and the note name WHO holds the slot."""
    day = (today or datetime.now(IST).date()).isoformat()
    held_by = {str(c.get("trade_id") or "?"): list(c.get("held_by") or [])
               for c in (conflicts or [])}
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
                "held_by": held_by, "view": view,
                **({"firm_view_unavailable": firm_unavailable}
                   if firm_unavailable else {})}) + "\n")
    except OSError:
        pass
    if notify_fn and not noted_today:
        rep = (conflicts or [{}])[0]
        holders = ", ".join(rep.get("held_by") or [])
        held_note = f" held by {holders}" if holders else ""
        try:
            notify_fn(
                f"🧱 **Exposure gate — {ticker}**: suppressed a duplicate "
                f"{direction} spread proposal.\n"
                f"{len(blocked_by)} open {direction} position(s) already "
                f"(`{blocked_by[0]}`{held_note}, "
                f"{(rep.get('strategy') or 'spread').replace('_', ' ')}, "
                f"exp {rep.get('expiry') or '?'}).\n"
                f"Further {direction} {ticker} duplicates today are "
                "blocked silently (decision #68, firm-wide).")
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
