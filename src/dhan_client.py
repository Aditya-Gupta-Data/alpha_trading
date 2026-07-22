"""
src/dhan_client.py — DhanHQ market-data engine (DATA ONLY)
==========================================================

The single price/quote/OHLC source for the whole engine, replacing yfinance.
Uses the official dhanhq v2 SDK (DhanContext + dhanhq) keyed by
DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN from .env.

STRICT SAFETY RULE: this module calls ONLY market-data endpoints
(historical_daily_data, quote_data, option_chain). It NEVER calls order /
trade / fund-transfer methods, so the project's paper-only guarantee holds —
Dhan is used here strictly for data, exactly as VISION_PLAN Phase 8 scoped.

Security IDs below were pulled from Dhan's official scrip master
(api-scrip-master-detailed.csv) and verified, not guessed — a wrong id would
silently price the wrong stock. (Note: Dhan's own docs example maps ONGC to
2885, which is actually RELIANCE; the correct ONGC id is 2475.)

Public wrappers:
  get_daily_ohlc(ticker, days=5)   -> [{date, open, high, low, close, volume}]
  get_ohlc_since(ticker, start)    -> same, from an ISO start date (plan tracker)
  get_live_price(ticker)           -> float last traded price (or None)
  get_quote(ticker)                -> data_fetcher-compatible dict (or None)
  get_daily_closes(ticker, days)   -> [close, ...] oldest first (suggestions)
  get_option_chain(index_ticker, expiry_date)
  get_expiry_list(index_ticker)
"""

import time
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl  # POSIX (Linux VM + macOS) — the cross-process gate below
except ImportError:  # pragma: no cover — non-POSIX host
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
_IST = timezone(timedelta(hours=5, minutes=30))

# Dhan's market-data endpoints are rate-limited (quotes ~1/sec). Callers loop
# over the watchlist, so a single retry after a short pause absorbs a
# transient "too many requests" without failing the whole refresh.
_RATE_PAUSE = 1.1

# DH-905 fix, part 1 (2026-07-17): pace calls proactively — every Dhan API
# call goes through _throttle() so a first attempt isn't burned on a rejection.
_last_api_call = 0.0  # per-process fallback timestamp

# DH-905 fix, part 2 (2026-07-22, ops sweep): the one Dhan account has ONE rate
# budget, but during market hours the engine runs SEVERAL processes at once
# (the live loop, the 2-hourly report cards, the 15-min intraday tracker, the
# equity desk). A per-process gap can't see the others, so their calls collide
# and Dhan answers DH-905 in bursts. This gate is shared across every process
# on the host: each caller flock's a tiny file, reserves the next _RATE_PAUSE
# time-slot, then sleeps (outside the lock) until its slot — so every Dhan call
# on the box is spaced >= _RATE_PAUSE apart no matter which process makes it.
_THROTTLE_FILE = ROOT / "data" / ".dhan_throttle"


def _throttle() -> None:
    """Space Dhan calls >= _RATE_PAUSE apart HOST-WIDE (all processes share the
    same one-account budget). Fail-open to per-process pacing — throttle
    bookkeeping must never break a real market-data call."""
    global _last_api_call
    if fcntl is None:
        return _throttle_local()
    try:
        _THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_THROTTLE_FILE, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                last = float(f.read().strip() or 0.0)
            except (ValueError, OSError):
                last = 0.0
            now = time.time()
            # self-heal a corrupt file or a backwards clock: a slot can never
            # be more than one pause in the future.
            if not (0.0 <= last <= now + _RATE_PAUSE):
                last = now
            slot = max(now, last + _RATE_PAUSE)
            f.seek(0)
            f.truncate()
            f.write(f"{slot:.6f}")
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        _last_api_call = time.monotonic()
    except Exception:
        _throttle_local()


def _throttle_local() -> None:
    """Per-process fallback: the original single-process pacing."""
    global _last_api_call
    wait = _RATE_PAUSE - (time.monotonic() - _last_api_call)
    if wait > 0:
        time.sleep(wait)
    _last_api_call = time.monotonic()


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_env()

