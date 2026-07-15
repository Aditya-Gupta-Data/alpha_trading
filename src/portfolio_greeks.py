"""
src/portfolio_greeks.py — portfolio-level Greeks advisory (Stage 5 gap)
=======================================================================

The research audit's #1 structural gap (docs/gemini_research_gap_analysis.md
§5): the engine thinks PER TRADE. The exposure gate (#68) caps how many
spreads of one direction can be open, but nothing looks at the book's
AGGREGATE risk. Ten "neutral" iron condors are not ten independent bets —
they are one concentrated SHORT-VEGA position, and if India VIX gaps from
12 to 25 the whole book bleeds at once, no single trade's stop involved.

This module aggregates the open paper book's net Greeks from Dhan's own
per-strike chain Greeks (delta/theta/gamma/vega — no Black-Scholes engine
needed, they ship in the chain), and warns when a budget is breached.

Two budgets, both as a fraction of realized equity, both binary verdicts
(#63 — never a blended score):

  NET VEGA   the spread-seller's real risk. net_vega = Rs. P&L per +1
             IV point across the whole book. Breach when a reference vol
             shock (default +5 IV points, the 12->17 class of move)
             would cost more than `greeks_vega_budget_pct` of equity.

  NET DELTA  hidden directional drift. A book of "neutral" condors can
             lean net-short delta via volatility skew and quietly become
             a directional bet. net_delta_notional = Rs. of underlying
             exposure (per-point delta x that index's spot). Breach when
             |net_delta_notional| exceeds `greeks_delta_budget_pct` of
             equity.

Net THETA (Rs./day of decay collected) and net GAMMA are REPORTED, not
budgeted: theta is informational (a healthy seller book runs positive),
and gamma/pin-risk is already handled structurally by the tracker's
2-days-before-expiry exit (#27), so a second gamma cap would be theatre.

DOCTRINE, identical to the exposure gate (#68) and live bridge (#41):
  * ADVISORY ONLY — reads the journal + chains, writes nothing to trade
    state, settles nothing, proposes nothing. Its only write is its own
    snapshot ledger (logs/greeks_snapshots.jsonl).
  * FAIL-OPEN / HONEST ABSTENTION — a leg with no chain Greeks makes its
    whole position UNPRICEABLE; that position is excluded from the
    aggregate and counted, never guessed (#50 NULL-honesty). Any error
    anywhere degrades to "no advisory", never a raised exception.
  * ONE card per breach-type per IST day — the ledger IS the once-per-day
    memory (Issue-8 pattern, no new state file), same as the exposure
    gate. Every run still snapshots (the history the ledger is for).
  * NOT wired into the live loop — a standalone advisory + CLI
    (`python3 -m src.portfolio_greeks`), the eod_summary/portfolio_report
    class of read-only cron card. The hot trading path stays byte-
    identical; scheduling is a later, separate call.

Config knobs (all optional in config.json, defaults below so old copies
keep working): `portfolio_greeks_advisory` (default True — the kill
switch), `greeks_vega_shock_points` (5.0), `greeks_vega_budget_pct`
(15.0), `greeks_delta_budget_pct` (30.0).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "logs" / "greeks_snapshots.jsonl"
CONFIG_PATH = ROOT / "config.json"
IST = timezone(timedelta(hours=5, minutes=30))

# Defaults; overridable in config.json (see module docstring).
DEFAULT_VEGA_SHOCK_POINTS = 5.0
DEFAULT_VEGA_BUDGET_PCT = 15.0
DEFAULT_DELTA_BUDGET_PCT = 30.0

_GREEK_KEYS = ("delta", "theta", "gamma", "vega")


def _config() -> dict:
    """config.json as a dict, or {} if unreadable — read directly (not via
    src.config) so a missing optional key never raises, matching the
    live_bridge kill-switch pattern."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _chain_node(chain: dict, strike: float, option_type: str) -> dict | None:
    """The ce/pe node for one strike, tolerant of Dhan's six-decimal key
    format and a plain string key. None when absent."""
    oc = (chain or {}).get("oc") or {}
    node = oc.get(f"{float(strike):.6f}") or oc.get(str(strike)) or {}
    return node.get(option_type.lower()) or None


def leg_greeks(chain: dict, strike: float, option_type: str) -> dict | None:
    """Per-SHARE {delta, theta, gamma, vega} for one option leg from the
    chain's own Greeks, or None if the strike/side or ANY Greek is
    missing (an incomplete Greek set is not a guessable zero — the whole
    leg abstains). Greeks are reported by Dhan for the option as if long;
    the caller applies the BUY/SELL sign."""
    node = _chain_node(chain, strike, option_type)
    if not node:
        return None
    g = node.get("greeks") or {}
    out = {}
    for k in _GREEK_KEYS:
        v = g.get(k)
        if v is None:
            return None
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            return None
    return out


