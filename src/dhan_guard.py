"""
src/dhan_guard.py — response-shape hardening & audit wrapper for dhan_client
============================================================================

Observation-week finding (Issues 5/6, 2026-07-09): Dhan's SDK answers are
shape-shifters. The same endpoint can return

  * flat success:            {"status": "success", "data": <payload>}
  * doubly nested success:   {"status": "success", "data": {"data": <payload>}}
  * structured failure:      {"status": "failure",
                              "remarks": {"error_code": "DH-906",
                                          "error_type": "Order_Error",
                                          "error_message": "Invalid Token"},
                              "data": ""}
  * raw error dicts:         {"errorType": ..., "errorCode": "DH-905", ...}

The existing dhan_client parsers degrade to []/None but throw the error
DETAIL away — which is why the DH-906 token deaths looked identical to
"the market's just quiet" until someone read raw logs. This module keeps
the fail-safe empty-state contract AND preserves a structured audit trail:

    from src.dhan_guard import SafeDhanClient

    safe = SafeDhanClient()
    quote = safe.get_quote("TCS.NS")     # same shape dhan_client returns
    if quote is None and safe.last_error:
        print(safe.last_error.code)      # e.g. "DH-906"

  * Every endpoint returns exactly what its dhan_client counterpart
    returns on success, and the same empty state ([] / None) on failure —
    drop-in, never raises by default.
  * Failures are classified into DhanApiError records (code, message,
    endpoint, raw excerpt) on `last_error` and appended to `audit` so an
    ops sweep can distinguish "token dead" from "no data".
  * strict=True flips the contract to raising DhanApiError for callers
    that prefer exceptions over sentinel checks.

Pure wrapper: reuses dhan_client's _resolve/_get_client (so the
SECURITY_ID_MAP and the self-healing token seam stay single-sourced) and
calls only the same market-data SDK endpoints — the paper-only guarantee
(decision #11) is untouched.
"""

import time
from datetime import datetime, timedelta, timezone

from src import dhan_client as dc

_IST = timezone(timedelta(hours=5, minutes=30))

# Codes that mean "credentials/token", not "market data" — the ops-visible
# distinction that was lost when everything degraded to a silent [].
#
# DH-905 IS NOT AN AUTH ERROR (corrected 2026-07-20). It used to be in this
# set, which made `auth_failures()` — documented as "what an ops sweep should
# page on" — raise a dead-token alarm every time we merely asked a bad
# question. Three independent places in this repo agree it is an INPUT error:
#   * Dhan's own payload calls it  'error_type': 'Input_Exception'
#   * dhan_client.get_ohlc_since   guards a same-day/empty range because
#                                  "Dhan rejects [it] with DH-905"
#   * renew_token                  gets DH-905 from the DEPRECATED endpoint
#                                  ("Renewal of token not allowed")
# None of those is a credential problem, and treating them as one sends the
# owner off to rotate a perfectly healthy token.
AUTH_ERROR_CODES = {"DH-901", "DH-906"}

# Codes that mean "we asked the wrong question" — a bad symbol, a stale
# security id, or a date range the API won't serve. Real and worth fixing,
# but the token is fine; these must never page as a credential outage.
INPUT_ERROR_CODES = {"DH-905"}

# Every code we can name. Used ONLY to spot a code inside a free-text
# `remarks` string — DH-905 stays here so string-shaped failures are still
# identified by code, even though they no longer count as auth.
KNOWN_ERROR_CODES = AUTH_ERROR_CODES | INPUT_ERROR_CODES


class DhanApiError(Exception):
    """One classified Dhan failure. Carried on SafeDhanClient.last_error
    (default mode) or raised (strict mode)."""

    def __init__(self, code: str, message: str, endpoint: str = "", raw: str = ""):
        self.code = code or "UNKNOWN"
        self.message = message or ""
        self.endpoint = endpoint
        self.raw = (raw or "")[:300]
        self.ts = datetime.now(tz=_IST).isoformat(timespec="seconds")
        super().__init__(f"{self.code} on {endpoint or '?'}: {self.message}")

    @property
    def is_auth_error(self) -> bool:
        """Credentials/token — the owner must act. Pages."""
        return self.code in AUTH_ERROR_CODES

    @property
    def is_input_error(self) -> bool:
        """We asked a bad question (symbol, security id, date range). Worth
        fixing in code, but the token is healthy — must not page as auth."""
        return self.code in INPUT_ERROR_CODES

    def as_dict(self) -> dict:
        return {"ts": self.ts, "endpoint": self.endpoint, "code": self.code,
                "message": self.message, "auth_error": self.is_auth_error,
                "input_error": self.is_input_error}


