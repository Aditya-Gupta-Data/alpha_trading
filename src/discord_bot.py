"""
Alpha Trading -- Phase 5 (step 1): the Discord Analyst bot
===========================================================

Puts the analyst on your phone: a Discord bot that answers a /analyze
slash command with the Phase 4E forecast (technicals + news sentiment),
and holds a plain conversation when you reply to it (replies go to Gemini,
the same free-tier model the news processor uses).

STRICT GUARDRAIL (VISION_PLAN.md): this bot is read-only on the ENGINE.
It imports the forecast layer to ANSWER QUESTIONS -- it does not import
portfolio/trade/strategy/journal, cannot execute paper trades, and there
is no broker anywhere in this project. The Phase 9 two-way bridge keeps
that rule intact: /pending's Approve/Reject buttons do NOT touch the
journal from here -- they POST to the authenticated gateway
(`/api/discord/action` on src/api_server.py, x-api-key required), which
owns the mutation with exactly the --review-pending semantics. The bot is
just another API client; the only state it can change is a PAPER journal
decision, and only through the gateway.

Commands:
  /analyze              -- forecast every watchlist stock
  /analyze ticker:ONGC  -- forecast one stock (`.NS` added if omitted)
  /pending              -- list PENDING_APPROVAL proposals with tappable
                           ✅ Approve / ❌ Reject buttons (each opens a
                           one-line "why" prompt, journaled verbatim)
  /positions            -- open paper positions (entry, target/stop,
                           time in trade) via the gateway's read-only
                           /api/discord/positions endpoint
  (reply to any bot message, or @mention it, to chat -- answered by Gemini)

Setup: DISCORD_BOT_TOKEN in .env (from the Discord Developer Portal, with
Message Content Intent enabled); GEMINI_API_KEY in .env for the chat side
(without it, the bot still runs -- /analyze works, chat replies apologize).
For /pending, the machine running the bot must also reach the Phase 9
gateway: API_KEY in .env plus BRIDGE_BASE_URL (default
http://127.0.0.1:8000 -- right when the bot runs on the same VM as the
gateway, which also makes the quick-tunnel URL irrelevant here).
Run:   python3 -m src.discord_bot     (stays running until Ctrl+C)
"""

import asyncio
import json
import os
import ssl
import urllib.request
from pathlib import Path

import certifi

# discord.py opens its own HTTPS connections (via aiohttp) and builds its
# SSL context when it is imported -- so SSL_CERT_FILE must point at
# certifi's CA bundle BEFORE the discord import below, or macOS/VM Python
# fails with CERTIFICATE_VERIFY_FAILED (same missing-CA-store problem
# news_processor.py hit; verified: setting this after the import is too late).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import discord
from discord import app_commands

from src import deploy_log
from src.forecast import describe, forecast, load_news, load_tickers, load_weights

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
NEWS_PATH = ROOT / "data" / "news_sentiment.json"

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# Same "-latest" alias reasoning as news_processor.py: pinned Gemini model
# names get deprecated and 404; the alias tracks Google's current model.
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
HTTP_TIMEOUT = 30  # seconds
DISCORD_MSG_LIMIT = 2000  # hard Discord cap per message


def _load_env() -> None:
    """Same self-contained .env reader as notifier/news_processor -- each
    standalone entry point carries its own copy on purpose (modularity)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


# ---------------------------------------------------------------- forecasts

def _normalize_ticker(raw: str) -> str:
    """'ongc' -> 'ONGC.NS'; anything already carrying an exchange suffix
    (.NS/.BO) passes through unchanged."""
    ticker = raw.strip().upper()
    if "." not in ticker:
        ticker += ".NS"
    return ticker


def _run_forecasts(tickers: list) -> list:
    """Blocking (Dhan price fetch) -- always called via asyncio.to_thread so
    the bot's Discord heartbeat never starves. Returns display lines."""
    news = load_news()
    weights = load_weights()
    lines = []
    for ticker in tickers:
        result = forecast(ticker, news, weights)
        if result is None:
            # Data path is DhanHQ (get_daily_closes), NOT Yahoo — the old
            # "on Yahoo Finance" wording here predated the 2026-07-06
            # migration and actively misdirected diagnosis (ledger Issue 7).
            lines.append(f"{ticker}: not enough price history to forecast "
                         "(needs 200+ trading days from DhanHQ — likely a "
                         "token/data outage or a missing SECURITY_ID_MAP "
                         "entry for this ticker).")
        else:
            lines.append(describe(result))
    return lines


