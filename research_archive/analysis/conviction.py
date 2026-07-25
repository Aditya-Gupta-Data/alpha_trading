"""
src/analysis/conviction.py — Multi-Factor Conviction Engine
===========================================================

A normalized Conviction Score in [0.0, 1.0] for a ticker on a date — the
continuous confidence metric that DRIVES dynamic position sizing (replacing
rigid risk tiers). Weighted blend of the edges we actually have data for:

  Smart-Money Footprint  (40%): net institutional (bulk/block) flow over a
      rolling window from the 12.5-yr deals ledger. Heavy accumulation -> 1.0,
      balanced/none -> 0.5, active distribution -> 0.0. (net/gross normalisation)
  Fundamental Quality    (40%): as-of ROE + Debt/Eq + CFO/PAT (cash-is-king).
      Pristine -> 1.0; a cash-negative value-trap is crushed toward 0.0.
  Macro/Sector Tailwind  (20%): is the stock a Top-3 leader in a sector that is
      itself outperforming NIFTY? both -> 1.0, one -> 0.5, neither -> 0.0.

STRICT point-in-time (all inputs must be as-of<=date). NULL-honest: a missing
factor scores a NEUTRAL 0.5 (never a fabricated edge). Pure factor functions
(numeric in -> score out) so the sim can feed precomputed inputs; `compute()`
orchestrates from the live data sources. Read-only; NOT wired into the engine.
"""
from src.analysis import smart_money_trend as SM

WEIGHTS = {"smart_money": 0.40, "fundamental": 0.40, "sector": 0.20}
CONVICTION_VETO = 0.40          # below this -> no trade (save cash)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def smart_money_factor(deals_for_ticker: list, as_of: str, window: int = 60) -> float:
    """net/gross of institutional flow -> [0,1]; 0.5 when there's no data."""
    niv = SM.net_institutional_volume(deals_for_ticker, as_of, window)
    buy, sell = niv["buy_value_rs"], niv["sell_value_rs"]
    gross = buy + sell
    if niv["n_deals"] == 0 or gross <= 0:
        return 0.5
    return _clamp(((buy - sell) / gross + 1) / 2)


def fundamental_factor(roe, debteq, cfo, pat) -> float:
    """ROE + low-leverage + cash-quality -> [0,1]; 0.5 when no fundamentals."""
    if roe is None:
        return 0.5
    roe_s = _clamp(roe / 0.20)                         # 20% ROE = full marks
    de_s = _clamp(1 - debteq / 1.0) if debteq is not None else 0.5
    if cfo is not None and pat is not None and pat > 0:
        cfo_s = _clamp((cfo / pat) / 1.0)              # CFO >= PAT = full marks
    elif cfo is not None and cfo > 0:
        cfo_s = 0.7
    else:
        cfo_s = 0.0                                    # no/negative cash
    score = (roe_s + de_s + cfo_s) / 3
    if cfo is not None and cfo <= 0:                   # cash-negative value trap
        score *= 0.25
    return _clamp(score)


def sector_factor(is_top3: bool, sector_outperforms_nifty: bool) -> float:
    return 0.5 * (1.0 if is_top3 else 0.0) + 0.5 * (1.0 if sector_outperforms_nifty else 0.0)


def conviction_score(sm: float, fund: float, sector: float) -> float:
    return _clamp(WEIGHTS["smart_money"] * sm
                  + WEIGHTS["fundamental"] * fund
                  + WEIGHTS["sector"] * sector)


def score_from_inputs(deals_for_ticker, as_of, fund_tuple, is_top3,
                      sector_outperforms) -> dict:
    """The seam the sim calls with precomputed inputs. Returns the score +
    the per-factor breakdown (for inspection / sizing)."""
    sm = smart_money_factor(deals_for_ticker, as_of)
    fu = fundamental_factor(*fund_tuple)
    se = sector_factor(is_top3, sector_outperforms)
    return {"conviction": round(conviction_score(sm, fu, se), 3),
            "smart_money": round(sm, 3), "fundamental": round(fu, 3),
            "sector": round(se, 3),
            "veto": conviction_score(sm, fu, se) < CONVICTION_VETO}


if __name__ == "__main__":
    # demo with hand values
    for sm, fu, se in [(1.0, 1.0, 1.0), (0.5, 0.5, 0.0), (0.0, 0.8, 0.5), (0.9, 0.9, 1.0)]:
        c = conviction_score(sm, fu, se)
        print(f"  sm={sm} fund={fu} sector={se} -> conviction {c:.3f} "
              f"-> risk {5.0*c:.2f}%  {'VETO' if c < CONVICTION_VETO else ''}")
