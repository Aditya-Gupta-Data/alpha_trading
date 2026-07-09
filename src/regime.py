"""
Alpha Trading — Regime-Aware Memory: the market-condition vocabulary
=====================================================================

Roadmap item #4. Every trade the learning stack remembers now carries a
REGIME TAG — what the market *was* when the trade was conceived — so
every learning surface can ask the sharper question: not "how do iron
condors do?" but "how do iron condors do in conditions like today's?"

The regime is two orthogonal axes, both computable at proposal time AND
retroactively from stored data (which is what makes historical backfill
possible):

  trend     the proposer's own market_view read — bullish / bearish /
            neutral (the exact three-way view the pipeline ACTS on; no
            second trend definition to drift out of sync)
  vix_band  low (<13) / mid (13–16) / high (>16) / unknown — the same
            boundaries the trade planner's IV matrix and the evolution
            miner already use (this module is now their single source).

Where the tags live:
  * journal entries      entry["regime"] = {"trend", "vix_band", "vix"}
                         (attached at creation; additive key — old
                         entries simply lack it, readers tolerate)
  * outcomes table       regime_trend / regime_vix columns (in-place
                         ALTER on connect, like post_mortem before it)
  * simulated_trades     regime_trend / regime_vix columns (idempotent
                         ALTER in the simulator's ensure_schema)

Consumers:
  * brain_map.query_similar_events(tags, regime=...) — memory stats
    filtered to matching conditions, with the in-regime vs overall
    contrast surfaced
  * the skeptic's feature vector — two new regime features (this is a
    FEATURE_NAMES contract change = mandatory retrain, decision #44;
    a shipped model has never existed, so the moment is clean)
  * backfill CLI: python3 -m src.regime backfill --db <path>
    (recomputes trend as-of each historical trade's proposal date from
    the bars cache — same as-of discipline as the Phase 7 simulator)

Pure module: stdlib only, no market data, no network.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The single source of VIX-band truth (evolution.py re-exports this;
# trade_planner's IV matrix uses the same 13/16 boundaries).
VIX_BANDS = ((0, 13, "low"), (13, 16, "mid"), (16, 999, "high"))

TRENDS = ("bullish", "neutral", "bearish")

# Numeric encodings for the skeptic's feature vector. Trend is signed
# (bullish +1 / neutral 0 / bearish -1, unknown 0); the band is ordinal
# with unknown = -1 so "no VIX reading" is distinguishable from "calm".
_TREND_CODE = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
_VIX_CODE = {"low": 0.0, "mid": 1.0, "high": 2.0}


def vix_band(vix) -> str:
    if vix is None:
        return "unknown"
    try:
        v = float(vix)
    except (TypeError, ValueError):
        return "unknown"
    for lo, hi, name in VIX_BANDS:
        if lo <= v < hi:
            return name
    return "unknown"


def regime_for(view, vix) -> dict:
    """The regime dict a journal entry carries, from exactly what the
    proposal pipeline already knows at creation time."""
    trend = view if view in TRENDS else "unknown"
    return {"trend": trend, "vix_band": vix_band(vix),
            "vix": round(float(vix), 2) if isinstance(vix, (int, float)) else None}


def regime_tag(regime: dict) -> str:
    """Compact display/query form, e.g. 'bearish+mid_iv'."""
    if not regime:
        return "unknown+unknown_iv"
    return f"{regime.get('trend', 'unknown')}+{regime.get('vix_band', 'unknown')}_iv"


def encode_for_model(trend, band) -> tuple:
    """(trend_code, vix_band_code) floats for the skeptic's frozen
    feature vector. Unknown trend -> 0.0, unknown band -> -1.0."""
    return (_TREND_CODE.get(trend, 0.0), _VIX_CODE.get(band, -1.0))


# --- historical backfill ------------------------------------------------------

def trend_as_of(closes: list) -> str:
    """The proposer's own market_view over an as-of closes slice — the
    same trend the pipeline would have read that day. 'unknown' when the
    history is too thin for the 200-day SMA."""
    from src.options_proposer import market_view
    from src.simulator import analysis_from_closes
    analysis = analysis_from_closes("backfill", closes)
    return market_view(analysis) if analysis else "unknown"


def backfill_simulated_trades(conn, bars_by_underlying: dict) -> dict:
    """Fill regime_trend/regime_vix on every simulated_trades row that
    lacks them. vix_band comes from the row's own stored vix; trend is
    recomputed AS-OF the proposal date from the supplied bars (never a
    byte of future data — the simulator's own discipline). Idempotent:
    rows already tagged are skipped; rows whose date predates the bars
    coverage get trend='unknown' rather than a guess."""
    from src.simulator import ensure_schema
    ensure_schema(conn)
    _ensure_sim_regime_columns(conn)
    rows = conn.execute(
        "SELECT journal_ref, underlying, proposed_on, vix FROM "
        "simulated_trades WHERE regime_trend IS NULL").fetchall()
    stats = {"examined": len(rows), "tagged": 0, "trend_unknown": 0}
    closes_cache = {u: [(b[0], float(b[3])) for b in bars]
                    for u, bars in (bars_by_underlying or {}).items()}
    for ref, underlying, day, vix in [(r[0], r[1], r[2], r[3]) for r in rows]:
        series = closes_cache.get(underlying, [])
        closes = [c for d, c in series if d <= day]
        trend = trend_as_of(closes) if closes else "unknown"
        if trend == "unknown":
            stats["trend_unknown"] += 1
        conn.execute(
            "UPDATE simulated_trades SET regime_trend = ?, regime_vix = ? "
            "WHERE journal_ref = ?", (trend, vix_band(vix), ref))
        stats["tagged"] += 1
    conn.commit()
    return stats


def _ensure_sim_regime_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(simulated_trades)")}
    for col in ("regime_trend", "regime_vix"):
        if col not in cols:
            conn.execute(f"ALTER TABLE simulated_trades ADD COLUMN {col} TEXT")
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Regime-Aware Memory: backfill regime tags onto "
                    "historical simulated trades")
    parser.add_argument("command", choices=["backfill"])
    parser.add_argument("--db", default=None,
                        help="brain_map DB path (default: data/brain_map.db)")
    parser.add_argument("--bars-cache", default=str(ROOT / "data" / "bars_cache.json"))
    args = parser.parse_args()

    from src import brain_map
    cache = json.loads(Path(args.bars_cache).read_text())
    bars = {u: [tuple(b) for b in blist] for u, blist in cache["bars"].items()}
    connection = brain_map.connect(args.db)
    summary = backfill_simulated_trades(connection, bars)
    connection.close()
    print(json.dumps(summary))
