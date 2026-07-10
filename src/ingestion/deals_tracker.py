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
# Append-only raw-deal ledger (one normalized deal per line, stamped with
# the report date). The daily bulk_deals.json is an overwritten aggregate;
# this file is the permanent per-deal history the entity-affinity learning
# layer (src/knowledge_graph/entity_affinity.py) accumulates over months.
HISTORY_PATH = ROOT / "data" / "deals_history.jsonl"

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

def _nse_opener():
    """A cookie-jar urllib opener with the browser-ish handshake NSE wants.
    Shared by the daily pull and the historical backfill."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=_SSL_CTX),
    )


def _fetch_nse_largedeals(timeout: int = HTTP_TIMEOUT):
    """The live Option-A path: warm NSE's cookie jar on the homepage, then
    pull the large-deal JSON and flatten its bulk + block sections into
    tagged raw rows [{..., "deal_type": "bulk"|"block"}]. Returns
    (rows, raw_bytes) — raw_bytes is the exact payload NSE served, kept so
    run() can archive it immutably (census doctrine). Returns None on ANY
    failure (no network, 401, timeout, shape change) so the caller falls
    open to the snapshot. Never raises."""
    opener = _nse_opener()
    try:
        # 1. Warm the session — NSE hands out the cookies its API demands.
        warm = urllib.request.Request(_NSE_HOME, headers=_NSE_HEADERS)
        opener.open(warm, timeout=timeout).read()
        # 2. Pull the report.
        api = urllib.request.Request(_NSE_LARGEDEAL_API, headers=_NSE_HEADERS)
        with opener.open(api, timeout=timeout) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
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
    return rows, raw


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

def _collect_deals(snapshot_path=None, watchlist_path=None,
                   use_live: bool = True) -> tuple:
    """Fetch + normalize the day's deals once. Returns
    (deals, wl, source, raw_rows, raw_payload): normalized deal dicts, the
    loaded watchlist, the source label, the pre-normalization rows (census
    input), and the exact NSE payload bytes when the live path answered
    (None otherwise). Live NSE first (Option A) when use_live; otherwise /
    on failure the local snapshot (Option B); otherwise nothing. Shared by
    build_deals_matrix (aggregate view) and run (aggregate + raw history)
    so the fetch happens exactly once. No writes, no raises."""
    wl = load_watchlist(watchlist_path)
    raw_rows, raw_payload, source = None, None, "none"
    if use_live:
        fetched = _fetch_nse_largedeals()
        if fetched is not None:
            # (rows, raw_bytes) from the live fetcher; a legacy/test fake
            # returning a bare list still works.
            if isinstance(fetched, tuple):
                raw_rows, raw_payload = fetched
            else:
                raw_rows = fetched
            source = "nse"
    if raw_rows is None:
        raw_rows = _load_snapshot(snapshot_path)
        source = "snapshot" if raw_rows else "none"
    deals = [normalize_deal(r, wl["aliases"]) for r in raw_rows]
    deals = [d for d in deals if d is not None]
    if not deals:
        source = "none"
    return deals, wl, source, raw_rows, raw_payload


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
    deals, wl, source, _, _ = _collect_deals(snapshot_path, watchlist_path,
                                             use_live)
    return {
        "as_of": today.isoformat(),
        "source": source,
        "entries": aggregate_deals(deals, wl["marquee"]),
    }


def append_raw_deals(deals: list, as_of: str, path=None) -> int:
    """Append the day's normalized deals to the raw history ledger, one JSON
    line each stamped with `as_of`. Idempotent per DAY: if the ledger
    already holds any row for `as_of`, this is a no-op (a re-run of the
    same EOD pull must not double-count the concentration math downstream).
    Returns the number of rows written (0 if the day was already present or
    there were no deals). Never raises — a write/read failure is logged."""
    if not deals:
        return 0
    path = Path(path) if path is not None else HISTORY_PATH
    try:
        # Day-level dedup: scan existing as_of values (cheap — the ledger is
        # a few dozen rows/day) and skip if this date is already folded in.
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("as_of") == as_of:
                        return 0
                except ValueError:
                    continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            for deal in deals:
                row = dict(deal)
                row["as_of"] = as_of
                f.write(json.dumps(row) + "\n")
        return len(deals)
    except OSError as exc:
        print(f"  (deals tracker: could not append raw history {path} [{exc}])")
        return 0


def read_deal_history(path=None) -> list:
    """The raw ledger -> a list of normalized deal rows (each carrying its
    `as_of` date). A missing/broken ledger degrades to []. Malformed lines
    are skipped, never fatal. Never raises — mirrors journal.read_all."""
    path = Path(path) if path is not None else HISTORY_PATH
    if not path.exists():
        return []
    rows = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return rows


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


def _alias_candidates(deals: list, limit: int = 10) -> list:
    """Near-duplicate client-name candidates for HUMAN review of the alias
    table (config/entity_groups.json client_aliases). Heuristic: disclosed
    names sharing their first two tokens but differing overall are probably
    the same entity spelled two ways ('SBI MUTUAL FUND A/C X' vs 'SBI
    MUTUAL FUNDS LIMITED'). NOTHING auto-merges — a false merge fakes
    concentration, the exact failure the affinity thresholds exist to
    avoid; these only surface on the census/ops card."""
    by_prefix = {}
    for d in deals:
        name = (d.get("client") or "").strip().upper()
        tokens = name.split()
        if len(tokens) < 2:
            continue
        by_prefix.setdefault(" ".join(tokens[:2]), set()).add(name)
    out = []
    for prefix, names in sorted(by_prefix.items()):
        if len(names) > 1:
            out.append({"prefix": prefix, "names": sorted(names)})
        if len(out) >= limit:
            break
    return out


def build_census(deals: list, raw_rows: list, source: str,
                 as_of: str) -> dict:
    """One per-day data-quality row for the census lake dataset: is the
    disclosure tape thinning, drifting, or fragmenting before it poisons
    months of affinity accumulation? Pure function, never raises."""
    tickers = {d["ticker"] for d in deals}
    clients = {d["client"] for d in deals if d.get("client")}
    ungrouped = 0
    try:
        # Lazy import: entity_affinity imports this module at load time, so
        # the reverse import must happen at call time, not module level.
        from src.knowledge_graph.entity_affinity import (
            group_for_ticker, load_entity_groups, UNGROUPED)
        ttg = load_entity_groups()["ticker_to_group"]
        ungrouped = sum(1 for d in deals
                        if group_for_ticker(d["ticker"], ttg) == UNGROUPED)
    except Exception:
        ungrouped = -1   # census must never block the pull; -1 = unknown
    return {
        "as_of": as_of,
        "source": source,
        "raw_rows": len(raw_rows or []),
        "normalized": len(deals),
        "dropped": max(0, len(raw_rows or []) - len(deals)),
        "distinct_clients": len(clients),
        "distinct_tickers": len(tickers),
        "block_legs": sum(1 for d in deals if d["deal_type"] == "block"),
        "buy_legs": sum(1 for d in deals if d["side"] == "buy"),
        "sell_legs": sum(1 for d in deals if d["side"] == "sell"),
        "ungrouped_deals": ungrouped,
        "alias_candidates": _alias_candidates(deals),
    }


def run(output_path=None, snapshot_path=None, watchlist_path=None,
        history_path=None, use_live: bool = True, today: date = None,
        lake_root=None) -> dict:
    """Fetch once, then persist the full capture set: the overwritten daily
    aggregate data/bulk_deals.json, an append to the raw history ledger
    data/deals_history.jsonl (the substrate the entity-affinity layer
    learns from), the EXACT raw NSE payload hashed into the lake (a silent
    upstream revision becomes a visible hash change), and a per-day census
    row (tape-quality telemetry). All self-owned advisory artifacts — never
    portfolio/journal/brain_map. Returns the matrix. Any write failure is
    logged, not raised."""
    today = today or date.today()
    deals, wl, source, raw_rows, raw_payload = _collect_deals(
        snapshot_path, watchlist_path, use_live)
    matrix = {
        "as_of": today.isoformat(),
        "source": source,
        "entries": aggregate_deals(deals, wl["marquee"]),
    }
    out = Path(output_path) if output_path is not None else OUTPUT_PATH
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(matrix, indent=2))
        print(f"  (deals tracker: wrote {len(matrix['entries'])} ticker(s) "
              f"[{matrix['source']}] -> {out})")
    except OSError as exc:
        print(f"  (deals tracker: could not write {out} [{exc}])")
    written = append_raw_deals(deals, matrix["as_of"], path=history_path)
    if written:
        print(f"  (deals tracker: appended {written} raw deal(s) for "
              f"{matrix['as_of']} -> history ledger)")
    # Census + immutable raw archive (fail-open; the pull never depends on
    # the lake being writable).
    try:
        from src import lake
        if raw_payload:
            lake.archive_blob("deals_raw", matrix["as_of"], "largedeal",
                              raw_payload, ext="json", root=lake_root)
        census = build_census(deals, raw_rows, source, matrix["as_of"])
        lake.write_partition("deals_census", matrix["as_of"], [census],
                             root=lake_root)
        if census["alias_candidates"]:
            print(f"  (deals tracker: {len(census['alias_candidates'])} "
                  "alias candidate group(s) for human review — see census)")
    except Exception as exc:
        print(f"  (deals tracker: census/archive skipped [{exc}])")
    return matrix


if __name__ == "__main__":
    # Manual smoke test: python3 -m src.ingestion.deals_tracker
    print(json.dumps(run(), indent=2))