def position_greeks(spread: dict, chain: dict) -> dict | None:
    """Net {delta, theta, gamma, vega} for ONE spread, signed by side
    (BUY long = +, SELL short = -) and scaled by lot_size x lots — i.e.
    the position's actual contribution to the book. None if any leg is
    unpriceable (the whole position abstains — a half-priced spread's
    net Greek is a lie)."""
    legs = (spread or {}).get("legs") or []
    if not legs:
        return None
    qty = int(spread.get("lot_size", 0)) * int(spread.get("lots", 1))
    if qty <= 0:
        return None
    net = {k: 0.0 for k in _GREEK_KEYS}
    for leg in legs:
        g = leg_greeks(chain, leg.get("strike"), leg.get("option_type", ""))
        if g is None:
            return None
        sign = 1.0 if str(leg.get("side", "")).upper() == "BUY" else -1.0
        for k in _GREEK_KEYS:
            net[k] += sign * g[k] * qty
    return {k: round(v, 4) for k, v in net.items()}


def aggregate(open_spreads: list, chains: dict, spots: dict) -> dict:
    """Roll every open spread's Greeks into one book-level view.

    open_spreads : [{"trade_id","ticker","expiry","spread":{legs,lot_size,
                     lots,...}}, ...] — the raw spread blocks, NOT
                    positions.py display rows (those drop the legs).
    chains       : {(ticker, expiry): chain_dict} — each position priced
                    on ITS OWN expiry's chain, never a nearest-expiry
                    stand-in.
    spots        : {ticker: spot_price} — for notional-delta conversion.

    Returns net_delta/theta/gamma/vega, net_delta_notional (Rs., additive
    across underlyings), priced/unpriced counts, and a per-position
    breakdown. Unpriceable positions are listed, never zero-filled."""
    net = {k: 0.0 for k in _GREEK_KEYS}
    net_delta_notional = 0.0
    priced, unpriced, detail = [], [], []
    for pos in open_spreads or []:
        ticker = pos.get("ticker")
        spread = pos.get("spread") or {}
        chain = chains.get((ticker, pos.get("expiry")))
        pg = position_greeks(spread, chain) if chain else None
        if pg is None:
            unpriced.append(pos.get("trade_id"))
            detail.append({"trade_id": pos.get("trade_id"), "ticker": ticker,
                           "priced": False})
            continue
        spot = spots.get(ticker)
        notional = pg["delta"] * float(spot) if spot else None
        if notional is not None:
            net_delta_notional += notional
        for k in _GREEK_KEYS:
            net[k] += pg[k]
        priced.append(pos.get("trade_id"))
        detail.append({"trade_id": pos.get("trade_id"), "ticker": ticker,
                       "priced": True, "greeks": pg,
                       "delta_notional": (round(notional, 2)
                                          if notional is not None else None)})
    return {
        "net_delta": round(net["delta"], 4),
        "net_theta": round(net["theta"], 2),
        "net_gamma": round(net["gamma"], 6),
        "net_vega": round(net["vega"], 2),
        "net_delta_notional": round(net_delta_notional, 2),
        "priced_ids": priced,
        "unpriced_ids": unpriced,
        "priced_count": len(priced),
        "unpriced_count": len(unpriced),
        "detail": detail,
    }


def evaluate(agg: dict, equity: float, config: dict = None) -> dict:
    """Apply the equity-scaled budgets to an aggregate. Returns the verdict:
    per-budget OK/BREACH (binary, #63), the breach list, and the numbers.
    A non-positive equity or an all-unpriced book yields no verdict
    (abstain — there is nothing honest to say)."""
    cfg = config or {}
    shock = float(cfg.get("greeks_vega_shock_points", DEFAULT_VEGA_SHOCK_POINTS))
    vega_pct = float(cfg.get("greeks_vega_budget_pct", DEFAULT_VEGA_BUDGET_PCT))
    delta_pct = float(cfg.get("greeks_delta_budget_pct", DEFAULT_DELTA_BUDGET_PCT))

    if not equity or equity <= 0 or agg.get("priced_count", 0) == 0:
        return {"verdict": "abstain", "breaches": [],
                "reason": ("no priced positions" if agg.get("priced_count", 0) == 0
                           else "equity unavailable")}

    vega_shock_loss = abs(agg["net_vega"]) * shock
    vega_budget = equity * vega_pct / 100.0
    delta_notional = abs(agg["net_delta_notional"])
    delta_budget = equity * delta_pct / 100.0

    breaches = []
    if vega_shock_loss > vega_budget:
        breaches.append("vega")
    if delta_notional > delta_budget:
        breaches.append("delta")

    return {
        "verdict": "breach" if breaches else "ok",
        "breaches": breaches,
        "equity": round(equity, 2),
        "vega": {"net_vega": agg["net_vega"], "shock_points": shock,
                 "shock_loss": round(vega_shock_loss, 2),
                 "budget": round(vega_budget, 2),
                 "pct_of_budget": (round(vega_shock_loss / vega_budget * 100, 1)
                                   if vega_budget else None)},
        "delta": {"net_delta_notional": agg["net_delta_notional"],
                  "budget": round(delta_budget, 2),
                  "pct_of_budget": (round(delta_notional / delta_budget * 100, 1)
                                    if delta_budget else None)},
        "net_theta": agg["net_theta"],
        "net_gamma": agg["net_gamma"],
    }


