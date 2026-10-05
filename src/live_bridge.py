"""
Alpha Trading — Phase 6H: the live market-hour data adapter
============================================================

Decouples the pipeline from historical/daily-close replay: during NSE
market hours (Mon-Fri 09:15-15:30 IST) this module ingests LIVE candle
snapshots through the verified DhanHQ V2 token framework and serves two
real-time jobs:

  ENTRY — `fetch_live_market_state(underlying)` is a drop-in for
  `market_loop.fetch_market_state` (the loop's documented injection
  seam: `run_market_loop(fetch_fn=live_bridge.fetch_live_market_state)`).
  Instead of yesterday's closes it appends the live spot as today's
  provisional close, so the SMA/RSI read — the exact same
  `analysis_from_closes` math the Phase 7 simulator replays — reacts to
  the market as it moves, not as it closed.

  EXIT — `evaluate_open_positions()` marks every ACTIVE open spread in
  the journal against the live spot using `plan_tracker`'s own pure
  helpers (`_spread_mark`, the no-arbitrage clamp, the 65% profit take,
  the pre-expiry gamma rule), and returns advisory exit signals in real
  time — hours before the tracker's end-of-day sweep would see them.

HARD SANDBOX RULE — this module is READ-ONLY on all trade state:
  * it never writes journal.jsonl (no `journal.log`, no `rewrite_all`),
  * never touches portfolio.json or settles cash
    (`_settle_spread_cash` stays the plan tracker's exclusive job),
  * never places anything (dhan_client is data-only, decision #11).
  A live exit signal is an ALERT to the human, not an execution; the
  actual resolution still lands through the tracker's daily sweep, so
  the paper sandbox remains the single source of truth.

Everything is injectable (quote/closes/VIX fetchers, clock, journal
entries), so tests run fully offline against played-back packet streams.

Run the live loop from the project folder:

    python3 -m src.live_bridge
"""

import asyncio
import re
from datetime import date, datetime, timedelta

from src import journal
from src import plan_tracker as pt
from src.execution.live_pricer import crossed_close_price, leg_quote
from src.market_loop import (MARKET_CLOSE, MARKET_OPEN,
                             is_market_open, ist_now)
from src.simulator import analysis_from_closes

POLL_INTERVAL_SECONDS = 60      # live snapshots every minute
CANDLE_MINUTES = 15             # aggregation bucket (matches the loop cadence)
UNDERLYINGS = ("NIFTY 50", "NIFTY BANK")


# --- packet parsing ------------------------------------------------------

def parse_packet(raw) -> dict | None:
    """Normalize one live incoming data point to {"ticker", "price", "ts"}.

    Accepts the two shapes the Dhan framework hands us:
      * `dhan_client.get_quote` dicts — {"ticker", "current_price", ...}
      * raw quote sections — {"last_price": ..} (ticker/ts supplied or None)
    Returns None for anything unparseable — a malformed packet is dropped,
    never propagated (the live loop must survive any garbage byte)."""
    if not isinstance(raw, dict):
        return None
    price = raw.get("current_price", raw.get("last_price", raw.get("ltp")))
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    ts = raw.get("ts")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None
    return {"ticker": raw.get("ticker"), "price": round(price, 2),
            "ts": ts or ist_now()}