def _chunk(lines: list) -> list:
    """Packs lines into as few messages as possible under Discord's cap."""
    chunks, current = [], ""
    for line in lines:
        candidate = f"{current}\n\n{line}" if current else line
        if len(candidate) > DISCORD_MSG_LIMIT - 10:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ["Nothing to report."]


# ------------------------------------------------------------------- gemini

def _chat_context() -> str:
    """A small factual context block so Gemini answers as ADiTrader's
    analyst rather than a generic chatbot. News sentiment is the only
    data file included -- deliberately NOT the portfolio or journal."""
    parts = [
        "Watchlist tickers: " + ", ".join(load_tickers()),
    ]
    if NEWS_PATH.exists():
        with open(NEWS_PATH) as f:
            news = json.load(f)
        reads = [
            f"{t}: {e['sentiment_score']:+d} ({e['headline_focus']})"
            for t, e in news.get("tickers", {}).items() if not e.get("stale")
        ]
        if reads:
            parts.append("Latest news sentiment (-5 bearish .. +5 bullish): "
                         + "; ".join(reads))
    return "\n".join(parts)


def _ask_gemini(user_text: str, api_key: str) -> str:
    """Blocking single-turn chat call -- run via asyncio.to_thread."""
    prompt = (
        "You are the ADiTrader Analyst, a friendly assistant inside a Discord "
        "server, helping one non-technical user think about Indian (NSE/BSE) "
        "stocks from their personal PAPER-TRADING tool. Be conversational and "
        "brief (this is a chat message, not a report). Plain English, no "
        "jargon unless the user uses it first. Never claim to place trades -- "
        "the tool is paper-only; proposals are approved by the user via the "
        "/pending buttons or a terminal session, and approving only journals "
        "a paper decision (no broker exists). If asked for financial advice, "
        "share the data-driven read but remind them it's their call.\n\n"
        f"Context:\n{_chat_context()}\n\n"
        f"The user says: {user_text}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7},
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as resp:
        body = json.loads(resp.read())
    return body["candidates"][0]["content"]["parts"][0]["text"].strip()


# ------------------------------------------------- Phase 9 bridge client
#
# Everything below talks to the strict gateway (src/api_server.py) over
# HTTP with the x-api-key header -- the bot NEVER touches the journal or
# any engine module directly. Blocking urllib calls, always run through
# asyncio.to_thread from the async handlers.

BRIDGE_DEFAULT_URL = "http://127.0.0.1:8000"
BRIDGE_TIMEOUT = 15  # seconds


def _bridge_base_url() -> str:
    return (os.environ.get("BRIDGE_BASE_URL") or BRIDGE_DEFAULT_URL).rstrip("/")


def _bridge_call(method: str, path: str, payload: dict = None) -> tuple:
    """One authenticated gateway call -> (http_status, parsed_json_body).
    Never raises for HTTP error statuses (401/404/409 are meaningful
    answers here); connection-level failures do raise, and callers report
    them as 'gateway unreachable'."""
    base = _bridge_base_url()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"x-api-key": os.environ.get("API_KEY", ""),
                 "Content-Type": "application/json"},
    )
    ctx = _SSL_CTX if base.startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=BRIDGE_TIMEOUT, context=ctx) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"ok": False, "error": f"HTTP {e.code}"}
        return e.code, body


def _fetch_pending() -> list:
    """GET /api/discord/pending -> the list of undecided proposals."""
    status, body = _bridge_call("GET", "/api/discord/pending")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(body.get("error") if isinstance(body, dict)
                           else f"gateway answered HTTP {status}")
    return body.get("pending") or []