def build_card(agg: dict, verdict: dict) -> str:
    """The Discord/terminal advisory card — plain-English-led, honest
    about coverage. Reports even on OK so a scheduled run is legible."""
    v = verdict.get("verdict")
    if v == "abstain":
        return ("🧮 **Portfolio Greeks** — no advisory: "
                f"{verdict.get('reason', 'nothing to price')}.")
    head = ("⚠️ **Portfolio Greeks — BUDGET BREACH**"
            if v == "breach" else "🧮 **Portfolio Greeks — within budget**")
    cover = (f"{agg['priced_count']} position(s) priced"
             + (f", {agg['unpriced_count']} un-priceable (no chain Greeks)"
                if agg["unpriced_count"] else ""))
    veg, dlt = verdict["vega"], verdict["delta"]
    lines = [
        head,
        f"_{cover}. Equity Rs.{verdict['equity']:,.0f}._",
        (f"• **Vega**: net Rs.{veg['net_vega']:,.0f}/IV-pt → a +{veg['shock_points']:g} "
         f"pt vol shock costs ~Rs.{veg['shock_loss']:,.0f} "
         f"({veg['pct_of_budget']:g}% of the Rs.{veg['budget']:,.0f} budget)"
         + ("  ❌" if "vega" in verdict["breaches"] else "  ✅")),
        (f"• **Delta**: net notional Rs.{dlt['net_delta_notional']:,.0f} directional "
         f"({dlt['pct_of_budget']:g}% of the Rs.{dlt['budget']:,.0f} budget)"
         + ("  ❌" if "delta" in verdict["breaches"] else "  ✅")),
        (f"• Theta Rs.{verdict['net_theta']:,.0f}/day "
         f"{'collected' if verdict['net_theta'] >= 0 else 'paid'}, "
         f"net gamma {verdict['net_gamma']:+.4f} (informational)"),
    ]
    if v == "breach":
        lines.append("Advisory only — nothing settles here. Consider "
                     "trimming the concentrated leg before adding exposure "
                     "(research §5 / decision below).")
    return "\n".join(lines)


# ------------------------------------------------------- ledger + dedup

def _carded_today(breach_type: str, day: str) -> bool:
    """True if a Discord card was already sent today for this breach type
    — the ledger's `carded` field is the once-per-day memory."""
    try:
        if not LEDGER_PATH.exists():
            return False
        for line in LEDGER_PATH.read_text().splitlines():
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if (str(rec.get("ts", "")).startswith(day)
                    and breach_type in (rec.get("carded") or [])):
                return True
    except OSError:
        pass
    return False