class StaleDataError(DhanApiError):
    """A 200-OK response whose market data is older than the engine may
    trust mid-session (the API deception threat: transport says fine,
    the snapshot is delayed). Raised in strict mode; empty-state +
    last_error in default mode, like every other classified failure —
    either way the stale numbers never reach the engine's math."""

    def __init__(self, age_seconds: float, max_age: float,
                 ts_text: str, endpoint: str = ""):
        super().__init__(
            "STALE_DATA",
            f"market data is {age_seconds:.0f}s old (max {max_age:g}s "
            f"during market hours) — data timestamp {ts_text}",
            endpoint=endpoint)
        self.age_seconds = age_seconds


# --- freshness validation (the API deception guard) -------------------------

# How old a LIVE quote/chain snapshot may be, in seconds, while the market
# is open. 60s is generous for indices ticking every second — anything
# beyond it during a volatile session is a delayed feed, not a quiet one.
# NOTE: the guard is therefore applied to INDEX instruments only (see
# _quote_sec) — an individual equity or OTM option legitimately goes
# minutes without a trade, and voiding those quotes silently dropped real
# open positions from every mark sweep.
STALE_QUOTE_MAX_AGE_SECONDS = 60

# An "age" beyond this during market hours almost certainly means the
# timestamp was misparsed or miszoned (e.g. an epoch already expressed in
# IST wall-clock reads ~5.5h old), not that the feed is genuinely hours
# behind. Treat it like an unparseable field (absence of evidence) rather
# than blanking every live mark on a format variant.
IMPLAUSIBLE_AGE_SECONDS = 3 * 3600

# Timestamp keys observed/plausible across Dhan payload variants — checked
# in order, first parseable wins.
_TIMESTAMP_FIELDS = ("last_trade_time", "lastTradeTime", "ltt",
                     "exchange_time", "exchangeTime", "as_of", "timestamp")


