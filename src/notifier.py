"""
Sends alerts.

Always prints to the screen. Also emails you via Gmail if credentials are
set in a local .env file (see .env.example and README.md for setup).

Discord: send_discord_message() (async) pushes the same kind of message to
a Discord channel via src/discord_client.py when DISCORD_WEBHOOK_URL is set
in .env — used by the API's background loops for watchlist alerts and
resolved-trade Episodes. Fail-safe like email: unconfigured or failing
Discord never raises, it just returns False.

broadcast_alert(payload) (async) dispatches structured Discord embed cards
for trade lifecycle events (opened / closed / stop_loss / eod). Embeds are
colour-coded, field-gridded, and posted directly to DISCORD_WEBHOOK_URL via
httpx — the {"embeds": [...]} API, not the {"content": "..."} text path.

fire_broadcast(payload) is the sync bridge for contexts that cannot await
(plan_tracker CLI, options_proposer terminal): it detects whether an event
loop is already running and either schedules a Task (API async context) or
calls asyncio.run() (CLI). Never raises — trade journal is never blocked.
"""

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM")
EMAIL_APP_PASSWORD = os.environ.get("ALERT_EMAIL_APP_PASSWORD")
EMAIL_TO = os.environ.get("ALERT_EMAIL_TO") or EMAIL_FROM


# ---- test-environment webhook muzzle (Phase 6J) --------------------------
# Discord webhook HTTP requests must only ever leave this process from a
# TRUE live run. Test suites and backtest loops are muzzled: the send is
# logged locally and reported as not-delivered (False). The Phase 7
# simulator needs no check here — it is source-guarded against importing
# this module at all.
#
# Tests that exercise the dispatch machinery itself set
# WEBHOOK_MUZZLE_OVERRIDE = False (see tests/test_notifier.py's autouse
# fixture); everything else running under pytest is muzzled automatically.
WEBHOOK_MUZZLE_OVERRIDE = None   # None = decide from the environment


def webhooks_muzzled() -> bool:
    """True when webhook traffic must not leave this process: the
    IS_TEST_ENV env flag is set truthy, or a pytest run is in progress
    (PYTEST_CURRENT_TEST is set). Checked per call — env changes and the
    module override both take effect immediately."""
    if WEBHOOK_MUZZLE_OVERRIDE is not None:
        return bool(WEBHOOK_MUZZLE_OVERRIDE)
    if os.environ.get("IS_TEST_ENV", "").strip().lower() in ("1", "true", "yes"):
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _muzzle_log(kind: str, detail: str) -> bool:
    print(f"  (webhook muzzled [test env] — {kind} logged locally, "
          f"not sent: {detail})")
    return False


def _send_email(subject: str, body: str) -> None:
    if not EMAIL_FROM or not EMAIL_APP_PASSWORD:
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"  (email failed to send: {e})")


def send_digest(subject: str, lines: list) -> None:
    _send_email(subject, "\n".join(lines))


async def send_discord_message(message: str, thread_id: str = None) -> bool:
    """Push one message to Discord via the webhook client. Async because
    the network call is httpx-async (the API's event loop awaits it
    directly). Returns False instead of raising when Discord is
    unconfigured or unreachable."""
    if webhooks_muzzled():
        return _muzzle_log("discord message", repr(message[:120]))
    try:
        from src.discord_client import send_webhook_message
    except Exception as e:
        print(f"  (discord client unavailable: {e})")
        return False
    return await send_webhook_message(message, thread_id=thread_id)


# ---- broadcast_alert: structured Discord embed notifications -------------
# Colour palette for trade lifecycle events (Discord uses RGB as a decimal int).
_COLOUR = {
    "opened":           0x2ECC71,   # green  — new approved position
    "closed":           0xE67E22,   # orange — default for closed (before verdict check)
    "stop_loss":        0xE74C3C,   # red    — stop hit
    "eod":              0x3498DB,   # blue   — end-of-day summary card
    "wealth_sweep":     0xF1C40F,   # gold   — paper profit locked into GOLDBEES
    "portfolio_report": 0x9B59B6,   # purple — intraday portfolio report card
    "ceo_brief":        0x1ABC9C,   # teal   — daily cross-department CEO brief
}
_COLOUR_WIN  = 0x2ECC71   # green override: winning closed trade
_COLOUR_LOSS = 0xE74C3C   # red override:   losing closed trade


def _embed_colour(payload: dict) -> int:
    """Pick embed colour from event type; for 'closed' events refine by
    verdict so wins are green and losses are red."""
    event = payload.get("event", "")
    if event == "closed":
        verdict = (payload.get("verdict") or "").lower()
        if "win" in verdict:
            return _COLOUR_WIN
        if "loss" in verdict or "stop" in verdict:
            return _COLOUR_LOSS
        return _COLOUR["closed"]
    return _COLOUR.get(event, 0x95A5A6)   # grey fallback for unknown events


