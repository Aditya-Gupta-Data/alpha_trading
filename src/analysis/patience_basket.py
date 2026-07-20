"""
src/analysis/patience_basket.py — the Patience Basket (Dept 8, advisory)
========================================================================

The owner's master strategy (2026-07-19 night): all queued Darlings are
fundamentally phenomenal; we ONLY act when one is simultaneously CHEAP
(Valuation Engine) and IN its accumulation zone (dynamic pricer) and not
overextended (Law 3). No FOMO: a runner is let go; another Darling will
fall into our lap. This module is the JOIN that makes that watchable —
it decides nothing (Law #63: advisory, annotate-only).

Per queued symbol it merges:
  dynamic_pricer   -> close, buy zone, stop, extension state
  valuation_scorer -> 1-100 score (1 = deeply undervalued)
  darlings_queue   -> forensic score + flagged status

and buckets the basket:

  RIPE                in zone AND valuation <= RIPE_MAX_VALUATION (40)
                      AND not overextended — the only bucket that ever
                      deserves attention on the day
  in_zone_not_cheap   price is right, valuation isn't
  cheap_not_in_zone   valuation is right, price hasn't come in
  below_zone          fell through the shelf (discount or warning — the
                      deep-read decides which, not this module)
  waiting             everything else fundamentally fine
  no_valuation        vetoed/insufficient at the valuation stage

Output: data/patience_basket.json. NEWLY-ripe names (vs the previous
basket file) fire ONE de-duped Discord card via fire_broadcast — the
owner directive that review-worthy events reach Discord in real time;
repeat-ripe names never re-fire (no spam, the state file IS the ledger).

--eod runs the whole evening chain in order: today's bhavcopy ->
pricer -> valuation -> basket -> card. MAC-ONLY (boundary doctrine).

CLI:  python3 -m src.analysis.patience_basket [--eod] [--dry-run]
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
LEVELS_PATH = ROOT / "data" / "darlings_levels.json"
VALUATION_PATH = ROOT / "data" / "darlings_valuation.json"
BASKET_PATH = ROOT / "data" / "patience_basket.json"

IST = timezone(timedelta(hours=5, minutes=30))
RIPE_MAX_VALUATION = 40            # documented, owner-tunable


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def build(queue_path=None, levels_path=None, valuation_path=None,
          prev_basket: dict = None) -> dict:
    """Join the three artifacts -> the bucketed basket + newly_ripe."""
    queue = _load(queue_path or QUEUE_PATH, {})
    levels = {r["symbol"]: r for r in
              _load(levels_path or LEVELS_PATH, {}).get("levels") or []}
    valuation = _load(valuation_path or VALUATION_PATH, {})
    scores = valuation.get("scores") or {}
    forensic = {o["symbol"]: o for o in
                (queue.get("passed") or []) + (queue.get("flagged") or [])}

    buckets = {"ripe": [], "in_zone_not_cheap": [], "cheap_not_in_zone": [],
               "below_zone": [], "waiting": [], "no_valuation": []}
    for sym in queue.get("tickers") or []:
        lv = levels.get(sym)
        val = scores.get(sym, {}).get("score")
        fx = (forensic.get(sym, {}).get("forensic") or {}).get("score")
        row = {"symbol": sym, "valuation": val, "forensic": fx}
        if lv and lv.get("status") == "ok":
            zone_lo, zone_hi = lv["buy_zone"]
            close = lv["close"]
            row.update({"close": close, "buy_zone": lv["buy_zone"],
                        "stop": lv["stop"], "extension": lv["extension"]})
            in_zone = (None not in (zone_lo, zone_hi, close)
                       and zone_lo <= close <= zone_hi)
            below = (None not in (zone_lo, close) and close < zone_lo)
        else:
            in_zone = below = False
        if val is None:
            buckets["no_valuation"].append(row)
        elif in_zone and val <= RIPE_MAX_VALUATION \
                and row.get("extension") != "overextended":
            buckets["ripe"].append(row)
        elif in_zone:
            buckets["in_zone_not_cheap"].append(row)
        elif val <= RIPE_MAX_VALUATION and below:
            buckets["below_zone"].append(row)
        elif val <= RIPE_MAX_VALUATION:
            buckets["cheap_not_in_zone"].append(row)
        elif below:
            buckets["below_zone"].append(row)
        else:
            buckets["waiting"].append(row)

    prev = prev_basket if prev_basket is not None else _load(BASKET_PATH, {})
    prev_ripe = {r["symbol"] for r in prev.get("ripe") or []}
    newly_ripe = [r for r in buckets["ripe"]
                  if r["symbol"] not in prev_ripe]

    return {"as_of": datetime.now(IST).replace(tzinfo=None)
                                      .isoformat(timespec="seconds"),
            "ripe_rule": f"in zone AND valuation<={RIPE_MAX_VALUATION} "
                         "AND not overextended",
            **buckets, "newly_ripe": [r["symbol"] for r in newly_ripe],
            "advisory_note": "ADVISORY-ONLY (Law #63): the basket "
                             "annotates; no capital moves itself."}


def broadcast_newly_ripe(basket: dict, broadcast_fn=None) -> bool:
    """ONE card, only when something newly ripened. Fail-open."""
    newly = basket.get("newly_ripe") or []
    if not newly:
        return False
    try:
        if broadcast_fn is None:
            from src.notifier import fire_broadcast
            broadcast_fn = fire_broadcast
        rows = {r["symbol"]: r for r in basket.get("ripe") or []}
        lines = []
        for s in newly:
            r = rows.get(s, {})
            zone = r.get("buy_zone") or ["?", "?"]
            lines.append(f"{s}: ₹{r.get('close')} in zone "
                         f"₹{zone[0]}–{zone[1]}, val {r.get('valuation')}, "
                         f"stop ₹{r.get('stop')}")
        broadcast_fn({
            "event": "patience_basket_ripe", "ticker": "DARLINGS",
            "date": basket.get("as_of", ""),
            "description": ("🧺 Patience Basket — newly RIPE "
                            f"({len(newly)}):\n" + "\n".join(lines)
                            + "\nAdvisory only — levels move at next EOD."),
        })
        return True
    except Exception as e:
        print(f"  (basket broadcast skipped: {e})")
        return False


def run(write: bool = True, broadcast: bool = True,
        broadcast_fn=None, **paths) -> dict:
    basket = build(**{k: v for k, v in paths.items()
                      if k in ("queue_path", "levels_path",
                               "valuation_path", "prev_basket")})
    if broadcast:
        basket["card_fired"] = broadcast_newly_ripe(basket, broadcast_fn)
    if write:
        out = Path(paths.get("basket_path") or BASKET_PATH)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(basket, indent=1))
    return basket


def eod_chain() -> dict:
    """The Mac evening chain: today's bhavcopy -> pricer -> valuation ->
    basket (+card). Each stage fail-opens; a missing bhavcopy (holiday)
    still refreshes the join from existing artifacts."""
    from datetime import date as _date
    from src.analysis.dynamic_pricer import run as pricer_run
    from src.analysis.valuation_scorer import run as valuation_run
    from src.ingestion.bhavcopy_clerk import fetch_day
    from src.ingestion.fo_bhavcopy import fetch_recent
    day = fetch_day(_date.today())
    fo = fetch_recent(3)          # F&O bundle leg (owner: no manual DLs)
    pricer_run()
    valuation_run()
    basket = run()
    basket["bhavcopy"] = day
    basket["fo_snapshot_as_of"] = fo.get("snapshot_as_of")
    return basket


if __name__ == "__main__":
    import sys

    if "--eod" in sys.argv:
        b = eod_chain()
    else:
        b = run(write="--dry-run" not in sys.argv,
                broadcast="--dry-run" not in sys.argv)
    for bucket in ("ripe", "in_zone_not_cheap", "cheap_not_in_zone",
                   "below_zone", "waiting", "no_valuation"):
        print(f"{bucket:20} {len(b.get(bucket) or [])}")
    for r in b.get("ripe") or []:
        print(f"  RIPE {r['symbol']:14} ₹{r.get('close')} "
              f"zone {r.get('buy_zone')} val {r.get('valuation')} "
              f"forensic {r.get('forensic')}")
