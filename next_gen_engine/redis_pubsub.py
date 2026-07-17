"""
next_gen_engine/redis_pubsub.py — Event-Driven Architecture adapter (DRAFT)
============================================================================

Blueprint Phase 4 (owner, 2026-07-17). The engine is a monolith today:
master_scheduler composes market_loop + live_bridge in one process, and a
Dhan hiccup in the data path can stall alerting. The target architecture
decouples them over a message bus — the execution engine PUBLISHES events
(proposal.created, position.exited, quote.tick), and independent consumers
(Discord alerter, data lake writer, dashboard) SUBSCRIBE.

This is a DRAFT adapter: the Publisher/Subscriber base classes and the
event envelope, with the redis transport kept behind a lazily-resolved
client seam. That means:
  * `redis` is NOT a hard dependency — importing this module never fails
    on a box without redis installed (it isn't in requirements.txt). The
    real client is resolved only when you actually connect().
  * every class takes an injectable `client`, so the routing/envelope
    logic is unit-tested against an in-memory FakeBroker with no server.

Envelope (JSON on the wire): {event, ts, v, payload}. Channels are
namespaced `alpha.<domain>.<event>` (e.g. alpha.proposal.created) so a
consumer can pattern-subscribe `alpha.proposal.*`.

CANONICAL MERGE TARGET: NEW infra. When adopted, the publish calls slot
into options_proposer (proposal.created), live_bridge (position.exited /
quote.tick) and portfolio_manager (account.halted); the Discord bridge and
lake writer become subscriber processes. Big architectural change — stays
a reviewed draft until the owner commits to running a broker.
"""
import json
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
ENVELOPE_VERSION = 1
CHANNEL_PREFIX = "alpha"


def channel_for(domain: str, event: str) -> str:
    """`alpha.<domain>.<event>` — the one place channel names are formed."""
    return f"{CHANNEL_PREFIX}.{domain}.{event}"


def make_envelope(event: str, payload: dict) -> dict:
    """The versioned event envelope. `event` is the fully-qualified name
    (`domain.event`); payload must be JSON-serialisable."""
    return {"event": event, "v": ENVELOPE_VERSION,
            "ts": datetime.now(IST).isoformat(timespec="seconds"),
            "payload": payload}


def _resolve_redis_client(url: str):
    """Lazily build a real redis client. Isolated so the import cost (and
    the hard dependency) exists ONLY on the path that truly connects —
    never at module import, never in tests."""
    try:
        import redis  # noqa: F401  (optional dependency, not in requirements)
    except ImportError as e:
        raise RuntimeError(
            "redis package not installed — `pip install redis` before "
            "using the live transport (kept out of requirements.txt until "
            "the EDA migration is committed)") from e
    return redis.Redis.from_url(url, decode_responses=True)


class EventPublisher:
    """Publishes enveloped events to `alpha.<domain>.<event>`. Inject a
    `client` (real redis or a FakeBroker) or pass a `url` to lazily build
    the real one at connect()."""

    def __init__(self, client=None, url: str = "redis://localhost:6379/0"):
        self._client = client
        self._url = url

    def connect(self):
        if self._client is None:
            self._client = _resolve_redis_client(self._url)
        return self._client

    def publish(self, domain: str, event: str, payload: dict) -> dict:
        """Publish and return the envelope that went out. Fail-open by
        contract for the caller's sake — a publish must never take down
        the execution path, so transport errors surface as
        {"published": False, "error": ...} rather than raising."""
        client = self.connect()
        env = make_envelope(f"{domain}.{event}", payload)
        channel = channel_for(domain, event)
        try:
            n = client.publish(channel, json.dumps(env))
            return {"published": True, "channel": channel,
                    "subscribers": n, "envelope": env}
        except Exception as e:
            return {"published": False, "channel": channel,
                    "error": str(e), "envelope": env}


class EventSubscriber:
    """Subscribes to channels/patterns and dispatches decoded envelopes to
    a handler. `handler(envelope: dict) -> None`. Inject a `client`
    (real redis pubsub-capable or a FakeBroker)."""

    def __init__(self, handler, client=None,
                 url: str = "redis://localhost:6379/0"):
        self._handler = handler
        self._client = client
        self._url = url
        self._pubsub = None

    def connect(self):
        if self._client is None:
            self._client = _resolve_redis_client(self._url)
        return self._client

    def subscribe(self, *patterns: str) -> None:
        client = self.connect()
        self._pubsub = client.pubsub()
        # pattern subscribe supports `alpha.proposal.*` style wildcards
        self._pubsub.psubscribe(*patterns)

    @staticmethod
    def decode(raw) -> dict | None:
        """Decode one wire message's data into an envelope, or None on
        junk (a malformed frame is skipped, never fatal)."""
        if isinstance(raw, (bytes, str)):
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                return None
            return obj if isinstance(obj, dict) else None
        return raw if isinstance(raw, dict) else None

    def dispatch(self, message: dict) -> bool:
        """Route ONE raw pubsub message dict ({type, data, ...}) to the
        handler. Returns True if the handler ran. Handler exceptions are
        swallowed (one bad consumer must not kill the subscriber loop)."""
        if not message or message.get("type") not in ("message", "pmessage"):
            return False
        env = self.decode(message.get("data"))
        if env is None:
            return False
        try:
            self._handler(env)
            return True
        except Exception as e:
            print(f"[EventSubscriber] handler error on "
                  f"{env.get('event')}: {e}")
            return False

    def run_forever(self, poll_timeout: float = 1.0) -> None:  # pragma: no cover
        """The live consume loop (skeleton — exercised in integration, not
        unit tests). Reconnect/backoff is a deploy-time hardening TODO."""
        if self._pubsub is None:
            raise RuntimeError("call subscribe(...) before run_forever()")
        for message in self._pubsub.listen():
            self.dispatch(message)


class FakeBroker:
    """In-memory stand-in for a redis client — enough of the publish /
    pubsub / psubscribe surface to unit-test the adapter with no server.
    Not for production; lives here so the tests are self-contained."""

    def __init__(self):
        self.published = []            # [(channel, data), ...]
        self._patterns = []

    # --- publisher surface ---
    def publish(self, channel, data):
        self.published.append((channel, data))
        return sum(1 for p in self._patterns if _match(p, channel))

    # --- subscriber surface ---
    def pubsub(self):
        return self

    def psubscribe(self, *patterns):
        self._patterns.extend(patterns)

    def deliver(self, channel, data):
        """Test helper: hand the matching pattern messages back as redis
        would, so a subscriber's dispatch() can be driven deterministically."""
        return [{"type": "pmessage", "pattern": p, "channel": channel,
                 "data": data}
                for p in self._patterns if _match(p, channel)]


def _match(pattern: str, channel: str) -> bool:
    """Minimal redis-glob match for the `*` suffix/segment wildcard the
    envelope scheme uses (full glob is redis's job in production)."""
    if pattern == channel:
        return True
    if pattern.endswith("*"):
        return channel.startswith(pattern[:-1])
    return False
