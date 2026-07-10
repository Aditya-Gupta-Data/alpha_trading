"""
src/ingestion/deals_tracker.py — end-of-day bulk & block deals footprint
========================================================================

Phase-8 advisory ingestion (scratchpad build). Every trading day SEBI
requires the exchanges to publish, after close, the large trades that
crossed the tape: **bulk deals** (a single client trading >0.5% of a
company's listed shares in the normal window) and **block deals**
(negotiated large trades, min ₹10 cr, in the separate block window).
Together they are a *smart-money footprint* — you can see which stocks
FIIs / DIIs / mutual funds / marquee investors accumulated or distributed
that day, by name and by side. This module turns that raw disclosure into
one compact, schema-checked snapshot the forecast layer can lean on.

Why this is NOT in dhan_client.py (non-negotiable #4): Dhan's Data API
does not serve bulk/block deals — the data only exists in NSE/BSE's own
end-of-day reports. That makes NSE a *second, data-only, advisory* source,
authorized by an explicit DECISIONS.md entry (#33). It stays strictly out
of the trade-execution path: this module places, modifies, or proposes no
trades, and writes nothing but its own advisory snapshot.

Data path (Option A + Option B fallback, in that order):

  A. NSE's public large-deal report
     (https://www.nseindia.com/api/snapshot-capital-market-largedeal),
     fetched behind the browser-style cookie handshake NSE requires. The
     data is EOD-only (published ~19:00 IST); there is no real-time path
     and this module is never meant to run intraday.
  B. `data/bulk_deals_snapshot.json` — a hand-maintainable local snapshot
     of raw deal rows, used when the live fetch is unavailable (offline
     box, NSE shape change, no network). If that's missing too, the day
     simply has no entries (source "none") — never a guess.

Aggregation per ticker (the signal, not the raw rows):
  * net_qty      Σ(buy qty − sell qty) across every deal that day. A bulk
                 deal usually discloses BOTH counterparties, so a raw count
                 is noise; the *net* is the footprint.
  * buy_deals / sell_deals   how many disclosed legs on each side.
  * block_deal   True if any block-window deal touched the name (higher
                 conviction than a bulk-window print).
  * marquee_names / marquee_net   the curated "names that matter"
                 (config/deals_watchlist.json) seen on either side, and
                 whether they were net accumulating / distributing / mixed.

Fail-open by design: no network, an NSE shape change, an unreadable
config, an offline box — every path degrades to an empty snapshot or a
"none" source. Nothing here raises to a caller.

Manual check:  python3 -m src.ingestion.deals_tracker
"""

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_PATH = ROOT / "data" / "bulk_deals.json"
SNAPSHOT_PATH = ROOT / "data" / "bulk_deals_snapshot.json"
WATCHLIST_PATH = ROOT / "config" / "deals_watchlist.json"

# Python's urllib doesn't use the system CA store; certifi (already a
# project dependency) makes the NSE HTTPS fetch verify cleanly everywhere.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

HTTP_TIMEOUT = 20

# NSE serves its JSON APIs only to a session that first picked up cookies
# from a normal page load, and it 401s a bare urllib User-Agent — so we
# warm a cookie jar on the homepage, then call the API with browser-ish
# headers. Purely public data; no auth, no key.
_NSE_HOME = "https://www.nseindia.com/"
_NSE_LARGEDEAL_API = (
    "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
)
_NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
}

# The three report sections NSE returns under one payload. Short-deal
# (short-selling) data is disclosed the same way but is a different signal
# (bearish positioning, not accumulation) — deliberately ignored for now.
_BULK_KEYS = ("BULK_DEALS_DATA", "BULK_DEALS", "bulkDeals", "bulk_deals")
_BLOCK_KEYS = ("BLOCK_DEALS_DATA", "BLOCK_DEALS", "blockDeals", "block_deals")

