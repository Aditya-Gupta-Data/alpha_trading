"""
Alpha Trading -- Phase 5 (step 1): the Discord Analyst bot
===========================================================

Puts the analyst on your phone: a Discord bot that answers a /analyze
slash command with the Phase 4E forecast (technicals + news sentiment),
and holds a plain conversation when you reply to it (replies go to Gemini,
the same free-tier model the news processor uses).

STRICT GUARDRAIL (VISION_PLAN.md): this bot is read-only on the system.
It imports the forecast layer to ANSWER QUESTIONS -- it does not import
portfolio/trade/strategy, cannot execute paper trades, and there is no
broker anywhere in this project. Trade approval stays in the terminal
(`python3 -m src.trade`) until a later Phase 5 step deliberately moves it.

Commands:
  /analyze              -- forecast every watchlist stock
  /analyze ticker:ONGC  -- forecast one stock (`.NS` added if omitted)
  (reply to any bot message, or @mention it, to chat -- answered by Gemini)

Setup: DISCORD_BOT_TOKEN in .env (from the Discord Developer Portal, with
Message Content Intent enabled); GEMINI_API_KEY in .env for the chat side
(without it, the bot still runs -- /analyze works, chat replies apologize).
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
            lines.append(f"{ticker}: not enough price history to forecast "
                         f"(needs 200+ trading days on Yahoo Finance).")
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
        "the tool is paper-only and trades are approved in a terminal "
        "session, not by you. If asked for financial advice, share the "
        "data-driven read but remind them it's their call.\n\n"
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


# ---------------------------------------------------------------------- bot

intents = discord.Intents.default()
intents.message_content = True  # needs Message Content Intent in the portal

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


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
