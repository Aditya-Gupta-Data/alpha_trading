"""
The node half: news/inbox → classify → exposure → materiality → priced-in → card → ship to the VM.

    python -m src.event_engine.emit [--no-ship] [--inbox-only] [--reassess-only]

Runs on the Mini PC (cron, node-only lines). It never trades. A card is shipped through the ONE
node→VM artifact lane (`firm_treasury.vm_push_file`) as `data/event_card__<id>.json`, which the VM
executor reads. When the LLM is unavailable or its daily cap is spent, the run stops and the item is
NOT marked seen, so it is retried next run — abstain, never invent.

Every run then RE-ASSESSES (no LLM) the last 5 days' cards that suggested nothing: materiality from the
stored exposures, and priced-in for each material exposure still `unknown` for want of prices — the
08:40 run cannot see the announcement day's close (the bhavcopy lands 19:15), so the 21:40 run can.
A card that gains a suggestion is rewritten in place (`reassessed_at`) and shipped (2026-10-23).
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

from pathlib import Path

from src.event_engine import CARD_DIR, IST, STATE_DIR, settings
from src.event_engine import card as card_mod
from src.event_engine import detect, exposure, materiality, priced_in
from src.event_engine.llm import EventLLM

SHIP_PREFIX = "event_card__"
MAX_ANNOUNCED_LAG_DAYS = 7          # an LLM date older than publication by more than this is a citation
REASSESS_DAYS = 5


def _published(item: dict) -> str | None:
    pub = item.get("published") or ""
    try:
        return parsedate_to_datetime(pub).astimezone(IST).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(pub[:19]).date().isoformat()
    except ValueError:
        return None


def announcement_date(event: dict, item: dict, today: date = None) -> tuple:
    """(announced_on, note). The model's date unless it is missing, unparseable or more than
    MAX_ANNOUNCED_LAG_DAYS before the item's PUBLICATION date (a historical reference, as NAM-INDIA's
    2025-02-12 on a 2026-10-23 article) — then the publication date, else today IST (ingestion). An
    item with no publication date (a hand-dropped inbox text, often an older document on purpose)
    keeps a parseable model date. `note` names a fallback."""
    pub = _published(item)
    fallback = pub or (today or datetime.now(IST).date()).isoformat()
    basis = "publication" if pub else "ingestion"
    raw = event.get("announced_on")
    if not raw:
        return fallback, f"announced_on missing: the {basis} date {fallback} used"
    try:
        said = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return fallback, f"announced_on {raw!r} unparseable: the {basis} date {fallback} used"
    lag = (date.fromisoformat(pub) - said).days if pub else 0
    if lag > MAX_ANNOUNCED_LAG_DAYS:
        return fallback, (f"announced_on {said} is {lag} days before the {basis} date: treated as a "
                          f"historical reference, {fallback} used")
    return said.isoformat(), None


def _announced(event: dict, item: dict) -> str | None:
    return announcement_date(event, item)[0]


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
        iid, tkey = detect.item_id(item), detect.title_key(item)
        if iid in seen:
            continue
        if tkey and tkey in seen:                         # the same headline again: no LLM spend, no 2nd card
            seen.add(iid)
            summary["skipped"]["duplicate headline"] = summary["skipped"].get("duplicate headline", 0) + 1
            continue
        if not detect.in_universe(item):                  # cannot reach the VM's option universe: no LLM spend
            _mark_seen(seen, iid, tkey)
            why = "outside the VM option universe (pre-LLM)"
            summary["skipped"][why] = summary["skipped"].get(why, 0) + 1
            continue
        summary["items"] += 1
        c = detect.classify(item, llm)
        if not c["ok"]:
            if c["event"] is None and c["reason"] != "no primary text (gate 1: headline only)":
                summary["stopped"] = c["reason"]          # the LLM is down or capped: retry next run
                break
            _mark_seen(seen, iid, tkey)
            summary["skipped"][c["reason"]] = summary["skipped"].get(c["reason"], 0) + 1
            continue
        summary["classified"] += 1
        ex = exposure.extract(item, llm)
        if not ex["ok"]:
            summary["stopped"] = ex["reason"]
            break
        mats = [materiality.assess(e, cfg["materiality_threshold_pct"]) for e in ex["exposures"]]
        announced, note = announcement_date(c["event"], item)
        if note:
            ex = dict(ex, flags=list(ex.get("flags") or []) + [note])
        priced = [priced_in.assess(e["ticker"], m, announced, bars_fn=bars_fn, cfg=cfg)
                  for e, m in zip(ex["exposures"], mats)]
        card = card_mod.build(item, dict(c["event"], announced_on=announced), ex, mats, priced, cfg)
        path = card_mod.write(card, card_dir or CARD_DIR)
        summary["cards"].append({"event_id": card["event_id"], "suggestions": len(card["suggestions"])})
        if do_ship and card["suggestions"] and ship(path, ship_fn):
            summary["shipped"] += 1
        _mark_seen(seen, iid, tkey)
    return summary


def reassess(card_dir=None, bars_fn=None, do_ship=True, ship_fn=None, cfg=None, today: date = None,
             days: int = REASSESS_DAYS) -> dict:
    """Re-judge the last `days` days' cards that suggested nothing, with no LLM: the announcement date
    through `announcement_date`, materiality from the stored exposures, priced-in only where it is
    `unknown` for want of prices (a resolved verdict is never re-read). A changed card is rewritten in
    place; one that now suggests is shipped. Never raises per card."""
    cfg = cfg or settings()
    today = today or datetime.now(IST).date()
    root = Path(card_dir or CARD_DIR)
    out = {"checked": 0, "updated": [], "shipped": 0, "errors": []}
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            card = json.loads(path.read_text())
            if card.get("suggestions"):
                continue
            made = datetime.fromisoformat(str(card.get("generated_at"))).date()
            if (today - made).days > days:
                continue
            out["checked"] += 1
            src = card.get("source") or {}
            announced, note = announcement_date({"announced_on": card.get("announced_on")},
                                                {"published": src.get("published")}, today=made)
            exposures = card.get("exposure") or []
            mats = [materiality.assess(e, cfg["materiality_threshold_pct"]) for e in exposures]
            old = {p.get("ticker"): p for p in card.get("priced_in") or []}
            priced = []
            for e, m in zip(exposures, mats):
                p = old.get(e.get("ticker"))
                if (p is None or p.get("reason") == "not material" or p.get("reason") in priced_in.NO_PRICE_REASONS
                        or str(p.get("reason") or "").startswith("price read failed")) and m.get("passes_gate"):
                    p = priced_in.assess(e["ticker"], m, announced, bars_fn=bars_fn, cfg=cfg)
                elif not m.get("passes_gate"):
                    p = dict(p or {"ticker": e.get("ticker"), "verdict": "unknown", "event_move_pct": None,
                                   "justified_move_pct": None, "direction": None}, reason="not material")
                priced.append(p)
            sugg = card_mod.suggestions_for(exposures, mats, priced, cfg)
            if (mats, priced, sugg, announced) == (card.get("materiality"), card.get("priced_in"),
                                                    card.get("suggestions") or [], card.get("announced_on")):
                continue
            flags = list(card.get("flags") or [])
            if note and note not in flags:
                flags.append(note)
            card.update(announced_on=announced, materiality=mats, priced_in=priced, suggestions=sugg, flags=flags,
                        reassessed_at=datetime.now(IST).isoformat(timespec="seconds"))
            path = card_mod.write(card, root)
            out["updated"].append({"event_id": card.get("event_id"), "suggestions": len(sugg),
                                   "verdicts": {p.get("ticker"): p.get("verdict") for p in priced
                                                if p.get("reason") != "not material"}})
            if do_ship and sugg and ship(path, ship_fn):
                out["shipped"] += 1
        except Exception as exc:
            out["errors"].append(f"{path.name}: {type(exc).__name__}: {exc}")
    return out


def _mark_seen(seen, iid: str, tkey) -> None:
    seen.add(iid)
    if tkey:
        seen.add(tkey)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Event engine, node half: emit event cards")
    ap.add_argument("--no-ship", action="store_true")
    ap.add_argument("--inbox-only", action="store_true", help="skip the RSS feeds")
    ap.add_argument("--reassess-only", action="store_true", help="only re-judge recent cards (no LLM)")
    a = ap.parse_args(argv)
    s = {} if a.reassess_only else run(do_ship=not a.no_ship, inbox_only=a.inbox_only)
    if a.reassess_only or s.get("status") == "ok":
        s["reassessed"] = reassess(do_ship=not a.no_ship)
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] event_engine.emit: {json.dumps(s, default=str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
