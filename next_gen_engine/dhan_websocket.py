"""
next_gen_engine/dhan_websocket.py — DhanHQ live-feed streaming template (DRAFT)
===============================================================================

Blueprint Phase 4 (owner, 2026-07-17). The engine reads daily/hourly OHLC
today (dhan_client, REST, throttled). The target is a real-time intraday
tick stream: DhanHQ's market-feed WebSocket pushes binary packets (ticker /
quote / full / 20-level market depth) which we decode into ticks and fan
out (in the EDA target, onto redis_pubsub as `alpha.quote.tick`).

This is a DRAFT TEMPLATE — deliberately NOT the full client. It nails the
three things worth getting right up front, all UNIT-TESTABLE with no
socket:
  * connection management shape (subscribe frames, backoff schedule);
  * ping/pong keep-alive timing (pure clock math, KeepAlive);
  * BINARY MESSAGE PARSING skeleton for Dhan's little-endian packets
    (parse_header + a ticker-packet decoder), so the wire format is
    captured and regression-tested from day one.

`websockets` is NOT in requirements.txt; it is imported lazily inside
connect() only, so importing this module (and the whole test suite) never
needs the package. The async connect loop is a documented skeleton
(pragma: no cover) — the value under test is the framing/parsing.

DHAN BINARY HEADER (little-endian, 8 bytes), per the market-feed docs:
    int8   feed_response_code   (2=ticker, 4=quote, 5=oi, 6=prev_close,
                                 8=full, 50=disconnect, ...)
    int16  message_length       (bytes, incl. header)
    int8   exchange_segment
    int32  security_id
Ticker payload (code 2): float32 last_price, int32 last_trade_time.

CANONICAL MERGE TARGET: NEW real-time data path. When adopted it becomes
`src/ingestion/dhan_feed.py`, feeding the candle capture / live_bridge
exit alerts from ticks instead of REST polls, and publishing onto the
Phase-4 bus. Uses the SAME token seam as dhan_client (token_provider) —
never a second credential source.
"""
import struct
from dataclasses import dataclass

WS_URL = "wss://api-feed.dhan.co"       # market-feed endpoint (v2)

HEADER_FMT = "<bHbi"                     # int8, int16, int8, int32 (LE)
HEADER_SIZE = struct.calcsize(HEADER_FMT)

# feed response codes we care about first
CODE_TICKER = 2
CODE_QUOTE = 4
CODE_FULL = 8
CODE_DISCONNECT = 50

# subscribe request modes (what we ask Dhan to stream per instrument)
MODE_TICKER = "TICKER"
MODE_QUOTE = "QUOTE"
MODE_FULL = "FULL"


@dataclass
class Tick:
    """One decoded market event. Extend with depth levels as the FULL /
    20-depth decoders land."""
    security_id: int
    exchange_segment: int
    code: int
    last_price: float | None = None
    last_trade_time: int | None = None


def parse_header(data: bytes) -> dict | None:
    """Decode the 8-byte packet header, or None if the buffer is too
    short (a truncated frame is skipped, never an exception)."""
    if not data or len(data) < HEADER_SIZE:
        return None
    code, length, segment, security_id = struct.unpack(
        HEADER_FMT, data[:HEADER_SIZE])
    return {"code": code, "length": length, "segment": segment,
            "security_id": security_id}


def parse_ticker(data: bytes, header: dict) -> Tick | None:
    """Decode a ticker packet (code 2): header + float32 LTP + int32 LTT.
    None when the payload is short — degrade, don't raise."""
    need = HEADER_SIZE + 8
    if len(data) < need:
        return None
    last_price, last_trade_time = struct.unpack(
        "<fi", data[HEADER_SIZE:need])
    return Tick(security_id=header["security_id"],
                exchange_segment=header["segment"], code=header["code"],
                last_price=round(last_price, 2),
                last_trade_time=last_trade_time)


