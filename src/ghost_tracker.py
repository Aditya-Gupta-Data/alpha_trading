"""
src/ghost_tracker.py — the ghost portfolio: what the refused trades would have done
===================================================================================

Level 1, 2026-08-07, on the architect's question: **"what would have
happened if we took them?"**

On 2026-08-07 the desk evaluated nine underlyings ~25 times and entered
nothing. Everything it wanted was refused for size, margin or exposure —
and the desk has no idea whether that saved money or cost it. The
proposal ledger (built the same day) records the refusals; this reads
them back and marks each refused structure to market.

WHAT IT IS NOT — the boundary IS the safety property, and it is the whole
reason this file exists as its own module:

  * It is **NOT ON ANY EXECUTION PATH**. `plan_tracker`, `live_bridge`,
    `portfolio_manager` and the market loop neither import it nor know it
    exists. A test enforces that.
  * It locks **no margin**, touches **no journal**, writes **no**
    `brain_map` row and moves **no** capital. Its only output is its own
    file, `data/ghost_portfolio_pnl.jsonl`.
  * It is **on-demand**. No cron line. The owner runs it and reads it.
  * Deleting this module and its output would change nothing about how
    the desk trades. That is the point.

HOW A GHOST IS PRICED

A rejected row now carries the structure the engine actually built —
`legs` (side / option_type / strike / premium), `lot_size`, `expiry` and
the net premium it was built at. Marking it is then arithmetic:

    V      = Σ(BUY leg price) − Σ(SELL leg price)      # position value
    P&L    = (V_now − V_entry) × lot_size × lots
    V_entry = −entry_net                               # credit in = negative cost

Prices come from the **EOD option-chain archive** (`chain_archiver`'s
`data/lake/chains/<slug>/date=…` partitions) — the same close the desk
would have marked against, already on disk, no token, no API call.
`--live` prices from the live chain instead, through the ONE market-data
door, and is deliberately opt-in: during market hours it competes for the
same rate-limited token the live loop needs, and no observability tool
gets to do that by default.

WHERE IT GOES BLIND, STATED OUT LOUD (see `--json` `by_status`):

  * `NO_CHAIN_ARCHIVE` — chains are archived for **NIFTY 50 and NIFTY
    BANK only**. FINNIFTY, MIDCPNIFTY and all five equity options have no
    EOD chain anywhere in this system, so their ghosts are UNPRICEABLE
    and are reported as such. They are not dropped, and they are
    certainly not filled in with a model price: a fabricated mark on a
    refused trade would corrupt the exact comparison this file exists to
    make.
  * `NO_STRIKE` — the strike is not in that day's captured chain.
  * `NO_STRUCTURE` — the refusal predates the structure capture (any row
    written before 2026-08-07), so there are no legs to price.
  * `EXPIRED` — the ghost's expiry has passed; there is no live mark and
    the archive stops at the last captured session.

`lots` is honest too. A size-refused trade was sized to ZERO lots by the
engine, so there is no authorised size to use: the ghost is priced at ONE
lot and the row says `lots_assumed: true`. Reading the P&L as if the desk
would have taken one lot is the reader's decision, made in the open.

CLI
    python3 -m src.ghost_tracker                    # mark today's ghosts, EOD prices
    python3 -m src.ghost_tracker --date 2026-08-10  # a specific session's ghosts
    python3 -m src.ghost_tracker --live             # live chain instead of the archive
    python3 -m src.ghost_tracker --json
    python3 -m src.ghost_tracker --dry-run          # price, print, write nothing
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import lake, proposal_ledger

ROOT = Path(__file__).resolve().parent.parent
GHOST_PATH = ROOT / "data" / "ghost_portfolio_pnl.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))

# Which underlyings have archived chains — read from the ARCHIVER ITSELF,
# never copied. A second copy of this map is a second thing to let rot,
# and the failure mode is silent: on 2026-08-07 the archiver went 2 -> 9
# and a duplicated map here kept reporting seven of them UNPRICEABLE
# while their chains sat on disk. Everything absent from it is honestly
# unpriceable rather than modelled.
try:
    from src.ingestion.chain_archiver import UNDERLYINGS as CHAIN_SLUGS
except Exception:                                   # pragma: no cover
    CHAIN_SLUGS = {"NIFTY 50": "nifty", "NIFTY BANK": "banknifty"}

# How many sessions back to look for a chain partition before giving up:
# the archive skips weekends and holidays, so "yesterday" is often not the
# last captured day.
CHAIN_LOOKBACK_DAYS = 7


def _now(now=None) -> datetime:
    return now or datetime.now(IST)


def is_ghost(row: dict) -> bool:
    """A refused evaluation — the only kind that has a ghost. EXECUTED and
    PROPOSED_PENDING rows became real trades and the journal owns them."""
    return str((row or {}).get("fate") or "").startswith("REJECTED")


def load_ghosts(ledger_path=None, session_date: str = None) -> list:
    """Refused evaluations from the proposal ledger, newest last."""
    return [r for r in proposal_ledger.read_rows(path=ledger_path,
                                                 session_date=session_date)
            if is_ghost(r)]


def dedupe(ghosts: list) -> list:
    """One ghost per (underlying, strategy, expiry, strike-set).

    The loop re-evaluates every 15 minutes, so a trade refused all day
    appears ~25 times. Counting each as its own ghost would multiply the
    hypothetical P&L by the polling frequency — a number that says more
    about the cron than about the market. The FIRST sighting wins: that is
    when the desk would have entered."""
    seen, out = set(), []
    for g in ghosts:
        legs = g.get("legs") or []
        key = (g.get("underlying"), g.get("strategy"), g.get("expiry"),
               tuple(sorted((l.get("strike"), l.get("option_type"),
                             l.get("side")) for l in legs)))
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out


# ------------------------------------------------------------ pricing

def _chain_from_archive(underlying: str, expiry: str, as_of: date,
                        lake_root=None) -> tuple:
    """(oc, captured_day) from the EOD archive, walking back over
    weekends/holidays. (None, None) when nothing was captured."""
    slug = CHAIN_SLUGS.get(underlying)
    if not slug:
        return None, None
    for back in range(CHAIN_LOOKBACK_DAYS + 1):
        day = (as_of - timedelta(days=back)).isoformat()
        try:
            rows = lake.read_day(f"chains/{slug}", day, root=lake_root)
        except Exception:
            rows = []
        for r in rows:
            if str(r.get("expiry")) == str(expiry):
                return (r.get("oc") or {}), day
    return None, None


def _chain_live(underlying: str, expiry: str) -> dict | None:
    """The live chain, through the ONE market-data door. Opt-in only."""
    try:
        from src.dhan_client import get_option_chain
        data = get_option_chain(underlying, expiry) or {}
        return data.get("oc") or None
    except Exception:
        return None


def leg_price(oc: dict, strike, option_type: str):
    """Last price for one leg out of a chain dict, or None.

    Chain keys are stringified floats ('24600.000000'), so the lookup
    normalises rather than assuming a format. A zero last_price is a
    DEAD strike, not a free option — it reads as None, because pricing a
    leg at 0 would silently hand the ghost a fabricated profit."""
    if not oc:
        return None
    side = str(option_type or "").lower()
    if side not in ("ce", "pe"):
        return None
    try:
        want = float(strike)
    except (TypeError, ValueError):
        return None
    node = None
    for k, v in oc.items():
        try:
            if abs(float(k) - want) < 1e-6:
                node = v
                break
        except (TypeError, ValueError):
            continue
    if not isinstance(node, dict):
        return None
    px = (node.get(side) or {}).get("last_price")
    try:
        px = float(px)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def position_value(legs: list, oc: dict):
    """Σ(BUY) − Σ(SELL) at current prices, or (None, reason) if any leg is
    unpriceable. A partially-priced spread is not a mark — it is a guess
    with three quarters of the evidence."""
    total = 0.0
    for leg in legs or []:
        px = leg_price(oc, leg.get("strike"), leg.get("option_type"))
        if px is None:
            return None, f"no price for {leg.get('option_type')} " \
                         f"{leg.get('strike')}"
        total += px if str(leg.get("side")).upper() == "BUY" else -px
    return round(total, 2), None


def mark_ghost(ghost: dict, as_of: date = None, live: bool = False,
               lake_root=None, chain_fn=None) -> dict:
    """One refused trade, marked to market. Never raises."""
    as_of = as_of or _now().date()
    legs = ghost.get("legs") or []
    lots = ghost.get("lots")
    lots_assumed = not bool(lots)
    lots = int(lots or 1)
    lot_size = ghost.get("lot_size")
    entry_net = ghost.get("entry_net")
    out = {
        "as_of": as_of.isoformat(),
        "marked_at": _now().isoformat(timespec="seconds"),
        "session_date": ghost.get("session_date"),
        "underlying": ghost.get("underlying"),
        "fate": ghost.get("fate"),
        "reason": ghost.get("reason"),
        "strategy": ghost.get("strategy"),
        "direction": ghost.get("direction"),
        "expiry": ghost.get("expiry"),
        "lots": lots,
        "lots_assumed": lots_assumed,
        "lot_size": lot_size,
        "entry_net": entry_net,
        "max_loss": ghost.get("max_loss"),
        "status": "PRICED",
        "pnl": None,
        "price_source": None,
    }
    if not legs or entry_net is None or not lot_size:
        return dict(out, status="NO_STRUCTURE",
                    detail="the refusal carries no built structure "
                           "(row predates the 2026-08-07 capture)")
    if ghost.get("expiry") and str(ghost["expiry"]) < as_of.isoformat():
        return dict(out, status="EXPIRED",
                    detail=f"expiry {ghost['expiry']} has passed")
    if chain_fn is not None:
        oc, source = chain_fn(ghost.get("underlying"), ghost.get("expiry")), "injected"
    elif live:
        oc, source = _chain_live(ghost.get("underlying"),
                                 ghost.get("expiry")), "live"
    else:
        oc, day = _chain_from_archive(ghost.get("underlying"),
                                      ghost.get("expiry"), as_of,
                                      lake_root=lake_root)
        source = f"archive:{day}" if oc else None
    if not oc:
        if ghost.get("underlying") not in CHAIN_SLUGS:
            return dict(out, status="NO_CHAIN_ARCHIVE",
                        detail=f"no chain is archived for "
                               f"{ghost.get('underlying')} — only "
                               f"{', '.join(sorted(CHAIN_SLUGS))}")
        return dict(out, status="NO_CHAIN_ARCHIVE",
                    detail=f"no captured chain for expiry "
                           f"{ghost.get('expiry')} within "
                           f"{CHAIN_LOOKBACK_DAYS} days of {as_of}")
    value_now, why = position_value(legs, oc)
    if value_now is None:
        return dict(out, status="NO_STRIKE", price_source=source, detail=why)
    # entry_net is credit-positive; the position's cost basis is its
    # negative. P&L is the change in what the position is worth.
    value_entry = -float(entry_net)
    pnl = round((value_now - value_entry) * float(lot_size) * lots, 2)
    return dict(out, status="PRICED", price_source=source,
                value_entry=round(value_entry, 2), value_now=value_now,
                pnl=pnl)


def run(ledger_path=None, session_date: str = None, as_of: date = None,
        live: bool = False, out_path=None, dry_run: bool = False,
        lake_root=None, chain_fn=None) -> dict:
    """Mark every ghost for a session and append the marks. Returns the
    report; writes nothing on --dry-run."""
    as_of = as_of or _now().date()
    session_date = session_date or as_of.isoformat()
    ghosts = dedupe(load_ghosts(ledger_path=ledger_path,
                                session_date=session_date))
    marks = [mark_ghost(g, as_of=as_of, live=live, lake_root=lake_root,
                        chain_fn=chain_fn) for g in ghosts]
    if marks and not dry_run:
        p = Path(out_path) if out_path else GHOST_PATH
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                for m in marks:
                    fh.write(json.dumps(m) + "\n")
        except OSError as exc:
            return {"as_of": as_of.isoformat(), "session_date": session_date,
                    "ghosts": len(marks), "error": f"write failed: {exc}",
                    "marks": marks}
    priced = [m for m in marks if m["status"] == "PRICED"]
    by_status = {}
    for m in marks:
        by_status[m["status"]] = by_status.get(m["status"], 0) + 1
    return {
        "as_of": as_of.isoformat(),
        "session_date": session_date,
        "ghosts": len(marks),
        "priced": len(priced),
        "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "total_pnl": round(sum(m["pnl"] or 0.0 for m in priced), 2),
        "best": max((m for m in priced), key=lambda m: m["pnl"], default=None),
        "worst": min((m for m in priced), key=lambda m: m["pnl"], default=None),
        "dry_run": bool(dry_run),
        "marks": marks,
    }


def render_lines(report: dict) -> list:
    """The report as plain lines — honest about what it could not price."""
    if not report.get("ghosts"):
        return [f"ghost portfolio: no refused trades on "
                f"{report.get('session_date')}"]
    verdict = ("would have MADE" if report["total_pnl"] > 0
               else "would have LOST" if report["total_pnl"] < 0 else "flat")
    lines = [f"ghost portfolio {report['session_date']} "
             f"(marked {report['as_of']}): {report['ghosts']} refused "
             f"trade(s), {report['priced']} priced",
             f"  {verdict} Rs.{abs(report['total_pnl']):,.2f}"]
    for status, n in report["by_status"].items():
        if status != "PRICED":
            lines.append(f"  {status:<18} {n} (unpriced, NOT in the total)")
    for m in report["marks"]:
        if m["status"] != "PRICED":
            continue
        sign = "+" if (m["pnl"] or 0) >= 0 else ""
        assumed = " [1 lot assumed]" if m.get("lots_assumed") else ""
        lines.append(f"  {m['underlying']:<18} {m['strategy'] or '—':<18} "
                     f"{sign}Rs.{m['pnl']:,.2f}{assumed}")
    return lines


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(
        description="What the refused trades would have done (read-only; "
                    "on no execution path)")
    ap.add_argument("--date", help="session date to mark (YYYY-MM-DD)")
    ap.add_argument("--as-of", help="valuation date (default today)")
    ap.add_argument("--live", action="store_true",
                    help="price off the LIVE chain instead of the EOD "
                         "archive (competes for the token — off by default)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    rep = run(session_date=args.date,
              as_of=date.fromisoformat(args.as_of) if args.as_of else None,
              live=args.live, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        for line in render_lines(rep):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
