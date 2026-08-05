"""
src/morning_brief.py — the pre-open Morning Brief (Directive 2, docs/
ceo_view_discord_design.md).

The DISTINCT card the owner asked for: what a 3-day-away owner needs to
glance at before the market opens, in plain English. This is deliberately
NOT `src.suggest` (08:00, Mon-Fri) — that stays the per-ticker technical
read. This is the account-level narrative: last night's macro read, what
reports today, and the book going into the session.

Deliberately reads only artifacts already written overnight — no live
Dhan quote/VIX fetch (the market hasn't opened; a pre-open quote pull is
fragile and unnecessary for this card):
  data/macro_regime.json        — analysis.macro_regime.declare (19:50 IST)
  data/strategy_scoreboard.json — analysis.strategy_scoreboard (Saturdays)
  data/earnings_calendar.json   — ingestion.earnings_calendar (19:20 IST)
  data/journal.jsonl            — open positions
  firm_mtm / portfolio_manager  — firm MTM + halt status

Cron: 08:05 IST Mon-Fri (after renew_token 07:00 and suggest 08:00, well
before master_scheduler 09:10). One card via `notifier.fire_broadcast`
(event="morning_brief"). Fail-open per field, like every other digest.

    python3 -m src.morning_brief [--dry-run]
"""
from datetime import date
from pathlib import Path

from src import ceo_language
from src.eod_summary import (_read_journal, _open_approved_spreads,
                             _open_approved_equities)
from src.ingestion import earnings_calendar as ec

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = ROOT / "config" / "watchlist.yaml"
RESULTS_LOOKAHEAD_DAYS = 3


def _today() -> str:
    return date.today().isoformat()


def _watchlist_tickers(path=None) -> list:
    try:
        import yaml
        cfg = yaml.safe_load(Path(path or WATCHLIST_PATH).read_text()) or {}
        seen, out = set(), []
        for item in cfg.get("watchlist") or []:
            t = item.get("ticker")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out
    except (OSError, ValueError):
        return []


def _open_tickers(journal_path=None) -> list:
    entries = _read_journal(journal_path)
    tickers = []
    for e in _open_approved_spreads(entries):
        u = (e.get("spread") or {}).get("underlying")
        if u:
            tickers.append(u)
    for e in _open_approved_equities(entries):
        t = e.get("ticker")
        if t:
            tickers.append(t)
    return tickers


def _events_field(watchlist_path=None, journal_path=None,
                  calendar_path=None, calendar=None, today=None,
                  lookahead=RESULTS_LOOKAHEAD_DAYS) -> str:
    """Plain-English results-date proximity for the book + watchlist.
    Honest absence: no calendar file / no known date = not mentioned,
    never guessed (earnings_calendar's own #50 discipline, reused)."""
    calendar = calendar if calendar is not None else ec.load_calendar(calendar_path)
    tickers = list(dict.fromkeys(
        _open_tickers(journal_path) + _watchlist_tickers(watchlist_path)))
    if not tickers:
        return "No tracked tickers today."
    lines = []
    for t in tickers:
        d = ec.days_to_results(t, today=today, calendar=calendar)
        if d is None:
            continue
        if d == 0:
            lines.append(f"**{t}** reports results today.")
        elif d <= lookahead:
            lines.append(f"**{t}** reports in {d} day{'s' if d != 1 else ''}.")
    if not lines:
        return f"No results due in the tracked book within {lookahead} days."
    return "\n".join(lines)


def _book_field(journal_path=None) -> str:
    entries = _read_journal(journal_path)
    spreads = _open_approved_spreads(entries)
    equities = _open_approved_equities(entries)
    total = len(spreads) + len(equities)
    lines = [f"{len(spreads)} option spread(s), {len(equities)} equity "
            f"position(s) — {total} open going into today."]
    try:
        from src import firm_mtm
        lines.append(firm_mtm.render_line())
    except Exception:
        pass
    return "\n".join(lines)


# ------------------------------------------- buy-zone proximity (08-05)

