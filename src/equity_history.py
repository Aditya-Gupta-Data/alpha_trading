"""equity_history.py — the forward record of TRUE NET EQUITY per paper account.

The database keeps a realized-equity point at every settlement
(`equity_curve` for PAPER_10L, `paper_equity_curve` for the shadows), but
never the value of the OPEN positions: the engine's marks
(data/market_snapshot.json) are overwritten every minute. So nothing could
show how PAPER_10L, PAPER_2L, PAPER_2L_ROT and PAPER_2L_LIVE moved between
settlements (owner request 2026-10-09: one multi-line equity graph).

`record()` appends one `net_equity_history` row per account — its realized
equity, unrealized P&L and True Net Equity — computed by the dashboard's own
`treasury()` (the ONE reader the KPI cards use, so the graph and the cards
can never disagree). It runs on the trading VM every 15 minutes, first thing
in scripts/publish_dashboard_mirror.sh (cron */15 9-16 weekdays + 21:05), so
each mirror push carries the newest point to the dashboard box.

Abstains, never guesses: a non-trading day records nothing; an account whose
open positions cannot all be priced (net equity None) is skipped by name.
Read-only on everything except its own table.

  python3 -m src.equity_history --record
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
TABLE = "net_equity_history"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    account_id        TEXT NOT NULL,
    ts                TEXT NOT NULL,      -- IST, naive ISO seconds
    equity            REAL NOT NULL,      -- realized equity (starting capital + realized P&L)
    unrealized_pnl    REAL NOT NULL,
    net_equity        REAL NOT NULL,      -- True Net Equity = equity + unrealized
    starting_capital  REAL NOT NULL,      -- contributed capital at this time (the % base)
    marks_as_of       TEXT,               -- the marks the unrealized figure was priced on
    marked_of         TEXT,               -- "priced/open" — partial marks are said, never hidden
    PRIMARY KEY (account_id, ts)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
    if "marked_of" not in cols:
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN marked_of TEXT")
    conn.commit()


def record(now: datetime = None, conn=None, treasury_fn=None) -> dict:
    """Append one row per priced paper account. Returns {"recorded":
    [accounts], "skipped": {account: reason}, "ts"} or {"skipped_day":
    reason}. Never raises on one account's gap; raises only if the
    database itself cannot be written (the caller logs it)."""
    from src import nse_calendar
    now = now or datetime.now(IST)
    if now.tzinfo is not None:
        now = now.astimezone(IST).replace(tzinfo=None)
    if not nse_calendar.is_trading_day(now.date()):
        return {"skipped_day": f"{now.date()} is not an NSE trading day — nothing recorded"}
    if treasury_fn is None:
        from src.dashboard import data as _data
        treasury_fn = _data.treasury
    t = treasury_fn()
    if not isinstance(t, dict) or t.get("error"):
        return {"skipped_day": f"treasury unreadable ({(t or {}).get('error') if isinstance(t, dict) else t})"}
    ts = now.replace(microsecond=0).isoformat()
    rows, skipped = [], {}
    for acct in sorted(k for k in t if k.startswith("PAPER_")):
        a = t[acct] or {}
        if a.get("equity") is None or not a.get("starting_capital"):
            skipped[acct] = "no realized equity read — nothing recorded"
            continue
        if a.get("net_equity") is not None and a.get("unrealized_pnl") is not None:
            unreal, net = float(a["unrealized_pnl"]), float(a["net_equity"])
        elif not a.get("open_positions"):
            # NOTHING open: the realized equity IS the true net equity (the
            # card shows no unrealized figure because there is nothing to
            # price, not because a price is missing) — a flat book is a point
            # on the graph, never a gap
            unreal, net = 0.0, float(a["equity"])
        else:
            skipped[acct] = (f"{a.get('open_positions')} open position(s), not all priced — no true net "
                             "equity this time")
            continue
        rows.append((acct, ts, float(a["equity"]), unreal, net, float(a["starting_capital"]),
                     a.get("marks_as_of"), a.get("marked_of")))
    own = conn is None
    if own:
        from src import brain_map
        conn = brain_map.connect()
    try:
        ensure_schema(conn)
        conn.executemany(f"INSERT OR IGNORE INTO {TABLE} (account_id, ts, equity, unrealized_pnl, net_equity, "
                         "starting_capital, marks_as_of, marked_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"ts": ts, "recorded": [r[0] for r in rows], "skipped": skipped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--record", action="store_true", help="append one true-net-equity row per account")
    args = ap.parse_args(argv)
    if not args.record:
        ap.print_help()
        return 0
    try:
        out = record()
    except Exception as exc:                       # fail-open: the mirror push still runs
        print(f"[equity_history] not recorded: {exc}")
        return 0
    if out.get("skipped_day"):
        print(f"[equity_history] {out['skipped_day']}")
    else:
        extra = "; ".join(f"{a}: {r}" for a, r in out["skipped"].items())
        print(f"[equity_history] {out['ts']} recorded {', '.join(out['recorded']) or 'nothing'}"
              + (f" (skipped — {extra})" if extra else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