def _build_embed(payload: dict) -> dict:
    """payload dict → one Discord embed object.

    Recognised event types and their required payload keys:
      opened:    ticker, date, strategy, lots, lot_size, max_loss, max_profit,
                 expiry, signal, [short_id]
      closed:    ticker, date, resolution, pnl_rs, r_multiple, verdict,
                 days_in_trade, [frictions_rs], [strategy], [short_id]
      stop_loss: same as closed
      eod:       date, description, fields (pre-built list of Discord field dicts)
      ceo_brief: date, description, fields (pre-built — see ceo_brief.py)
      wealth_sweep: ticker, date, description, sweep_rs, trade_pnl,
                 sweep_pct, [mock_units], [short_id]
    """
    event  = payload.get("event", "event")
    ticker = payload.get("ticker", "?")
    today  = payload.get("date", "")

    titles = {
        "opened":           f"🟢 Trade Opened — {ticker}",
        "closed":           f"📊 Trade Closed — {ticker}",
        "stop_loss":        f"🔴 Stop-Loss Hit — {ticker}",
        "eod":              f"📋 End-of-Day Summary — {today}",
        "wealth_sweep":     f"🔒 Paper Wealth Sweep — {ticker}",
        "portfolio_report": f"🗂️ Portfolio Report Card — {payload.get('time', today)}",
        "ceo_brief":        f"🧭 Daily CEO Brief — {today}",
    }
    title = titles.get(event, f"📌 {event.title()} — {ticker}")

    fields: list = []

    if event == "opened":
        strategy = (payload.get("strategy") or "spread").replace("_", " ").title()
        fields += [
            {"name": "Strategy",   "value": strategy,                                     "inline": True},
            {"name": "Lots",       "value": str(payload.get("lots", "?")),                "inline": True},
            {"name": "Max Loss",   "value": f"Rs.{payload.get('max_loss', 0):,.0f}",      "inline": True},
            {"name": "Max Profit", "value": f"Rs.{payload.get('max_profit', 0):,.0f}",    "inline": True},
            {"name": "Expiry",     "value": payload.get("expiry", "?"),                   "inline": True},
        ]
        if payload.get("signal"):
            fields.append({"name": "Signal", "value": payload["signal"], "inline": False})
        if payload.get("short_id"):
            fields.append({"name": "Trade ID", "value": f"`{payload['short_id']}`", "inline": True})

    elif event in ("closed", "stop_loss"):
        resolution = (payload.get("resolution") or event).replace("_", " ").title()
        pnl        = payload.get("pnl_rs")
        r_val      = payload.get("r_multiple")
        days       = payload.get("days_in_trade")
        frictions  = payload.get("frictions_rs")
        fields += [
            {"name": "Resolution",
             "value": resolution, "inline": True},
            {"name": "P&L",
             "value": f"Rs.{pnl:+,.2f}" if pnl is not None else "?", "inline": True},
            {"name": "R-Multiple",
             "value": f"{r_val:+.2f}R" if r_val is not None else "?", "inline": True},
            {"name": "Days in Trade",
             "value": str(days) if days is not None else "?", "inline": True},
        ]
        if frictions is not None:
            fields.append({"name": "Frictions", "value": f"Rs.{frictions:,.2f}", "inline": True})
        if payload.get("verdict"):
            fields.append({"name": "Verdict", "value": payload["verdict"], "inline": False})
        if payload.get("short_id"):
            fields.append({"name": "Trade ID", "value": f"`{payload['short_id']}`", "inline": True})

    elif event in ("eod", "portfolio_report", "ceo_brief"):
        # Fields are pre-built by the summary job (eod_summary.py /
        # portfolio_report.py / ceo_brief.py) and passed directly.
        fields = list(payload.get("fields") or [])

    elif event == "wealth_sweep":
        sweep = payload.get("sweep_rs")
        pnl   = payload.get("trade_pnl")
        units = payload.get("mock_units")
        fields += [
            {"name": "Sweep Amount",
             "value": f"Rs.{sweep:,.2f}" if sweep is not None else "?", "inline": True},
            {"name": "From Winning P&L",
             "value": f"Rs.{pnl:+,.2f}" if pnl is not None else "?", "inline": True},
            {"name": "Sweep Rate",
             "value": f"{payload.get('sweep_pct', 50):g}%", "inline": True},
        ]
        if units is not None:
            fields.append({"name": "Mock Units", "value": f"{units:.2f}", "inline": True})
        if payload.get("short_id"):
            fields.append({"name": "Source Trade", "value": f"`{payload['short_id']}`", "inline": True})

    footer_parts = ["Alpha Trading Paper", today]
    if payload.get("strategy") and event != "eod":
        footer_parts.append(payload["strategy"].replace("_", " "))
    embed: dict = {
        "title":  title,
        "color":  _embed_colour(payload),
        "fields": fields,
        "footer": {"text": "  •  ".join(p for p in footer_parts if p)},
    }
    if payload.get("description"):
        embed["description"] = payload["description"]
    return embed