def parse_market_timestamp(value) -> datetime | None:
    """Best-effort parse of a Dhan payload timestamp into an aware IST
    datetime: epoch seconds, epoch milliseconds, "YYYY-MM-DD HH:MM:SS",
    or ISO-8601. None when unparseable — never raises."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            if value <= 0:
                return None
            epoch = float(value)
            if epoch > 1e12:          # milliseconds
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, tz=_IST)
        text = str(value).strip()
        if not text:
            return None
        if text.replace(".", "", 1).isdigit():
            return parse_market_timestamp(float(text))
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1)
                                        if " " in text else text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_IST)  # Dhan clocks are IST
        return parsed
    except (ValueError, OverflowError, OSError):
        return None


def freshness_error(payload: dict, endpoint: str = "",
                    max_age: float = STALE_QUOTE_MAX_AGE_SECONDS,
                    now: datetime = None) -> StaleDataError | None:
    """A StaleDataError when `payload` carries a parseable data timestamp
    older than `max_age` seconds DURING MARKET HOURS, else None.

    Deliberate asymmetries:
      * outside market hours the check never fires — an evening quote is
        legitimately hours old (the day's close), not deceptive;
      * a payload with NO recognizable timestamp field passes — absence
        of evidence is not staleness, and failing closed on it would
        brick every code path against payload variants that simply omit
        the field. Only a present, parseable, out-of-date timestamp is
        treated as deception."""
    if not isinstance(payload, dict):
        return None
    now = now or datetime.now(tz=_IST)
    from src.market_loop import is_market_open
    if not is_market_open(now):
        return None
    for field in _TIMESTAMP_FIELDS:
        ts = parse_market_timestamp(payload.get(field))
        if ts is None:
            continue
        age = (now - ts).total_seconds()
        if age > IMPLAUSIBLE_AGE_SECONDS:
            return None   # miszoned/misparsed stamp, not a delayed feed
        if age > max_age:
            return StaleDataError(age, max_age,
                                  f"{payload.get(field)!r} ({field})",
                                  endpoint=endpoint)
        return None   # first parseable timestamp is fresh — done
    return None


def classify_failure(resp) -> DhanApiError | None:
    """A DhanApiError if `resp` is any known failure shape, else None.

    Handles, in order of specificity:
      * {"status": "failure", "remarks": {"error_code": ..., ...}}  — the
        nested remarks dict observed live (DH-906 token deaths)
      * {"status": "failure", "remarks": "<string>"}                — the
        SDK sometimes flattens remarks to a plain string
      * {"errorCode"/"errorType"/"errorMessage": ...}               — raw
        API error bodies that skip the status envelope entirely
      * non-dict / missing-status responses                          — the
        transport handed back something unparseable
    """
    if not isinstance(resp, dict):
        return DhanApiError("BAD_SHAPE", f"non-dict response: {type(resp).__name__}",
                            raw=str(resp))
    if resp.get("status") == "success":
        return None
    remarks = resp.get("remarks")
    if isinstance(remarks, dict):
        return DhanApiError(str(remarks.get("error_code") or "UNKNOWN"),
                            str(remarks.get("error_message") or ""),
                            raw=str(resp))
    if "errorCode" in resp or "errorType" in resp:
        return DhanApiError(str(resp.get("errorCode") or "UNKNOWN"),
                            str(resp.get("errorMessage") or resp.get("errorType") or ""),
                            raw=str(resp))
    if isinstance(remarks, str) and remarks.strip():
        # e.g. {"status": "failure", "remarks": "DH-906 : Invalid Token"}
        code = next((c for c in sorted(KNOWN_ERROR_CODES) if c in remarks),
                    "UNKNOWN")
        return DhanApiError(code, remarks.strip(), raw=str(resp))
    return DhanApiError("UNKNOWN", f"status={resp.get('status')!r}", raw=str(resp))


# The double-nesting unwrap now lives in dhan_client (2026-07-10
# refinement): the raw parsers there and every endpoint here must share
# ONE copy of that knowledge — the 2026-07-09 incidents proved a fix
# applied in only one style silently strands the other. Re-exported under
# the old name because callers and tests import it from this module.
unwrap_payload = dc.unwrap_payload


class SafeDhanClient:
    """Hardened, audited access to the five market-data endpoints.

    Same return shapes and empty states as the dhan_client module
    functions; adds `last_error` (DhanApiError or None, per call) and
    `audit` (rolling list of classified failures, oldest first)."""

    AUDIT_MAX = 200   # keep the trail bounded on long sessions

    def __init__(self, strict: bool = False,
                 max_quote_age_seconds: float = STALE_QUOTE_MAX_AGE_SECONDS,
                 now_fn=None):
        self.strict = strict
        self.max_quote_age_seconds = max_quote_age_seconds
        self._now_fn = now_fn or (lambda: datetime.now(tz=_IST))
        self.last_error: DhanApiError | None = None
        self.audit: list = []

    # ------------------------------------------------------------ plumbing

    def _fail(self, endpoint: str, err: DhanApiError, empty):
        err.endpoint = endpoint
        self.last_error = err
        self.audit.append(err.as_dict())
        if len(self.audit) > self.AUDIT_MAX:
            del self.audit[:len(self.audit) - self.AUDIT_MAX]
        if self.strict:
            raise err
        return empty

    def _call(self, endpoint: str, fn, *args):
        """One raw SDK call with a single rate-limit retry (matching
        dhan_client's convention) → (resp, None) or (None, DhanApiError)."""
        resp, last_exc = None, None
        for attempt in range(2):
            try:
                resp = fn(*args)
                last_exc = None
            except Exception as e:          # transport/SDK raise
                resp, last_exc = None, e
            if isinstance(resp, dict) and resp.get("status") == "success":
                return resp, None
            if attempt == 0:
                time.sleep(dc._RATE_PAUSE)
        if last_exc is not None:
            return None, DhanApiError("TRANSPORT", str(last_exc))
        return None, classify_failure(resp)

    def _client_and_instr(self, ticker: str, endpoint: str, empty):
        instr = dc._resolve(ticker)
        if instr is None:
            return None, None, self._fail(
                endpoint, DhanApiError("UNMAPPED", f"no SECURITY_ID_MAP entry "
                                       f"for {ticker!r}"), empty)
        client = dc._get_client()
        if client is None:
            return None, None, self._fail(
                endpoint, DhanApiError("NO_CREDENTIALS",
                                       "DHAN_CLIENT_ID / access token missing"),
                empty)
        return instr, client, None

    # ------------------------------------------------------------ endpoints

    def get_expiry_list(self, index_ticker: str) -> list:
        self.last_error = None
        instr, client, failed = self._client_and_instr(
            index_ticker, "expiry_list", [])
        if failed is not None:
            return failed
        resp, err = self._call("expiry_list", client.expiry_list,
                               int(instr["id"]), instr["seg"])
        if err is not None:
            return self._fail("expiry_list", err, [])
        data = unwrap_payload(resp)
        if isinstance(data, list):
            return data
        return self._fail("expiry_list",
                          DhanApiError("BAD_SHAPE",
                                       f"expected list, got {type(data).__name__}",
                                       raw=str(resp)), [])

    def get_option_chain(self, index_ticker: str, expiry_date: str) -> dict | None:
        self.last_error = None
        instr, client, failed = self._client_and_instr(
            index_ticker, "option_chain", None)
        if failed is not None:
            return failed
        resp, err = self._call("option_chain", client.option_chain,
                               int(instr["id"]), instr["seg"], expiry_date)
        if err is not None:
            return self._fail("option_chain", err, None)
        data = unwrap_payload(resp, inner_marker="oc")
        if isinstance(data, dict) and "oc" in data:
            stale = freshness_error(data, endpoint="option_chain",
                                    max_age=self.max_quote_age_seconds,
                                    now=self._now_fn())
            if stale is not None:
                return self._fail("option_chain", stale, None)
            return data
        return self._fail("option_chain",
                          DhanApiError("BAD_SHAPE", "no 'oc' in payload",
                                       raw=str(resp)), None)

    def _quote_sec(self, ticker: str) -> dict | None:
        self.last_error = None
        instr, client, failed = self._client_and_instr(ticker, "quote_data", None)
        if failed is not None:
            return failed
        resp, err = self._call("quote_data", client.quote_data,
                               {instr["seg"]: [int(instr["id"])]})
        if err is not None:
            return self._fail("quote_data", err, None)
        data = unwrap_payload(resp, inner_marker=instr["seg"])
        try:
            sec = data[instr["seg"]][str(instr["id"])]
        except (KeyError, TypeError):
            return self._fail("quote_data",
                              DhanApiError("BAD_SHAPE",
                                           f"no {instr['seg']}/{instr['id']} "
                                           "in quote payload", raw=str(resp)),
                              None)
        if not isinstance(sec, dict):
            return self._fail("quote_data",
                              DhanApiError("BAD_SHAPE", "instrument entry is "
                                           f"{type(sec).__name__}", raw=str(resp)),
                              None)
        # Freshness (the API-deception guard) applies to INDEXES only:
        # they tick every second, so a >60s stamp mid-session really is a
        # delayed feed. Equities/derivatives legitimately idle for minutes
        # — voiding those quotes dropped real positions from mark sweeps.
        if instr["seg"] == "IDX_I":
            stale = freshness_error(sec, endpoint="quote_data",
                                    max_age=self.max_quote_age_seconds,
                                    now=self._now_fn())
            if stale is not None:
                return self._fail("quote_data", stale, None)
        return sec

    def get_live_price(self, ticker: str) -> float | None:
        sec = self._quote_sec(ticker)
        if sec is None or sec.get("last_price") is None:
            return None
        return float(sec["last_price"])

    def get_quote(self, ticker: str) -> dict | None:
        """Same shape as dhan_client.get_quote:
        {ticker, current_price, prev_close, percent_change} or None."""
        sec = self._quote_sec(ticker)
        if sec is None or sec.get("last_price") is None:
            return None
        last = float(sec["last_price"])
        prev = float((sec.get("ohlc") or {}).get("close") or last)
        pct = 0.0 if prev == 0 else (last - prev) / prev * 100
        return {"ticker": ticker, "current_price": round(last, 2),
                "prev_close": round(prev, 2), "percent_change": round(pct, 2)}

    def get_ohlc_since(self, ticker: str, start_iso: str) -> list:
        """Daily bars from start_iso to today, oldest first — same contract
        as dhan_client.get_ohlc_since including the same-day guard."""
        from datetime import date
        self.last_error = None
        if start_iso >= date.today().isoformat():
            return []   # no completed bar yet — same clean wait as dhan_client
        instr, client, failed = self._client_and_instr(
            ticker, "historical_daily_data", [])
        if failed is not None:
            return failed
        resp, err = self._call("historical_daily_data",
                               client.historical_daily_data,
                               instr["id"], instr["seg"], instr["inst"],
                               start_iso, date.today().isoformat())
        if err is not None:
            return self._fail("historical_daily_data", err, [])
        d = unwrap_payload(resp, inner_marker="timestamp")
        if not isinstance(d, dict) or not d.get("timestamp"):
            return self._fail("historical_daily_data",
                              DhanApiError("BAD_SHAPE", "no timestamp arrays "
                                           "in payload", raw=str(resp)), [])
        # Shared ragged-tolerant assembly (dhan_client): a truncated OHLC
        # array degrades to fewer bars — never an IndexError, which would
        # break this class's never-raises-by-default contract.
        bars = dc._bars_from_arrays(d)
        if not bars:
            return self._fail("historical_daily_data",
                              DhanApiError("BAD_SHAPE", "empty/ragged OHLC "
                                           "arrays in payload",
                                           raw=str(resp)), [])
        return bars

    # ------------------------------------------------------------ audit view

    def auth_failures(self) -> list:
        """The audit entries that mean credentials/token, not market data —
        what an ops sweep should page on. Excludes DH-905 input errors: see
        AUTH_ERROR_CODES for why a bad date range is not a dead token."""
        return [a for a in self.audit if a.get("auth_error")]

    def input_failures(self) -> list:
        """Bad-question failures (DH-905): a wrong symbol, a stale security
        id, or an unserved date range. A code bug to chase, not an outage."""
        return [a for a in self.audit if a.get("input_error")]
