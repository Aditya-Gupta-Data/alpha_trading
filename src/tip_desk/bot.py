"""The #tip-desk lane of the Discord bot (decision #189). Listens only in TIP_DESK_CHANNEL_ID and only to the owner
(AUTHORIZED_DISCORD_USER_ID); buffers split tips for 120 s; hands every message to the gateway's /api/tipdesk/*
and renders the answer. It holds no trade state and imports no engine module — like the /pending buttons, the
only way it changes anything is an authenticated gateway call. Buttons: [Confirm Paper] [Cancel] on a card,
[Exit Now] per open tip on an exit prompt. There is no live button (RULE 7)."""
from __future__ import annotations

import asyncio
import os
import time

import discord

from src.tip_desk import IGNORED_KINDS, TRADE_KINDS
from src.tip_desk import card as card_mod
from src.tip_desk.buffer import TipBuffer

_HOOKS = {"bridge_call": None, "is_owner": None}
_BUFFER = {"buf": None}
CARD_MESSAGES = {}          # group_id -> the card's Discord message (in memory: edits only, a restart replies anew)
TIP_TEMPLATE = r"tip:(?P<action>confirm|cancel|exit):(?P<ref>.+)"
STATUS_RETIRES = {"cancelled", "confirmed", "expired", "paper_only", "not_found"}


def configure(bridge_call, is_owner) -> None:
    _HOOKS.update(bridge_call=bridge_call, is_owner=is_owner)


def channel_id() -> int | None:
    raw = str(os.environ.get("TIP_DESK_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def is_tip_channel(ch_id) -> bool:
    cid = channel_id()
    try:
        return cid is not None and int(ch_id) == cid
    except (TypeError, ValueError):
        return False


def buffer() -> TipBuffer:
    if _BUFFER["buf"] is None:
        from src import config
        _BUFFER["buf"] = TipBuffer(float(config.TIP_DESK["buffer_seconds"]))
    return _BUFFER["buf"]


def plan_ingest(group: dict, message_text: str) -> dict:
    """What to send the gateway: the joined text when the group is (or became) a tip worth a card or a reject,
    else this message alone, so the ledger logs it once and an unchanged card is not re-quoted."""
    from src.tip_desk.calls import direction_of
    kind = group["parsed"]["kind"]
    joined = kind not in IGNORED_KINDS and not (kind in TRADE_KINDS and not group["changed"])
    if kind in IGNORED_KINDS and len(group["message_ids"]) > 1 and direction_of(group["text"])[0]:
        joined = True                      # a direction split over two chatter lines is still a call (Arm 2)
    return {"group_id": group["group_id"], "text": group["text"] if joined else message_text,
            "message_ids": group["message_ids"], "joined": joined}


def embed_for(card: dict) -> discord.Embed:
    view = card_mod.trade_card(card)
    color = 0xE67E22 if view["stale"] else (0x2ECC71 if view["can_confirm"] else 0x95A5A6)
    return discord.Embed(title=view["title"][:256], description="\n".join(view["lines"])[:4000], color=color)


class TipButton(discord.ui.DynamicItem[discord.ui.Button], template=TIP_TEMPLATE):
    """Persistent: the action and its card/tip id live in the custom_id, so taps survive a bot restart."""

    LABELS = {"confirm": ("Confirm Paper", discord.ButtonStyle.success, "✅"),
              "cancel": ("Cancel", discord.ButtonStyle.secondary, "✖️"),
              "exit": ("Exit Now", discord.ButtonStyle.danger, "🚪")}

    def __init__(self, action: str, ref: str, label: str = None):
        self.action, self.ref = action, ref
        text, style, emoji = self.LABELS[action]
        super().__init__(discord.ui.Button(label=(label or text)[:80], style=style, emoji=emoji,
                                           custom_id=f"tip:{action}:{ref}"[:100]))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], match["ref"])

    async def callback(self, interaction: discord.Interaction):
        if not _HOOKS["is_owner"](getattr(interaction.user, "id", None)):
            await interaction.response.send_message("Only the owner can use the tip desk.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        path, payload = {"confirm": ("/api/tipdesk/confirm", {"card_id": self.ref, "mode": "paper"}),
                         "cancel": ("/api/tipdesk/cancel", {"card_id": self.ref}),
                         "exit": ("/api/tipdesk/exit", {"group_id": self.ref})}[self.action]
        try:
            status, body = await asyncio.to_thread(_HOOKS["bridge_call"], "POST", path, payload)
        except Exception as e:
            await interaction.followup.send(f"Gateway unreachable: {e}")
            return
        await interaction.followup.send(result_note(self.action, status, body))
        retire = bool(body.get("ok")) or body.get("status") in STATUS_RETIRES
        if retire and self.action != "exit" and interaction.message is not None:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass


