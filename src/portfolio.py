"""
Phase 3: the paper portfolio.

Fake money, real prices. The portfolio lives in data/portfolio.json and the
math here works exactly like a real account would — but there is deliberately
no broker connection anywhere in this project, so it is structurally
impossible for it to touch real money.

Safety rails:
  - can never spend more cash than the portfolio has
  - whole shares only
  - one position can never exceed MAX_POSITION_PCT of the whole portfolio
"""

import json
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PORTFOLIO_PATH = DATA_DIR / "portfolio.json"

STARTING_CASH = 100_000.0  # Rs. 1,00,000 fake starting capital (decision #12)
MAX_POSITION_PCT = 25      # no single stock may be >25% of the portfolio


def load() -> dict:
    """Load the portfolio, creating a fresh one on first run."""
    if not PORTFOLIO_PATH.exists():
        portfolio = {
            "cash": STARTING_CASH,
            "holdings": {},  # ticker -> {"shares": int, "avg_price": float}
            "created": date.today().isoformat(),
        }
        save(portfolio)
        return portfolio
    with open(PORTFOLIO_PATH, "r") as f:
        return json.load(f)


def save(portfolio: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(portfolio, f, indent=2)


def total_value(portfolio: dict, prices: dict) -> float:
    """Cash + market value of holdings. `prices` maps ticker -> latest price;
    holdings with no price available are valued at their buy price."""
    value = portfolio["cash"]
    for ticker, pos in portfolio["holdings"].items():
        price = prices.get(ticker, pos["avg_price"])
        value += pos["shares"] * price
    return value


def max_affordable_shares(portfolio: dict, price: float, prices: dict) -> int:
    """How many whole shares we may buy, respecting cash and the 25% cap."""
    if price <= 0:
        return 0
    cap = total_value(portfolio, prices) * MAX_POSITION_PCT / 100
    budget = min(portfolio["cash"], cap)
    return int(budget // price)


def _infer_instrument_type(ticker: str) -> str:
    """Infer the instrument type from the ticker symbol."""
    t_upper = ticker.upper()
    if t_upper.endswith(".NS") or t_upper.endswith(".BO"):
        return "STOCK"
    if "NIFTY" in t_upper or "BANK" in t_upper or t_upper.startswith("^"):
        if t_upper.endswith("CE") or t_upper.endswith("PE"):
            return "OPTION"
        return "INDEX"
    if t_upper.endswith("CE") or t_upper.endswith("PE"):
        return "OPTION"
    return "STOCK"


# --- 2026 fiscal friction stack (Phase 5) -------------------------------
# Rates as of April 2026. STT is levied EXCLUSIVELY on the sell side
# (options premium sales); Stamp Duty exclusively on the buy side. GST
# applies to the service charges (brokerage + exchange + SEBI), never to
# the taxes themselves.
STT_RATE_SELL = 0.0015          # 0.15%  on sell-side premium/turnover
STAMP_DUTY_RATE_BUY = 0.00003   # 0.003% on buy-side premium/turnover
BROKERAGE_FLAT = 20.0           # Rs.20 per executed leg (discount broker)
EXCHANGE_CHARGE_RATE = 0.0000345  # ~0.00345% NSE transaction charge
SEBI_FEE_RATE = 0.000001        # 0.0001% SEBI turnover fee
GST_RATE = 0.18                 # 18% on brokerage + exchange + SEBI

# SPAN simulation: what NSE would block for an UNHEDGED short option —
# roughly the premium plus a % of the strike notional. Deliberately heavy:
# the whole point of the offset math is that defined-risk spreads escape it.
NAKED_SHORT_MARGIN_PCT = 0.15


def calculate_trade_frictions(instrument_type: str, side: str, premium: float, quantity: int) -> float:
    """The 2026 cost of one executed leg: STT (sell side ONLY), Stamp Duty
    (buy side ONLY), flat brokerage, NSE exchange charges, SEBI turnover
    fees, and 18% GST on the service charges (brokerage+exchange+SEBI —
    GST is never charged on STT/stamp duty). Deducted from paper cash so
    the AI learns *net* profitability, not gross."""
    side_upper = side.upper()
    turnover = float(premium) * int(quantity)

    stt = STT_RATE_SELL * turnover if side_upper == "SELL" else 0.0
    stamp_duty = STAMP_DUTY_RATE_BUY * turnover if side_upper == "BUY" else 0.0

    brokerage = BROKERAGE_FLAT
    exchange_charges = EXCHANGE_CHARGE_RATE * turnover
    sebi_fees = SEBI_FEE_RATE * turnover
    gst = GST_RATE * (brokerage + exchange_charges + sebi_fees)

    total_friction = stt + stamp_duty + brokerage + exchange_charges + sebi_fees + gst
    return round(total_friction, 2)


def calculate_span_margin(legs: list, lot_size: int) -> dict:
    """Simulate NSE SPAN margin for a basket of option legs, WITH the
    hedge offsets a real clearing house grants — so a defined-risk spread
    blocks only its net risk, not the massive naked-short requirement.

    Each leg: {"side": "BUY"|"SELL", "option_type": "CE"|"PE",
               "strike": float, "premium": float}.

    Model (paper-trading approximation, not the real SPAN engine):
      * long legs block no margin — their premium is paid as cost;
      * each SHORT leg is paired with the nearest same-type LONG leg
        (a vertical hedge). The pair blocks its worst-case loss:
        (strike width - net credit) x lot, floored at 0 for debit pairs
        (max loss there is the debit already paid);
      * an UNPAIRED short is naked: premium x lot plus
        NAKED_SHORT_MARGIN_PCT of strike notional. Strategy code must
        never produce one (decision #27) but the math stays honest.

    Returns {"total_margin", "naked_margin", "offset_savings"} — the
    savings line is what makes hedged structures visibly affordable."""
    lot = int(lot_size)
    shorts = [dict(l) for l in legs if l["side"].upper() == "SELL"]
    longs = [dict(l) for l in legs if l["side"].upper() == "BUY"]

    # What the same shorts would block with no hedges recognized at all.
    naked_margin = sum(
        s["premium"] * lot + NAKED_SHORT_MARGIN_PCT * s["strike"] * lot
        for s in shorts
    )

    total = 0.0
    available_longs = list(longs)
    for s in shorts:
        same_type = [l for l in available_longs
                     if l["option_type"] == s["option_type"]]
        if same_type:
            hedge = min(same_type, key=lambda l: abs(l["strike"] - s["strike"]))
            available_longs.remove(hedge)
            width = abs(hedge["strike"] - s["strike"])
            net_credit = s["premium"] - hedge["premium"]
            total += max(0.0, (width - net_credit) * lot)
        else:
            total += s["premium"] * lot + NAKED_SHORT_MARGIN_PCT * s["strike"] * lot

    return {
        "total_margin": round(total, 2),
        "naked_margin": round(naked_margin, 2),
        "offset_savings": round(naked_margin - total, 2),
    }


def buy(portfolio: dict, ticker: str, shares: int, price: float, instrument_type: str | None = None) -> None:
    if instrument_type is None:
        instrument_type = _infer_instrument_type(ticker)
    cost = shares * price
    frictions = calculate_trade_frictions(instrument_type, "BUY", price, shares)
    total_cost = cost + frictions
    if shares <= 0 or total_cost > portfolio["cash"] + 1e-9:
        raise ValueError(
            f"can't buy {shares} x {ticker} at {price}: not enough cash "
            f"(cost Rs.{cost:.2f} + frictions Rs.{frictions:.2f})"
        )
    portfolio["cash"] = round(portfolio["cash"] - total_cost, 2)
    pos = portfolio["holdings"].get(ticker)
    if pos:
        total_shares = pos["shares"] + shares
        pos["avg_price"] = round(
            (pos["shares"] * pos["avg_price"] + cost) / total_shares, 2
        )
        pos["shares"] = total_shares
    else:
        portfolio["holdings"][ticker] = {"shares": shares, "avg_price": round(price, 2)}


def sell(portfolio: dict, ticker: str, price: float, instrument_type: str | None = None) -> int:
    """Sell the entire position. Returns the number of shares sold."""
    if instrument_type is None:
        instrument_type = _infer_instrument_type(ticker)
    pos = portfolio["holdings"].get(ticker)
    if not pos:
        raise ValueError(f"can't sell {ticker}: not holding it")
    shares = pos["shares"]
    frictions = calculate_trade_frictions(instrument_type, "SELL", price, shares)
    portfolio["cash"] = round(portfolio["cash"] + shares * price - frictions, 2)
    del portfolio["holdings"][ticker]
    return shares
