"""
Alpha Trading — Chat Agent: the ADiTrader Discord reasoning mirror
=================================================================

An async Discord bot that listens for @ADiTrader mentions and routes
read-only reasoning questions through local Ollama (llama3). Every
request is a fully stateless transaction — no conversation history
accumulates in RAM.

Zero execution or write pathways: this module imports only src.brain_map
(read-only) and never touches portfolio, journal, trade, strategy, or
any broker module.

Setup (.env):
  DISCORD_BOT_TOKEN          — Discord Developer Portal bot token
  AUTHORIZED_DISCORD_USER_ID — integer user ID; all other traffic silently ignored
  OLLAMA_BASE_URL            — optional, default http://localhost:11434

Run:
  python3 -m src.chat_agent
"""

import os
import sqlite3
from pathlib import Path

import certifi

# SSL cert path must be set before discord.py is imported (aiohttp builds its
# SSL context at import time — same fix as discord_bot.py).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import discord
import httpx

from src import brain_map

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
_DB_PATH = ROOT / "data" / "brain_map.db"

_SYSTEM_PROMPT = (
    "You are the ADiTrader reasoning mirror. "
    "Ground responses directly in this factual data: {context}. "
    "Be brief, quantitative, and blunt."
)  # 23 words — well under the 100-word ceiling


def _load_env() -> None:
    """Self-contained .env reader — same pattern as every other entry point
    in this project (modularity rule: no shared loader import)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_env()

_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

_raw_uid = os.environ.get("AUTHORIZED_DISCORD_USER_ID", "")
if not _raw_uid.strip().isdigit():
    raise RuntimeError(
        "AUTHORIZED_DISCORD_USER_ID must be set to a Discord user integer ID in .env"
    )
_AUTHORIZED_USER_ID = int(_raw_uid.strip())


# ---------------------------------------------------------------------------
# Context extraction — the only place this module touches the database
# ---------------------------------------------------------------------------

def _context_from_conn(conn: sqlite3.Connection) -> str:
    """Build a dense terse-CSV context string from an open Brain Map connection.

    Pulls:
      • 3 most-recent simulated_trades rows (ordered by proposed_on DESC)
      • up to 3 active graph_edges rows (invalid_at IS NULL when that column
        exists; falls back to an unconditional LIMIT 3 if the column is absent)

    Returns "(no data)" for an empty or schema-less DB. Never raises.
    """
    lines: list[str] = []

    # --- simulated_trades ---
    try:
        rows = conn.execute(
            "SELECT underlying, strategy, result, pnl_net, r_multiple, proposed_on "
            "FROM simulated_trades ORDER BY proposed_on DESC LIMIT 3"
        ).fetchall()
        for r in rows:
            r_val = f"{r['r_multiple']:.2f}" if r["r_multiple"] is not None else "n/a"
            lines.append(
                f"sim|{r['underlying']}|{r['strategy']}|{r['result']}"
                f"|pnl={r['pnl_net']:.0f}|r={r_val}|on={r['proposed_on']}"
            )
    except sqlite3.OperationalError:
        pass  # table absent — degrade silently

    # --- graph_edges (prefer invalid_at filter; fall back if column missing) ---
    for sql in (
        "SELECT source_node, relation, target_node, confidence_score "
        "FROM graph_edges WHERE invalid_at IS NULL LIMIT 3",
        "SELECT source_node, relation, target_node, confidence_score "
        "FROM graph_edges LIMIT 3",
    ):
        try:
            edges = conn.execute(sql).fetchall()
            for e in edges:
                conf = (
                    f"{e['confidence_score']:.2f}"
                    if e["confidence_score"] is not None
                    else "n/a"
                )
                lines.append(
                    f"edge|{e['source_node']}|{e['relation']}"
                    f"|{e['target_node']}|conf={conf}"
                )
            break
        except sqlite3.OperationalError:
            continue

    return "\n".join(lines) if lines else "(no data)"


def fetch_agent_context(query: str, db_path=None) -> str:
    """Return a dense terse-CSV context string built from the 3 most recent
    simulated_trades entries and active graph_edges rows.

    `query` is accepted for API symmetry but is not used for DB filtering —
    the function always returns the latest snapshot. Pass `db_path` to target
    a specific file (or ':memory:' in tests — but prefer injecting via
    _context_from_conn directly for in-memory test DBs with pre-seeded data).
    """
    path = str(db_path or _DB_PATH)
    if path != ":memory:" and not Path(path).exists():
        return "(no data)"
    try:
        conn = brain_map.connect(path)
    except Exception:
        return "(no data)"
    try:
        return _context_from_conn(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ollama inference — stateless, one HTTP round-trip per call, no history
# ---------------------------------------------------------------------------

async def _call_ollama(prompt: str, context: str) -> str:
    system = _SYSTEM_PROMPT.format(context=context)
    payload = {
        "model": "llama3",
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{_OLLAMA_BASE}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------

class ChatAgent(discord.Client):
    async def on_ready(self) -> None:
        print(f"[chat_agent] logged in as {self.user} — read-only reasoning mirror active")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.author.id != _AUTHORIZED_USER_ID:
            return  # silently drop unauthorized traffic
        if self.user not in message.mentions:
            return  # only respond to @mentions

        query = message.content.replace(f"<@{self.user.id}>", "").strip()
        if not query:
            return

        async with message.channel.typing():
            context = fetch_agent_context(query)
            try:
                reply = await _call_ollama(query, context)
            except Exception as exc:
                reply = f"[Ollama error: {exc}]"

        # Discord hard cap: 2 000 characters per message
        await message.reply(reply[:2000])


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in .env")
    intents = discord.Intents.default()
    intents.message_content = True
    ChatAgent(intents=intents).run(token)


if __name__ == "__main__":
    main()