# --------------------------- Discord budget (Directive 4, decision #84)
#
# "Maximum 5 Discord messages per day. Silence the noise, consolidate
# the signal." ONE gate at the ONE door every embed card already passes
# through. Three buckets by event name:
#   ALWAYS    — a system crash pages regardless of the budget (the 1-2
#               reserved slots); it also counts, honestly.
#   SCHEDULED — the daily digests (EOD, CEO brief, tier summary, the
#               Saturday digest/performance cards) send while the day's
#               budget lasts.
#   DROP      — the 2-hourly portfolio snapshot: stale by digest time
#               and fully covered by the EOD card; its data ledgers keep
#               accumulating, only the ping dies.
#   (everything else) — SPOOLED to logs/discord_digest_queue.jsonl and
#               rendered inside the next digest's "📦 Batched" section:
#               trades, treasury rotations, 🧠 sizing changes, review
#               flags. Logged silently, shown in the evening — exactly
#               the owner's rule. Unknown events spool too: nothing
#               signal-bearing ever silently dies.
# The budget state is per-machine (Issue-8 ledger-as-memory); totals
# stay ≤5 by construction because each machine's SCHEDULED list is
# tiny (VM: EOD+CEO weekdays / digest+performance Saturday; Mac: the
# tier card) plus crash slots.

BUDGET_STATE_PATH = ROOT / "logs" / ".discord_budget.json"
DIGEST_QUEUE_PATH = ROOT / "logs" / "discord_digest_queue.jsonl"
BUDGET_ALWAYS = {"system_crash"}
BUDGET_SCHEDULED = {"eod", "ceo_brief", "darling_tiers", "digest",
                    "performance", "weekly_digest"}
BUDGET_DROP = {"portfolio_report"}


def _ist_today_str():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).date().isoformat()


def _ist_now_str():
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).isoformat(timespec="seconds")


def _budget_state(state_path=None) -> dict:
    import json
    p = Path(state_path) if state_path else BUDGET_STATE_PATH
    today = _ist_today_str()
    try:
        state = json.loads(p.read_text())
        if state.get("date") == today:
            return state
    except (OSError, ValueError):
        pass
    return {"date": today, "sent": 0}


def _budget_save(state: dict, state_path=None) -> None:
    import json
    p = Path(state_path) if state_path else BUDGET_STATE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state))
    except OSError:
        pass


def budget_gate(payload: dict, state_path=None, queue_path=None,
                enabled=None) -> str:
    """"send" | "spool" | "drop" for one card. Spooling appends the card
    to the digest queue so the next digest carries it. `enabled` is a
    test seam; production always reads the config."""
    from src.config import DISCORD_BUDGET_ENABLED, DISCORD_DAILY_BUDGET
    if enabled is None:
        enabled = DISCORD_BUDGET_ENABLED
    if not enabled:
        return "send"
    event = str(payload.get("event") or "")
    if event in BUDGET_ALWAYS:
        return "send"                       # crash pages past any budget
    if event in BUDGET_DROP:
        return "drop"
    state = _budget_state(state_path)
    if event in BUDGET_SCHEDULED and state["sent"] < DISCORD_DAILY_BUDGET:
        return "send"
    _spool(payload, queue_path)
    return "spool"


def _spool(payload: dict, queue_path=None) -> None:
    import json
    p = Path(queue_path) if queue_path else DIGEST_QUEUE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": _ist_now_str(),
                                "event": payload.get("event"),
                                "ticker": payload.get("ticker"),
                                "description":
                                    str(payload.get("description") or "")[:400]
                                }) + "\n")
    except OSError:
        pass                                # spooling is best-effort


def _budget_count_send(state_path=None) -> None:
    state = _budget_state(state_path)
    state["sent"] += 1
    _budget_save(state, state_path)