# ticker -> Dhan instrument. `seg`/`inst` are the exact strings the SDK wants
# for quote_data securities keys and historical_daily_data arguments.
SECURITY_ID_MAP = {
    "RELIANCE.NS":   {"id": "2885",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "TCS.NS":        {"id": "11536", "seg": "NSE_EQ", "inst": "EQUITY"},
    "HDFCBANK.NS":   {"id": "1333",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "ICICIBANK.NS":  {"id": "4963",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "INFY.NS":       {"id": "1594",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "ONGC.NS":       {"id": "2475",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "HINDUNILVR.NS": {"id": "1394",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "ITC.NS":        {"id": "1660",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "MARUTI.NS":     {"id": "10999", "seg": "NSE_EQ", "inst": "EQUITY"},
    "TMPV.NS":       {"id": "3456",  "seg": "NSE_EQ", "inst": "EQUITY"},
    # Tata Motors demerger sibling (CV entity) — id verified against the
    # scrip master 2026-07-17 alongside the LTIM/TATAMOTORS watchlist fix.
    "TMCV.NS":       {"id": "759782", "seg": "NSE_EQ", "inst": "EQUITY"},
    # Cash-equity universe expansion (2026-07-16): ids verified against
    # api-scrip-master-detailed.csv via the FUTSTK UNDERLYING_SECURITY_ID
    # link + a segment-E equity-row cross-check; the method reproduced all
    # 9 previously hand-verified equity ids exactly before being trusted.
    "SBIN.NS":       {"id": "3045",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "BHARTIARTL.NS": {"id": "10604", "seg": "NSE_EQ", "inst": "EQUITY"},
    "LT.NS":         {"id": "11483", "seg": "NSE_EQ", "inst": "EQUITY"},
    "AXISBANK.NS":   {"id": "5900",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "KOTAKBANK.NS":  {"id": "1922",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "BAJFINANCE.NS": {"id": "317",   "seg": "NSE_EQ", "inst": "EQUITY"},
    "ASIANPAINT.NS": {"id": "236",   "seg": "NSE_EQ", "inst": "EQUITY"},
    "TITAN.NS":      {"id": "3506",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "SUNPHARMA.NS":  {"id": "3351",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "TATASTEEL.NS":  {"id": "3499",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "NTPC.NS":       {"id": "11630", "seg": "NSE_EQ", "inst": "EQUITY"},
    "POWERGRID.NS":  {"id": "14977", "seg": "NSE_EQ", "inst": "EQUITY"},
    "ULTRACEMCO.NS": {"id": "11532", "seg": "NSE_EQ", "inst": "EQUITY"},
    "WIPRO.NS":      {"id": "3787",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "HCLTECH.NS":    {"id": "7229",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "M&M.NS":        {"id": "2031",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "HINDALCO.NS":   {"id": "1363",  "seg": "NSE_EQ", "inst": "EQUITY"},
    "JSWSTEEL.NS":   {"id": "11723", "seg": "NSE_EQ", "inst": "EQUITY"},
    # --- Sector-universe expansion (2026-07-16, Top-Down Sectoral)
    # 46 ids verified against api-scrip-master-detailed.csv (NSE, INSTRUMENT=EQUITY).
    # DEFERRED (symbol drift, Yahoo-only until re-checked): LTIM, TATAMOTORS.
    "ADANIENT.NS":   {"id": "25"    , "seg": "NSE_EQ", "inst": "EQUITY"},
    "ADANIGREEN.NS": {"id": "3563"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "ALKEM.NS":      {"id": "11703" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "ASHOKLEY.NS":   {"id": "212"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "AUROPHARMA.NS": {"id": "275"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BAJAJ-AUTO.NS": {"id": "16669" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BAJAJFINSV.NS": {"id": "16675" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BALKRISIND.NS": {"id": "335"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BANKBARODA.NS": {"id": "4668"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BIOCON.NS":     {"id": "11373" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BOSCHLTD.NS":   {"id": "2181"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BPCL.NS":       {"id": "526"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "BRITANNIA.NS":  {"id": "547"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "CIPLA.NS":      {"id": "694"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "COALINDIA.NS":  {"id": "20374" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "COFORGE.NS":    {"id": "11543" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "COLPAL.NS":     {"id": "15141" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "DABUR.NS":      {"id": "772"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "DIVISLAB.NS":   {"id": "10940" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "DRREDDY.NS":    {"id": "881"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "EICHERMOT.NS":  {"id": "910"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "GAIL.NS":       {"id": "4717"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "GODREJCP.NS":   {"id": "10099" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "HEROMOTOCO.NS": {"id": "1348"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "HINDPETRO.NS":  {"id": "1406"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "HINDZINC.NS":   {"id": "1424"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "INDUSINDBK.NS": {"id": "5258"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "IOC.NS":        {"id": "1624"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "JINDALSTEL.NS": {"id": "6733"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "LTTS.NS":       {"id": "18564" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "LUPIN.NS":      {"id": "10440" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "MARICO.NS":     {"id": "4067"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "MPHASIS.NS":    {"id": "4503"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "NESTLEIND.NS":  {"id": "17963" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "NMDC.NS":       {"id": "15332" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "PERSISTENT.NS": {"id": "18365" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "PNB.NS":        {"id": "10666" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "SAIL.NS":       {"id": "2963"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "TATACONSUM.NS": {"id": "3432"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "TATAPOWER.NS":  {"id": "3426"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "TECHM.NS":      {"id": "13538" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "TORNTPHARM.NS": {"id": "3518"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "TVSMOTOR.NS":   {"id": "8479"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "VBL.NS":        {"id": "18921" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "VEDL.NS":       {"id": "3063"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "ZYDUSLIFE.NS":  {"id": "7929"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    # --- BATTERY_EV thematic (2026-07-16), ids from scrip master ---
    "ARE&M.NS":      {"id": "100"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "EXIDEIND.NS":   {"id": "676"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "GRAPHITE.NS":   {"id": "592"   , "seg": "NSE_EQ", "inst": "EQUITY"},
    "HBLENGINE.NS":  {"id": "13966" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "KPITTECH.NS":   {"id": "9683"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "MOTHERSON.NS":  {"id": "4204"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "OLECTRA.NS":    {"id": "10637" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "SONACOMS.NS":   {"id": "4684"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "TATACHEM.NS":   {"id": "3405"  , "seg": "NSE_EQ", "inst": "EQUITY"},
    "UNOMINDA.NS":   {"id": "14154" , "seg": "NSE_EQ", "inst": "EQUITY"},
    # The Wealth-Locking Flywheel's sweep instrument. id verified against
    # Dhan's scrip master by ingestion/scrip_master on 2026-07-20 (NSE,
    # segment E, SEM_TRADING_SYMBOL "GOLDBEES", SM_SYMBOL_NAME
    # "NIP IND ETF GOLD BEES", series EQ) — the ARCHITECTURE.md blocker
    # that kept wealth_lock advisory-only. The weekly clerk re-checks it.
    "GOLDBEES.NS":   {"id": "14428" , "seg": "NSE_EQ", "inst": "EQUITY"},
    "NIFTY 50":      {"id": "13",    "seg": "IDX_I",  "inst": "INDEX"},
    "NIFTY BANK":    {"id": "25",    "seg": "IDX_I",  "inst": "INDEX"},
    # id 21 verified against api-scrip-master-detailed.csv on 2026-07-06
    # (NSE, segment I, SYMBOL_NAME "INDIA VIX").
    "INDIA VIX":     {"id": "21",    "seg": "IDX_I",  "inst": "INDEX"},
}

# Friendly / legacy aliases -> a key in SECURITY_ID_MAP. Lets the rest of the
# engine keep passing the tickers it already uses (bare symbols from the demo
# trades, yfinance-style ^ index symbols, common index names).
_ALIASES = {
    "^NSEI": "NIFTY 50", "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50",
    "^NSEBANK": "NIFTY BANK", "BANKNIFTY": "NIFTY BANK",
    "^INDIAVIX": "INDIA VIX", "INDIAVIX": "INDIA VIX", "VIX": "INDIA VIX",
}

_client = None
_client_token = None   # the token the cached client was built with


def _get_client():
    """Lazily build the dhanhq client. Returns None if creds are missing so
    callers can degrade instead of crashing at import.

    Self-healing (Issue 5, 2026-07-09): the token comes from
    token_provider.get_token() — a LIVE read of .env — instead of the
    startup os.environ snapshot. When a renewal rewrites .env mid-session,
    the next call here sees the new token and rebuilds the SDK client, so
    a long-running session recovers from an external renewal with no
    restart. A stat-per-call is negligible at the engine's cadences."""
    global _client, _client_token
    from src import token_provider
    cid = os.environ.get("DHAN_CLIENT_ID")
    token = token_provider.get_token()
    if not cid or not token:
        return None
    if _client is not None and token == _client_token:
        return _client
    from dhanhq import DhanContext, dhanhq
    _client = dhanhq(DhanContext(cid, token))
    _client_token = token
    return _client


def _resolve(ticker: str) -> dict | None:
    """Map any accepted ticker spelling to a SECURITY_ID_MAP instrument."""
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t in _ALIASES:
        t = _ALIASES[t]
    if t in SECURITY_ID_MAP:
        return SECURITY_ID_MAP[t]
    # bare NSE symbol like "TCS" -> "TCS.NS"
    if "." not in t and not t.startswith("^") and f"{t}.NS" in SECURITY_ID_MAP:
        return SECURITY_ID_MAP[f"{t}.NS"]
    return None


# ----------------------------------------------------------- payload shapes

def unwrap_payload(resp, inner_marker: str = None):
    """The innermost `data` payload regardless of single or double nesting
    — Dhan's SDK answers are shape-shifters ({"data": X} one day,
    {"data": {"data": X}} the next; both observed live 2026-07-09).
    `inner_marker` names a key the REAL payload must contain (e.g. "oc"
    for chains, "timestamp" for bars) so a wrapper dict that happens to
    hold a "data" key is never unwrapped one level too far. Returns None
    on any unusable shape.

    THE single copy of this knowledge: every parser in this module and
    every SafeDhanClient endpoint (dhan_guard re-exports this) goes
    through here, so the next nesting change is a one-line fix."""
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    for _ in range(2):   # at most two unwraps: {"data": {"data": X}}
        if not isinstance(data, dict):
            break
        if inner_marker is not None and inner_marker in data:
            break
        if "data" in data:
            data = data["data"]
        else:
            break
    return data


# ------------------------------------------------------------------ OHLC

def _fetch_daily(instr: dict, from_date: str, to_date: str) -> list:
    """Raw historical_daily_data -> list of bar dicts oldest first, or []."""
    client = _get_client()
    if client is None:
        return []
    resp = None
    for attempt in range(2):
        _throttle()
        try:
            resp = client.historical_daily_data(
                instr["id"], instr["seg"], instr["inst"], from_date, to_date
            )
        except Exception as e:
            print(f"  Dhan historical fetch error: {e}")
            resp = None
        if isinstance(resp, dict) and resp.get("status") == "success":
            break
        if attempt == 0:
            time.sleep(_RATE_PAUSE)  # transient rate limit — retry once
    if not isinstance(resp, dict) or resp.get("status") != "success":
        print(f"  Dhan historical returned: {str(resp)[:160]}")
        return []
    d = unwrap_payload(resp, inner_marker="timestamp")
    if not isinstance(d, dict):
        return []
    return _bars_from_arrays(d)


def _bars_from_arrays(d: dict) -> list:
    """Bar dicts from Dhan's parallel arrays, tolerant of RAGGED payloads:
    the row count is the SHORTEST of the four OHLC arrays (a truncated
    array must degrade to fewer bars, never an IndexError — the empty-
    state-on-failure contract callers rely on), and volume is optional
    per-row."""
    ts = d.get("timestamp") or []
    ohlc = {k: d.get(k) or [] for k in ("open", "high", "low", "close")}
    n = min(len(ts), *(len(v) for v in ohlc.values()))
    vol = d.get("volume") or []
    bars = []
    for i in range(n):
        try:
            bars.append({
                "date": datetime.fromtimestamp(ts[i], tz=_IST).date().isoformat(),
                "open": float(ohlc["open"][i]),
                "high": float(ohlc["high"][i]),
                "low": float(ohlc["low"][i]),
                "close": float(ohlc["close"][i]),
                "volume": float(vol[i]) if i < len(vol) and vol[i] is not None
                          else 0.0,
            })
        except (TypeError, ValueError, OSError, OverflowError):
            continue   # one junk row never voids the rest of the series
    return bars


def get_daily_ohlc(ticker: str, days: int = 5) -> list:
    """Last `days` trading days of daily OHLC, oldest first. Fetches a padded
    calendar window (markets are closed on weekends/holidays) then trims."""
    instr = _resolve(ticker)
    if instr is None:
        return []
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=days * 2 + 10)).isoformat()
    bars = _fetch_daily(instr, from_date, to_date)
    return bars[-days:] if days and len(bars) > days else bars


def get_ohlc_since(ticker: str, start_iso: str) -> list:
    """All daily OHLC bars from `start_iso` (inclusive) to today, oldest
    first. Used by the plan tracker to resolve stop/target hits on real
    daily highs/lows (not a naive last price)."""
    instr = _resolve(ticker)
    if instr is None:
        return []
    # A trade opened today (or in the future) has no completed daily bar since
    # entry yet — and Dhan rejects a same-day/empty range with DH-905. Return
    # [] so the tracker cleanly waits for the next session (matches the old
    # yfinance "empty history" behaviour, just without the noisy error).
    if start_iso >= date.today().isoformat():
        return []
    return _fetch_daily(instr, start_iso, date.today().isoformat())


def get_daily_closes(ticker: str, days: int = 400) -> list:
    """Closing prices oldest first — the indicator engine (SMA/RSI) input."""
    return [b["close"] for b in get_daily_ohlc(ticker, days=days)]


# ----------------------------------------------------------------- quotes

def _quote_sec(ticker: str) -> dict | None:
    """The per-instrument quote dict from quote_data, or None."""
    instr = _resolve(ticker)
    client = _get_client()
    if instr is None or client is None:
        return None
    resp = None
    for attempt in range(2):
        _throttle()
        try:
            resp = client.quote_data({instr["seg"]: [int(instr["id"])]})
        except Exception as e:
            print(f"  Dhan quote error for {ticker}: {e}")
            resp = None
        if isinstance(resp, dict) and resp.get("status") == "success":
            break
        if attempt == 0:
            time.sleep(_RATE_PAUSE)  # transient rate limit — retry once
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    d = unwrap_payload(resp, inner_marker=instr["seg"])
    try:
        return d[instr["seg"]][str(instr["id"])]
    except (KeyError, TypeError):
        return None


def get_live_price(ticker: str) -> float | None:
    sec = _quote_sec(ticker)
    if sec is None or sec.get("last_price") is None:
        return None
    return float(sec["last_price"])


def get_live_price_by_id(security_id, segment: str = "NSE_EQ") -> float | None:
    """Quote for an instrument NOT in SECURITY_ID_MAP, addressed by its
    scrip-master id (the darling desk's path, decision #83 — ids come from
    the weekly `darling_ids.json` built off Dhan's public master, never a
    guess). Same throttle/retry/unwrap discipline as _quote_sec."""
    client = _get_client()
    if client is None or security_id in (None, ""):
        return None
    resp = None
    for attempt in range(2):
        _throttle()
        try:
            resp = client.quote_data({segment: [int(security_id)]})
        except Exception as e:
            print(f"  Dhan quote error for id {security_id}: {e}")
            resp = None
        if isinstance(resp, dict) and resp.get("status") == "success":
            break
        if attempt == 0:
            time.sleep(_RATE_PAUSE)
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    d = unwrap_payload(resp, inner_marker=segment)
    try:
        sec = d[segment][str(int(security_id))]
        return float(sec["last_price"]) if sec.get("last_price") is not None \
            else None
    except (KeyError, TypeError, ValueError):
        return None


def get_quote(ticker: str) -> dict | None:
    """Drop-in replacement for the old data_fetcher.get_quote — same shape:
    {ticker, current_price, prev_close, percent_change} or None."""
    sec = _quote_sec(ticker)
    if sec is None or sec.get("last_price") is None:
        return None
    last = float(sec["last_price"])
    prev = float((sec.get("ohlc") or {}).get("close") or last)
    pct = 0.0 if prev == 0 else (last - prev) / prev * 100
    return {
        "ticker": ticker,
        "current_price": round(last, 2),
        "prev_close": round(prev, 2),
        "percent_change": round(pct, 2),
    }


def get_india_vix() -> float | None:
    """Latest India VIX level, or None when unavailable (no creds, market
    data hiccup). Callers must treat None as "regime unknown" and fail
    safe — the Phase 5 strategy layer refuses to propose range-bound
    spreads without a VIX reading rather than assuming calm."""
    return get_live_price("INDIA VIX")


# ----------------------------------------------------------- option chain

def get_expiry_list(index_ticker: str) -> list:
    """List of ISO expiry date strings for an index underlying.

    Dhan's SDK wraps the payload in `{"status", "data": ...}` — but the
    inner `data` value has been observed DOUBLY nested
    (`{"data": {"data": [...], "status": ...}}`), not the single list the
    outer shape implies. Found live 2026-07-09: the single-unwrap version
    silently handed pick_expiry a dict instead of a list, which iterated
    its KEYS as if they were dates and matched nothing — every proposal
    cycle failed with "no usable expiry" regardless of real market
    conditions. Unwrap defensively so either shape (and a plain list, in
    case Dhan reverts) works, and anything else degrades to []."""
    instr = _resolve(index_ticker)
    client = _get_client()
    if instr is None or client is None:
        return []
    _throttle()
    try:
        resp = client.expiry_list(int(instr["id"]), instr["seg"])
    except Exception as e:
        print(f"  Dhan expiry_list error: {e}")
        return []
    data = unwrap_payload(resp)
    return data if isinstance(data, list) else []


def get_option_chain(index_ticker: str, expiry_date: str) -> dict | None:
    """Option chain for an index underlying at a given expiry (YYYY-MM-DD).

    Returns the flat {"last_price", "oc": {...}} dict options_proposer
    expects. Same doubly-nested SDK response as get_expiry_list
    (`{"data": {"data": {"last_price", "oc"}}}`) — found live 2026-07-09
    right after fixing that one; unwrap defensively here too."""
    instr = _resolve(index_ticker)
    client = _get_client()
    if instr is None or client is None:
        return None
    _throttle()
    try:
        resp = client.option_chain(int(instr["id"]), instr["seg"], expiry_date)
    except Exception as e:
        print(f"  Dhan option_chain error: {e}")
        return None
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    data = unwrap_payload(resp, inner_marker="oc")
    return data if isinstance(data, dict) else None


if __name__ == "__main__":
    # Manual smoke test: python3 -m src.dhan_client
    print("ONGC.NS quote:", get_quote("ONGC.NS"))
    print("TCS.NS last 3 daily bars:", get_daily_ohlc("TCS.NS", days=3))
