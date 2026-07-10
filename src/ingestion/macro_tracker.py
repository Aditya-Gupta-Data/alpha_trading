"""
src/ingestion/macro_tracker.py — multi-horizon macro directional matrix
=======================================================================

Phase 7 scratchpad build. Tracks the four macro variables that move Indian
equity indexes from the outside — Crude Oil, Gold (India/MCX vs World/spot),
and USDINR — and folds each one into a three-horizon directional matrix:

    SHORT   1–5 trading days      (5-bar trend)
    MEDIUM  weeks                 (21-bar trend)
    LONG    structural cycles     (126-bar trend)

Data path (Option A + Option B fallback, in that order per metric):

  A. Dhan live bars, through dhan_guard's hardened plumbing. MCX/currency
     security IDs are NOT hardcoded here: the project rule (see
     dhan_client.py's header) is that a security id must be verified
     against the scrip master, never guessed — a wrong id silently prices
     the wrong instrument. Verified ids go in `config/macro_securities.json`
     ({"CRUDE": {"id": "...", "seg": "MCX_COMM", "inst": "FUTCOM"}, ...});
     a metric without an entry simply skips the live path.
  B. `data/macro_snapshot.json` — a hand-maintainable local snapshot. Any
     metric (or any single horizon) the live path can't fill falls open to
     it; if that's missing too, the horizon is "unknown" (never a guess).

The matrix ends with `index_impact`: each metric's directional permutation
is mapped onto NIFTY 50 / NIFTY BANK bias per horizon via the
INDEX_IMPACT_WEIGHTS system parameters (rising crude / rising USDINR are
imported-inflation headwinds; gold strength is a risk-off proxy; banks are
the most FX-sensitive book). So "crude falling short-term, rising medium,
structurally bullish long" becomes three signed index-bias numbers the
resonance engine can weigh against open positions.

Fail-open by design: no credentials, no snapshot, an offline box — every
path degrades to "unknown" directions and zero impact. Nothing here ever
raises to a caller, places trades, or writes any file.

Manual check:  python3 -m src.ingestion.macro_tracker
"""

import json
from datetime import date, timedelta
from pathlib import Path

from src import dhan_client as dc
from src.dhan_guard import DhanApiError, SafeDhanClient, unwrap_payload

ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT_PATH = ROOT / "data" / "macro_snapshot.json"
SECURITIES_PATH = ROOT / "config" / "macro_securities.json"

# ------------------------------------------------------------- vocabulary

MACRO_METRICS = ("CRUDE", "GOLD_INDIA", "GOLD_WORLD", "USDINR")

HORIZONS = ("SHORT", "MEDIUM", "LONG")

# Trading-day lookback per horizon, and the flat band (± total % move over
# the window) below which a drift is noise, not a trend.
TREND_WINDOWS = {"SHORT": 5, "MEDIUM": 21, "LONG": 126}
FLAT_BANDS_PCT = {"SHORT": 0.75, "MEDIUM": 2.0, "LONG": 5.0}

DIRECTIONS = ("rising", "falling", "flat", "unknown")

# The snapshot file is hand-edited, so accept the words a human (or a
# headline) would actually use and coerce them to the strict vocabulary.
_DIRECTION_SYNONYMS = {
    "rising": "rising", "up": "rising", "gaining": "rising",
    "bullish": "rising", "spiking": "rising", "rallying": "rising",
    "strengthening": "rising", "appreciating": "rising",
    "falling": "falling", "down": "falling", "crashing": "falling",
    "bearish": "falling", "declining": "falling", "dropping": "falling",
    "weakening": "falling", "depreciating": "falling",
    "flat": "flat", "sideways": "flat", "neutral": "flat",
    "stable": "flat", "rangebound": "flat", "range_bound": "flat",
    "unchanged": "flat",
}

# Snapshot files may spell horizons the human way; normalize on load.
_HORIZON_KEYS = {
    "SHORT": ("SHORT", "short", "short_term", "st"),
    "MEDIUM": ("MEDIUM", "medium", "medium_term", "mid", "mt"),
    "LONG": ("LONG", "long", "long_term", "structural", "lt"),
}

# ------------------------------------------- index-impact system parameters

# Signed weight of a RISING metric on each index (falling flips the sign,
# flat/unknown contributes zero). Magnitudes are deliberate system
# parameters, not fitted values:
#   CRUDE   -0.40 / -0.30  India imports ~85% of its oil; rising crude is
#                          an inflation + current-account headwind, felt a
#                          touch less by pure financials.
#   USDINR  -0.50 / -0.60  Rupee depreciation = FPI outflows + imported
#                          inflation; banks carry the most FX beta.
#   GOLD_WORLD -0.20       Global risk-off proxy.
#   GOLD_INDIA -0.10       Mostly a currency echo of world gold; the extra
#                          information lives in the divergence flag below.
INDEX_IMPACT_WEIGHTS = {
    "CRUDE":      {"NIFTY 50": -0.40, "NIFTY BANK": -0.30},
    "USDINR":     {"NIFTY 50": -0.50, "NIFTY BANK": -0.60},
    "GOLD_WORLD": {"NIFTY 50": -0.20, "NIFTY BANK": -0.20},
    "GOLD_INDIA": {"NIFTY 50": -0.10, "NIFTY BANK": -0.10},
}