def _decide(trade_id: str, action: str, why: str) -> tuple:
    """POST /api/discord/action -> (status, body). 200 decided, 404 gone,
    409 already tracker-resolved."""
    return _bridge_call("POST", "/api/discord/action",
                        {"action": action, "trade_id": trade_id, "why": why})


def _format_pending(p: dict) -> str:
    """One pending proposal -> the Discord message the buttons ride on."""
    strategy = (p.get("strategy") or "proposal").replace("_", " ").title()
    net = p.get("net_per_share")
    net_text = f"Rs.{net:,.2f}" if isinstance(net, (int, float)) else "n/a"
    return (
        f"⏳ **Pending: {strategy} on {p.get('ticker')}**"
        f"  (trade id `{p.get('trade_id')}`)\n"
        f"proposed {p.get('proposed_on')} | expiry {p.get('expiry')} | "
        f"{p.get('lots')} lot(s) x {p.get('lot_size')}\n"
        f"net {p.get('net_kind')} {net_text}/share | "
        f"max loss Rs.{p.get('max_loss', 0):,.0f} | "
        f"max profit Rs.{p.get('max_profit', 0):,.0f}\n"
        f"signal: {p.get('signal')}\n"
        f"*(paper only -- approving hands the exit to the plan tracker)*"
    )


def _fetch_positions() -> list:
    """GET /api/discord/positions -> open approved paper positions."""
    status, body = _bridge_call("GET", "/api/discord/positions")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(body.get("error") if isinstance(body, dict)
                           else f"gateway answered HTTP {status}")
    return body.get("positions") or []


def _positions_embed(items: list) -> "discord.Embed":
    """Open positions -> one embed, a field per position (Discord caps
    embeds at 25 fields; the overflow is summarized in the footer)."""
    embed = discord.Embed(
        title=f"📂 Open Paper Positions ({len(items)})",
        color=0x3498DB,
        description="Live from the journal — the plan tracker manages "
                    "every exit. Paper only.",
    )
    shown = items[:25]
    for p in shown:
        days = p.get("days_in_trade")
        in_trade = f"{days}d in trade" if days is not None else "entry date unknown"
        if p.get("kind") == "spread":
            bounds = (f"max profit Rs.{p.get('max_profit_rs', 0):,.0f} / "
                      f"max loss Rs.{p.get('max_loss_rs', 0):,.0f}")
            entry = (f"entry {p.get('entry_price')} net/share, "
                     f"{p.get('lots')} lot(s), expiry {p.get('expiry')}")
        else:
            bounds = (f"target Rs.{p.get('target')} / "
                      f"stop Rs.{p.get('stop_loss')}")
            entry = f"entry Rs.{p.get('entry_price')}"
        strategy = (p.get("strategy") or p.get("kind") or "?").replace("_", " ")
        embed.add_field(
            name=f"{p.get('ticker')} — {strategy}",
            value=f"{entry}\n{bounds}\n{in_trade}  •  id `{p.get('trade_id')}`",
            inline=False,
        )
    if len(items) > len(shown):
        embed.set_footer(text=f"+{len(items) - len(shown)} more — Discord "
                              "caps an embed at 25 fields.")
    return embed


def _custom_id(action: str, trade_id: str) -> str:
    """The stable component id the persistent buttons round-trip through:
    'adit:approve:<trade_id>' / 'adit:reject:<trade_id>'."""
    return f"adit:{action}:{trade_id}"


_DECISION_TEMPLATE = r"adit:(?P<action>approve|reject):(?P<trade_id>[^:]+)"