def parse_message(data: bytes) -> Tick | dict | None:
    """Top-level dispatch for one binary frame. Returns a Tick for decoded
    packets, a {"disconnect": ...} marker for the server disconnect code,
    or None for junk/unsupported codes (safe to skip). The parsers not yet
    written (QUOTE/FULL/20-depth) fall through to None BY DESIGN — the
    template decodes what it claims to and is honest about the rest."""
    header = parse_header(data)
    if header is None:
        return None
    if header["code"] == CODE_DISCONNECT:
        return {"disconnect": True, "security_id": header["security_id"]}
    if header["code"] == CODE_TICKER:
        return parse_ticker(data, header)
    # QUOTE / FULL / market-depth decoders: TODO at merge time.
    return None


def build_subscribe_frame(instruments: list, mode: str = MODE_TICKER) -> dict:
    """The JSON control frame sent AFTER connect to subscribe instruments.
    `instruments` = [(exchange_segment, security_id), ...]. Dhan caps
    instruments per message (100) — chunk upstream if longer. Shape mirrors
    the documented RequestCode=15 subscribe payload."""
    return {
        "RequestCode": 15,
        "InstrumentCount": len(instruments),
        "InstrumentList": [
            {"ExchangeSegment": seg, "SecurityId": str(sid)}
            for seg, sid in instruments],
        "Mode": mode,
    }


class KeepAlive:
    """Pure ping/pong keep-alive bookkeeping (no socket, fully testable).
    The connection sends a ping every `interval` s; if a pong (or any
    inbound frame) hasn't arrived within `timeout` s of the last ping, the
    link is presumed dead and the caller should reconnect."""

    def __init__(self, interval: float = 10.0, timeout: float = 30.0):
        self.interval = interval
        self.timeout = timeout
        self._last_ping = None      # None = never pinged (distinct from t=0.0)
        self._last_rx = 0.0

    def note_rx(self, now: float) -> None:
        """Any inbound frame (data or pong) proves the link is alive."""
        self._last_rx = now

    def due_for_ping(self, now: float) -> bool:
        """Due if we have never pinged, or `interval` has elapsed since."""
        if self._last_ping is None:
            return True
        return (now - self._last_ping) >= self.interval

    def mark_ping(self, now: float) -> None:
        self._last_ping = now

    def is_stale(self, now: float) -> bool:
        """Link presumed dead: we pinged and nothing came back in time."""
        if self._last_ping is None:
            return False
        return (now - self._last_rx) > self.timeout


def backoff_schedule(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Exponential reconnect backoff with a ceiling: 1, 2, 4, 8, ... capped
    at `cap`. Deterministic (jitter is a deploy-time add)."""
    if attempt < 0:
        attempt = 0
    return float(min(cap, base * (2 ** attempt)))


class DhanFeedClient:
    """Skeleton streaming client. The framing/parsing/keepalive helpers
    above are real and tested; the async transport is a documented outline
    that lazily imports `websockets` only when actually run."""

    def __init__(self, token: str = None, url: str = WS_URL,
                 on_tick=None):
        self._token = token
        self._url = url
        self._on_tick = on_tick or (lambda tick: None)
        self.keepalive = KeepAlive()

    def _resolve_token(self) -> str:
        """Reuse the SAME token seam as the REST client — never a second
        credential source (token_provider re-reads .env on change)."""
        if self._token:
            return self._token
        from src.token_provider import get_token
        return get_token()

    def handle_frame(self, data: bytes, now: float) -> None:
        """Decode one inbound binary frame and fan out any Tick. Also the
        keep-alive rx notification. Pure enough to unit-test; the socket
        loop just calls this per frame."""
        self.keepalive.note_rx(now)
        parsed = parse_message(data)
        if isinstance(parsed, Tick):
            self._on_tick(parsed)

    async def run(self, instruments: list,
                  mode: str = MODE_TICKER) -> None:  # pragma: no cover
        """The live loop OUTLINE (integration territory, not unit-tested):
        connect with the token, send build_subscribe_frame, then per frame
        call handle_frame(); drive KeepAlive for ping/pong; on drop, sleep
        backoff_schedule(attempt) and reconnect. Left as a skeleton until
        the owner commits to the real-time path."""
        import websockets  # noqa: F401 (optional dep, lazy on the run path)
        raise NotImplementedError(
            "DhanFeedClient.run is a deploy-time skeleton — the framing, "
            "parsing and keep-alive helpers in this module are the tested "
            "surface; wire the transport when the real-time path is adopted")