def snapshot_and_notify(agg: dict, verdict: dict, *, notify_fn=None,
                        now_fn=None) -> dict:
    """Append one snapshot row (always — the ledger is the history) and
    fire at most ONE Discord card per breach-type per IST day. Returns
    {"snapshotted", "carded":[...]}. Every failure is swallowed —
    bookkeeping never changes the read."""
    now = (now_fn or (lambda: datetime.now(IST)))()
    day = now.date().isoformat()
    breaches = verdict.get("breaches", [])
    to_card = [b for b in breaches if not _carded_today(b, day)]

    if to_card and notify_fn:
        try:
            notify_fn(build_card(agg, verdict))
        except Exception:
            to_card = []  # card didn't go out — don't record it as carded

    row = {
        "ts": f"{day}T{now.time().isoformat(timespec='seconds')}",
        "verdict": verdict.get("verdict"),
        "breaches": breaches,
        "carded": to_card,
        "net_vega": agg.get("net_vega"),
        "net_delta_notional": agg.get("net_delta_notional"),
        "net_theta": agg.get("net_theta"),
        "net_gamma": agg.get("net_gamma"),
        "priced_count": agg.get("priced_count"),
        "unpriced_count": agg.get("unpriced_count"),
        "equity": verdict.get("equity"),
    }
    try:
        LEDGER_PATH.parent.mkdir(exist_ok=True)
        with open(LEDGER_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
        snapshotted = True
    except OSError:
        snapshotted = False
    return {"snapshotted": snapshotted, "carded": to_card}


# ------------------------------------------------------- live entry point

def _open_spreads_from_journal(entries: list = None) -> list:
    """Open spread positions WITH their raw legs — the aggregate needs
    strike/side/lot detail that positions.py display rows drop. Reuses
    plan_tracker's own `_spread_trackable` predicate so this can't drift
    from what the tracker considers open."""
    from src import journal
    from src.plan_tracker import _spread_trackable
    if entries is None:
        entries = journal.read_all()
    out = []
    for e in entries:
        if (e.get("decision") == "approved" and e.get("outcome") is None
                and e.get("spread") and _spread_trackable(e)):
            s = e["spread"]
            out.append({"trade_id": e.get("short_id"), "ticker": e.get("ticker"),
                        "expiry": s.get("expiry"), "spread": s})
    return out


def run_advisory(*, entries=None, chain_fn=None, spot_fn=None,
                 equity=None, notify_fn=None, config=None,
                 now_fn=None) -> dict:
    """Fetch → aggregate → evaluate → snapshot/notify, all fail-open.

    Every I/O seam is injectable for offline tests; the production path
    fills them from SafeDhanClient + the capital pool. Returns the
    verdict dict (plus `aggregate` and `card`) or a `{"skipped": ...}`
    marker. Honors the `portfolio_greeks_advisory` kill switch."""
    try:
        cfg = config if config is not None else _config()
        if not cfg.get("portfolio_greeks_advisory", True):
            return {"skipped": "disabled in config"}

        open_spreads = _open_spreads_from_journal(entries)
        if not open_spreads:
            return {"skipped": "no open spreads"}

        # One chain fetch per distinct (ticker, expiry) in the book.
        if chain_fn is None or spot_fn is None or equity is None:
            from src.dhan_guard import SafeDhanClient
            _client = SafeDhanClient()
            if chain_fn is None:
                chain_fn = _client.get_option_chain
            if spot_fn is None:
                def spot_fn(t, _c=_client):
                    return _c.get_live_price(t)
            if equity is None:
                from src import brain_map, portfolio_manager as pm
                conn = brain_map.connect()
                try:
                    equity = pm.equity(conn)
                finally:
                    conn.close()

        chains, spots = {}, {}
        for pos in open_spreads:
            key = (pos["ticker"], pos["expiry"])
            if key not in chains:
                try:
                    chains[key] = chain_fn(pos["ticker"], pos["expiry"])
                except Exception:
                    chains[key] = None
            if pos["ticker"] not in spots:
                # Chain top-level last_price is the spot; fall back to a
                # dedicated spot fetch only if the chain didn't come back.
                ch = chains.get(key) or {}
                spot = ch.get("last_price")
                if not spot and spot_fn:
                    try:
                        spot = spot_fn(pos["ticker"])
                    except Exception:
                        spot = None
                spots[pos["ticker"]] = float(spot) if spot else None

        agg = aggregate(open_spreads, chains, spots)
        verdict = evaluate(agg, equity, cfg)
        result = snapshot_and_notify(agg, verdict, notify_fn=notify_fn,
                                     now_fn=now_fn)
        return {"aggregate": agg, "verdict": verdict,
                "card": build_card(agg, verdict), **result}
    except Exception as e:
        print(f"  (portfolio-greeks advisory skipped — failing open: {e})")
        return {"skipped": f"error: {e}"}


def main() -> None:
    """CLI: `python3 -m src.portfolio_greeks` — run the advisory and print
    the card. Posts to Discord too (via notifier) unless --quiet."""
    import sys
    quiet = "--quiet" in sys.argv
    notify_fn = None
    if not quiet:
        try:
            from src import notifier

            def notify_fn(msg):
                notifier.fire_broadcast({"embeds": [{"description": msg,
                                                     "color": 0xE67E22}]})
        except Exception:
            notify_fn = None
    out = run_advisory(notify_fn=notify_fn)
    if "card" in out:
        print(out["card"])
    else:
        print(f"portfolio-greeks: {out.get('skipped', out)}")


if __name__ == "__main__":
    main()
