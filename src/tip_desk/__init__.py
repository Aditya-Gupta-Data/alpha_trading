"""
src/tip_desk — the #tip-desk: owner-pasted WhatsApp tips → a 1-tap PAPER card on PAPER_TIP_SANDBOX (decision #189).

    parser.py    text → one of OPTION_BUY / DEFINED_SPREAD / EXIT_SIGNAL / UNSAFE_REJECT / INCOMPLETE / CHATTER
    buffer.py    the 120 s rolling window that joins a tip split across messages (bot side, in memory)
    resolver.py  parsed tip → a live Dhan contract (expiry list + chain) and its crossed ask
    sizing.py    lots by TOTAL PREMIUM (the premium paid is the max loss), the sandbox cap, the stale-price flag
    store.py     the append-only tip ledger + cards + positions (tables in brain_map.db)
    desk.py      ingest / confirm / cancel / exit — what the gateway endpoints call
    manager.py   VM loop: stop loss, T1/T2/T3 booking + trail, 15:15 square-off on expiry day
    card.py      pure text for the Discord card

Paper only (RULE 7): there is no order path; a confirm is a paper fill at the live ask. The book is ISOLATED:
its own account on the paper-account door, never a shadow of the primary, no daily breaker (#189), and a lock
with no journal row never fills the #68 exposure slot, so nothing here can block or halt another book.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

ACCOUNT = "PAPER_TIP_SANDBOX"
IST = timezone(timedelta(hours=5, minutes=30))

OPTION_BUY = "OPTION_BUY"
DEFINED_SPREAD = "DEFINED_SPREAD"
EXIT_SIGNAL = "EXIT_SIGNAL"
UNSAFE_REJECT = "UNSAFE_REJECT"
INCOMPLETE = "INCOMPLETE"
CHATTER = "CHATTER"
TRADE_KINDS = frozenset({OPTION_BUY, DEFINED_SPREAD})
IGNORED_KINDS = frozenset({INCOMPLETE, CHATTER})


def settings() -> dict:
    from src import config
    return dict(config.TIP_DESK)


def now_ist() -> datetime:
    return datetime.now(IST)
