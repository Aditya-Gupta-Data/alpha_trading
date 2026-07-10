"""
src/wealth_lock.py — Phase §5 Wealth-Locking Flywheel, PAPER SCOPE ONLY
=======================================================================

Decision (2026-07-10): the 50% Gold-ETF profit sweep from
docs/scalable_implementation_roadmap.md §5 runs in the PAPER environment
first, to validate the tracking logic long before any real-capital
question arises (§5.2's "real vs paper" call stays open; this module is
the "paper" answer and nothing here places or prices a real order).

What it does: when a settled paper trade closes PROFITABLY, 50% of the
net simulated gain is earmarked for GOLDBEES (Nippon India Gold BeES
ETF, NSE) — recorded as a mock purchase in the `wealth_lock_ledger`
table (brain_map.db, additive — same ownership pattern as
portfolio_manager's tables) and announced with a Discord embed:

    🔒 PAPER SWEEP REQUIRED: Buy Rs.X of GOLDBEES

Design rules (matching portfolio_manager, which calls this):
  * sqlite3-only, every function takes an injectable `conn` — the whole
    module tests offline against ':memory:'.
  * Additive and advisory: nothing here mutates account_state,
    margin_locks, portfolio.json or the journal. The sweep is a LEDGER
    entry + an alert, not a cash movement — the simulated capital pool
    is not reduced (validating the tracking flow is the whole point;
    debiting the pool is a triage-time decision once tracking is trusted).
  * GOLDBEES is deliberately NOT in dhan_client.SECURITY_ID_MAP yet
    (adding an unverified security id silently prices the wrong
    instrument — the map's own warning). Until the verified id lands,
    `price_fn` returns None and the ledger stores the swept rupee amount
    with mock_units NULL; the alert still fires with the amount.
  * Fail-safe seam: sweep_on_settlement never raises — a broken ledger
    prints a note and returns swept=False; settling the trade is never
    blocked (same contract as the margin gate).

Inspect the ledger from the project folder:

    python3 -m src.wealth_lock
"""

from datetime import datetime

from src import brain_map

SWEEP_PCT = 50.0            # % of net profit earmarked per winning trade
SWEEP_INSTRUMENT = "GOLDBEES"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wealth_lock_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    journal_ref  TEXT NOT NULL,        -- the winning trade's journal short_id
    trade_pnl    REAL NOT NULL,        -- the settled net P&L that triggered this
    sweep_rs     REAL NOT NULL,        -- 50% of trade_pnl, the earmarked amount
    instrument   TEXT NOT NULL,        -- 'GOLDBEES'
    mock_price   REAL,                 -- ETF price at sweep time (NULL = unknown)
    mock_units   REAL,                 -- sweep_rs / mock_price (NULL = unknown)
    status       TEXT NOT NULL         -- 'logged' (paper scope has no fills)
);
"""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def build_sweep_alert(entry: dict) -> dict:
    """A wealth_lock_ledger row dict -> the broadcast_alert payload for
    the sweep card. Pure function — tests assert on this exact shape."""
    amount = float(entry["sweep_rs"])
    units = entry.get("mock_units")
    description = (f"🔒 PAPER SWEEP REQUIRED: Buy Rs.{amount:,.2f} of "
                   f"{entry['instrument']}")
    if units is not None:
        description += f" (≈{units:.2f} units @ Rs.{entry['mock_price']:,.2f})"
    return {
        "event": "wealth_sweep",
        "ticker": entry["instrument"],
        "date": (entry.get("ts") or _now_iso())[:10],
        "description": description,
        "sweep_rs": round(amount, 2),
        "trade_pnl": round(float(entry["trade_pnl"]), 2),
        "sweep_pct": SWEEP_PCT,
        "mock_units": units,
        "short_id": entry.get("journal_ref"),
    }


def record_sweep(conn, journal_ref: str, pnl_net: float,
                 price_fn=None) -> dict | None:
    """Write one sweep row for a profitable settlement and return the row
    as a dict (None when no sweep applies: pnl <= 0 or a duplicate ref).

    Idempotent per trade: a journal_ref that already has a ledger row is
    skipped — the tracker's sweeps may re-touch a resolved entry, and a
    win must never be swept twice."""
    ensure_schema(conn)
    pnl = round(float(pnl_net), 2)
    if pnl <= 0:
        return None
    dup = conn.execute("SELECT 1 FROM wealth_lock_ledger WHERE journal_ref = ?",
                       (journal_ref,)).fetchone()
    if dup is not None:
        return None
    sweep_rs = round(pnl * SWEEP_PCT / 100.0, 2)
    mock_price = None
    if price_fn is not None:
        try:
            raw = price_fn(SWEEP_INSTRUMENT)
            mock_price = float(raw) if raw else None
        except Exception as e:
            print(f"  (wealth lock: price lookup failed, storing amount "
                  f"only: {e})")
    mock_units = (round(sweep_rs / mock_price, 4)
                  if mock_price and mock_price > 0 else None)
    row = {
        "ts": _now_iso(), "journal_ref": journal_ref, "trade_pnl": pnl,
        "sweep_rs": sweep_rs, "instrument": SWEEP_INSTRUMENT,
        "mock_price": mock_price, "mock_units": mock_units, "status": "logged",
    }
    conn.execute(
        "INSERT INTO wealth_lock_ledger (ts, journal_ref, trade_pnl, sweep_rs, "
        "instrument, mock_price, mock_units, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (row["ts"], row["journal_ref"], row["trade_pnl"], row["sweep_rs"],
         row["instrument"], row["mock_price"], row["mock_units"], row["status"]))
    conn.commit()
    return row


def sweep_on_settlement(journal_ref: str, pnl_net: float, conn=None,
                        price_fn=None, notify: bool = True) -> dict:
    """The post-trade hook portfolio_manager calls after settling a lock.
    Never raises; returns {"swept": bool, ...} either way. When a sweep
    is recorded and notify=True, the Discord card is dispatched through
    notifier.fire_broadcast (muzzled automatically under tests)."""
    try:
        owns = conn is None
        if conn is None:
            conn = brain_map.connect()
        row = record_sweep(conn, journal_ref, pnl_net, price_fn=price_fn)
        if owns:
            conn.close()
        if row is None:
            return {"swept": False, "reason": "no profit to sweep or already swept"}
        payload = build_sweep_alert(row)
        if notify:
            from src.notifier import fire_broadcast
            fire_broadcast(payload)
        return {"swept": True, "sweep_rs": row["sweep_rs"],
                "instrument": row["instrument"], "payload": payload}
    except Exception as e:
        print(f"  (wealth lock unavailable — sweep skipped: {e})")
        return {"swept": False, "reason": str(e)}


def ledger_summary(conn) -> dict:
    """Totals for the CLI / EOD card: how much paper profit has been
    locked away so far."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(sweep_rs), 0), "
        "COALESCE(SUM(trade_pnl), 0) FROM wealth_lock_ledger").fetchone()
    return {"sweeps": int(row[0]), "total_swept_rs": round(float(row[1]), 2),
            "total_winning_pnl_rs": round(float(row[2]), 2),
            "instrument": SWEEP_INSTRUMENT}


if __name__ == "__main__":
    import json
    connection = brain_map.connect()
    print(json.dumps(ledger_summary(connection), indent=2))
    connection.close()
