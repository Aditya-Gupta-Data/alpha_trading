"""
src/book_context.py — "what we hold and WHY" (the book's memory at entry)
=========================================================================

The owner's ask (2026-07-15): a layer that at ANY time can answer "what
positions are open, why was each taken", keep that ready, and when a NEW
proposal arrives, present the newcomer IN THE CONTEXT of the existing
book — so a decision (human or auto) is never made as if the book were
empty.

The pieces already existed, scattered: positions.py says WHAT is open,
the journal carries WHY each entry was taken (`signal`, `regime`, the
frozen receipt), explain.py reconstructs one trade after the fact, and
the #68 exposure gate BLOCKS exact duplicates. What was missing is the
join: one read-model of the whole book with its reasons, and one line of
that context on every new proposal card.

Three views, one vocabulary:

  position_dossier(entry)   WHY + status for one open spread: the entry
                            signal (the reason recorded at birth), the
                            stamped direction, entry regime, age, expiry
                            distance, capital at risk.
  book_summary(entries)     Every open spread's dossier + the book's
                            shape (counts by direction and underlying,
                            total max loss). Derives ONLY from the
                            journal + date math — no Dhan, no DB locks —
                            so it works identically at 09:20 during the
                            session, at 22:00 after close, and on a
                            Sunday ("alag alag time par bhi sahi chale").
                            Live marks/P&L deliberately stay
                            portfolio_report's job.
  book_line(ticker, dir)    ONE annotation line for a NEW proposal's
                            Discord card: what the book already holds on
                            that underlying (and overall), so the
                            newcomer is judged in context.

DOCTRINE: ANNOTATE-ONLY (#63 stage 1 — facts, no verdict authority).
This module never blocks, scores, or approves; the #68 gate keeps the
blocking monopoly and runs BEFORE this enrichment. Read-only on all
state; every function fails open (None / empty) — a broken book read can
never break a proposal. CLI: `python3 -m src.book_context`.
"""

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def _today(today: date = None) -> date:
    return today or datetime.now(IST).date()


def _days_between(iso_day: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat((iso_day or "")[:10])).days
    except ValueError:
        return None


def position_dossier(entry: dict, today: date = None) -> dict | None:
    """The WHY + status card for one open spread journal entry, or None
    for anything that isn't an open tracked spread. Every field comes
    from what the entry itself recorded at birth — nothing re-derived,
    nothing fetched."""
    from src.exposure_gate import direction_of
    from src.plan_tracker import _spread_trackable
    if not (entry.get("decision") == "approved"
            and entry.get("outcome") is None
            and entry.get("spread") and _spread_trackable(entry)):
        return None
    t = _today(today)
    s = entry["spread"]
    lots = int(s.get("lots", 1))
    expiry = s.get("expiry")
    days_to_expiry = None
    if expiry:
        d = _days_between(expiry, t)
        days_to_expiry = -d if d is not None else None
    regime = entry.get("regime") or {}
    return {
        "trade_id": entry.get("short_id"),
        "ticker": entry.get("ticker"),
        "strategy": s.get("strategy"),
        "direction": direction_of(s),
        "why": entry.get("signal") or "(no signal recorded)",
        "opened_on": entry.get("date"),
        "days_in_trade": max(0, _days_between(entry.get("date"), t) or 0),
        "expiry": expiry,
        "days_to_expiry": days_to_expiry,
        "lots": lots,
        "max_loss_rs": (s.get("max_loss") or 0) * lots,
        "entry_regime": {k: regime[k] for k in ("regime_trend", "regime_vix")
                         if regime.get(k)} or None,
    }


def book_summary(entries: list = None, today: date = None) -> dict:
    """The whole open book with its reasons + shape. Journal-only read;
    fail-open to an empty book on any error."""
    try:
        if entries is None:
            from src import journal
            entries = journal.read_all()
        dossiers = []
        for e in entries:
            try:
                d = position_dossier(e, today)
            except Exception:
                d = None  # one malformed entry never hides the rest
            if d:
                dossiers.append(d)
        dossiers.reverse()  # journal is append-ordered; newest first
        by_direction, by_ticker = {}, {}
        for d in dossiers:
            if d["direction"]:
                by_direction[d["direction"]] = by_direction.get(d["direction"], 0) + 1
            by_ticker[d["ticker"]] = by_ticker.get(d["ticker"], 0) + 1
        return {
            "positions": dossiers,
            "count": len(dossiers),
            "by_direction": by_direction,
            "by_ticker": by_ticker,
            "total_max_loss_rs": round(sum(d["max_loss_rs"] for d in dossiers), 2),
        }
    except Exception as e:
        print(f"  (book_context unavailable — failing open: {e})")
        return {"positions": [], "count": 0, "by_direction": {},
                "by_ticker": {}, "total_max_loss_rs": 0.0}


def render_book(summary: dict = None, today: date = None) -> str:
    """The 'ready rakhe' view: the whole book with reasons, any time of
    day, terminal or Discord."""
    s = summary if summary is not None else book_summary(today=today)
    if not s["count"]:
        return "📖 **Book** — empty: no open spread positions."
    dirs = ", ".join(f"{n} {d}" for d, n in sorted(s["by_direction"].items()))
    lines = [f"📖 **Book** — {s['count']} open ({dirs}); "
             f"total max loss Rs.{s['total_max_loss_rs']:,.0f}"]
    for d in s["positions"]:
        exp = (f"exp {d['expiry']} ({d['days_to_expiry']}d)"
               if d["days_to_expiry"] is not None else f"exp {d['expiry']}")
        regime = ""
        if d["entry_regime"]:
            regime = " · entered in " + "/".join(d["entry_regime"].values())
        lines.append(
            f"• `{d['trade_id']}` {d['ticker']} "
            f"{(d['strategy'] or 'spread').replace('_', ' ')} "
            f"({d['direction'] or '?'}) — {d['days_in_trade']}d in, {exp}, "
            f"max loss Rs.{d['max_loss_rs']:,.0f}{regime}\n"
            f"    why: {d['why']}")
    return "\n".join(lines)


def book_line(ticker: str, direction: str = None, entries: list = None,
              today: date = None) -> str | None:
    """ONE annotation line for a NEW proposal on `ticker`/`direction`:
    what the book already holds, so the newcomer is judged in context.
    None when the book is empty (an empty-book line is noise) or on any
    error (fail-open). ANNOTATE-ONLY: states facts, renders no verdict —
    the #68 gate already blocked true duplicates before this runs."""
    try:
        s = book_summary(entries, today)
        if not s["count"]:
            return None
        same = [d for d in s["positions"] if d["ticker"] == ticker]
        dirs = ", ".join(f"{n} {d}" for d, n in sorted(s["by_direction"].items()))
        head = f"📖 **Book**: {s['count']} open ({dirs})."
        if same:
            bits = []
            for d in same:
                bits.append(f"`{d['trade_id']}` {d['direction'] or '?'} "
                            f"{(d['strategy'] or 'spread').replace('_', ' ')}, "
                            f"{d['days_in_trade']}d — {d['why']}")
            head += f" Already on {ticker}: " + " | ".join(bits)
        elif direction:
            head += (f" Nothing on {ticker} yet — this would be the book's "
                     f"first {direction} {ticker} position.")
        return head
    except Exception:
        return None


def main() -> None:
    print(render_book())


if __name__ == "__main__":
    main()