class CandleAggregator:
    """Buckets a stream of parsed packets into OHLC candles of
    `minutes` width (default 15). Pure and deterministic: same packet
    playback, same candles — the offline test harness replays recorded
    streams straight through here."""

    def __init__(self, minutes: int = CANDLE_MINUTES):
        self.minutes = int(minutes)
        self._candles: list = []     # finished + current, oldest first

    def _bucket_start(self, ts: datetime) -> datetime:
        return ts.replace(minute=(ts.minute // self.minutes) * self.minutes,
                          second=0, microsecond=0)

    def ingest(self, packet: dict) -> dict | None:
        """Fold one parsed packet in; returns the candle it landed in
        (None for a None/unparsed packet, which is ignored)."""
        if not packet:
            return None
        start = self._bucket_start(packet["ts"])
        price = packet["price"]
        if self._candles and self._candles[-1]["start"] == start:
            c = self._candles[-1]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
        else:
            c = {"start": start, "open": price, "high": price,
                 "low": price, "close": price}
            self._candles.append(c)
        return c

    def candles(self) -> list:
        return list(self._candles)

    def last_price(self) -> float | None:
        return self._candles[-1]["close"] if self._candles else None


class CandleSink:
    """Phase-0 capture-forward tap: persists COMPLETED candles from the
    aggregators into the lake (data/lake/candles/<slug>/date=.../). The
    aggregator computed these all along and threw them away at session end
    — and intraday high/low SEQUENCING is the known blind spot of the
    tracker's daily-OHLC resolution, so this history is unbuyable later.

    Honesty rules (#55): every row carries source="poll" — poll-sampled
    candles are NOT true OHLC (highs/lows between polls never print) and
    must never be mistaken for exchange candles; a missing bucket between
    consecutive candles is recorded as an explicit gap row, never
    interpolated. A candle is sealed (written once) only when a LATER
    bucket has opened for that underlying. Fail-open: a lake failure never
    touches the cycle. READ-ONLY on all trade state, like everything here."""

    def __init__(self, lake_root=None, source: str = "poll"):
        self.lake_root = lake_root
        self.source = source
        self._written = {}   # underlying -> set of sealed start-isoformats

    @staticmethod
    def _slug(underlying: str) -> str:
        return "".join(ch if ch.isalnum() else "-"
                       for ch in underlying.lower()).strip("-")

    def observe(self, underlying: str, aggregator: "CandleAggregator") -> int:
        """Seal + persist every completed candle not yet written. Returns
        rows written (gap markers included). Never raises."""
        try:
            candles = aggregator.candles()
            if len(candles) < 2:
                return 0            # nothing completed yet
            seen = self._written.setdefault(underlying, set())
            width = timedelta(minutes=aggregator.minutes)
            rows_by_day, prev_start = {}, None
            for c in candles[:-1]:                 # last is still forming
                start = c["start"]
                key = start.isoformat()
                if key in seen:
                    prev_start = start
                    continue
                day = start.date().isoformat()
                if prev_start is not None and start - prev_start > width:
                    rows_by_day.setdefault(day, []).append({
                        "type": "gap", "underlying": underlying,
                        "after": prev_start.isoformat(),
                        "before": key, "source": self.source,
                    })
                rows_by_day.setdefault(day, []).append({
                    "type": "candle", "underlying": underlying,
                    "start": key, "minutes": aggregator.minutes,
                    "open": c["open"], "high": c["high"],
                    "low": c["low"], "close": c["close"],
                    "source": self.source,
                })
                seen.add(key)
                prev_start = start
            from src import lake
            written = 0
            for day, rows in rows_by_day.items():
                written += lake.append_rows(
                    f"candles/{self._slug(underlying)}", day, rows,
                    root=self.lake_root)
            return written
        except Exception as exc:
            print(f"  (candle sink: persist failed for {underlying} [{exc}])")
            return 0

    def observe_all(self, aggregators: dict) -> int:
        return sum(self.observe(u, agg)
                   for u, agg in (aggregators or {}).items())


# --- ENTRY: the fetch_market_state drop-in -------------------------------

def fetch_live_market_state(underlying: str, *, quote_fn=None,
                            closes_fn=None, vix_fn=None,
                            now_fn=ist_now) -> dict | None:
    """`market_loop.fetch_market_state`'s live twin — same contract
    ({"analysis", "vix"} (+ "vol_overrides") build_proposal overrides, or
    None to skip the cycle), but the trend read includes the CURRENT
    intraday spot: the live snapshot is appended to the daily-close
    history as today's provisional close before the SMA/RSI math runs.

    Returns None outside market hours (a live read of a closed market is
    meaningless), on a dead quote, or with insufficient history — the
    market loop just skips the cycle, exactly as with the daily fetcher."""
    if not is_market_open(now_fn()):
        return None
    if quote_fn is None or closes_fn is None or vix_fn is None:
        from src.dhan_client import get_daily_closes, get_india_vix, get_quote
        quote_fn = quote_fn or get_quote
        closes_fn = closes_fn or get_daily_closes
        vix_fn = vix_fn or get_india_vix

    packet = parse_packet(quote_fn(underlying) or {})
    if packet is None:
        return None
    closes = list(closes_fn(underlying) or [])
    if not closes:
        return None
    analysis = analysis_from_closes(underlying, closes + [packet["price"]])
    if analysis is None:
        return None  # not enough history for the 200-day SMA read

    state: dict = {"analysis": analysis, "vix": vix_fn()}
    try:
        from src.vol_bridge import compute_regime_overrides
        vol_overrides = compute_regime_overrides()
        if vol_overrides:
            state["vol_overrides"] = vol_overrides
    except Exception:
        pass  # bridge unavailable — run with default params
    return state


# --- EXIT: real-time evaluation of open positions ------------------------

def _open_spreads(entries=None) -> list:
    """Active open spread positions from the journal: approved,
    tracker-shaped, not yet resolved. Injectable for offline playback."""
    if entries is None:
        entries = journal.read_all()
    return [e for e in entries
            if e.get("decision") == "approved" and pt._spread_trackable(e)]


def evaluate_position(entry: dict, spot: float, today: date = None) -> dict:
    """One open spread against one live spot: the tracker's exact exit
    arithmetic (modeled mark, no-arbitrage clamp, 65% profit take,
    pre-expiry rule) evaluated NOW instead of at the daily close.

    Returns {"short_id", "ticker", "strategy", "signal", "live_pnl_rs",
    "capture_pct", "days_left"} where signal is "profit_take" /
    "pre_expiry_exit" / "hold". Purely advisory — nothing is mutated. There
    is no mid-trade stop signal (decision #105): a defined-risk spread is
    held to target or expiry."""
    spread = entry["spread"]
    today = today or date.today()
    expiry = date.fromisoformat(spread["expiry"])
    entry_day = date.fromisoformat(entry["date"])
    total_days = max(1, (expiry - entry_day).days)
    days_left = (expiry - today).days
    frac_left = max(0.0, days_left / total_days)

    lot = int(spread["lot_size"])
    lots = int(spread.get("lots", 1))
    qty = lot * lots
    max_profit_ps = float(spread["max_profit"]) / lot if lot else 0.0
    max_loss_ps = float(spread["max_loss"]) / lot if lot else 0.0

    m_entry = pt._spread_entry_mark(spread)
    m_now = pt._spread_mark(spread, float(spot), frac_left)
    # The tracker's no-arbitrage clamp: a defined-risk structure can never
    # exceed its own max profit / max loss, so neither may the model.
    profit_ps = max(-max_loss_ps, min(m_now - m_entry, max_profit_ps))

    capture = (profit_ps / max_profit_ps * 100) if max_profit_ps > 0 else 0.0
    ratchet = None
    from src import profit_ratchet as pr
    from src.config import RATCHET_ENABLED
    if RATCHET_ENABLED and pr.is_directional(spread) and max_profit_ps > 0:
        # decision #110: directional spreads ride the profit ratchet — the
        # persisted peak (EOD walk / intraday rung notes) plus the live mark.
        prior = entry.get("ratchet") or {}
        peak = max(float(prior.get("peak_capture_pct") or -1e9), capture)
        ratchet = pr.state(peak, prior.get("locked_pct"))
        ratchet["capture_pct"] = round(capture, 2)
        ratchet["new_rung"] = (ratchet["locked_pct"] is not None
                               and (prior.get("locked_pct") is None
                                    or float(ratchet["locked_pct"]) > float(prior["locked_pct"])))
    if ratchet is not None:
        if pr.ratchet_hit(capture, ratchet["locked_pct"]):
            signal = "ratchet_hit"
        elif days_left <= pt._forced_exit_days(entry.get("ticker")):
            signal = "pre_expiry_exit"
        else:
            signal = "hold"
    elif (max_profit_ps > 0
            and profit_ps >= pt.OPTION_PROFIT_TAKE_FRACTION * max_profit_ps):
        signal = "profit_take"
    elif days_left <= pt._forced_exit_days(entry.get("ticker")):
        # Stock options leave before expiry WEEK (physical settlement);
        # index options keep the 2-day rule. Same one predicate as the
        # tracker so the live advisory and the settlement path can never
        # disagree about when a position must be out.
        signal = "pre_expiry_exit"
    else:
        signal = "hold"
    return {"short_id": entry.get("short_id"), "ticker": entry["ticker"],
            "strategy": spread.get("strategy"), "signal": signal,
            "live_pnl_rs": round(profit_ps * qty, 2),
            "capture_pct": round(capture, 2), "days_left": days_left,
            "ratchet": ratchet}


def evaluate_open_positions(spot_by_ticker: dict, entries=None,
                            today: date = None) -> list:
    """Every active open spread whose underlying has a live spot, marked
    in real time. Positions with no quote this cycle are skipped (never
    guessed). Read-only by hard rule — see the module docstring."""
    results = []
    for entry in _open_spreads(entries):
        spot = spot_by_ticker.get(entry["ticker"])
        if spot is None:
            continue
        results.append(evaluate_position(entry, float(spot), today))
    return results


class AlertRegistry:
    """Remembers (short_id, signal) pairs already alerted so a live exit
    condition fires ONE Discord note, not one per polling minute."""

    def __init__(self):
        self._seen: set = set()

    def fresh(self, sig: dict) -> bool:
        key = (sig["short_id"], sig["signal"])
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


# --------------------------------------------- intraday square-off (#69)

def _is_long(leg: dict) -> bool:
    return str(leg.get("side") or "").upper() == "BUY"


def _crossed_exit_price(leg: dict, q: dict) -> tuple:
    """(price, reason) to CLOSE one leg of a primary/2L/ROT position now —
    `live_pricer.crossed_close_price` (long sold at the bid, short bought
    back at the ask; refuses a crossed book and a quote > 50% off a live
    last price) with ONE difference: a long leg with no bid is REFUSED,
    not priced at 0. The live arm values an unsellable long at 0 because
    it is measuring friction on its own book; these quotes SETTLE the
    primary's trade (and gate the 65% take / the ratchet lock), so the
    primary never exits on a made-up price (Architect ruling 1,
    2026-10-05)."""
    if _is_long(leg) and q.get("bid") is None:
        return None, "no bid to sell the long leg"
    price, why = crossed_close_price(leg, q)
    if price is not None and price <= 0:              # defensive: leg_quote drops <= 0 sides
        return None, "non-positive crossed price"
    return price, why


def _leg_quotes_for(entry: dict) -> dict | None:
    """The CROSSED close price for every leg of one open spread, from the
    live option chain: {(strike, 'CE'/'PE') -> price} — a long leg at the
    BID it can be sold at, a short leg at the ASK it is bought back at,
    never the last traded price (ARCHITECT RULING 1, 2026-10-05: every
    intraday exit crosses the spread, as the entry already does under
    #70 and PAPER_2L_LIVE under #120). None when the chain is unreachable
    or ANY leg's crossed side is unusable — no bid on a long, no ask on a
    short, a crossed book (bid > ask), or a quote > 50% off a live last
    price (live_pricer.STALE_QUOTE_FRACTION). The caller then falls back
    to the EOD path, never a modeled fill (decision #69). Every refusal is
    printed with its reason. Consumers: the square-off (#69/#110), the
    rotation eviction (#115) and its approval-time prefetch.

    LEDGER ISSUE 44 (2026-10-05): from 2026-07-15 to 2026-10-05 this door
    was DEAD. It imported `options_proposer._premium`, which decision #70
    removed the day after #69 shipped, inside a bare `except Exception:
    return None` — so every call returned None, every intraday square-off
    (#69, #110) declined as "no_chain_quotes" and no capital-rotation
    eviction (#115) could ever execute. The quotes now come from
    `live_pricer.leg_quote` / `crossed_close_price` (the same tolerant
    strike-key match and data-quality rules as the live arm), imported at
    MODULE level so a missing name fails loudly at import; the try below
    covers only the network fetch, and its failure is printed with the
    door's reason (dhan_client.last_chain_error)."""
    from src import dhan_client
    ref = entry.get("short_id")
    try:
        spread = entry["spread"]
        chain = dhan_client.get_option_chain(entry["ticker"], spread["expiry"])
    except Exception as exc:
        print(f"  (square-off quotes for {ref}: chain fetch failed: {exc})", flush=True)
        return None
    if not chain:
        why = dhan_client.last_chain_error() or "empty response"
        print(f"  (square-off quotes for {ref}: option chain unavailable ({why}))", flush=True)
        return None
    quotes = {}
    for leg in spread.get("legs") or []:
        price, why = _crossed_exit_price(leg, leg_quote(chain, leg))
        if price is None:
            print(f"  (square-off quotes for {ref}: refused on "
                  f"{float(leg['strike']):g}{str(leg['option_type']).upper()}: {why})", flush=True)
            return None
        quotes[(float(leg["strike"]), str(leg["option_type"]).upper())] = price
    return quotes or None


def _square_off_note(sig: dict) -> str:
    """The square-off outcome for the bridge's log line (ledger Issue 44):
    empty when no square-off was attempted (advisory-only signals)."""
    status = sig.get("square_off_status")
    if sig.get("squared_off"):
        return " — SQUARED OFF intraday on real quotes"
    if not status:
        return ""
    extra = ""
    if sig.get("square_off_real_capture_pct") is not None:
        extra = f", {sig['square_off_real_capture_pct']:.0f}% on real quotes"
    elif sig.get("square_off_reason"):
        extra = f": {sig['square_off_reason']}"
    return f" — intraday fill declined ({status}{extra}); the EOD path owns it"


def intraday_square_off(sig: dict, entries=None, quotes_fn=_leg_quotes_for,
                        today: date = None) -> dict:
    """The production square-off seam: profit-take signal -> CROSSED chain
    quotes (`_leg_quotes_for`: long legs at the bid, short legs at the
    ask — ruling 1, 2026-10-05) -> plan_tracker.resolve_intraday_profit_take
    (the ONE settlement path; decision #41's read-only rule is amended by
    #69 for exactly this call). Approved trades only; every failure returns a
    status and leaves the trade to the EOD path. Never raises."""
    try:
        entry = next((e for e in _open_spreads(entries)
                      if e.get("short_id") == sig["short_id"]), None)
        if entry is None:
            return {"status": "not_open", "short_id": sig["short_id"]}
        if entry.get("decision") != "approved":
            return {"status": "not_approved", "short_id": sig["short_id"]}
        quotes = quotes_fn(entry)
        if not quotes:
            return {"status": "no_chain_quotes", "short_id": sig["short_id"]}
        return pt.resolve_intraday_profit_take(
            sig["short_id"], quotes,
            model_capture_pct=sig.get("capture_pct"), today=today,
            resolution=sig.get("signal") or "profit_take",
            locked_pct=(sig.get("ratchet") or {}).get("locked_pct"))
    except Exception as exc:
        return {"status": "error", "short_id": sig.get("short_id"),
                "reason": str(exc)}


# ------------------------------- the live arm's tick, in the log (audit F02)

LIVE_NOTE_REPEAT_SECONDS = 30 * 60
# Exit outcomes that REPEAT on every 60-s tick while the condition lasts
# (de-duplicated); every other exit outcome — settled, unfilled, a door
# error — happens once and always prints.
_REPEATING_EXIT_STATUSES = ("held_loss_beyond_max", "held_impossible_mark", "held_recent_attempt")


def _reason_class(reason) -> str:
    """A reason with its numbers blanked: 'quote 230 is >50% off last 60'
    re-priced at 231 on the next chain is the same state, not a new one."""
    return re.sub(r"\d+(?:[.,]\d+)*", "#", str(reason or ""))


def _fmt(v, spec: str = "g") -> str:
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "?"


def _exit_text(x: dict) -> str:
    ref, sig, st = x.get("journal_ref"), x.get("signal"), x.get("status")
    if st == "settled":
        return (f"{ref} exit SETTLED ({sig}): {_fmt(x.get('capture_pct'), '.0f')}% capture, P&L "
                f"Rs.{_fmt(x.get('pnl_net'), '+,.2f')} net ({x.get('basis')})")
    if st == "held_loss_beyond_max":
        return (f"{ref} {sig} exit HELD: a crossed exit would lose {_fmt(x.get('would_loss_ps'))}/share, "
                f"beyond the structure's max loss {_fmt(x.get('max_loss_ps'))} — position kept")
    if st == "held_impossible_mark":
        return f"{ref} {sig} exit HELD: {x.get('reason')} — position kept"
    if st == "held_recent_attempt":
        return f"{ref} {sig} exit waits: the last attempt ({x.get('last')}) is inside the quote interval"
    if st == "unfilled":
        return f"{ref} {sig} exit NOT FILLED (ticket {x.get('ticket_id') or '-'}: {x.get('reason')}) — position kept"
    if st == "exit_error":
        return f"{ref} {sig} exit door ERROR ({x.get('reason')}) — row left 'exiting' for the next tick's resume"
    return f"{ref} {sig} exit: {st}" + (f" ({x['reason']})" if x.get("reason") else "")


class LiveTickLog:
    """PAPER_2L_LIVE's tick summary, in the scheduler log (audit F02:
    live_cycle used to throw it away, so an arm that abstained or held all
    week and a healthy one both left nothing in master_scheduler.log).

    A routine tick — every open row marked, nothing exited — prints
    nothing. Otherwise ONE header line with the tick's counts, then one
    line per row that needs a word, each naming its ref and reason.
    One-off outcomes (an exit settled, unfilled or erroring, a resumed
    crash, a repair) always print. REPEATING states — an abstention, a
    hold, a predicate that could not be re-verified, a tick-level skip —
    print once per (row, reason) per LIVE_NOTE_REPEAT_SECONDS on the cycle
    clock, never a line per 60-s tick; when everything a tick has to say
    is such a repeat, its header is suppressed too. Process memory only:
    a restart prints each standing state once more (the safe direction)."""

    def __init__(self, repeat_s: float = LIVE_NOTE_REPEAT_SECONDS):
        self.repeat_s = float(repeat_s)
        self._last: dict = {}

    def _due(self, key: tuple, now: datetime) -> bool:
        last = self._last.get(key)
        if last is not None and (now - last).total_seconds() < self.repeat_s:
            return False
        self._last[key] = now
        return True

    def lines(self, summary, now: datetime) -> list:
        """The lines to print for one tick summary (empty for a routine one)."""
        if not isinstance(summary, dict):
            return []
        self._last = {k: t for k, t in self._last.items()          # bounded memory
                      if (now - t).total_seconds() < self.repeat_s}
        items = []                                                  # (dedup key | None, text)
        for r in summary.get("resumes") or []:
            items.append((None, f"{r.get('journal_ref')} crashed exit resumed: {r.get('status')}"))
        for ref in summary.get("repaired") or []:
            items.append((None, f"{ref} position row rebuilt from its filled entry ticket"))
        for ref in summary.get("late_released") or []:
            items.append((None, f"{ref} lock released late at the row's settled P&L"))
        for x in summary.get("exits") or []:
            st = x.get("status")
            key = (x.get("account_id"), x.get("journal_ref"), "exit", st) \
                if st in _REPEATING_EXIT_STATUSES else None
            items.append((key, _exit_text(x)))
        notes = summary.get("row_notes") or []
        for n in notes:
            kind, reason = n.get("kind"), n.get("reason")
            window = (f" [forced-exit window, {n.get('days_left')}d to expiry]"
                      if n.get("in_exit_window") else "")
            last = f"; last mark {n.get('last_mark_ts') or 'never'}" if kind == "abstained" else ""
            items.append(((n.get("account_id"), n.get("journal_ref"), kind, _reason_class(reason)),
                          f"{n.get('journal_ref')} {kind}{window}: {reason}{last}"))
        # a summary that counts more than it names still gets a (deduped) line
        abst, unconf = int(summary.get("abstained") or 0), int(summary.get("unconfirmed") or 0)
        if abst > sum(1 for n in notes if n.get("kind") == "abstained") \
                or unconf > sum(1 for n in notes if n.get("kind") == "unconfirmed"):
            items.append((("*", "*", "counts", abst, unconf),
                          f"{abst} abstained / {unconf} unconfirmed (no per-row detail in the summary)"))
        skipped = summary.get("skipped")
        if skipped and skipped != "market closed":
            items.append((("*", "*", "skipped", _reason_class(skipped)), f"tick skipped: {skipped}"))
        kept = [text for key, text in items if key is None or self._due(key, now)]
        if not kept:
            return []
        parts = [f"{int(summary.get('rows') or 0)} open", f"{int(summary.get('marked') or 0)} marked"]
        parts += [f"{abst} abstained"] if abst else []
        parts += [f"{unconf} unconfirmed"] if unconf else []
        parts += [f"{len(summary['exits'])} exit outcome(s)"] if summary.get("exits") else []
        parts += [f"{summary['resumed']} resumed"] if summary.get("resumed") else []
        parts += ["skipped"] if skipped and skipped != "market closed" else []
        return ([f"[Live Bridge] live account tick: {', '.join(parts)}."]
                + [f"  (live account: {t})" for t in kept])


_LIVE_TICK_LOG = LiveTickLog()      # live_cycle's default when no log is passed


def live_cycle(underlyings=UNDERLYINGS, *, quote_fn=None, entries=None,
               aggregators: dict = None, registry: AlertRegistry = None,
               notify_fn=None, now_fn=ist_now,
               publish_snapshot: bool = False,
               candle_sink: "CandleSink" = None,
               flip_registry=None, closes_fn=None,
               square_off_fn=None, live_account_fn=None,
               live_log: "LiveTickLog" = None) -> list:
    """One synchronous pass of the live loop: snapshot each underlying,
    fold it into its candle aggregator, mark every open position, and
    push an advisory alert for each NEW exit signal. Returns the alerts
    fired this cycle (empty outside market hours). Fully injectable —
    the async daemon and the offline playback tests share this body.

    `publish_snapshot` (the production daemon passes True) writes this
    cycle's spots + every position mark to data/market_snapshot.json via
    src.market_snapshot, so read-only viewers (the dashboard, a synced
    Mac copy) can serve live marks without hitting Dhan themselves — the
    loop stays the single quote consumer (decision #48). Fail-safe: a
    publish failure never touches the cycle. Default False so the offline
    playback tests and any other caller are unaffected."""
    now = now_fn()
    if not is_market_open(now):
        return []
    if quote_fn is None:
        from src.dhan_client import get_quote
        quote_fn = get_quote
    aggregators = aggregators if aggregators is not None else {}
    registry = registry or AlertRegistry()

    spots = {}
    for u in underlyings:
        try:
            packet = parse_packet(dict(quote_fn(u) or {}, ticker=u, ts=now))
        except Exception:
            packet = None  # one dead quote never kills the cycle
        if packet is None:
            continue
        aggregators.setdefault(u, CandleAggregator()).ingest(packet)
        spots[u] = packet["price"]

    # Phase-0 capture tap: seal + persist completed candles. Additive and
    # fail-open — a None sink (offline tests, legacy callers) is a no-op.
    if candle_sink is not None:
        candle_sink.observe_all(aggregators)

    # Mark every open position ONCE, then reuse the list for both the
    # published snapshot and the alert scan below.
    marks = evaluate_open_positions(spots, entries, today=now.date())
    if publish_snapshot:
        from src import market_snapshot
        market_snapshot.write(spots, marks, now=now)
    # decision #120: the live-quote arm marks and exits on its own chain
    # quotes. None (offline callers, tests) is a byte-identical no-op; the
    # daemon passes execution.live_pricer.tick. Fail-open: a broken tick
    # can never touch this cycle's alerts or the primary's square-off.
    # Audit F02: its summary is READ, not dropped — `live_log` prints the
    # non-routine part of it (de-duplicated; a routine tick prints nothing).
    if live_account_fn is not None:
        summary = None
        try:
            summary = live_account_fn(now)
        except Exception as e:
            print(f"  (live account tick skipped: {e})")
        try:
            for line in (live_log or _LIVE_TICK_LOG).lines(summary, now):
                print(line, flush=True)
        except Exception as e:
            print(f"  (live account tick summary not printed: {e})", flush=True)

    fired = []
    # decision #110: a directional spread that crossed a NEW ratchet rung
    # intraday has its peak/lock persisted (rare: at most one write per
    # rung per trade), so the lock survives a give-back into the close.
    # Only with a square_off_fn armed (the production daemon) — offline
    # callers stay read-only.
    if square_off_fn is not None:
        for sig in marks:
            r = sig.get("ratchet") or {}
            if r.get("new_rung"):
                try:
                    pt.note_ratchet(sig["short_id"], r["peak_capture_pct"], r["locked_pct"])
                except Exception:
                    pass
    for sig in marks:
        if sig["signal"] == "hold" or not registry.fresh(sig):
            continue
        fired.append(sig)
        # Decision #69: with a square_off_fn armed (the production daemon,
        # config-gated), a profit-take signal is EXECUTED intraday on real
        # chain quotes instead of advised. None (the default — offline
        # tests, legacy callers) keeps the loop byte-identical read-only
        # advisory (#41). Every non-squared status falls back to the
        # advisory + the EOD path untouched.
        squared = None
        if square_off_fn is not None and sig["signal"] in ("profit_take", "ratchet_hit"):
            try:
                squared = square_off_fn(sig)
            except Exception as exc:
                squared = {"status": "error", "reason": str(exc)}
            # Ledger Issue 44: the outcome reaches the LOG, not only Discord —
            # the dead quote door hid for 81 days because a decline was
            # written nowhere a sweep could read it.
            sig["square_off_status"] = (squared or {}).get("status")
            if (squared or {}).get("reason"):
                sig["square_off_reason"] = squared["reason"]
            if (squared or {}).get("real_capture_pct") is not None:
                sig["square_off_real_capture_pct"] = squared["real_capture_pct"]
        if squared and squared.get("status") == "squared_off":
            sig["squared_off"] = True
            if notify_fn:
                notify_fn(
                    f"✅ **SQUARED OFF intraday — {sig['ticker']} "
                    f"{(sig['strategy'] or 'spread').replace('_', ' ')}** "
                    f"(`{sig['short_id']}`)\n"
                    f"{sig['signal'].replace('_', ' ')} filled at {squared['capture_pct']:.0f}% "
                    f"of max profit on REAL chain quotes — P&L "
                    f"Rs.{squared['pnl_rs']:,.2f} net (booked now, "
                    "decision #69; model had said "
                    f"{sig['capture_pct']:.0f}%).")
            continue
        if notify_fn:
            emoji = {"profit_take": "🎯", "ratchet_hit": "🔒"}.get(sig["signal"], "⏳")
            fallback = ""
            if squared is not None:
                fallback = (f" (intraday fill declined: "
                            f"{squared.get('status')} — EOD path owns it)")
            notify_fn(
                f"{emoji} **LIVE exit signal — {sig['ticker']} "
                f"{(sig['strategy'] or 'spread').replace('_', ' ')}** "
                f"(`{sig['short_id']}`)\n"
                f"{sig['signal'].replace('_', ' ')} at "
                f"{sig['capture_pct']:.0f}% of max profit "
                f"(modeled P&L Rs.{sig['live_pnl_rs']:,.2f}, "
                f"{sig['days_left']}d to expiry). Advisory only — the "
                f"tracker settles at the daily close.{fallback}")

    # Decision #68: trend-flip exit advisory. A None flip_registry (the
    # default — offline tests, legacy callers) is a byte-identical no-op.
    # Belt-and-braces try/except on top of the advisory's own fail-open:
    # this block can never touch exit alerts or snapshot publishing.
    if flip_registry is not None:
        try:
            from src import exposure_gate
            for u, spot in spots.items():
                adv = exposure_gate.trend_flip_advisory(
                    u, spot, registry=flip_registry, entries=entries,
                    closes_fn=closes_fn, today=now.date())
                if adv and notify_fn:
                    notify_fn(adv["card"])
        except Exception as e:
            print(f"  (trend-flip advisory skipped: {e})")
    return fired


def _notify_discord(text: str) -> bool:
    """Fail-safe fire-and-forget Discord push (same shape as the
    proposer's): Discord being down never touches the live loop."""
    from src import notifier
    try:
        return asyncio.run(notifier.send_discord_message(text))
    except Exception as e:
        print(f"  (live-bridge discord notify failed: {e})")
        return False


async def run_live_loop(underlyings=UNDERLYINGS,
                        interval: float = POLL_INTERVAL_SECONDS,
                        quote_fn=None, notify_fn=_notify_discord,
                        now_fn=ist_now) -> None:
    """The live daemon: one `live_cycle` every `interval` seconds during
    market hours (outside them it just sleeps, like the market loop).
    Candle aggregators and the alert de-dup registry persist across
    cycles. Advisory alerts only — nothing here executes or settles."""
    aggregators: dict = {}
    registry = AlertRegistry()
    candle_sink = CandleSink()   # Phase-0 tap: sealed candles -> the lake
    # Decision #68: persistent per-ticker trend state so a flip fires ONE
    # advisory card, not one per polling minute (AlertRegistry's pattern).
    from src.exposure_gate import TrendFlipRegistry
    flip_registry = TrendFlipRegistry()
    # Decision #69: intraday profit-take square-off, config-gated so it
    # can be disabled without a code change ("intraday_profit_take": false
    # in config.json). Default ON — the owner's 2026-07-14 call.
    square_off_fn = None
    try:
        import json as _json
        from pathlib import Path as _Path
        _cfg = _json.loads((_Path(__file__).resolve().parent.parent
                            / "config.json").read_text())
    except Exception:
        _cfg = {}
    if _cfg.get("intraday_profit_take", True):
        square_off_fn = intraday_square_off
        print("[Live Bridge] intraday profit-take square-off ARMED "
              "(crossed bid/ask chain quotes, decision #69 + ruling 2026-10-05).", flush=True)
    live_account_fn = None
    live_log = LiveTickLog()     # audit F02: the arm's tick summary -> this log, de-duplicated
    try:
        from src import portfolio_manager as _pm
        if _pm.live_account_enabled():
            from src.execution import live_pricer as _lp
            live_account_fn = _lp.tick
            print("[Live Bridge] PAPER_2L_LIVE live-quote arm ARMED "
                  "(crossed bid/ask marks + exits, decision #120).", flush=True)
    except Exception as e:
        print(f"[Live Bridge] live-quote arm not armed ({e}).", flush=True)
    print(f"[Live Bridge] armed — {', '.join(underlyings)} every "
          f"{interval:g}s during "
          f"{MARKET_OPEN:%H:%M}-{MARKET_CLOSE:%H:%M} IST "
          f"({CANDLE_MINUTES}m candles; advisory exit alerts; "
          "candle capture -> lake).", flush=True)
    while True:
        try:
            fired = await asyncio.to_thread(
                live_cycle, underlyings, quote_fn=quote_fn,
                aggregators=aggregators, registry=registry,
                notify_fn=notify_fn, now_fn=now_fn, publish_snapshot=True,
                candle_sink=candle_sink, flip_registry=flip_registry,
                square_off_fn=square_off_fn, live_account_fn=live_account_fn,
                live_log=live_log)
            for sig in fired:
                print(f"[Live Bridge] {sig['ticker']}: {sig['signal']} "
                      f"({sig['capture_pct']:.0f}% capture){_square_off_note(sig)}.",
                      flush=True)
        except Exception as e:
            print(f"[Live Bridge] cycle failed ({e}) — loop continues.",
                  flush=True)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(run_live_loop())
    except KeyboardInterrupt:
        print("\n[Live Bridge] stopped.")