DARLINGS_DAILY_PATH = ROOT / "data" / "lake" / "darlings_daily.jsonl"
LEVELS_PATH = ROOT / "data" / "darlings_levels.json"
PROXIMITY_PCT = 3.0      # within this much of the zone floor = "approaching"
MAX_PROXIMITY_LINES = 8


def _latest_darling_closes(path=None) -> dict:
    """{symbol: price} from the newest `day` in the darlings day-tap.

    Cron #26 has captured ALL ~105 darlings' closes since 2026-08-04 — the
    ones we do NOT hold included, which is the whole point: entry zones are
    only visible for names outside the book. Nothing read the file until
    now. Accepts both the flat legacy file and the date-partitioned layout.
    """
    import json as _json
    base = Path(path) if path else DARLINGS_DAILY_PATH
    lines = []
    try:
        if base.exists():
            lines = base.read_text().splitlines()
        else:                                   # partitioned (post-rotation)
            dataset = base.parent / base.name[:-len(".jsonl")]
            days = sorted(p for p in dataset.glob("date=*") if p.is_dir()) \
                if dataset.is_dir() else []
            if days:
                for part in sorted(days[-1].glob("*.jsonl")):
                    lines += part.read_text().splitlines()
    except OSError:
        return {}
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_json.loads(line))
        except ValueError:
            continue
    if not rows:
        return {}
    newest = max(str(r.get("day") or "") for r in rows)
    out = {}
    for r in rows:
        if str(r.get("day") or "") == newest and r.get("price") is not None:
            out[str(r.get("ticker") or "").replace(".NS", "")] = r["price"]
    return out


def _levels_by_symbol(path=None) -> dict:
    import json as _json
    try:
        doc = _json.loads((Path(path) if path else LEVELS_PATH).read_text())
    except (OSError, ValueError):
        return {}
    return {row.get("symbol"): row for row in (doc.get("levels") or [])
            if isinstance(row, dict) and row.get("symbol")}


def buy_zone_proximity(closes: dict, levels: dict,
                       pct: float = PROXIMITY_PCT) -> list:
    """Darlings sitting IN or just ABOVE their buy zone, cheapest-first.

    `buy_zone` is [floor, ceiling] from `dynamic_pricer` (anchored VWAP
    widened to the nearest HVN). Two states worth a human's attention:
      IN ZONE       floor <= close <= ceiling — actionable today
      APPROACHING   above the ceiling but within `pct`% of it
    A close BELOW the floor is deliberately NOT reported here: that is the
    stop/overextension side of the book, not an entry signal, and
    `equity_entry_checks` owns it. NULL-honest: a name without levels or
    without a close is skipped, never assumed."""
    out = []
    for symbol, close in (closes or {}).items():
        row = (levels or {}).get(symbol)
        zone = (row or {}).get("buy_zone")
        if not (isinstance(zone, (list, tuple)) and len(zone) == 2):
            continue
        try:
            floor, ceiling = float(zone[0]), float(zone[1])
            close = float(close)
        except (TypeError, ValueError):
            continue
        if ceiling <= 0:
            continue
        if floor <= close <= ceiling:
            out.append({"symbol": symbol, "close": close, "state": "IN ZONE",
                        "distance_pct": 0.0, "zone": [floor, ceiling]})
        elif close > ceiling:
            gap = (close / ceiling - 1) * 100
            if gap <= pct:
                out.append({"symbol": symbol, "close": close,
                            "state": "APPROACHING",
                            "distance_pct": round(gap, 2),
                            "zone": [floor, ceiling]})
    return sorted(out, key=lambda r: (r["state"] != "IN ZONE",
                                      r["distance_pct"], r["symbol"]))


