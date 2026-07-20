"""
src/analysis/darling_tiers.py — the 7-Tier Darling grading engine (Dept 8)
==========================================================================

Owner directive 2026-07-20 (the Lifecycle Portfolio Management System):
the binary RIPE/waiting basket is scrapped. Every darling is graded into
exactly one tier every EOD, so a name is never "done" after entry — the
same table that says BUY also says HOLD, SELL and WATCH for the names we
already track. ADVISORY-ONLY (Law #63) and zero-capital: the grades feed
the PAPER_TELEMETRY shadow book, never real capital.

THE TIERS (approved mechanical definitions, 2026-07-20 — no vibes):

  strong_buy   in zone + valuation <= 25 + clean forensics + not
               overextended (Law 3)
  weak_buy     in-or-near zone (near = within 5% above the zone ceiling)
               + valuation <= 45 + clean forensics + not overextended
  strong_hold  momentum strong: close > 50-DMA AND 50-DMA > 200-DMA
  weak_hold    valuation 70-84, OR consolidating within 1 ATR of the
               stop reference (trailing pivot floor when the pricer has
               one, else the hard stop), OR the honest fallback when no
               other rule fires — tighten trails, do nothing else
  weak_sell    close broke below the buy zone, OR losing volume (20-day
               avg turnover < 60-day avg) while valuation >= 70
  strong_sell  valuation >= 85, OR close below the hard stop, OR PINNED
               by the weekly fundamental re-screen (Directive 2, the
               No-Orphan rule)
  watch        the skeptical bucket: forensic caution band (flagged in
               the queue). Never graded a Buy mechanically — the "absurd
               discount" override is a HUMAN call; the facts columns
               (in_zone, valuation) are recorded so a human can spot it.
  ungraded     TIER 0 (NULL-honesty, approved pushback #2): missing
               levels or a vetoed/insufficient valuation. A grade is
               never manufactured from absent data.

Precedence is top-down as coded in grade_one — first matching rule wins,
and every row records WHICH rule fired (a grade is derived, explainable,
never hand-assigned).

PINS (data/darling_pins.json, written by weekly_recalibration): a name
that failed the weekly fundamental check while we hold an open paper
shadow is pinned — grade "strong_sell" when the screen REJECTED it,
"ungraded" when the screen merely lost the data to judge it (a sell
verdict is never manufactured from absence either). A pin holds the name
in the table even after it leaves the darlings queue; the daily run
clears any pin whose shadow has closed, at which point the name drops
off completely.

CARD POLICY (approved pushback #4): Discord fires on FAMILY transitions
only (buy / hold / sell / watch / ungraded). Intrafamily moves (e.g.
strong_buy -> weak_buy) stay silent but visible in the daily table. The
first-ever grading fires ONE distribution summary instead of a per-name
flood. The tier file itself is the de-dup ledger.

Output: data/darling_tiers.json. CLI:
    python3 -m src.analysis.darling_tiers [--dry-run]
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE_PATH = ROOT / "data" / "darlings_queue.json"
LEVELS_PATH = ROOT / "data" / "darlings_levels.json"
VALUATION_PATH = ROOT / "data" / "darlings_valuation.json"
PINS_PATH = ROOT / "data" / "darling_pins.json"
TIERS_PATH = ROOT / "data" / "darling_tiers.json"

IST = timezone(timedelta(hours=5, minutes=30))

# Approved mechanical thresholds (2026-07-20) — owner-tunable, documented.
NEAR_ZONE_PCT = 5.0        # "near zone" = within 5% above the zone ceiling
STRONG_BUY_MAX_VAL = 25
WEAK_BUY_MAX_VAL = 45
WEAK_HOLD_MIN_VAL = 70
STRONG_SELL_MIN_VAL = 85
NEAR_STOP_ATRS = 1.0       # "consolidating near stop" = within 1 ATR
VOL_FAST_N, VOL_SLOW_N = 20, 60   # "losing volume" = 20d avg < 60d avg

TIER_ORDER = ["strong_buy", "weak_buy", "strong_hold", "weak_hold",
              "weak_sell", "strong_sell", "watch", "ungraded"]
FAMILY = {"strong_buy": "buy", "weak_buy": "buy",
          "strong_hold": "hold", "weak_hold": "hold",
          "weak_sell": "sell", "strong_sell": "sell",
          "watch": "watch", "ungraded": "ungraded"}


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------- turnover

def volume_read(bars: list) -> dict:
    """20d-vs-60d average traded value from bhavcopy bars. NULL-honest:
    fewer than 60 turnover-bearing sessions -> losing_volume None (a
    volume verdict is never guessed from a short tape)."""
    vals = [b.get("turnover_lacs") for b in bars or []
            if b.get("turnover_lacs") is not None]
    if len(vals) < VOL_SLOW_N:
        return {"losing_volume": None}
    t20 = sum(vals[-VOL_FAST_N:]) / VOL_FAST_N
    t60 = sum(vals[-VOL_SLOW_N:]) / VOL_SLOW_N
    return {"losing_volume": t20 < t60,
            "t20_avg_lacs": round(t20, 1), "t60_avg_lacs": round(t60, 1)}


def load_turnover(symbols: list, lake_dir=None) -> dict:
    """{symbol: volume_read} for the whole cohort in one lake pass."""
    try:
        from src.ingestion.bhavcopy_clerk import bars_for_many
        bars = bars_for_many(symbols, days=VOL_SLOW_N, lake_dir=lake_dir)
    except Exception:
        bars = {}
    return {s: volume_read(bars.get(s)) for s in symbols}


# --------------------------------------------------------------- grading

def grade_one(sym: str, level: dict, score, forensic_score,
              flagged: bool, pin: dict, vol: dict) -> dict:
    """One symbol -> its tier row. First matching rule wins; the row
    records the rule so every grade is explainable from its own facts."""
    lv_ok = bool(level) and level.get("status") == "ok"
    close = level.get("close") if lv_ok else None
    zone = (level.get("buy_zone") if lv_ok else None) or [None, None]
    zone_lo, zone_hi = (zone + [None, None])[:2]
    stop = level.get("stop") if lv_ok else None
    atr = level.get("atr14") if lv_ok else None
    d50 = level.get("dma50") if lv_ok else None
    d200 = level.get("dma200") if lv_ok else None
    ext = level.get("extension") if lv_ok else None
    trail = level.get("trailing_floor") if lv_ok else None

    in_zone = (None not in (zone_lo, zone_hi, close)
               and zone_lo <= close <= zone_hi)
    near_zone = (None not in (zone_hi, close)
                 and zone_hi < close <= zone_hi * (1 + NEAR_ZONE_PCT / 100))
    below_zone = None not in (zone_lo, close) and close < zone_lo
    momentum = (None if None in (close, d50, d200)
                else close > d50 and d50 > d200)
    # Stop reference: the trailing pivot floor once the pricer confirms
    # one (a run-up name trails on pivots), else the hard zone-fail stop.
    ref_stop = trail if trail is not None else stop
    near_stop = (None if None in (close, ref_stop, atr) or not atr
                 else close <= ref_stop + NEAR_STOP_ATRS * atr)
    losing = (vol or {}).get("losing_volume")

    row = {"symbol": sym, "valuation": score, "forensic": forensic_score,
           "close": close, "buy_zone": zone if lv_ok else None,
           "stop": stop, "extension": ext, "in_zone": in_zone,
           "near_zone": near_zone, "momentum": momentum,
           "losing_volume": losing, "near_stop": near_stop,
           "pinned": (pin or {}).get("reason")}

    not_hot = ext != "overextended"
    if pin:
        tier = pin.get("grade") or "strong_sell"
        rule = f"pinned ({tier}): {pin.get('reason')}"
    elif not lv_ok or None in (close, stop, zone_lo, zone_hi):
        tier, rule = "ungraded", "no usable price levels"
    elif score is None:
        tier, rule = "ungraded", "no valuation score (vetoed/insufficient)"
    elif flagged:
        tier, rule = "watch", "forensic caution band — human call only"
    elif score >= STRONG_SELL_MIN_VAL:
        tier, rule = "strong_sell", f"valuation {score} >= {STRONG_SELL_MIN_VAL}"
    elif close < stop:
        tier, rule = "strong_sell", "close below the hard stop"
    elif below_zone:
        tier, rule = "weak_sell", "close broke below the buy zone"
    elif losing and score >= WEAK_HOLD_MIN_VAL:
        tier, rule = "weak_sell", ("losing volume (20d avg < 60d avg) + "
                                   f"valuation {score} >= {WEAK_HOLD_MIN_VAL}")
    elif in_zone and score <= STRONG_BUY_MAX_VAL and not_hot:
        tier, rule = "strong_buy", (f"in zone + valuation {score} <= "
                                    f"{STRONG_BUY_MAX_VAL} + clean forensics")
    elif (in_zone or near_zone) and score <= WEAK_BUY_MAX_VAL and not_hot:
        where = "in zone" if in_zone else "near zone (<=5% above ceiling)"
        tier, rule = "weak_buy", f"{where} + valuation {score} <= {WEAK_BUY_MAX_VAL}"
    elif score >= WEAK_HOLD_MIN_VAL:
        tier, rule = "weak_hold", (f"valuation {score} in "
                                   f"[{WEAK_HOLD_MIN_VAL}, {STRONG_SELL_MIN_VAL})"
                                   " — tighten trails")
    elif near_stop:
        tier, rule = "weak_hold", ("consolidating within "
                                   f"{NEAR_STOP_ATRS:g} ATR of the stop "
                                   "reference — tighten trails")
    elif momentum:
        tier, rule = "strong_hold", "momentum strong (close > 50DMA > 200DMA)"
    else:
        tier, rule = "weak_hold", "holding pattern — no buy/sell/momentum rule fired"

    row.update({"tier": tier, "family": FAMILY[tier], "rule": rule})
    return row


def build(queue_path=None, levels_path=None, valuation_path=None,
          pins: dict = None, turnover: dict = None,
          prev_tiers: dict = None, lake_dir=None) -> dict:
    """Grade the whole cohort (queue ∪ pinned holds) -> the tier table +
    family transitions vs the previous table."""
    queue = _load(queue_path or QUEUE_PATH, {})
    levels = {r["symbol"]: r for r in
              _load(levels_path or LEVELS_PATH, {}).get("levels") or []}
    scores = _load(valuation_path or VALUATION_PATH, {}).get("scores") or {}
    pins = pins if pins is not None else {}
    flagged = {o["symbol"] for o in queue.get("flagged") or []}
    forensic = {o["symbol"]: (o.get("forensic") or {}).get("score")
                for o in (queue.get("passed") or [])
                + (queue.get("flagged") or [])}

    cohort = sorted(set(queue.get("tickers") or []) | set(pins))
    if turnover is None:
        turnover = load_turnover(cohort, lake_dir=lake_dir)

    tiers = {t: [] for t in TIER_ORDER}
    for sym in cohort:
        row = grade_one(sym, levels.get(sym),
                        scores.get(sym, {}).get("score"),
                        forensic.get(sym), sym in flagged,
                        pins.get(sym), turnover.get(sym))
        tiers[row["tier"]].append(row)
    for t in tiers:
        tiers[t].sort(key=lambda r: (r["valuation"] is None,
                                     r["valuation"], r["symbol"]))

    prev = prev_tiers if prev_tiers is not None else _load(TIERS_PATH, {})
    return {"as_of": datetime.now(IST).replace(tzinfo=None)
                                      .isoformat(timespec="seconds"),
            "tiers": tiers,
            "counts": {t: len(tiers[t]) for t in TIER_ORDER},
            "transitions": transitions(tiers, prev),
            "first_grading": not (prev.get("tiers") or {}),
            "advisory_note": "ADVISORY-ONLY (Law #63), zero-capital: "
                             "tiers feed the PAPER_TELEMETRY shadow book "
                             "only. Grades are derived from recorded "
                             "facts — see each row's rule."}


def transitions(tiers: dict, prev: dict) -> list:
    """Family moves vs the previous table (the card policy's substrate).
    A symbol new to the table transitions from None; intrafamily moves
    are recorded with family_change=False and never fire a card."""
    if not (prev.get("tiers") or {}):
        return []                    # first grading — summary card instead
    prev_tier = {r["symbol"]: r["tier"]
                 for rows in prev["tiers"].values() for r in rows}
    out = []
    for t in TIER_ORDER:
        for r in tiers[t]:
            was = prev_tier.get(r["symbol"])
            if was == r["tier"]:
                continue
            out.append({"symbol": r["symbol"], "from_tier": was,
                        "to_tier": r["tier"],
                        "from_family": FAMILY.get(was),
                        "to_family": r["family"],
                        "family_change": FAMILY.get(was) != r["family"]})
    return out


# ----------------------------------------------------------------- pins

def clear_stale_pins(pins: dict, open_fn=None) -> tuple:
    """Drop every pin whose paper shadow has closed (the No-Orphan rule's
    exit door: pinned until the position closes, then gone completely).
    Returns (surviving_pins, cleared_symbols)."""
    if not pins:
        return {}, []
    if open_fn is None:
        open_fn = _open_darling_symbols
    try:
        held = open_fn()
    except Exception:
        return pins, []              # can't read the book -> keep pins
    survivors = {s: p for s, p in pins.items() if s in held}
    return survivors, sorted(set(pins) - set(survivors))


def _open_darling_symbols() -> set:
    """Base symbols of open darling-leg shadows in the telemetry ledger."""
    from src import knowledge_graph_logger as kg
    out = set()
    for ticker, entry in (kg.open_positions() or {}).items():
        setup = ((entry.get("kyu_trigger") or {}).get("setup")) or ""
        if setup.startswith("darling"):
            out.add(ticker.split(".")[0])
    return out


# ----------------------------------------------------------------- cards

def broadcast_tiers(result: dict, broadcast_fn=None) -> bool:
    """ONE card: the first-grading distribution summary, or the day's
    family transitions. Intrafamily moves never fire. Fail-open."""
    firsts = result.get("first_grading")
    fam_moves = [t for t in result.get("transitions") or []
                 if t["family_change"]]
    if not firsts and not fam_moves:
        return False
    try:
        if broadcast_fn is None:
            from src.notifier import fire_broadcast
            broadcast_fn = fire_broadcast
        if firsts:
            counts = result.get("counts") or {}
            dist = ", ".join(f"{t} {counts.get(t, 0)}" for t in TIER_ORDER
                             if counts.get(t))
            desc = ("🗂 Darling Tiers live — first grading.\n"
                    f"Distribution: {dist}.\n"
                    "Cards now fire on family transitions only "
                    "(buy/hold/sell/watch).")
        else:
            rows = {r["symbol"]: r for rows in result["tiers"].values()
                    for r in rows}
            lines = []
            for t in fam_moves:
                r = rows.get(t["symbol"], {})
                frm = t["from_family"] or "new"
                lines.append(f"{t['symbol']}: {frm} → {t['to_family']} "
                             f"({t['to_tier']} — {r.get('rule')})")
            desc = (f"🗂 Darling Tiers — {len(fam_moves)} family "
                    "transition(s):\n" + "\n".join(lines))
        broadcast_fn({"event": "darling_tiers", "ticker": "DARLINGS",
                      "date": result.get("as_of", ""),
                      "description": desc + "\nAdvisory only — grades "
                                            "move at next EOD."})
        return True
    except Exception as e:
        print(f"  (tier broadcast skipped: {e})")
        return False


# ------------------------------------------------------------------ run

def run(write: bool = True, broadcast: bool = True, broadcast_fn=None,
        open_fn=None, turnover: dict = None, **paths) -> dict:
    """The EOD grading pass: clear closed-out pins, grade the cohort,
    fire the (family-transition-only) card, persist the table."""
    pins_path = Path(paths.get("pins_path") or PINS_PATH)
    pins_file = _load(pins_path, {})
    pins, cleared = clear_stale_pins(pins_file.get("pins") or {}, open_fn)

    result = build(pins=pins, turnover=turnover,
                   **{k: v for k, v in paths.items()
                      if k in ("queue_path", "levels_path",
                               "valuation_path", "prev_tiers", "lake_dir")})
    result["pins"] = pins
    result["pins_cleared"] = cleared
    if broadcast:
        result["card_fired"] = broadcast_tiers(result, broadcast_fn)
    if write:
        out = Path(paths.get("tiers_path") or TIERS_PATH)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1))
        if cleared:
            pins_path.write_text(json.dumps(
                {"as_of": result["as_of"], "pins": pins}, indent=1))
    return result


if __name__ == "__main__":
    import sys

    dry = "--dry-run" in sys.argv
    res = run(write=not dry, broadcast=not dry)
    print(f"darling tiers as of {res['as_of']}"
          + (" (dry-run)" if dry else ""))
    for t in TIER_ORDER:
        rows = res["tiers"][t]
        if not rows:
            continue
        print(f"\n{t.upper()} ({len(rows)}):")
        for r in rows:
            print(f"  {r['symbol']:<12} val {str(r['valuation']):>4} "
                  f"close {str(r['close']):>9}  {r['rule']}")
    if res.get("pins_cleared"):
        print(f"\npins cleared (shadow closed): {res['pins_cleared']}")
    moves = [t for t in res["transitions"] if t["family_change"]]
    if moves:
        print(f"\nfamily transitions: "
              + ", ".join(f"{m['symbol']} {m['from_family']}→{m['to_family']}"
                          for m in moves))