IMPACT_INDEXES = ("NIFTY 50", "NIFTY BANK")

_DIRECTION_CODE = {"rising": 1.0, "falling": -1.0, "flat": 0.0, "unknown": 0.0}


# ------------------------------------------------------------- coercion

def coerce_direction(value) -> str:
    """Any human/LLM spelling of a trend -> the strict vocabulary, with
    "unknown" for anything unrecognizable. Never raises, never guesses."""
    if value is None:
        return "unknown"
    word = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return _DIRECTION_SYNONYMS.get(word, "unknown")


def classify_trend(closes: list, window: int, flat_band_pct: float) -> str:
    """Directional read over the last `window` bars of `closes` (oldest
    first): total % move beyond ±flat_band_pct is rising/falling, inside
    the band is flat, not enough history is unknown."""
    if not isinstance(closes, list) or len(closes) < window + 1:
        return "unknown"
    try:
        start = float(closes[-(window + 1)])
        end = float(closes[-1])
    except (TypeError, ValueError):
        return "unknown"
    if start == 0:
        return "unknown"
    pct = (end - start) / start * 100.0
    if pct > flat_band_pct:
        return "rising"
    if pct < -flat_band_pct:
        return "falling"
    return "flat"


# ------------------------------------------------------------- file loads

def _load_securities(path=None) -> dict:
    """Verified Dhan instrument overrides for the macro metrics, or {} —
    a missing/broken file just means the live path is skipped (Option B
    takes over). Only entries with all of id/seg/inst are usable."""
    path = Path(path) if path is not None else SECURITIES_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (macro tracker: unreadable securities file {path} — "
              "skipping the Dhan live path)")
        return {}
    out = {}
    for name in MACRO_METRICS:
        entry = raw.get(name) if isinstance(raw, dict) else None
        if (isinstance(entry, dict)
                and all(entry.get(k) for k in ("id", "seg", "inst"))):
            out[name] = {"id": str(entry["id"]), "seg": str(entry["seg"]),
                         "inst": str(entry["inst"])}
    return out


