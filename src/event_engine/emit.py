"""
The node half: news/inbox → classify → exposure → materiality → priced-in → card → ship to the VM.

    python -m src.event_engine.emit [--no-ship] [--inbox-only]

Runs on the Mini PC (cron, node-only lines). It never trades. A card is shipped through the ONE
node→VM artifact lane (`firm_treasury.vm_push_file`) as `data/event_card__<id>.json`, which the VM
executor reads. When the LLM is unavailable or its daily cap is spent, the run stops and the item is
NOT marked seen, so it is retried next run — abstain, never invent.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from email.utils import parsedate_to_datetime

from src.event_engine import CARD_DIR, IST, STATE_DIR, settings
from src.event_engine import card as card_mod
from src.event_engine import detect, exposure, materiality, priced_in
from src.event_engine.llm import EventLLM

SHIP_PREFIX = "event_card__"


def _announced(event: dict, item: dict) -> str | None:
    if event.get("announced_on"):
        return str(event["announced_on"])[:10]
    pub = item.get("published") or ""
    try:
        return parsedate_to_datetime(pub).astimezone(IST).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(pub[:19]).date().isoformat()
    except ValueError:
        return None


def ship(path, ship_fn=None) -> bool:
    """Copy the card to its shipping name and push it to the VM's data/ (fail-open)."""
    out = STATE_DIR / "outbox"
    out.mkdir(parents=True, exist_ok=True)
    staged = out / f"{SHIP_PREFIX}{path.stem}.json"
    shutil.copyfile(path, staged)
    if ship_fn is None:
        from src.firm_treasury import vm_push_file as ship_fn
    try:
        return bool(ship_fn(staged))
    except Exception as exc:
        print(f"  (event_engine: ship failed for {path.name}: {exc})")
        return False


def run(items=None, llm=None, bars_fn=None, card_dir=None, do_ship=True, ship_fn=None,
        seen=None, cfg=None, inbox_only=False) -> dict:
    cfg = cfg or settings()
    summary = {"status": "ok", "items": 0, "classified": 0, "cards": [], "shipped": 0,
               "skipped": {}, "stopped": None}
    if not cfg.get("emit_enabled", True):
        summary["status"] = "disabled (event_engine.emit_enabled false)"
        return summary
    llm = llm or EventLLM()
    seen = seen if seen is not None else detect.SeenLedger()
    if items is None:
        items = detect.inbox_items() + ([] if inbox_only else detect.rss_items())
    for item in items:
        iid = detect.item_id(item)
        if iid in seen:
            continue
        summary["items"] += 1
        c = detect.classify(item, llm)
        if not c["ok"]:
            if c["event"] is None and c["reason"] != "no primary text (gate 1: headline only)":
                summary["stopped"] = c["reason"]          # the LLM is down or capped: retry next run
                break
            seen.add(iid)
            summary["skipped"][c["reason"]] = summary["skipped"].get(c["reason"], 0) + 1
            continue
        summary["classified"] += 1
        ex = exposure.extract(item, llm)
        if not ex["ok"]:
            summary["stopped"] = ex["reason"]
            break
        mats = [materiality.assess(e, cfg["materiality_threshold_pct"]) for e in ex["exposures"]]
        announced = _announced(c["event"], item)
        priced = [priced_in.assess(e["ticker"], m, announced, bars_fn=bars_fn, cfg=cfg)
                  for e, m in zip(ex["exposures"], mats)]
        card = card_mod.build(item, dict(c["event"], announced_on=announced), ex, mats, priced, cfg)
        path = card_mod.write(card, card_dir or CARD_DIR)
        summary["cards"].append({"event_id": card["event_id"], "suggestions": len(card["suggestions"])})
        if do_ship and card["suggestions"] and ship(path, ship_fn):
            summary["shipped"] += 1
        seen.add(iid)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Event engine, node half: emit event cards")
    ap.add_argument("--no-ship", action="store_true")
    ap.add_argument("--inbox-only", action="store_true", help="skip the RSS feeds")
    a = ap.parse_args(argv)
    s = run(do_ship=not a.no_ship, inbox_only=a.inbox_only)
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] event_engine.emit: {json.dumps(s, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