# NSE's row field names have drifted across redesigns; accept the known
# spellings for each field and take the first that's present.
_SYMBOL_FIELDS = ("symbol", "Symbol", "SYMBOL", "BD_SYMBOL")
_CLIENT_FIELDS = ("clientName", "CLIENT_NAME", "BD_CLIENT_NAME", "name",
                  "BD_SCRIP_NAME")
_SIDE_FIELDS = ("buySell", "BUY_SELL", "BD_BUY_SELL", "buyOrSell", "dealType")
_QTY_FIELDS = ("qty", "QTY", "BD_QTY_TRD", "quantity", "quantityTraded")
_PRICE_FIELDS = ("watp", "WATP", "BD_TP_WATP", "tradePrice", "wap",
                 "avgPrice")

SIDES = ("buy", "sell", "unknown")

_SIDE_SYNONYMS = {
    "buy": "buy", "b": "buy", "bought": "buy", "purchase": "buy",
    "buys": "buy", "p": "buy",
    "sell": "sell", "s": "sell", "sold": "sell", "sale": "sell",
    "sells": "sell",
}


# ------------------------------------------------------------- coercion

def coerce_side(value) -> str:
    """Any spelling NSE (or a hand-edited snapshot) uses for a side ->
    the strict "buy"/"sell" vocabulary, "unknown" for anything else.
    Never raises."""
    if value is None:
        return "unknown"
    word = str(value).strip().lower()
    return _SIDE_SYNONYMS.get(word, "unknown")


def _first_field(row: dict, names) -> object:
    """First present, non-empty value among candidate field names."""
    if not isinstance(row, dict):
        return None
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def _to_number(value) -> float | None:
    """NSE quantities/prices arrive as strings with commas ("1,50,000").
    Coerce to float, or None if it isn't a number. Never raises."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def normalize_symbol(symbol, aliases: dict = None) -> str | None:
    """An NSE trading symbol -> the project's ".NS" ticker convention.
    An explicit alias wins (for the rare symbol that doesn't map by simple
    suffixing); otherwise uppercase and append ".NS". Returns None for an
    empty/garbage symbol so it's dropped rather than mis-keyed."""
    if symbol is None:
        return None
    raw = str(symbol).strip().upper()
    if not raw:
        return None
    aliases = aliases or {}
    if raw in aliases and aliases[raw]:
        return str(aliases[raw])
    if raw.endswith(".NS") or raw.endswith(".BO"):
        return raw
    return f"{raw}.NS"


# ------------------------------------------------------------- file loads

def load_watchlist(path=None) -> dict:
    """config/deals_watchlist.json -> {"marquee": [lowercased substrings],
    "aliases": {NSE_SYMBOL: TICKER}}. A missing/broken file degrades to an
    empty watchlist — deals still aggregate, just with no marquee tagging.
    Never raises."""
    path = Path(path) if path is not None else WATCHLIST_PATH
    empty = {"marquee": [], "aliases": {}}
    if not path.exists():
        return empty
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (deals tracker: unreadable watchlist {path} — "
              "no marquee tagging this run)")
        return empty
    if not isinstance(raw, dict):
        return empty
    marquee = [str(n).strip().lower()
               for n in (raw.get("marquee_names") or [])
               if str(n).strip()]
    aliases_raw = raw.get("symbol_aliases") or {}
    aliases = {str(k).strip().upper(): str(v).strip()
               for k, v in aliases_raw.items()
               if str(k).strip() and str(v).strip()} if isinstance(
                   aliases_raw, dict) else {}
    return {"marquee": marquee, "aliases": aliases}


def _load_snapshot(path=None) -> list:
    """data/bulk_deals_snapshot.json -> a list of raw deal rows (Option B).
    Accepts either a bare list of rows or {"deals": [...]}. A missing/broken
    file degrades to []. Never raises."""
    path = Path(path) if path is not None else SNAPSHOT_PATH
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        print(f"  (deals tracker: unreadable snapshot {path} — "
              "treating as absent)")
        return []
    if isinstance(raw, dict):
        rows = raw.get("deals") or raw.get("rows") or []
    else:
        rows = raw
    return rows if isinstance(rows, list) else []