def _proximity_field(daily_path=None, levels_path=None,
                     closes=None, levels=None) -> str | None:
    """The card line, or None when there is nothing to say (no field, so a
    quiet morning stays byte-identical)."""
    closes = closes if closes is not None else _latest_darling_closes(daily_path)
    levels = levels if levels is not None else _levels_by_symbol(levels_path)
    hits = buy_zone_proximity(closes, levels)
    if not hits:
        return None
    lines = []
    for h in hits[:MAX_PROXIMITY_LINES]:
        tag = "🎯" if h["state"] == "IN ZONE" else "↘"
        near = ("in zone" if h["state"] == "IN ZONE"
                else f"{h['distance_pct']:g}% above")
        lines.append(f"{tag} **{h['symbol']}** {h['close']:g} — {near} "
                     f"({h['zone'][0]:g}–{h['zone'][1]:g})")
    if len(hits) > MAX_PROXIMITY_LINES:
        lines.append(f"…and {len(hits) - MAX_PROXIMITY_LINES} more")
    lines.append("_Advisory only — the desk's own halt stack still gates "
                 "every entry._")
    return "\n".join(lines)


def build_morning_brief(watchlist_path=None, journal_path=None,
                        calendar_path=None, calendar=None,
                        regime_sentence_fn=None, halt_lines_fn=None,
                        clock=None, darlings_daily_path=None,
                        levels_path=None, darling_closes=None,
                        darling_levels=None) -> dict:
    """Build the payload. Every seam is a parameter so the card is
    assertable offline; each field fails open independently."""
    today = (clock or _today)()
    try:
        today_date = date.fromisoformat(today)
    except ValueError:
        today_date = None
    fields = []

    try:
        halt_lines = halt_lines_fn() if halt_lines_fn else []
        if halt_lines:
            fields.append({"name": "🔴 SYSTEM PAUSED",
                           "value": "\n".join(halt_lines)[:1024],
                           "inline": False})
    except Exception:
        pass

    # Live read injected by main() only (07-23 sandbox rule, same as
    # ceo_brief/eod_summary): regime_sentence_fn=None omits the field
    # rather than reaching for the real data/macro_regime.json.
    try:
        sentence = regime_sentence_fn() if regime_sentence_fn else None
        if sentence:
            fields.append({"name": "🌍 Overnight Macro Read",
                           "value": sentence[:1024], "inline": False})
    except Exception:
        pass

    try:
        fields.append({"name": "📅 Today's Watchlist Events",
                       "value": _events_field(
                           watchlist_path=watchlist_path,
                           journal_path=journal_path,
                           calendar_path=calendar_path,
                           calendar=calendar, today=today_date)[:1024],
                       "inline": False})
    except Exception:
        pass

    # Buy-zone proximity (2026-08-05). Injected like every other live read
    # (the 07-23 sandbox rule): with no paths passed this reads the real
    # artifacts, which is what main() wants and what tests override.
    try:
        prox = _proximity_field(daily_path=darlings_daily_path,
                                levels_path=levels_path,
                                closes=darling_closes, levels=darling_levels)
        if prox:
            fields.append({"name": "🎯 Darling Buy-Zone Proximity",
                           "value": prox[:1024], "inline": False})
    except Exception:
        pass

    try:
        fields.append({"name": "💼 Book Going Into Today",
                       "value": _book_field(journal_path=journal_path)[:1024],
                       "inline": False})
    except Exception:
        pass

    return {"event": "morning_brief", "ticker": "", "date": today,
            "description": "☀️ Morning Brief — before the open.",
            "fields": fields}


def send_morning_brief(**kwargs) -> dict:
    payload = build_morning_brief(**kwargs)
    from src.notifier import fire_broadcast
    fire_broadcast(payload)
    return payload


def _render_text(payload: dict) -> str:
    out = [f"Morning Brief — {payload['date']}", payload["description"], ""]
    for f in payload["fields"]:
        out.append(f"{f['name']}\n{f['value']}\n")
    return "\n".join(out)


def main(argv=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    from src import portfolio_manager as pm
    kw = {"halt_lines_fn": pm.halt_banner_lines,
         "regime_sentence_fn": ceo_language.macro_regime_sentence}
    payload = build_morning_brief(**kw) if dry else send_morning_brief(**kw)
    print(_render_text(payload), flush=True)
    if dry:
        print("(dry run — nothing sent)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
