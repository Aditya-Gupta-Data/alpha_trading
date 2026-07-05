"""
Watchlist storage — read/add/remove items in config/watchlist.yaml.

This is part of the WEB layer. It imports the existing engine (data_fetcher) to
validate that a symbol returns a real price before saving, but it does NOT change
the engine's behaviour. Writes use ruamel.yaml in round-trip mode so the comments
and formatting already in watchlist.yaml are preserved.

Data model
----------
The watchlist is a flat list under `watchlist:`. Each entry has:
  - ticker      : the yfinance symbol (e.g. RELIANCE.NS, ^NSEI)
  - type        : "stock" or "index"   (optional; inferred if missing)
  - condition   : alert rule type       (optional — an entry can be watch-only)
  - value       : the rule's number     (optional, paired with condition)

An entry can therefore be "watch-only" (no condition/value): it just shows a live
price on the dashboard and never fires an alert. Stocks added before this feature
have no `type` and are treated as stocks.
"""

from pathlib import Path

from ruamel.yaml import YAML

from src.data_fetcher import get_quote

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "watchlist.yaml"

# Friendly name -> yfinance symbol for the common Indian indices.
# The user picks/types a clean name; we store and fetch the raw ^ ticker.
KNOWN_INDICES = {
    "NIFTY 50": "^NSEI",
    "NIFTY": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY BANK": "^NSEBANK",
    "SENSEX": "^BSESN",
    "NIFTY IT": "^CNXIT",
    "NIFTY MIDCAP 50": "^NSEMDCP50",
    "INDIA VIX": "^INDIAVIX",
}

# Reverse map (symbol -> first friendly name) for display.
_INDEX_DISPLAY = {}
for _name, _sym in KNOWN_INDICES.items():
    _INDEX_DISPLAY.setdefault(_sym, _name)

_yaml = YAML()
_yaml.preserve_quotes = True
# Match the existing watchlist.yaml style: list items indented under the key
# with the dash at column 2 (e.g. "  - ticker: ...").
_yaml.indent(mapping=2, sequence=4, offset=2)


# ── helpers ──────────────────────────────────────────────────────────────────

def _read_doc():
    """Load the YAML keeping comments/formatting (round-trip)."""
    if not CONFIG_PATH.exists():
        return {"watchlist": []}
    with open(CONFIG_PATH) as f:
        doc = _yaml.load(f)
    if doc is None:
        doc = {"watchlist": []}
    if "watchlist" not in doc or doc["watchlist"] is None:
        doc["watchlist"] = []
    return doc


def _write_doc(doc) -> None:
    with open(CONFIG_PATH, "w") as f:
        _yaml.dump(doc, f)


def infer_type(ticker: str) -> str:
    return "index" if ticker.startswith("^") else "stock"


def display_name(ticker: str, type_: str) -> str:
    if type_ == "index":
        return _INDEX_DISPLAY.get(ticker, ticker)
    return ticker.replace(".NS", "").replace(".BO", "")


def exchange_of(ticker: str, type_: str) -> str:
    if type_ == "index":
        return "Index"
    if ticker.endswith(".NS"):
        return "NSE"
    if ticker.endswith(".BO"):
        return "BSE"
    return ""


def load_items() -> list:
    """Return the raw watchlist entries as plain dicts."""
    return list(_read_doc()["watchlist"])


def resolve_ticker(symbol: str, type_: str):
    """
    Turn what the user typed into candidate yfinance tickers to try, in order.

    - index: map the friendly name; or accept a raw ^SYMBOL.
    - stock: accept an explicit .NS/.BO; otherwise try NSE (.NS) then BSE (.BO).
    Returns a list of candidate tickers (may be empty for an unknown index).
    """
    s = symbol.strip()
    if type_ == "index":
        key = s.upper()
        if key in KNOWN_INDICES:
            return [KNOWN_INDICES[key]]
        if s.startswith("^"):
            return [s]
        return []  # unknown index name
    # stock
    up = s.upper()
    if up.startswith("^"):
        return [up]
    if up.endswith(".NS") or up.endswith(".BO"):
        return [up]
    return [f"{up}.NS", f"{up}.BO"]


def add_item(symbol: str, type_: str) -> dict:
    """
    Validate the symbol against yfinance, then append it to watchlist.yaml as a
    watch-only entry (no alert rule). Returns a result dict:
        {"ok": True,  "ticker": ..., "type": ..., "name": ..., "price": ...}
        {"ok": False, "error": "..."}  on any failure (nothing is written).
    """
    if not symbol or not symbol.strip():
        return {"ok": False, "error": "Please enter a symbol or index name."}
    if type_ not in ("stock", "index"):
        return {"ok": False, "error": "type must be 'stock' or 'index'."}

    candidates = resolve_ticker(symbol, type_)
    if not candidates:
        known = ", ".join(sorted({n.title() for n in KNOWN_INDICES}))
        return {"ok": False, "error": f"Unknown index. Try one of: {known}, or a raw ^ symbol."}

    quote = None
    chosen = None
    for cand in candidates:
        q = get_quote(cand)
        if q is not None:
            quote, chosen = q, cand
            break
    if quote is None:
        return {"ok": False, "error": f"Could not fetch a price for '{symbol}'. Check the spelling."}

    doc = _read_doc()
    existing = {str(it.get("ticker")) for it in doc["watchlist"]}
    if chosen in existing:
        return {"ok": False, "error": f"{display_name(chosen, type_)} is already in your watchlist."}

    entry = {"ticker": chosen, "type": type_}
    doc["watchlist"].append(entry)
    _write_doc(doc)

    return {
        "ok": True,
        "ticker": chosen,
        "type": type_,
        "name": display_name(chosen, type_),
        "price": quote["current_price"],
        "percent_change": quote["percent_change"],
    }


def remove_item(ticker: str) -> dict:
    """Remove ALL entries (the instrument and any of its rules) for `ticker`."""
    doc = _read_doc()
    before = len(doc["watchlist"])
    doc["watchlist"] = [it for it in doc["watchlist"] if str(it.get("ticker")) != ticker]
    removed = before - len(doc["watchlist"])
    if removed == 0:
        return {"ok": False, "error": f"'{ticker}' is not in the watchlist."}
    _write_doc(doc)
    return {"ok": True, "ticker": ticker, "removed": removed}
