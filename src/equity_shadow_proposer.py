"""
src/equity_shadow_proposer.py — the Shadow Equity Engine (PAPER_TELEMETRY)
==========================================================================

Owner directive 2026-07-17: cash-equity signals are worth LOGGING in the
live market even at flat/negative expected value — paper costs nothing,
and the recorded false positives are training data for a better model.
This module paper-tracks the block-VWAP pullback setup over the
deal-covered equity universe and writes every entry/exit WITH ITS FULL
RATIONALE to the knowledge-graph telemetry ledger
(logs/equity_shadow_journal.jsonl via src/knowledge_graph_logger.py).

THE SETUP (the owner's "block VWAP pullback", built from the committed and
sim-tested smart-money scaffold src/analysis/smart_money_trend.py): price
has pulled back to sit just above the 6-month block-deal VWAP — the
smart-money floor. Entry = live price inside [floor, floor*(1+PULLBACK
BAND)]. Stop = floor*(1-STOP_PCT) — the thesis break is THE FLOOR FAILING,
which is exactly the falsification we want in the ledger. Target =
entry + REWARD_RISK * risk. TIME_STOP_DAYS force-resolves stale theses so
every line eventually yields an outcome row.

DELIBERATELY WIDE GATE (telemetry calibration, 2026-07-17): 90-day net
accumulation/distribution is RECORDED in the rationale, not required —
measured on the real ledger, requiring it left ~0 qualifying names (large
caps run value-negative on constant institutional profit-taking; the sim
already showed "required confirmation hurts, data-sparse"). Logging
floor-holds WITH the accumulation flag lets the outcome analysis decide
whether it matters — which is the whole point of a telemetry engine.

HARD TELEMETRY CONTRACT (amended by the owner ruling 2026-07-20):
  * block-leg events stay mode="PAPER_TELEMETRY" + capital_allocated=0;
    a DARLING entry the composition root's injected `capital_fn` funds
    (src/equity_desk.py — a slice of the firm's 10L paper pool) is
    stamped mode="PAPER_CAPITAL" with its locked notional, and its exit
    settles through the injected `settle_fn`. No injection (every legacy
    caller, every test default) = byte-identical zero-capital telemetry;
  * this module STILL never imports journal / portfolio_manager /
    equity_desk / options_proposer / notifier — capital lives behind the
    injected seams, wired only at patience_basket.eod_chain;
  * a funding rejection never suppresses the telemetry row — "log the
    false positives" survives the capital era;
  * one open shadow per ticker; no same-day re-entry after an exit;
  * fail-open per ticker: a quote/data outage skips that ticker silently.

WIRED AT THE COMPOSITION ROOT ONLY: master_scheduler (and market_loop's
__main__) pass shadow_fn=run_cycle into run_market_loop. Direct callers of
run_market_loop — tests, the Phase 7 simulator — get shadow_fn=None and
the shadow stays OFF; nothing here can leak into a backtest.
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import knowledge_graph_logger as kg
from src.analysis import sector_trend, smart_money_trend

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent

MODE = "PAPER_TELEMETRY"
CAPITAL_MODE = "PAPER_CAPITAL"   # darling entries the equity desk funds
PULLBACK_BAND_PCT = 5.0   # entry zone: floor .. floor*(1+5%)
STOP_PCT = 2.0            # stop 2% below the block-VWAP floor
REWARD_RISK = 2.0         # target = entry + 2 * (entry - stop)
TIME_STOP_DAYS = 10       # calendar days before a stale thesis is closed
GAP_SHOCK_PCT = 2.0       # exit this far below the stop = gapped through it

# --- the Darling leg (F&O tranche step 5, owner-approved 2026-07-20;
#     re-wired to the 7-tier lifecycle system the same day) -------------
# A Buy-family darling (darling_tiers: strong_buy, or weak_buy that is
# actually IN the zone — near-zone names are never chased) is
# paper-bought at the day's close with the pricer's own levels: stop =
# the zone-failure stop, target = the first trim pivot above entry (2R
# fallback when the pricer marked no trim). A patience thesis resolves
# slower than a block-pullback, so it carries its own time stop.
# Directive 2 (No-Orphan rule): an open darling shadow graded strong_sell
# is FORCE-EXITED at the day's close — pinned names (weekly fundamental
# break) as "fundamental_break", valuation-extreme grades as
# "strong_sell_tier"; both autopsied like any other exit.
DARLING_SETUP = "darling_buy"        # legacy ledger rows carry
LEGACY_DARLING_SETUP = "darling_ripe"  # the pre-tier setup id
DARLING_TIME_STOP_DAYS = 45
TIERS_PATH = ROOT / "data" / "darling_tiers.json"
LEVELS_PATH = ROOT / "data" / "darlings_levels.json"


def _is_darling(entry: dict) -> bool:
    setup = ((entry.get("kyu_trigger") or {}).get("setup")) or ""
    return setup.startswith("darling")


def _ist_today() -> str:
    return datetime.now(IST).date().isoformat()


def candidate_tickers(deals_by_ticker) -> list:
    """Deal-covered NSE equities we can actually quote: the deals ledger's
    tickers ∩ the verified SECURITY_ID_MAP equity rows. Bounding the scan
    to deal-covered names keeps the throttled quote cost proportional to
    where the signal can even exist."""
    from src.dhan_client import SECURITY_ID_MAP
    eq = {t for t, m in SECURITY_ID_MAP.items()
          if m.get("inst") == "EQUITY" and t.endswith(".NS")}
    return sorted(set(deals_by_ticker) & eq)


def _ticker_sector(ticker: str, universe: dict) -> str | None:
    for name, meta in universe.items():
        if ticker in (meta.get("constituents") or []):
            return name
    return None


def _sector_verdict(ticker: str, universe: dict) -> dict:
    """Recorded context, deliberately NOT an entry gate: for a
    failure-knowledge ledger the sector read is a feature to correlate
    against outcomes, not a filter that hides the failures."""
    sector = _ticker_sector(ticker, universe)
    if sector is None:
        return {"sector": None, "bullish": None}
    try:
        v = sector_trend.is_sector_bullish(sector, universe=universe)
        return {"sector": sector, "bullish": v.get("bullish"),
                "detail": {k: v[k] for k in ("close", "sma50", "sma200")
                           if k in v}}
    except Exception:
        return {"sector": sector, "bullish": None}


def evaluate_entry(ticker: str, price: float, deals: list, as_of: str,
                   vix, sector_verdict: dict, nifty_trend=None) -> dict | None:
    """The block-VWAP pullback read for one ticker. Returns a ready-to-log
    entry event in the owner's four-question learning frame (2026-07-17):
    kyu_trigger (WHY the signal fired) / kaise_context (HOW the market
    stood) / kya_kara_action (WHAT we did); the exit tracker later adds
    kya_sikha_autopsy (what we LEARNED). Returns None when the setup isn't
    there."""
    if price is None or not deals:
        return None
    smart = smart_money_trend.smart_money_ok(deals, as_of, price)
    floor = smart.get("block_vwap")
    if not floor:
        return None  # no block floor = no thesis to falsify
    if not (floor <= price <= floor * (1 + PULLBACK_BAND_PCT / 100)):
        return None  # not a pullback: below the floor, or too far above it
    stop = round(floor * (1 - STOP_PCT / 100), 2)
    target = round(price + REWARD_RISK * (price - stop), 2)
    trigger = [d for d in deals
               if d.get("deal_type") == "block" and d.get("side") == "buy"
               and d.get("as_of", "") < as_of][-3:]
    top = max(trigger, key=lambda d: d.get("value_rs") or 0) if trigger else None
    signal = (f"block-VWAP floor ₹{floor} held: price ₹{price} within "
              f"+{PULLBACK_BAND_PCT:g}% of the floor")
    if top:
        signal += (f"; anchor block: {top.get('client')} "
                   f"₹{(top.get('value_rs') or 0) / 1e7:.1f}cr "
                   f"{top.get('side')} on {top.get('as_of')}")
    return {
        "event": "entry", "id": kg.new_id(), "mode": MODE,
        "capital_allocated": 0, "ticker": ticker, "as_of": as_of,
        "kyu_trigger": {                       # WHY: the exact alpha signal
            "setup": "block_vwap_pullback",
            "signal": signal,
            "block_vwap": floor,
            "accumulation": smart.get("accumulation"),
            "net_value_rs": smart.get("net_value_rs"),
            "n_recent_deals": smart.get("n_recent_deals"),
            "trigger_deals": [
                {k: d.get(k) for k in
                 ("as_of", "client", "qty", "price", "value_rs")}
                for d in trigger],
        },
        "kaise_context": {                     # HOW: the market at entry
            "vix": vix,
            "sector": sector_verdict,
            "nifty_trend": nifty_trend,
        },
        "kya_kara_action": {                   # WHAT we did (paper)
            "side": "long",
            "entry_price": price,
            "stop": stop,
            "target": target,
            "simulated_risk_pct": round((price - stop) / price * 100, 2),
        },
    }


def propose_shadow_entries(deals_by_ticker=None, quote_fn=None, vix_fn=None,
                           universe=None, path=None, as_of=None,
                           sector_fn=None, nifty_trend_fn=None) -> list:
    """Scan the candidate universe and log new PAPER_TELEMETRY entries.
    Returns the entry events logged this call."""
    if deals_by_ticker is None:
        deals_by_ticker = smart_money_trend.load_deals_by_ticker()
    if not deals_by_ticker:
        return []
    if quote_fn is None:
        from src.dhan_client import get_live_price as quote_fn
    if vix_fn is None:
        from src.dhan_client import get_india_vix as vix_fn
    universe = universe if universe is not None else sector_trend.load_universe()
    if sector_fn is None:
        sector_fn = lambda t: _sector_verdict(t, universe)  # noqa: E731
    if nifty_trend_fn is None:
        nifty_trend_fn = _nifty_trend
    as_of = as_of or _ist_today()

    events = kg.read_events(path)
    open_now = kg.open_positions(events=events)
    exited_today = {e.get("ticker") for e in events
                    if e.get("event") == "exit"
                    and str(e.get("ts", "")).startswith(as_of)}
    try:
        vix = vix_fn()
    except Exception:
        vix = None
    try:
        nifty_trend = nifty_trend_fn()
    except Exception:
        nifty_trend = None

    logged = []
    for ticker in candidate_tickers(deals_by_ticker):
        if ticker in open_now or ticker in exited_today:
            continue
        try:
            price = quote_fn(ticker)
            entry = evaluate_entry(ticker, price, deals_by_ticker[ticker],
                                   as_of, vix, sector_fn(ticker),
                                   nifty_trend=nifty_trend)
        except Exception:
            continue  # one ticker's outage never voids the scan
        if entry is not None:
            logged.append(kg.log_event(entry, path=path))
    return logged


def _nifty_trend() -> dict | None:
    """The broad-market read for kaise_context, from the same SMA/RSI
    analyzer the options loop trusts. Fail-open: None when unavailable
    (offline, short history) — recorded as unknown, never guessed."""
    try:
        from src.suggestions import analyze
        a = analyze("NIFTY 50")
        if a is None:
            return None
        return {"uptrend": a.get("uptrend"), "rsi": a.get("rsi"),
                "fresh_cross": a.get("fresh_cross")}
    except Exception:
        return None


def categorize_failure(reason: str, exit_price: float, entry: dict,
                       sector_bullish_at_exit) -> str:
    """kya_sikha_autopsy's headline: WHY did the thesis break? Rule-based
    and honest — categories only claim what the recorded facts support.
    GAP_SHOCK_PCT below the stop means the exit gapped through the level
    (we never got the orderly stop the plan assumed)."""
    action = entry.get("kya_kara_action") or {}
    trigger = entry.get("kyu_trigger") or {}
    floor = trigger.get("block_vwap")
    is_darling = _is_darling(entry)
    stop = action.get("stop")
    if reason == "target":
        return ("Target hit: first trim pivot reached from the buy zone"
                if is_darling else
                "Target hit: the block-VWAP floor defense held")
    if reason == "time_stop":
        return "Time stop: thesis never resolved either way"
    # stop_loss taxonomies, most specific first
    if stop and exit_price <= stop * (1 - GAP_SHOCK_PCT / 100):
        return "Gap-down shock: price gapped through the stop"
    if sector_bullish_at_exit is False:
        return "Stop-loss hit: sector dragged it down"
    if is_darling:
        return ("Buy-zone defense failed: price broke the ATR stop below "
                "the zone (cheap got cheaper)")
    if floor and exit_price < floor:
        return "VWAP defense failed: institutional floor broke (trap)"
    return "Stop-loss hit: idiosyncratic (sector intact, floor intact at category time)"


def track_open_shadows(quote_fn=None, vix_fn=None, universe=None, path=None,
                       now=None, sector_fn=None) -> list:
    """Resolve open shadows against live prices: stop_loss / target /
    time_stop. Every exit logs kya_sikha_autopsy — an automatic,
    rule-based categorization of WHY the thesis resolved the way it did
    (especially the failures; the failure rows are the point)."""
    if quote_fn is None:
        from src.dhan_client import get_live_price as quote_fn
    if vix_fn is None:
        from src.dhan_client import get_india_vix as vix_fn
    now = now or datetime.now(IST)
    open_now = kg.open_positions(path=path)
    if not open_now:
        return []
    universe = universe if universe is not None else sector_trend.load_universe()
    if sector_fn is None:
        sector_fn = lambda t: _sector_verdict(t, universe)  # noqa: E731
    try:
        vix_exit = vix_fn()
    except Exception:
        vix_exit = None

    logged = []
    for ticker, entry in open_now.items():
        action = entry.get("kya_kara_action") or {}
        stop, target = action.get("stop"), action.get("target")
        entry_price = action.get("entry_price")
        if stop is None or target is None or entry_price is None:
            continue  # malformed line — leave it for manual review
        try:
            price = quote_fn(ticker)
        except Exception:
            price = None
        if price is None:
            continue
        reason = None
        if price <= stop:
            reason = "stop_loss"
        elif price >= target:
            reason = "target"
        else:
            try:
                opened = date.fromisoformat(entry["as_of"])
                # Per-entry override (the darling leg's patience thesis
                # carries its own horizon); block-leg entries without the
                # key keep the original default.
                tsd = action.get("time_stop_days") or TIME_STOP_DAYS
                if (now.date() - opened).days >= tsd:
                    reason = "time_stop"
            except (ValueError, TypeError, KeyError):
                pass
        if reason is None:
            continue
        try:
            sector_at_exit = sector_fn(ticker)
        except Exception:
            sector_at_exit = {"sector": None, "bullish": None}
        risk = entry_price - stop
        r_mult = round((price - entry_price) / risk, 2) if risk else None
        floor = (entry.get("kyu_trigger") or {}).get("block_vwap")
        held = None
        try:
            held = (now.date() - date.fromisoformat(entry["as_of"])).days
        except (ValueError, TypeError, KeyError):
            pass
        logged.append(kg.log_event({
            # mode rides the ENTRY's stamp: a desk-funded (PAPER_CAPITAL)
            # position's exit must not masquerade as pure telemetry.
            "event": "exit", "id": entry["id"],
            "mode": entry.get("mode", MODE),
            "capital_allocated": 0, "ticker": ticker,
            "exit_price": price, "reason": reason,
            "kya_sikha_autopsy": {          # what we LEARNED
                "category": categorize_failure(
                    reason, price, entry, sector_at_exit.get("bullish")),
                "r_multiple": r_mult,
                "held_days": held,
                "below_block_vwap": (price < floor) if floor else None,
                "sector_at_exit": sector_at_exit,
                "vix_at_exit": vix_exit,
            },
        }, path=path))
    return logged


# ------------------------------------------------------ the Darling leg

def evaluate_darling_entry(row: dict, level: dict, as_of: str,
                           vix=None, sector_verdict=None,
                           nifty_trend=None, price=None,
                           fill_basis="eod_close") -> dict | None:
    """One Buy-tier row + its pricer level -> a ready-to-log entry
    event in the same four-question frame as the block leg. None when the
    row is malformed (missing close/stop, or stop >= entry — a data
    anomaly must never mint an un-falsifiable thesis). `price` overrides
    the row's close (the VM live path, decision #83 — entry at the LIVE
    quote, stamped with its own fill_basis)."""
    sym = row.get("symbol")
    close, stop = row.get("close"), row.get("stop")
    if price is not None:
        close = price
    if not sym or close is None or stop is None or stop >= close:
        return None
    # Target = the first trim pivot that pays AT LEAST 1R. A pivot a few
    # ticks above entry would resolve as an instant near-zero "win" and
    # poison the ledger's win-rate (caught on the leg's first live run:
    # LTF trim 310.5 vs entry 310.05). No qualifying pivot -> 2R fallback.
    risk = close - stop
    trims = [t for t in (level or {}).get("trim_levels") or []
             if isinstance(t, (int, float)) and t >= close + risk]
    target = round(min(trims), 2) if trims \
        else round(close + REWARD_RISK * risk, 2)
    zone = row.get("buy_zone")
    tier = row.get("tier") or "strong_buy"
    signal = (f"darling {tier}: valuation {row.get('valuation')} + "
              f"close ₹{close} in buy zone {zone} ({row.get('rule')})")
    return {
        "event": "entry", "id": kg.new_id(), "mode": MODE,
        "capital_allocated": 0, "ticker": f"{sym}.NS", "as_of": as_of,
        "kyu_trigger": {                     # WHY: the exact alpha signal
            "setup": DARLING_SETUP,
            "signal": signal,
            "tier": tier,
            "valuation": row.get("valuation"),
            "forensic": row.get("forensic"),
            "buy_zone": zone,
            "anchored_vwap": (level or {}).get("anchored_vwap"),
        },
        "kaise_context": {                   # HOW: the market at entry
            "vix": vix,
            "sector": sector_verdict or {"sector": None, "bullish": None},
            "nifty_trend": nifty_trend,
        },
        "kya_kara_action": {                 # WHAT we did (paper)
            "side": "long",
            "entry_price": close,
            "fill_basis": fill_basis,        # honesty: "eod_close" =
                                             # bhavcopy close; "live" = the
                                             # VM desk's real-time quote
            "stop": stop,
            "target": target,
            "time_stop_days": DARLING_TIME_STOP_DAYS,
            "simulated_risk_pct": round((close - stop) / close * 100, 2),
        },
    }


def entry_eligible_rows(tiers: dict) -> list:
    """The Buy-family rows the shadow book may actually buy: every
    strong_buy (in-zone by rule), plus weak_buy rows that are IN the
    zone — a near-zone weak_buy is watched, never chased (the patience
    doctrine survives the tier system)."""
    t = tiers.get("tiers") or {}
    return list(t.get("strong_buy") or []) + \
        [r for r in t.get("weak_buy") or [] if r.get("in_zone")]


def propose_darling_entries(tiers_path=None, levels_path=None, path=None,
                            as_of=None, check_fn=None, universe=None,
                            vix_fn=None, nifty_trend_fn=None,
                            capital_fn=None, quote_fn=None,
                            fill_basis="eod_close") -> list:
    """Log an entry for every entry-eligible Buy-tier darling that clears
    the equity halt stack (equity_entry_checks — the single enforcement
    door: ban-list/expiry/overextension judged there, never re-implemented
    here). With an injected `capital_fn` (the equity desk), a funded entry
    is stamped PAPER_CAPITAL with its locked notional; a rejection keeps
    the zero-capital telemetry row WITH the rejection reason — the
    learning ledger never loses a line to the capital layer. Same dedup
    contract as the block leg: one open shadow per ticker, no same-day
    re-entry. Fail-open per symbol."""
    import json as _json
    tpath = Path(tiers_path) if tiers_path else TIERS_PATH
    lpath = Path(levels_path) if levels_path else LEVELS_PATH
    try:
        tiers = _json.loads(tpath.read_text())
    except (OSError, ValueError):
        return []                            # no tier table = nothing to do
    buyable = entry_eligible_rows(tiers)
    if not buyable:
        return []
    if check_fn is None:
        from src.analysis.equity_entry_checks import check_entry as check_fn
    try:
        levels = {r.get("symbol"): r for r in
                  (_json.loads(lpath.read_text()).get("levels") or [])}
    except (OSError, ValueError):
        levels = {}
    as_of = as_of or _ist_today()

    events = kg.read_events(path)
    open_now = kg.open_positions(events=events)
    exited_today = {e.get("ticker") for e in events
                    if e.get("event") == "exit"
                    and str(e.get("ts", "")).startswith(as_of)}
    vix = nifty = None
    try:
        vix = vix_fn() if vix_fn else None   # Mac EOD: usually None (no token)
    except Exception:
        pass
    try:
        nifty = (nifty_trend_fn or _nifty_trend)()
    except Exception:
        pass
    if universe is None:
        try:
            universe = sector_trend.load_universe()
        except Exception:
            universe = {}

    logged = []
    for row in buyable:
        sym = row.get("symbol")
        if not sym or f"{sym}.NS" in open_now or f"{sym}.NS" in exited_today:
            continue
        try:
            verdict = check_fn({"symbol": sym, "direction": "long",
                                "instrument": "delivery"})
            if not verdict.get("allowed"):
                print(f"  (darling shadow: {sym} blocked by "
                      f"{verdict.get('blocked_by')}: {verdict.get('reason')})")
                continue
            price = None
            if quote_fn is not None:         # the VM LIVE path (#83): the
                price = quote_fn(f"{sym}.NS")    # quote must sit INSIDE the
                zone = row.get("buy_zone") or [None, None]   # STRICT zone
                if (price is None or zone[0] is None or zone[1] is None
                        or not zone[0] <= price <= zone[1]):
                    continue                 # no quote / out of zone today
            entry = evaluate_darling_entry(
                row, levels.get(sym), as_of, vix=vix,
                sector_verdict=_sector_verdict(f"{sym}.NS", universe),
                nifty_trend=nifty, price=price, fill_basis=fill_basis)
        except Exception:
            continue                         # one symbol never voids the scan
        if entry is None:
            continue
        if capital_fn is not None:
            try:
                funding = capital_fn(entry)
            except Exception as exc:         # desk down = telemetry only
                funding = {"funded": False,
                           "reason": f"desk unavailable ({exc})"}
            entry["funding"] = {k: funding.get(k) for k in
                                ("funded", "qty", "notional", "lock_ref",
                                 "reason")}
            if funding.get("funded"):
                entry["mode"] = CAPITAL_MODE
                entry["capital_allocated"] = funding["notional"]
                entry["kya_kara_action"]["qty"] = funding["qty"]
        logged.append(kg.log_event(entry, path=path))
    return logged


def force_exit_strong_sell(tiers_path=None, path=None, quote_fn=None,
                           now=None) -> list:
    """Directive 2 (No-Orphan rule): an open darling shadow whose symbol
    is graded strong_sell is closed at the day's price — a name we would
    never buy today on BROKEN FUNDAMENTALS or an EXTREME valuation does
    not get to coast to its technical stop. Pinned names (weekly
    fundamental break) exit as "fundamental_break"; grade-driven ones as
    "strong_sell_tier". Both are autopsied; both are learning rows.
    Weak-sell grades do NOT force an exit — the position's own stop is
    already the thesis-break detector. Fail-open per ticker."""
    import json as _json
    tpath = Path(tiers_path) if tiers_path else TIERS_PATH
    try:
        tiers = _json.loads(tpath.read_text())
    except (OSError, ValueError):
        return []
    sell_rows = {r["symbol"]: r for r in
                 (tiers.get("tiers") or {}).get("strong_sell") or []}
    if not sell_rows:
        return []
    if quote_fn is None:
        quote_fn = _eod_close_quote_fn()
    now = now or datetime.now(IST)

    logged = []
    for ticker, entry in kg.open_positions(path=path).items():
        sym = ticker.split(".")[0]
        if sym not in sell_rows or not _is_darling(entry):
            continue
        row = sell_rows[sym]
        try:
            price = quote_fn(ticker)
        except Exception:
            price = None
        if price is None:
            continue                 # stays open; graded again tomorrow
        if row.get("pinned"):
            reason = "fundamental_break"
            category = ("Fundamental break: weekly re-screen dropped the "
                        "name — forced exit (No-Orphan rule): "
                        f"{row.get('pinned')}")
        else:
            reason = "strong_sell_tier"
            category = ("Strong Sell grade: "
                        f"{row.get('rule')} — thesis vacated")
        action = entry.get("kya_kara_action") or {}
        entry_price, stop = action.get("entry_price"), action.get("stop")
        risk = (entry_price - stop) if None not in (entry_price, stop) else None
        held = None
        try:
            held = (now.date() - date.fromisoformat(entry["as_of"])).days
        except (ValueError, TypeError, KeyError):
            pass
        logged.append(kg.log_event({
            "event": "exit", "id": entry["id"],
            "mode": entry.get("mode", MODE),
            "capital_allocated": 0, "ticker": ticker,
            "exit_price": price, "reason": reason,
            "kya_sikha_autopsy": {          # what we LEARNED
                "category": category,
                "tier_rule": row.get("rule"),
                "r_multiple": (round((price - entry_price) / risk, 2)
                               if risk else None),
                "held_days": held,
                "below_block_vwap": None,
                "sector_at_exit": {"sector": None, "bullish": None},
                "vix_at_exit": None,
            },
        }, path=path))
    return logged


def _eod_close_quote_fn(lake_dir=None):
    """quote_fn for the Mac EOD chain: the latest bhavcopy close instead
    of a live quote (the Mac holds no Dhan token by design). Raises on a
    missing symbol so track_open_shadows' fail-open skip applies."""
    from src.ingestion.bhavcopy_clerk import bars_for

    def _quote(ticker: str):
        bars = bars_for(ticker, days=7, lake_dir=lake_dir)
        if not bars:
            raise LookupError(f"no bhavcopy bars for {ticker}")
        return bars[-1].get("close")
    return _quote