# ------------------------------------------------------------- NSE path

def _fetch_nse_largedeals(timeout: int = HTTP_TIMEOUT) -> list | None:
    """The live Option-A path: warm NSE's cookie jar on the homepage, then
    pull the large-deal JSON and flatten its bulk + block sections into
    tagged raw rows [{..., "deal_type": "bulk"|"block"}]. Returns None on
    ANY failure (no network, 401, timeout, shape change) so the caller
    falls open to the snapshot. Never raises."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=_SSL_CTX),
    )
    try:
        # 1. Warm the session — NSE hands out the cookies its API demands.
        warm = urllib.request.Request(_NSE_HOME, headers=_NSE_HEADERS)
        opener.open(warm, timeout=timeout).read()
        # 2. Pull the report.
        api = urllib.request.Request(_NSE_LARGEDEAL_API, headers=_NSE_HEADERS)
        with opener.open(api, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError, TimeoutError) as exc:
        print(f"  (deals tracker: NSE live fetch failed [{exc}] — "
              "falling open to the local snapshot)")
        return None
    if not isinstance(payload, dict):
        return None
    rows = []
    for keys, deal_type in ((_BULK_KEYS, "bulk"), (_BLOCK_KEYS, "block")):
        section = _first_field(payload, keys)
        if isinstance(section, list):
            for row in section:
                if isinstance(row, dict):
                    tagged = dict(row)
                    tagged.setdefault("deal_type", deal_type)
                    rows.append(tagged)
    return rows


# ------------------------------------------------------------- normalize

def normalize_deal(raw: dict, aliases: dict = None) -> dict | None:
    """One raw NSE/snapshot row -> a strict normalized deal, or None if it
    can't be trusted (no symbol, no side, no quantity). Deal type defaults
    to "bulk" when a row doesn't say."""
    if not isinstance(raw, dict):
        return None
    ticker = normalize_symbol(_first_field(raw, _SYMBOL_FIELDS), aliases)
    if ticker is None:
        return None
    side = coerce_side(_first_field(raw, _SIDE_FIELDS))
    if side == "unknown":
        return None
    qty = _to_number(_first_field(raw, _QTY_FIELDS))
    if qty is None or qty <= 0:
        return None
    price = _to_number(_first_field(raw, _PRICE_FIELDS))
    client = _first_field(raw, _CLIENT_FIELDS)
    deal_type = str(raw.get("deal_type") or "bulk").strip().lower()
    if deal_type not in ("bulk", "block"):
        deal_type = "bulk"
    return {
        "ticker": ticker,
        "client": str(client).strip() if client else "",
        "side": side,
        "qty": int(qty),
        "price": price,
        "value_rs": round(qty * price, 2) if price is not None else None,
        "deal_type": deal_type,
    }


def _marquee_hits(client: str, marquee: list) -> bool:
    """True when a curated marquee substring appears in the client name
    (case-insensitive). Substring, not equality: NSE spells the same fund
    a dozen ways ("SBI MUTUAL FUND", "SBI MF A/C ...")."""
    if not client or not marquee:
        return False
    low = client.lower()
    return any(name in low for name in marquee)


# ------------------------------------------------------------- aggregate

