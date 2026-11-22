"""
The event book's option universe (owner ruling 2026-11-23 "widen the lists", decision #191). PAPER_EVENTS only.

Core = the 4 indices + 5 stocks in `options_proposer` (unchanged, still fetched by name). Extension = every F&O
stock in `data/fo_liquidity.json` whose tier is in `event_engine.universe_tiers`, not banned, with a Dhan id
(`dhan_client.SECURITY_ID_MAP`, else `data/darling_ids.json`). Lot sizes come from the exchange (the snapshot's
`lots` per expiry); a stock is a physically settled stock option, so it gets the stock entry floor and the 7-day
forced exit. Nothing here is read by the primary books.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.event_engine import ROOT

FO_PATH = ROOT / "data" / "fo_liquidity.json"
IDS_PATH = ROOT / "data" / "darling_ids.json"
INDEX_ALIASES = {"NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "BANKNIFTY": "NIFTY BANK",
                 "FINNIFTY": "NIFTY FIN SERVICE", "MIDCPNIFTY": "NIFTY MID SELECT"}


def _testing() -> bool:
    import os
    return "PYTEST_CURRENT_TEST" in os.environ


def _read(path) -> dict:
    if path is None:
        return {}
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_fo(path=None) -> dict:
    """The shipped liquidity snapshot; empty under pytest unless a path is given (no production data in tests)."""
    return _read(path or (None if _testing() else FO_PATH))


def load_ids(path=None) -> dict:
    return _read(path or (None if _testing() else IDS_PATH)).get("ids") or {}


def bare(ticker: str) -> str:
    t = str(ticker or "").strip().upper().removesuffix(".NS")
    return INDEX_ALIASES.get(t, t)


def security_id(sym: str, ids: dict = None) -> str | None:
    """The Dhan NSE_EQ id for a stock: the hand-verified map first, then the scrip-master build. Tier-free,
    so an open position can always be marked."""
    from src.dhan_client import SECURITY_ID_MAP
    row = SECURITY_ID_MAP.get(f"{sym}.NS")
    if row and row.get("seg") == "NSE_EQ":
        return str(row["id"])
    rec = (load_ids() if ids is None else ids).get(sym) or {}
    return str(rec["id"]) if rec.get("id") else None


def extension_symbols(cfg: dict, fo: dict = None) -> set:
    """Configured-tier, non-banned F&O stock symbols (bare), freshness not checked (the node's text filter)."""
    fo = load_fo() if fo is None else fo
    tiers = set(cfg.get("universe_tiers") or ())
    banned = {str(b).upper() for b in fo.get("banned") or []}
    return {str(s).upper() for s, r in (fo.get("symbols") or {}).items()
            if isinstance(r, dict) and r.get("tier") in tiers and str(s).upper() not in banned}


def resolve(ticker: str, cfg: dict, today: date, fo: dict = None, ids: dict = None) -> dict:
    """{ok, underlying, kind: index|core|extension, stock, lot, lots, security_id, reason}."""
    from src.options_proposer import EQUITY_OPTION_UNDERLYINGS, LOT_SIZES
    t = bare(ticker)
    if t in LOT_SIZES:
        return {"ok": True, "underlying": t, "kind": "index", "stock": False, "lot": int(LOT_SIZES[t]),
                "lots": {}, "security_id": None, "reason": None}
    fo = load_fo() if fo is None else fo
    row = (fo.get("symbols") or {}).get(t) or {}
    lots = {str(k): int(v) for k, v in (row.get("lots") or {}).items()}
    if f"{t}.NS" in EQUITY_OPTION_UNDERLYINGS:
        return {"ok": True, "underlying": f"{t}.NS", "kind": "core", "stock": True,
                "lot": int(EQUITY_OPTION_UNDERLYINGS[f"{t}.NS"]), "lots": lots, "security_id": None, "reason": None}
    no = {"ok": False, "underlying": None, "kind": None, "stock": True, "lot": None, "lots": {},
          "security_id": None}
    if not row:
        return dict(no, reason=f"{t} has no option contract in the VM's option universe")
    banned = {str(b).upper() for b in fo.get("banned") or []}
    if t in banned or row.get("tier") == "banned":
        return dict(no, reason=f"{t} is in the F&O ban list")
    if row.get("tier") not in set(cfg.get("universe_tiers") or ()):
        return dict(no, reason=f"{t} is {row.get('tier')}, outside event_engine.universe_tiers")
    try:
        age = (today - date.fromisoformat(str(fo.get("as_of"))[:10])).days
    except ValueError:
        age = None
    max_age = int(cfg.get("universe_max_age_days", 5))
    if age is None or age > max_age:
        return dict(no, reason=f"fo_liquidity.json is {age} days old (max {max_age}) — ban list not trusted")
    sid = security_id(t, ids)
    if not sid:
        return dict(no, reason=f"{t} has no Dhan security id (scrip master)")
    return {"ok": True, "underlying": f"{t}.NS", "kind": "extension", "stock": True, "lot": None, "lots": lots,
            "security_id": sid, "reason": None}


def lot_for(meta: dict, expiry: str) -> tuple:
    """(lot, reason): the exchange lot for that expiry; a core name falls back to its hard-coded lot."""
    lot = (meta.get("lots") or {}).get(str(expiry)[:10])
    if lot:
        return int(lot), None
    if meta.get("lot"):
        return int(meta["lot"]), None
    return None, f"no exchange lot size for {meta.get('underlying')} {expiry}"


def is_extension_stock(underlying: str) -> bool:
    from src.options_proposer import EQUITY_OPTION_UNDERLYINGS, LOT_SIZES
    u = str(underlying or "").upper()
    return u.endswith(".NS") and u not in EQUITY_OPTION_UNDERLYINGS and bare(u) not in LOT_SIZES


def floor_days(underlying: str) -> int:
    from src.options_proposer import EQUITY_MIN_DAYS_TO_EXPIRY, min_days_to_expiry_for
    return int(EQUITY_MIN_DAYS_TO_EXPIRY if is_extension_stock(underlying) else min_days_to_expiry_for(underlying))


def in_forced_exit_window(underlying: str, expiry, today) -> bool:
    """The house predicate, with an extension stock held to the stock-option window (physical settlement)."""
    from src.plan_tracker import _as_date, in_forced_exit_window as house
    if is_extension_stock(underlying):
        from src.options_proposer import EQUITY_FORCED_EXIT_DAYS
        if (_as_date(expiry) - _as_date(today)).days <= EQUITY_FORCED_EXIT_DAYS:
            return True
    return house(underlying, expiry, today)


def expiries(underlying: str) -> list:
    from src import dhan_client
    if is_extension_stock(underlying):
        sid = security_id(bare(underlying))
        return (dhan_client.get_expiry_list_by_id(sid, "NSE_EQ") or []) if sid else []
    return dhan_client.get_expiry_list(underlying) or []


def chain(underlying: str, expiry: str):
    """The live chain through the one data door; raises ChainUnavailable on an empty answer."""
    from src.execution import live_pricer
    if not is_extension_stock(underlying):
        return live_pricer._default_chain(underlying, expiry)
    if live_pricer._is_test_env():
        return None
    from src.dhan_client import get_option_chain_by_id, last_chain_error
    sid = security_id(bare(underlying))
    if not sid:
        raise live_pricer.ChainUnavailable(f"{underlying} has no Dhan security id")
    got = get_option_chain_by_id(sid, expiry, "NSE_EQ")
    if not got:
        raise live_pricer.ChainUnavailable(last_chain_error() or "empty response")
    return got
