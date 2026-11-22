"""The rolling window that joins a tip split across back-to-back messages (bot side, in memory).

A group lives `window_s` from its FIRST message. Each new message is classified alone first:
  * a complete trade (OPTION_BUY / DEFINED_SPREAD) or an EXIT_SIGNAL starts its own group;
  * UNSAFE joins only a group that holds no trade (so "Angle one BSE" + "CE SELL COULD BE DONE" is one
    rejected tip, but a sell line never rewrites a live trade card), then closes it;
  * INCOMPLETE / CHATTER joins the open group, which is re-classified on the joined text — this is how
    "Nifty TODAYS 25400 ce buy @25-28" + "Sl 5 tgt 54 78 110" becomes ONE trade with its levels.
A restart loses the window; the next message just starts a fresh group."""
from __future__ import annotations

from dataclasses import dataclass, field

from src.tip_desk import EXIT_SIGNAL, TRADE_KINDS, UNSAFE_REJECT
from src.tip_desk.parser import classify

LEVEL_KEYS = ("entry_low", "entry_high", "sl", "targets", "legs", "underlying", "expiry_hint")


@dataclass
class Group:
    group_id: str
    started: float
    message_ids: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    parsed: dict = None
    closed: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


class TipBuffer:
    def __init__(self, window_s: float = 120.0):
        self.window_s = float(window_s)
        self.open: Group | None = None

    def _fresh(self, now: float) -> Group | None:
        g = self.open
        if g is None or g.closed or now - g.started > self.window_s:
            return None
        return g

    def add(self, message_id, text: str, now: float, today=None) -> dict:
        """→ {group_id, message_ids, text, parsed, is_update, changed}. `is_update`: the group already
        carried a trade before this message; `changed`: this message changed the trade's levels."""
        alone = classify(text, today)
        g = self._fresh(now)
        starts_new = (alone["kind"] in TRADE_KINDS or alone["kind"] == EXIT_SIGNAL
                      or (alone["kind"] == UNSAFE_REJECT and g is not None and g.parsed
                          and g.parsed["kind"] in TRADE_KINDS))
        if g is None or starts_new:
            g = Group(group_id=str(message_id), started=now)
            self.open = g
        before = g.parsed
        g.message_ids.append(str(message_id))
        g.texts.append(str(text or ""))
        g.parsed = classify(g.text, today)
        was_trade = bool(before and before["kind"] in TRADE_KINDS)
        if was_trade and g.parsed["kind"] not in TRADE_KINDS:
            g.texts.pop()                      # a follow-up that would un-make the trade is not joined to it
            g.parsed = before
        if g.parsed["kind"] in (UNSAFE_REJECT, EXIT_SIGNAL):
            g.closed = True
        changed = (g.parsed["kind"] in TRADE_KINDS
                   and (not was_trade or any(before.get(k) != g.parsed.get(k) for k in LEVEL_KEYS)))
        return {"group_id": g.group_id, "message_ids": list(g.message_ids), "text": g.text,
                "parsed": g.parsed, "is_update": was_trade, "changed": changed}