class WhyModal(discord.ui.Modal):
    """The one-line 'why' prompt (same question the terminal asks). The
    reason is optional -- blank journals the bridge's default note."""

    why = discord.ui.TextInput(label="Why? (optional, one line)",
                               required=False, max_length=200)

    def __init__(self, action: str, trade_id: str, origin: discord.Message = None):
        title = ("Approve on paper" if action == "approve" else "Reject (skip)")
        super().__init__(title=f"{title} — {trade_id}"[:45])
        self._action = action
        self._trade_id = trade_id
        self._origin = origin

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            status, body = await asyncio.to_thread(
                _decide, self._trade_id, self._action, str(self.why.value or "").strip())
        except Exception as e:
            await interaction.followup.send(f"Gateway unreachable: {e}")
            return

        if status == 200:
            marker = "✅" if body.get("decision") == "approved" else "❌"
            note = (f"{marker} **{body.get('decision', '').upper()}** "
                    f"(`{self._trade_id}`) — journaled. "
                    + ("The plan tracker manages the exit from here."
                       if body.get("decision") == "approved"
                       else "The tracker will score the skip."))
        elif status == 404:
            note = (f"🤷 `{self._trade_id}` has no pending entry anymore — "
                    "it was probably already decided elsewhere.")
        elif status == 409:
            note = (f"⏱️ Too late — the tracker already resolved "
                    f"`{self._trade_id}` hypothetically. Left as-is "
                    "(no hindsight approvals).")
        else:
            note = (f"⚠️ Gateway said HTTP {status}: "
                    f"{body.get('error', 'unknown error')}")

        # Retire the buttons on the original proposal message (best-effort;
        # the journal is already correct even if this edit fails).
        origin = self._origin or interaction.message
        if origin is not None and status in (200, 404, 409):
            try:
                await origin.edit(content=f"{origin.content}\n\n{note}", view=None)
            except Exception:
                pass
        await interaction.followup.send(note)


class DecisionButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=_DECISION_TEMPLATE):
    """A persistent Approve/Reject button. Its state lives entirely in the
    custom_id, so taps still work after the bot restarts (Discord replays
    the id and this class is rebuilt from the regex template)."""

    def __init__(self, action: str, trade_id: str):
        self.action = action
        self.trade_id = trade_id
        super().__init__(discord.ui.Button(
            label="Approve (paper)" if action == "approve" else "Reject",
            style=(discord.ButtonStyle.success if action == "approve"
                   else discord.ButtonStyle.danger),
            emoji="✅" if action == "approve" else "❌",
            custom_id=_custom_id(action, trade_id),
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], match["trade_id"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            WhyModal(self.action, self.trade_id, origin=interaction.message))


def _pending_view(trade_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DecisionButton("approve", trade_id))
    view.add_item(DecisionButton("reject", trade_id))
    return view


# ---------------------------------------------------------------------- bot

intents = discord.Intents.default()
intents.message_content = True  # needs Message Content Intent in the portal

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
# Persistent buttons: taps on Approve/Reject keep working across bot
# restarts because the trade_id round-trips through the custom_id.
client.add_dynamic_items(DecisionButton)


@client.event
async def on_ready():
    # Guild-scoped sync makes /analyze usable immediately in your server
    # (a plain global sync can take up to an hour to appear).
    for guild in client.guilds:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    names = ", ".join(g.name for g in client.guilds) or "NO SERVERS YET"
    print(f"Logged in as {client.user} -- serving {len(client.guilds)} "
          f"server(s): {names}")
    if not client.guilds:
        print("The bot isn't in any server. Open the OAuth2 invite URL and "
              "add it to your private server, then restart.")


@tree.command(name="analyze",
              description="Forecast the watchlist (or one stock): bias, confidence, drivers")
@app_commands.describe(ticker="Optional: one stock, e.g. ONGC or ONGC.NS (default: whole watchlist)")
async def analyze(interaction: discord.Interaction, ticker: str = None):
    # Fetching a year of prices per ticker takes well over Discord's 3-second
    # answer window, so acknowledge first and follow up when done.
    await interaction.response.defer(thinking=True)
    tickers = [_normalize_ticker(ticker)] if ticker else load_tickers()
    if not tickers:
        await interaction.followup.send("The watchlist is empty -- add stocks "
                                        "in config/watchlist.yaml.")
        return
    try:
        lines = await asyncio.to_thread(_run_forecasts, tickers)
    except Exception as e:
        await interaction.followup.send(f"Forecast failed: {e}")
        return
    for chunk in _chunk(lines):
        await interaction.followup.send(chunk)


@tree.command(name="pending",
              description="Pending trade proposals with Approve/Reject buttons (paper only)")
async def pending(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        items = await asyncio.to_thread(_fetch_pending)
    except Exception as e:
        await interaction.followup.send(
            f"Can't reach the gateway at {_bridge_base_url()}: {e}\n"
            "(Is the alpha-trading service running, and API_KEY set in .env "
            "on this machine?)")
        return
    if not items:
        await interaction.followup.send(
            "No pending proposals right now — the market loop will alert "
            "here when it journals one.")
        return
    for p in items:
        await interaction.followup.send(_format_pending(p),
                                        view=_pending_view(p["trade_id"]))


def _fetch_pnl() -> dict:
    """GET /api/discord/pnl -> the realized + live-marked P&L card."""
    status, body = _bridge_call("GET", "/api/discord/pnl")
    if status != 200:
        raise RuntimeError(f"gateway answered {status}: {body[:200]}")
    data = json.loads(body)
    return data.get("card") or {}


@tree.command(name="pnl",
              description="P&L now: realized (banked) + live marked on open positions (paper)")
async def pnl(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        card = await asyncio.to_thread(_fetch_pnl)
    except Exception as e:
        await interaction.followup.send(
            f"Can't reach the gateway at {_bridge_base_url()}: {e}\n"
            "(Is the alpha-trading service running, and API_KEY set in .env "
            "on this machine?)")
        return
    await interaction.followup.send(card.get("text") or
                                    "(no P&L data available)")


@tree.command(name="positions",
              description="Open paper positions: entry, target/stop, time in trade (read-only)")
async def positions(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        items = await asyncio.to_thread(_fetch_positions)
    except Exception as e:
        await interaction.followup.send(
            f"Can't reach the gateway at {_bridge_base_url()}: {e}\n"
            "(Is the alpha-trading service running, and API_KEY set in .env "
            "on this machine?)")
        return
    if not items:
        await interaction.followup.send(
            "No open paper positions right now — everything is settled.")
        return
    await interaction.followup.send(embed=_positions_embed(items))


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return
    # Chat only when clearly addressed: a reply to one of the bot's
    # messages, or an @mention. Ordinary server chatter is left alone.
    is_reply_to_bot = (
        message.reference is not None
        and message.reference.resolved is not None
        and getattr(message.reference.resolved, "author", None) == client.user
    )
    if not is_reply_to_bot and not client.user.mentioned_in(message):
        return

    text = message.clean_content.replace(f"@{client.user.name}", "").strip()
    if not text:
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        await message.reply("I can run /analyze, but chat needs GEMINI_API_KEY "
                            "set in .env on the machine running me.")
        return

    async with message.channel.typing():
        try:
            answer = await asyncio.to_thread(_ask_gemini, text, api_key)
        except Exception as e:
            await message.reply(f"Sorry, my brain (Gemini) didn't answer: {e}")
            return
    for chunk in _chunk(answer.split("\n\n")):
        await message.reply(chunk)


def run() -> None:
    _load_env()
    deploy_log.record_startup("discord_bot")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in .env -- see "
                         "VISION_PLAN.md Phase 5 for how to create one.")
    print("Starting the ADiTrader Analyst bot (Ctrl+C to stop)...")
    try:
        client.run(token)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "\nDiscord refused the connection because 'Message Content "
            "Intent' is switched OFF for this bot.\nFix: "
            "https://discord.com/developers/applications -> your app -> "
            "Bot tab -> turn ON 'Message Content Intent' -> Save, then "
            "run this again."
        )
    except discord.LoginFailure:
        raise SystemExit(
            "\nDiscord rejected the token in .env (DISCORD_BOT_TOKEN). "
            "Get a fresh one: Developer Portal -> Bot tab -> Reset Token, "
            "then update .env."
        )


if __name__ == "__main__":
    run()