def result_note(action: str, status: int, body: dict) -> str:
    body = body if isinstance(body, dict) else {}
    if status != 200:
        return f"⚠️ Gateway answered HTTP {status}: {body.get('error') or body}"
    if action == "confirm":
        return card_mod.opened_text(body) if body.get("ok") else f"❌ Not filled: {body.get('reason')}"
    if action == "cancel":
        return "✖️ Card cancelled — logged, nothing traded." if body.get("ok") else f"❌ {body.get('reason')}"
    if not body.get("ok"):
        return f"❌ Not exited: {body.get('reason')}"
    parts = [f"{c['journal_ref'].rsplit(':', 1)[-1]} P&L Rs.{c['pnl']:+,.0f}" for c in body.get("closed") or []
             if c.get("closed")]
    return "🚪 Exited (paper): " + (", ".join(parts) or "nothing left open")


def card_view(card_id: str, can_confirm: bool) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    if can_confirm:
        v.add_item(TipButton("confirm", card_id))
    v.add_item(TipButton("cancel", card_id))
    return v


def exit_view(positions: list) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for i, p in enumerate(positions[:25], 1):
        v.add_item(TipButton("exit", p["group_id"], label=f"Exit #{i}"))
    return v


async def handle_message(message: discord.Message) -> None:
    if not _HOOKS["is_owner"](getattr(message.author, "id", None)):
        return
    group = buffer().add(message.id, message.content or "", now=time.monotonic())
    req = plan_ingest(group, message.content or "")
    try:
        status, body = await asyncio.to_thread(_HOOKS["bridge_call"], "POST", "/api/tipdesk/ingest",
                                               {k: req[k] for k in ("group_id", "text", "message_ids")})
    except Exception as e:
        await message.reply(f"Tip desk gateway unreachable: {e}")
        return
    if status != 200 or not isinstance(body, dict):
        await message.reply(f"⚠️ Tip desk gateway answered HTTP {status}.")
        return
    action = body.get("action")
    if action == "ignore":
        await _react(message, "🔇")
    elif action == "reject":
        await _react(message, "⛔")
        await message.reply(card_mod.unsafe_text(body))
    elif action == "exit_prompt":
        positions = body.get("positions") or []
        text = "**Exit signal** — open TIP_SANDBOX positions:\n" + "\n".join(card_mod.exit_lines(positions))
        await message.reply(text[:2000], view=exit_view(positions) if positions else None)
    elif action == "levels_updated":
        p = body.get("parsed") or {}
        tg = " / ".join(f"{t:g}" for t in p.get("targets") or []) or "—"
        await message.reply(f"🔧 Levels applied to the open position: SL {p.get('sl')}, targets {tg}.")
    elif action in ("card", "refuse"):
        await _post_card(message, body)


async def _post_card(message: discord.Message, body: dict) -> None:
    card = body.get("card") or {}
    gid = body.get("group_id")
    if not card.get("sizing") and body.get("action") == "refuse":
        content, embed, view = card_mod.refusal_text(body), None, None
    else:
        embed = embed_for(card)
        view = card_view(card["card_id"], body.get("action") == "card")
        content = None
    prior = CARD_MESSAGES.get(gid)
    if prior is not None and body.get("is_update"):
        try:
            await prior.edit(content=content, embed=embed, view=view)
            await _react(message, "🔧")
            return
        except Exception:
            pass
    sent = await message.reply(content=content, embed=embed, view=view)
    CARD_MESSAGES[gid] = sent
    while len(CARD_MESSAGES) > 200:
        CARD_MESSAGES.pop(next(iter(CARD_MESSAGES)))


async def _react(message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except Exception:
        pass