def drain_digest_queue(queue_path=None, max_lines: int = 12) -> str | None:
    """Everything spooled since the last digest, as compact lines — then
    the queue archives and truncates (the digest that drained it is the
    one place the owner reads it). None when nothing waited."""
    import json
    p = Path(queue_path) if queue_path else DIGEST_QUEUE_PATH
    try:
        raw = [ln for ln in p.read_text().splitlines() if ln.strip()]
    except OSError:
        return None
    if not raw:
        return None
    rows = []
    for ln in raw:
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    lines = []
    for r in rows[:max_lines]:
        first = str(r.get("description") or "").split("\n")[0][:80]
        lines.append(f"{str(r.get('ts', ''))[11:16]} · "
                     f"{r.get('event', '?')}: {first}")
    if len(rows) > max_lines:
        lines.append(f"…and {len(rows) - max_lines} more (full text in "
                     f"the drained ledger)")
    try:
        with (p.parent / (p.name + ".drained")).open("a") as f:
            for ln in raw:
                f.write(ln + "\n")
        p.write_text("")
    except OSError:
        pass
    return "\n".join(lines)


async def broadcast_alert(payload: dict) -> bool:
    """Dispatch one structured embed card to DISCORD_WEBHOOK_URL via httpx.

    payload must contain at minimum: event, ticker, date.
    See _build_embed() for the full per-event key reference.

    Posts {"embeds": [embed]} — the Discord rich-embed format; distinct from
    the {"content": "..."} text-webhook path (send_webhook_message). Always
    returns False instead of raising on misconfiguration / network failure.

    Since decision #84 every card passes the daily Discord budget first:
    suppressed cards return True (they reached the owner's digest queue —
    dedup ledgers may honestly mark them announced)."""
    if webhooks_muzzled():
        return _muzzle_log(
            "embed broadcast",
            f"{payload.get('event', '?')} {payload.get('ticker', '?')}")
    try:
        verdict = budget_gate(payload)
    except Exception:
        verdict = "send"                    # a broken gate never mutes
    if verdict in ("spool", "drop"):
        print(f"  (discord budget: {payload.get('event', '?')} "
              f"{'queued for the next digest' if verdict == 'spool' else 'dropped'})")
        return True
    try:
        from src.discord_client import _webhook_url, REQUEST_TIMEOUT_SECONDS
    except Exception as exc:
        print(f"  (broadcast_alert: discord client unavailable: {exc})")
        return False

    url = _webhook_url()
    if not url:
        return False

    try:
        import httpx
    except ImportError:
        print("  (broadcast_alert: httpx not installed)")
        return False

    body = {"embeds": [_build_embed(payload)]}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body)
        if resp.status_code >= 300:
            print(f"  (broadcast_alert: HTTP {resp.status_code})")
            return False
        try:
            _budget_count_send()            # only a DELIVERED card burns
        except Exception:                   # budget; a failed post doesn't
            pass
        return True
    except Exception as exc:
        print(f"  (broadcast_alert: network error: {exc})")
        return False


def fire_broadcast(payload: dict) -> None:
    """Sync bridge: dispatch broadcast_alert from any calling context.

    Two cases:
      * running event loop (FastAPI async context): schedules a Task
        (fire-and-forget — caller does not await the result).
      * no running loop (CLI, plan_tracker direct): asyncio.run().

    Never raises; any Discord/network failure is printed and swallowed so
    the trade journal is never blocked by a Discord outage.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        # We're in the event loop thread — create_task is safe here.
        loop.create_task(broadcast_alert(payload))
    except RuntimeError:
        # No running loop: CLI or asyncio.to_thread context.
        try:
            asyncio.run(broadcast_alert(payload))
        except Exception as exc:
            print(f"  (fire_broadcast: asyncio.run failed: {exc})")
    except Exception as exc:
        print(f"  (fire_broadcast failed: {exc})")


def format_episode(episode: dict) -> str:
    """One resolved-trade Episode dict (from brain_map.build_episode_snapshot)
    -> the structured Discord message body."""
    sentiment = episode.get("market_sentiment") or {}
    lines = [
        f"📕 **Trade Episode — {episode.get('ticker')}**",
        f"Resolution: {episode.get('resolution')} | Verdict: {episode.get('verdict')}",
        (f"Entry {episode.get('entry_date')} @ Rs.{episode.get('entry_price')} → "
         f"Exit {episode.get('exit_date')} @ Rs.{episode.get('exit_price')}"),
        f"R-multiple: {episode.get('r_multiple')} | Net P&L: Rs.{episode.get('pnl_rs')}",
        f"Signal at entry: {episode.get('signal')}",
    ]
    if episode.get("pattern_tags"):
        lines.append("Pattern tags: " + ", ".join(episode["pattern_tags"]))
    if sentiment.get("score") is not None:
        lines.append(f"Market sentiment: {sentiment['score']:+.2f} "
                     f"({sentiment.get('headline_focus') or 'no focus'})")
    return "\n".join(lines)
