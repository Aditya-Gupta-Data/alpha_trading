"""
The event card — the one artifact the node ships and the VM executes (playbook "Output format").
A card that fails a gate is still written: the engine learns which events mattered.

Suggestions appear only for exposures that pass materiality AND get a priced-in direction. Each
carries what the VM needs and nothing the VM must recompute with an LLM: ticker, direction,
horizon, the strike rules for both A/B paths, and the source tags of every number behind it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from src.event_engine import CARD_DIR, CARD_SCHEMA, IST, TAG_LLM, settings


def suggestions_for(exposures: list, materials: list, priced: list, cfg: dict = None) -> list:
    """One suggestion per exposure that passes materiality AND got a priced-in direction."""
    cfg = cfg or settings()
    by_ticker = {m["ticker"]: m for m in materials}
    pin = {p["ticker"]: p for p in priced}
    suggestions = []
    for e in exposures:
        m, p = by_ticker.get(e["ticker"]), pin.get(e["ticker"])
        if not (m and m.get("passes_gate") and p and p.get("direction")):
            continue
        suggestions.append({
            "ticker": e["ticker"], "direction": p["direction"],
            "instrument": "long_otm_" + ("call" if p["direction"] == "bullish" else "put"),
            "horizon_days": int(e.get("horizon_days") or cfg["horizon_days_default"]),
            "strike_paths": {"fixed": {"otm_pct": float(cfg["fixed_otm_pct"])},
                             "llm": {"price_response_pct": e.get("price_response_pct"),
                                     "source": e.get("price_response_source") or TAG_LLM}},
            "max_loss_per_trade_rs": float(cfg["max_loss_per_trade_rs"]),
            "basis": {"materiality_pct": m["effect_pct"], "materiality_source": m["source"],
                      "materiality_basis": m.get("basis", "revenue"),
                      "priced_in": p["verdict"], "event_move_pct": p["event_move_pct"],
                      "justified_move_pct": p["justified_move_pct"]},
            "invalidation": f"premium is the stop; exit by the pre-expiry rule, at {cfg['take_profit_multiple']:g}x, "
                            f"or after {int(e.get('horizon_days') or cfg['horizon_days_default'])} days",
            "confidence": 0.0,
        })
    return suggestions


def build(item: dict, event: dict, ex: dict, materials: list, priced: list, cfg: dict = None,
          second_order: dict = None) -> dict:
    cfg = cfg or settings()
    suggestions = suggestions_for(ex.get("exposures", []), materials, priced, cfg)
    from src.event_engine.detect import item_id
    eid = f"{(event.get('type') or 'event')}-{item_id(item)}"
    card = {"schema": CARD_SCHEMA, "event_id": eid,
            "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "source": {"title": item.get("title"), "url": item.get("url"), "from": item.get("source"),
                       "published": item.get("published"),
                       "text": (item.get("text") or "")[:6000]},
            "type": event.get("type"), "stage": event.get("stage"), "issuer": event.get("issuer"),
            "announced_on": event.get("announced_on"), "key_dates": event.get("key_dates") or {},
            "mechanics": ex.get("mechanics", []), "exposure": ex.get("exposures", []),
            "materiality": materials, "priced_in": priced, "suggestions": suggestions,
            "flags": ex.get("flags", []), "gates": {"materiality_threshold_pct": cfg["materiality_threshold_pct"]}}
    if second_order:
        card["second_order_read"] = second_order.get("line") or ""
        card["second_order_nodes"] = second_order.get("nodes") or []
    return card


def validate(card: dict) -> list:
    """Problems that make a card unexecutable (empty list = fine)."""
    probs = []
    if not isinstance(card, dict):
        return ["not a JSON object"]
    if card.get("schema") != CARD_SCHEMA:
        probs.append(f"schema {card.get('schema')!r} is not {CARD_SCHEMA}")
    if not card.get("event_id"):
        probs.append("no event_id")
    for s in card.get("suggestions") or []:
        if s.get("direction") not in ("bullish", "bearish"):
            probs.append(f"{s.get('ticker')}: direction {s.get('direction')!r}")
        if not s.get("ticker"):
            probs.append("a suggestion without a ticker")
    return probs


def write(card: dict, card_dir=None) -> Path:
    root = Path(card_dir or CARD_DIR)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{card['event_id']}.json"
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(card, indent=1, default=str))
    os.replace(tmp, path)
    return path
