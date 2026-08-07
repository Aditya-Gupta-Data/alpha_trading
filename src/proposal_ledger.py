"""
src/proposal_ledger.py — every evaluation the desk made, and what became of it
==============================================================================

Level 1, 2026-08-07. THE GAP THIS CLOSES: the journal records what the
desk DID. Nothing recorded what it ALMOST did. On 2026-08-07 the desk
evaluated nine underlyings roughly twenty-five times and entered nothing,
and the only trace was a wall of `[Market Loop] … no proposal (…)` print
lines — unqueryable, unaggregatable, and rotated away with the log. The
question "how much did the ₹10,000 per-trade cap actually cost us this
week?" had no answer anywhere in the system.

So this appends ONE line per evaluation to `data/proposal_ledger.jsonl`:
what was looked at, what came back, and which named gate refused it.

WHAT IT IS NOT, stated first because the boundary is the safety property:

  * It DECIDES NOTHING. It is called AFTER the proposer has already
    returned, with that result in hand. No branch anywhere reads this
    file. Deleting it would change no trade.
  * It never raises. `record()` swallows everything — an observability
    layer that can break the market loop is a downgrade, not a feature.
  * It classifies but never INVENTS. A reason string it does not
    recognise is `REJECTED_OTHER` **with the raw text kept verbatim**,
    so an unmapped refusal shows up as an unmapped refusal rather than
    being quietly filed under the nearest familiar bucket.

THE FATE CODES (`fate`):
  EXECUTED             — proposed and auto-approved into the book
  PROPOSED_PENDING     — proposed, awaiting a human (or auto-approval
                         declined downstream)
  REJECTED_RISK_CAP    — max loss/lot over the hard per-trade cap
  REJECTED_RISK_BUDGET — over the % options risk budget, or SPAN > cash
  REJECTED_MARGIN      — margin exhaustion: not enough liquid capital
  REJECTED_EXPOSURE    — the one-open-position-per-underlying+direction
                         gate (#68)
  REJECTED_NO_QUOTE    — no tradeable quotes at the chosen strikes
  REJECTED_NO_VIX      — India VIX unavailable, range strategies refused
  REJECTED_STRUCTURE   — the engine found no structure it wanted to trade
  NO_MARKET_STATE      — nothing was evaluated: no state this cycle
  REJECTED_OTHER       — unrecognised (the raw reason is on the row)

Rupee figures are pulled out of the reason text where the refusal states
them (`max_loss_rs`, `needed_rs`, `liquid_rs`) — that is the closest
thing to a "size" a refused proposal has, since a rejected trade never
got as far as an entry object. Absent = None, never 0.

Read it back with `summarise()` (or `python3 -m src.proposal_ledger`):
counts by fate, by underlying, and the capital the refusals name.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "data" / "proposal_ledger.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))

# Matched IN ORDER against the lowercased reason; first hit wins. These are
# the exact phrasings the live 2026-08-06/07 logs carry — when a refusal is
# reworded, its rows land in REJECTED_OTHER with the text attached, which is
# the visible failure this table is meant to have.
FATE_PATTERNS = (
    ("REJECTED_RISK_CAP", "hard per-trade risk cap"),
    ("REJECTED_RISK_BUDGET", "options risk budget"),
    ("REJECTED_MARGIN", "margin exhaustion"),
    ("REJECTED_MARGIN", "margin_blocked"),
    ("REJECTED_EXPOSURE", "exposure gate"),
    ("REJECTED_NO_QUOTE", "no tradeable quotes"),
    ("REJECTED_NO_VIX", "vix unavailable"),
    ("REJECTED_STRUCTURE", "structure blocked"),
    ("REJECTED_STRUCTURE", "no structure"),
)

_RUPEES = r"rs\.?\s*([\d,]+(?:\.\d+)?)"
AMOUNT_PATTERNS = {
    "max_loss_rs": re.compile(r"max loss\s+" + _RUPEES, re.I),
    "needed_rs": re.compile(r"needs\s+" + _RUPEES, re.I),
    "liquid_rs": re.compile(r"only\s+" + _RUPEES + r"\s+liquid", re.I),
}


def _now(now=None) -> datetime:
    return now or datetime.now(IST)


def classify(result: dict) -> str:
    """The fate code for one proposer result. Never guesses: an
    unrecognised refusal is REJECTED_OTHER, not the nearest bucket."""
    if result is None:
        return "NO_MARKET_STATE"
    reason = str(result.get("reason") or "").lower()
    if result.get("proposed"):
        return "EXECUTED" if result.get("auto_approved") else "PROPOSED_PENDING"
    for fate, needle in FATE_PATTERNS:
        if needle in reason:
            return fate
    return "REJECTED_OTHER"


def amounts_in(reason: str) -> dict:
    """The rupee figures a refusal states about itself. A refused trade
    has no entry object, so this text IS the only record of what it would
    have risked or needed."""
    out = {}
    text = str(reason or "")
    for field, pattern in AMOUNT_PATTERNS.items():
        m = pattern.search(text)
        if m:
            try:
                out[field] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return out


def _entry_facts(entry: dict) -> dict:
    """Strategy/direction/size off a journalled entry. Only a PROPOSED
    row has one — refusals get None, because the proposer never built the
    structure it was refused for and inferring one would be fiction."""
    if not isinstance(entry, dict):
        return {"strategy": None, "direction": None, "qty": None,
                "price": None, "risk_size_rs": None, "short_id": None}
    spread = entry.get("spread") or {}
    tags = entry.get("pattern_tags") or []
    levers = entry.get("risk_levers") or {}
    return {
        "strategy": spread.get("strategy") or (tags[0] if tags else None),
        "direction": spread.get("direction") or entry.get("view"),
        "qty": entry.get("shares"),
        "price": entry.get("price"),
        "risk_size_rs": levers.get("size"),
        "short_id": entry.get("short_id"),
    }


def _rejected_facts(result: dict) -> dict:
    """The refused structure the proposer now hands back (2026-08-07).

    Without this a refusal is a sentence with no strikes in it, and
    `ghost_tracker` has nothing to mark to market. The legs ride onto the
    row verbatim; `entry_net` is the per-lot premium the structure was
    built at, signed the way P&L reads it (a credit is money in)."""
    rej = (result or {}).get("rejected") or {}
    if not rej or not rej.get("legs"):
        return {}
    credit, debit = rej.get("net_credit"), rej.get("net_debit")
    entry_net = credit if credit is not None else (
        -debit if debit is not None else None)
    return {
        "strategy": rej.get("strategy"),
        "direction": rej.get("direction"),
        "legs": rej.get("legs"),
        "lot_size": rej.get("lot_size"),
        "lots": rej.get("lots"),
        "expiry": rej.get("expiry"),
        "entry_net": entry_net,
        "max_loss": rej.get("max_loss"),
        "max_profit": rej.get("max_profit"),
    }


def row_for(underlying: str, result: dict, state: dict = None,
            now=None) -> dict:
    """The ledger row. Pure — no I/O, so it is trivially testable."""
    ts = _now(now)
    reason = None if result is None else result.get("reason")
    facts = _entry_facts((result or {}).get("entry"))
    rejected = _rejected_facts(result)
    # The refused structure fills in what the entry object would have
    # carried — strategy/direction/expiry — WITHOUT overwriting a real
    # entry's own values on a proposal that actually fired.
    for k, v in rejected.items():
        if facts.get(k) is None:
            facts[k] = v
    row = {
        "ts": ts.isoformat(timespec="seconds"),
        "session_date": ts.date().isoformat(),
        "underlying": underlying,
        "fate": classify(result),
        "reason": reason,
        "vix": (state or {}).get("vix"),
        "spot": ((state or {}).get("analysis") or {}).get("price"),
        "expiry": (state or {}).get("expiry"),
        **facts,
        **amounts_in(reason),
    }
    return row


def _muzzled() -> bool:
    """True inside the test suite. The market-loop tests drive real
    cycles, and an un-pathed write from a test would append junk rows to
    the production ledger — a test must never touch a live data file. A
    test that wants the write passes `path`."""
    import os
    return (os.environ.get("IS_TEST_ENV", "").strip().lower()
            in ("1", "true", "yes")
            or bool(os.environ.get("PYTEST_CURRENT_TEST")))


def record(underlying: str, result: dict, state: dict = None, now=None,
           path=None) -> dict | None:
    """Append one evaluation. Returns the row (or None if the write
    failed) — and NEVER raises: this is telemetry hanging off the live
    loop, and telemetry does not get to break trading."""
    if path is None and _muzzled():
        return None
    try:
        row = row_for(underlying, result, state=state, now=now)
        p = Path(path) if path else LEDGER_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row
    except Exception:
        return None


def read_rows(path=None, session_date: str = None) -> list:
    """Rows, optionally for one session date. A corrupt line is skipped,
    never fatal — a half-written row must not blind the whole report."""
    p = Path(path) if path else LEDGER_PATH
    rows = []
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if session_date and row.get("session_date") != session_date:
                continue
            rows.append(row)
    except OSError:
        return []
    return rows


def summarise(path=None, session_date: str = None) -> dict:
    """What the desk evaluated and what stopped it — the opportunity-cost
    read the architect could not get from the journal."""
    rows = read_rows(path=path, session_date=session_date)
    by_fate, by_underlying = {}, {}
    blocked_capital = 0.0
    for r in rows:
        by_fate[r.get("fate")] = by_fate.get(r.get("fate"), 0) + 1
        u = r.get("underlying")
        by_underlying.setdefault(u, {})
        by_underlying[u][r.get("fate")] = \
            by_underlying[u].get(r.get("fate"), 0) + 1
        if str(r.get("fate", "")).startswith("REJECTED"):
            blocked_capital += float(r.get("needed_rs") or 0.0)
    return {"rows": len(rows), "session_date": session_date,
            "by_fate": dict(sorted(by_fate.items(), key=lambda kv: -kv[1])),
            "by_underlying": by_underlying,
            "margin_named_in_refusals_rs": round(blocked_capital, 2)}


def render_lines(path=None, session_date: str = None) -> list:
    """The summary as plain lines — the shape a report card can append."""
    s = summarise(path=path, session_date=session_date)
    if not s["rows"]:
        return ["proposal ledger: no evaluations recorded"]
    lines = [f"proposal ledger: {s['rows']} evaluation(s)"
             + (f" on {session_date}" if session_date else "")]
    for fate, n in s["by_fate"].items():
        lines.append(f"  {fate:<22} {n}")
    return lines


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="What the desk evaluated, and "
                                             "what stopped it")
    ap.add_argument("--date", help="session date (YYYY-MM-DD)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.json:
        print(json.dumps(summarise(session_date=args.date), indent=2))
    else:
        for line in render_lines(session_date=args.date):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