def _load_snapshot(path=None) -> dict:
    """data/macro_snapshot.json -> {"as_of": str|None, "metrics": {name:
    {"level": float|None, "horizons": {SHORT/MEDIUM/LONG: direction}}}}.
    Horizon keys and direction words are normalized on the way in; a
    missing/broken file degrades to empty metrics. Never raises."""
    path = Path(path) if path is not None else SNAPSHOT_PATH
    empty = {"as_of": None, "metrics": {}}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (macro tracker: unreadable snapshot {path} — "
              "treating as absent)")
        return empty
    if not isinstance(raw, dict):
        return empty
    metrics = {}
    for name in MACRO_METRICS:
        entry = (raw.get("metrics") or {}).get(name)
        if not isinstance(entry, dict):
            continue
        horizons = {}
        for horizon, spellings in _HORIZON_KEYS.items():
            value = next((entry[k] for k in spellings if k in entry), None)
            horizons[horizon] = coerce_direction(value)
        level = entry.get("level")
        try:
            level = float(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        metrics[name] = {"level": level, "horizons": horizons}
    as_of = raw.get("as_of")
    return {"as_of": str(as_of) if as_of else None, "metrics": metrics}


# ------------------------------------------------------------- Dhan path

# Enough calendar padding to cover the LONG window in trading days.
_FETCH_CALENDAR_DAYS = TREND_WINDOWS["LONG"] * 2 + 40


def _closes_via_dhan(name: str, instr: dict, safe: SafeDhanClient = None,
                     today: date = None) -> list | None:
    """Daily closes (oldest first) for one macro instrument via the
    hardened SafeDhanClient plumbing — same single-retry, classified-error,
    audited path every other Dhan call in the project takes. Returns None
    on ANY failure (no creds, auth-dead token, unauthorized segment, bad
    shape) so the caller falls open to the snapshot. Never raises: the
    guard is used in non-strict mode."""
    safe = safe or SafeDhanClient()
    client = dc._get_client()
    if client is None:
        # No credentials at all — the offline / fail-open case.
        safe._fail(f"macro:{name}",
                   DhanApiError("NO_CREDENTIALS",
                                "DHAN_CLIENT_ID / access token missing"),
                   None)
        return None
    today = today or date.today()
    from_date = (today - timedelta(days=_FETCH_CALENDAR_DAYS)).isoformat()
    resp, err = safe._call("historical_daily_data",
                           client.historical_daily_data,
                           instr["id"], instr["seg"], instr["inst"],
                           from_date, today.isoformat())
    if err is not None:
        safe._fail(f"macro:{name}", err, None)
        return None
    payload = unwrap_payload(resp, inner_marker="timestamp")
    if not isinstance(payload, dict) or not payload.get("close"):
        safe._fail(f"macro:{name}",
                   DhanApiError("BAD_SHAPE", "no close arrays in payload",
                                raw=str(resp)), None)
        return None
    try:
        closes = [float(c) for c in payload["close"]]
    except (TypeError, ValueError):
        safe._fail(f"macro:{name}",
                   DhanApiError("BAD_SHAPE", "non-numeric closes",
                                raw=str(resp)), None)
        return None
    return closes or None


# ------------------------------------------------------------- the matrix

def _index_impact(metrics: dict) -> dict:
    """The permutation mapping: every metric's per-horizon direction times
    its INDEX_IMPACT_WEIGHTS entry, summed per index per horizon and
    clamped to [-1, 1]. Unknown/flat directions contribute nothing."""
    impact = {}
    for index in IMPACT_INDEXES:
        impact[index] = {}
        for horizon in HORIZONS:
            total = 0.0
            for name, m in metrics.items():
                weight = INDEX_IMPACT_WEIGHTS.get(name, {}).get(index)
                if weight is None:
                    continue
                total += weight * _DIRECTION_CODE[m["horizons"][horizon]]
            impact[index][horizon] = round(max(-1.0, min(1.0, total)), 3)
    return impact


def _gold_divergence(metrics: dict) -> bool:
    """True when India and World gold disagree on the SHORT horizon with
    both actually known — the classic sign the move is currency (USDINR),
    not bullion, which is exactly when the FX weight deserves attention."""
    india = (metrics.get("GOLD_INDIA") or {}).get("horizons", {}).get("SHORT")
    world = (metrics.get("GOLD_WORLD") or {}).get("horizons", {}).get("SHORT")
    return (india not in (None, "unknown") and world not in (None, "unknown")
            and india != world)


def build_macro_matrix(snapshot_path=None, securities_path=None,
                       safe: SafeDhanClient = None, today: date = None) -> dict:
    """The Phase 7 entry point: one unified directional matrix.

        {"as_of": "YYYY-MM-DD",
         "source": "dhan" | "snapshot" | "mixed" | "none",
         "metrics": {name: {"level": float|None,
                            "horizons": {"SHORT"/"MEDIUM"/"LONG": direction},
                            "source": "dhan"|"mixed"|"snapshot"|"none"}},
         "index_impact": {"NIFTY 50": {horizon: bias}, "NIFTY BANK": {...}},
         "gold_divergence": bool}

    Per metric, per horizon: computed from live Dhan closes when the
    instrument is mapped AND the fetch succeeded AND there's enough
    history for that window; otherwise the snapshot's word; otherwise
    "unknown". Pure function of its inputs plus those two files — no
    writes, no raises."""
    today = today or date.today()
    securities = _load_securities(securities_path)
    snapshot = _load_snapshot(snapshot_path)
    safe = safe or SafeDhanClient()

    metrics = {}
    for name in MACRO_METRICS:
        closes = None
        if name in securities:
            closes = _closes_via_dhan(name, securities[name], safe=safe,
                                      today=today)
        snap = snapshot["metrics"].get(name) or {}
        snap_horizons = snap.get("horizons") or {}
        horizons, live_count = {}, 0
        for horizon in HORIZONS:
            window = TREND_WINDOWS[horizon]
            direction = "unknown"
            if closes is not None and len(closes) >= window + 1:
                direction = classify_trend(closes, window,
                                           FLAT_BANDS_PCT[horizon])
                live_count += 1
            if direction == "unknown":
                direction = snap_horizons.get(horizon, "unknown")
            horizons[horizon] = direction
        if closes:
            level = closes[-1]
            source = "dhan" if live_count == len(HORIZONS) else "mixed"
        elif snap:
            level = snap.get("level")
            source = ("snapshot"
                      if any(d != "unknown" for d in horizons.values())
                      else "none")
        else:
            level, source = None, "none"
        metrics[name] = {"level": level, "horizons": horizons,
                         "source": source}

    sources = {m["source"] for m in metrics.values()}
    if sources <= {"none"}:
        rollup = "none"
    elif sources <= {"dhan"}:
        rollup = "dhan"
    elif sources <= {"snapshot", "none"}:
        rollup = "snapshot"
    else:
        rollup = "mixed"

    return {
        "as_of": today.isoformat(),
        "source": rollup,
        "metrics": metrics,
        "index_impact": _index_impact(metrics),
        "gold_divergence": _gold_divergence(metrics),
    }


if __name__ == "__main__":
    # Manual smoke test: python3 -m src.ingestion.macro_tracker
    print(json.dumps(build_macro_matrix(), indent=2))
