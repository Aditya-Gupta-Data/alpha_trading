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
from datetime import date, datetime

from src import journal
from src import plan_tracker as pt
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
    "pre_expiry_exit" / "hold". Purely advisory — nothing is mutated."""
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

    if (max_profit_ps > 0
            and profit_ps >= pt.OPTION_PROFIT_TAKE_FRACTION * max_profit_ps):
        signal = "profit_take"
    elif days_left <= pt.PRE_EXPIRY_EXIT_DAYS:
        signal = "pre_expiry_exit"
    else:
        signal = "hold"
    capture = (profit_ps / max_profit_ps * 100) if max_profit_ps > 0 else 0.0
    return {"short_id": entry.get("short_id"), "ticker": entry["ticker"],
            "strategy": spread.get("strategy"), "signal": signal,
            "live_pnl_rs": round(profit_ps * qty, 2),
            "capture_pct": round(capture, 2), "days_left": days_left}


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


def live_cycle(underlyings=UNDERLYINGS, *, quote_fn=None, entries=None,
               aggregators: dict = None, registry: AlertRegistry = None,
               notify_fn=None, now_fn=ist_now) -> list:
    """One synchronous pass of the live loop: snapshot each underlying,
    fold it into its candle aggregator, mark every open position, and
    push an advisory alert for each NEW exit signal. Returns the alerts
    fired this cycle (empty outside market hours). Fully injectable —
    the async daemon and the offline playback tests share this body."""
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

    fired = []
    for sig in evaluate_open_positions(spots, entries, today=now.date()):
        if sig["signal"] == "hold" or not registry.fresh(sig):
            continue
        fired.append(sig)
        if notify_fn:
            emoji = "🎯" if sig["signal"] == "profit_take" else "⏳"
            notify_fn(
                f"{emoji} **LIVE exit signal — {sig['ticker']} "
                f"{(sig['strategy'] or 'spread').replace('_', ' ')}** "
                f"(`{sig['short_id']}`)\n"
                f"{sig['signal'].replace('_', ' ')} at "
                f"{sig['capture_pct']:.0f}% of max profit "
                f"(modeled P&L Rs.{sig['live_pnl_rs']:,.2f}, "
                f"{sig['days_left']}d to expiry). Advisory only — the "
                "tracker settles at the daily close.")
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
    print(f"[Live Bridge] armed — {', '.join(underlyings)} every "
          f"{interval:g}s during "
          f"{MARKET_OPEN:%H:%M}-{MARKET_CLOSE:%H:%M} IST "
          f"({CANDLE_MINUTES}m candles; advisory exit alerts).", flush=True)
    while True:
        try:
            fired = await asyncio.to_thread(
                live_cycle, underlyings, quote_fn=quote_fn,
                aggregators=aggregators, registry=registry,
                notify_fn=notify_fn, now_fn=now_fn)
            for sig in fired:
                print(f"[Live Bridge] {sig['ticker']}: {sig['signal']} "
                      f"({sig['capture_pct']:.0f}% capture).", flush=True)
        except Exception as e:
            print(f"[Live Bridge] cycle failed ({e}) — loop continues.",
                  flush=True)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(run_live_loop())
    except KeyboardInterrupt:
        print("\n[Live Bridge] stopped.")