def aggregate_deals(deals: list, marquee: list = None) -> dict:
    """Normalized deals -> the per-ticker footprint keyed by ".NS" ticker.
    Pure: no I/O, no raises. See the module docstring for each field."""
    marquee = marquee or []
    entries = {}
    for deal in deals:
        if not deal:
            continue
        ticker = deal["ticker"]
        e = entries.setdefault(ticker, {
            "net_qty": 0, "net_value_rs": 0.0,
            "buy_deals": 0, "sell_deals": 0,
            "block_deal": False,
            "marquee_names": [], "_marquee_net_qty": 0,
        })
        signed = deal["qty"] if deal["side"] == "buy" else -deal["qty"]
        e["net_qty"] += signed
        if deal["value_rs"] is not None:
            e["net_value_rs"] += (deal["value_rs"] if deal["side"] == "buy"
                                  else -deal["value_rs"])
        e["buy_deals" if deal["side"] == "buy" else "sell_deals"] += 1
        if deal["deal_type"] == "block":
            e["block_deal"] = True
        if _marquee_hits(deal["client"], marquee):
            if deal["client"] not in e["marquee_names"]:
                e["marquee_names"].append(deal["client"])
            e["_marquee_net_qty"] += signed

    # Finalize: round the notional and reduce the marquee net to a label.
    for e in entries.values():
        e["net_value_rs"] = round(e["net_value_rs"], 2)
        mnet = e.pop("_marquee_net_qty")
        if not e["marquee_names"]:
            e["marquee_net"] = "none"
        elif mnet > 0:
            e["marquee_net"] = "accumulating"
        elif mnet < 0:
            e["marquee_net"] = "distributing"
        else:
            e["marquee_net"] = "mixed"
    return entries


# ------------------------------------------------------------- build

def build_deals_matrix(snapshot_path=None, watchlist_path=None,
                       today: date = None, use_live: bool = True) -> dict:
    """The entry point: one advisory bulk/block-deals snapshot.

        {"as_of": "YYYY-MM-DD",
         "source": "nse" | "snapshot" | "none",
         "entries": {"TICKER.NS": {net_qty, net_value_rs, buy_deals,
                     sell_deals, block_deal, marquee_names, marquee_net}}}

    Live NSE first (Option A) when use_live; otherwise / on failure the
    local snapshot (Option B); otherwise no entries. Pure aside from the
    two reads — no writes, no raises."""
    today = today or date.today()
    wl = load_watchlist(watchlist_path)

    raw_rows, source = None, "none"
    if use_live:
        raw_rows = _fetch_nse_largedeals()
        if raw_rows is not None:
            source = "nse"
    if raw_rows is None:
        raw_rows = _load_snapshot(snapshot_path)
        source = "snapshot" if raw_rows else "none"

    deals = [normalize_deal(r, wl["aliases"]) for r in raw_rows]
    deals = [d for d in deals if d is not None]
    if not deals:
        source = "none"
    return {
        "as_of": today.isoformat(),
        "source": source,
        "entries": aggregate_deals(deals, wl["marquee"]),
    }


def load_deals(path=None) -> dict:
    """Reader for downstream consumers (e.g. a future forecast driver):
    data/bulk_deals.json -> its entries dict, or {} on any problem. Mirrors
    news_processor's load pattern — a consumer never depends on the file
    existing or being fresh. Never raises."""
    path = Path(path) if path is not None else OUTPUT_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def run(output_path=None, snapshot_path=None, watchlist_path=None,
        use_live: bool = True) -> dict:
    """Build the matrix and persist it to data/bulk_deals.json. The one
    write in this module, and it's a self-owned advisory artifact (like
    data/news_sentiment.json) — never portfolio/journal/brain_map. Returns
    the matrix. A write failure is logged, not raised."""
    matrix = build_deals_matrix(snapshot_path=snapshot_path,
                                watchlist_path=watchlist_path,
                                use_live=use_live)
    out = Path(output_path) if output_path is not None else OUTPUT_PATH
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(matrix, indent=2))
        print(f"  (deals tracker: wrote {len(matrix['entries'])} ticker(s) "
              f"[{matrix['source']}] -> {out})")
    except OSError as exc:
        print(f"  (deals tracker: could not write {out} [{exc}])")
    return matrix


if __name__ == "__main__":
    # Manual smoke test: python3 -m src.ingestion.deals_tracker
    print(json.dumps(run(), indent=2))