def run_darling_cycle(tiers_path=None, levels_path=None, path=None,
                      quote_fn=None, universe=None, check_fn=None,
                      now=None, as_of=None, capital_fn=None,
                      settle_fn=None) -> dict:
    """The Mac EOD darling shadow cycle: resolve open shadows against the
    day's bhavcopy closes (stop/target/time), force-exit Strong-Sell
    grades (Directive 2), SETTLE any desk-funded exits (injected
    settle_fn — capital freed today is buyable today), then log entries
    for today's Buy-tier names (injected capital_fn sizes and locks;
    None = zero-capital telemetry, byte-identical to the pre-desk leg).
    Composition-root seam (patience_basket.eod_chain); offline-honest
    end to end — no live quotes, no token, vix recorded as None."""
    quote_fn = quote_fn or _eod_close_quote_fn()
    exits = track_open_shadows(quote_fn=quote_fn,
                               vix_fn=lambda: None,
                               universe=universe, path=path, now=now)
    exits += force_exit_strong_sell(tiers_path=tiers_path, path=path,
                                    quote_fn=quote_fn, now=now)
    settlements = []
    if settle_fn is not None and exits:
        hosts = {e.get("id"): e for e in kg.read_events(path)
                 if e.get("event") == "entry"}
        for x in exits:
            host = hosts.get(x.get("id"))
            if not host or not (host.get("funding") or {}).get("funded"):
                continue
            try:
                s = settle_fn(host, x)
            except Exception as exc:         # sweep CLI reconciles later
                print(f"  (darling settlement failed for "
                      f"{x.get('ticker')}: {exc})")
                continue
            if s:
                settlements.append(s)
    entries = propose_darling_entries(tiers_path=tiers_path,
                                      levels_path=levels_path, path=path,
                                      as_of=as_of, check_fn=check_fn,
                                      universe=universe,
                                      capital_fn=capital_fn)
    return {"entries": entries, "exits": exits, "settlements": settlements}


def run_cycle(deals_by_ticker=None, quote_fn=None, vix_fn=None,
              universe=None, path=None, sector_fn=None,
              nifty_trend_fn=None) -> dict:
    """One shadow cycle: resolve open positions first (an exit today frees
    nothing — same-day re-entry is blocked), then scan for new entries.
    The market_loop seam; the caller wraps it fail-open."""
    exits = track_open_shadows(quote_fn=quote_fn, vix_fn=vix_fn,
                               universe=universe, path=path,
                               sector_fn=sector_fn)
    entries = propose_shadow_entries(deals_by_ticker=deals_by_ticker,
                                     quote_fn=quote_fn, vix_fn=vix_fn,
                                     universe=universe, path=path,
                                     sector_fn=sector_fn,
                                     nifty_trend_fn=nifty_trend_fn)
    return {"entries": entries, "exits": exits}


if __name__ == "__main__":
    # Manual smoke test: python3 -m src.equity_shadow_proposer
    res = run_cycle()
    print(f"shadow cycle — {len(res['entries'])} entries, "
          f"{len(res['exits'])} exits (mode={MODE})")
    for e in res["entries"] + res["exits"]:
        print(" ", e)
