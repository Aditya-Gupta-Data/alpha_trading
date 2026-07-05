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


def buy(portfolio: dict, ticker: str, shares: int, price: float) -> None:
    cost = shares * price
    if shares <= 0 or cost > portfolio["cash"] + 1e-9:
        raise ValueError(f"can't buy {shares} x {ticker} at {price}: not enough cash")
    portfolio["cash"] = round(portfolio["cash"] - cost, 2)
    pos = portfolio["holdings"].get(ticker)
    if pos:
        total_shares = pos["shares"] + shares
        pos["avg_price"] = round(
            (pos["shares"] * pos["avg_price"] + cost) / total_shares, 2
        )
        pos["shares"] = total_shares
    else:
        portfolio["holdings"][ticker] = {"shares": shares, "avg_price": round(price, 2)}


def sell(portfolio: dict, ticker: str, price: float) -> int:
    """Sell the entire position. Returns the number of shares sold."""
    pos = portfolio["holdings"].get(ticker)
    if not pos:
        raise ValueError(f"can't sell {ticker}: not holding it")
    shares = pos["shares"]
    portfolio["cash"] = round(portfolio["cash"] + shares * price, 2)
    del portfolio["holdings"][ticker]
    return shares
