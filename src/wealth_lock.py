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
  * Fail-safe seam: sweep_on_settlement never raises — a broken ledger
    prints a note and returns swept=False; settling the trade is never
    blocked (same contract as the margin gate).

THE FLYWHEEL MERGE (2026-07-20, owner directive): the earmark now becomes
a SIZED PAPER ORDER — `next_gen_engine/wealth_flywheel.build_sweep_order`
graduated into `size_sweep_order` below and its staging file was deleted
(the anti-orphan rule; the portfolio_risk_manager precedent). Whole units
only, with the un-investable remainder reported honestly as
`cash_residual_rs` — never rounded into the order.

THE SCRIP-VERIFICATION GATE — the owner's Null-Honesty condition on that
merge ("do not execute the flywheel if the ID ever fails a future master
check"). GOLDBEES is now in `SECURITY_ID_MAP` (id 14428, verified
2026-07-20), but an id that is correct today can rot tomorrow, so sizing
is gated on `goldbees_verified()`, which re-reads the weekly clerk's
report (`data/scrip_reconciliation.json`) at call time:

    verdict 'ok' AND the report is fresh   -> sizing runs
    mismatch / id_not_found                -> BLOCKED
    report missing, stale, or 'unavailable'-> BLOCKED

Blocked never means silent: the sweep is STILL recorded with the rupee
amount and `mock_units` NULL — i.e. the degraded mode is exactly the
pre-merge behavior, and the reason is stored on the row and shown on the
card. Freshness is judged AT READ TIME (the Issue-22 lesson: a `stale`
flag that never ages is how an 11-day-old file got trusted at full
weight), with a two-missed-runs tolerance since the clerk is weekly.

Never priced on a guess: with no verified id there is no quote path at
all, and `price_fn` stays injectable everywhere.

Inspect the ledger from the project folder:

    python3 -m src.wealth_lock
"""
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from src import brain_map

ROOT = Path(__file__).resolve().parent.parent
SCRIP_REPORT_PATH = ROOT / "data" / "scrip_reconciliation.json"

SWEEP_PCT = 50.0            # % of net profit earmarked per winning trade
SWEEP_INSTRUMENT = "GOLDBEES"
SWEEP_TICKER = "GOLDBEES.NS"        # the SECURITY_ID_MAP key
# The clerk runs weekly (Saturday); two missed runs means something is
# wrong with the check itself, so we stop trusting its last answer.
SCRIP_REPORT_MAX_AGE_DAYS = 14

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
    # Additive migration for the flywheel merge — pre-merge ledgers keep
    # their rows, the new columns simply read NULL on them.
    have = {r[1] for r in conn.execute(
        "PRAGMA table_info(wealth_lock_ledger)").fetchall()}
    for col, decl in (("order_qty", "INTEGER"),
                      ("cash_residual_rs", "REAL"),
                      ("sizing_blocked_reason", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE wealth_lock_ledger ADD COLUMN "
                         f"{col} {decl}")
    conn.commit()


# ------------------------------------------------- the verification gate

def goldbees_verified(report_path=None, now=None) -> dict:
    """Is our GOLDBEES security id still the one the exchange means?

    Reads the weekly scrip clerk's report and judges it AT READ TIME.
    Returns {"verified": bool, "reason": str}. Anything other than a
    fresh, explicit 'ok' is NOT verified — a missing, stale, or
    'unavailable' report can never be read as a pass (the clerk's own
    honesty rule, honored by its consumer)."""
    path = Path(report_path) if report_path else SCRIP_REPORT_PATH
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"verified": False,
                "reason": "no scrip reconciliation report — id unverified"}
    if report.get("status") != "verified":
        return {"verified": False,
                "reason": f"last scrip run was {report.get('status')} "
                          f"({report.get('code')}) — id unverified"}
    try:
        as_of = datetime.fromisoformat(report["as_of"])
        age = ((now or datetime.now()) - as_of).days
    except (KeyError, ValueError, TypeError):
        return {"verified": False,
                "reason": "scrip report carries no readable timestamp"}
    if age > SCRIP_REPORT_MAX_AGE_DAYS:
        return {"verified": False,
                "reason": f"scrip report is {age}d old (limit "
                          f"{SCRIP_REPORT_MAX_AGE_DAYS}d) — id unverified"}
    for row in report.get("rows") or []:
        if row.get("ticker") == SWEEP_TICKER:
            if row.get("verdict") == "ok":
                return {"verified": True,
                        "reason": f"scrip-verified {age}d ago (id "
                                  f"{row.get('id')})"}
            return {"verified": False,
                    "reason": f"scrip check FAILED: {row.get('verdict')} — "
                              + str(row.get("detail") or "")}
    return {"verified": False,
            "reason": f"{SWEEP_TICKER} absent from the scrip report"}


def default_price_fn(instrument: str = SWEEP_INSTRUMENT):
    """The live ETF quote, through the ONE market-data door (#48). Returns
    None — never raises, never guesses — when unavailable, which degrades
    the sweep to earmark-only. Silent under pytest: a unit test must never
    reach the network."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:
        from src.dhan_guard import SafeDhanClient
        return SafeDhanClient().get_live_price(SWEEP_TICKER)
    except Exception as e:
        print(f"  (wealth lock: GOLDBEES quote unavailable [{e}])")
        return None


def size_sweep_order(earmark_rs: float, etf_price) -> dict:
    """Earmarked rupees + an ETF price -> whole GOLDBEES units.

    Graduated from next_gen_engine/wealth_flywheel.build_sweep_order.
    Whole units only; the un-investable remainder is reported as
    `cash_residual_rs`, never rounded into the order. No usable price, or
    an earmark that can't buy one whole unit -> qty None and the full
    amount stays residual (honest abstention, it accumulates)."""
    if not etf_price or etf_price <= 0:
        return {"qty": None, "price": None, "notional_rs": None,
                "cash_residual_rs": earmark_rs}
    qty = math.floor(earmark_rs / etf_price)
    if qty < 1:
        return {"qty": None, "price": etf_price, "notional_rs": None,
                "cash_residual_rs": earmark_rs}
    notional = round(qty * etf_price, 2)
    return {"qty": qty, "price": etf_price, "notional_rs": notional,
            "cash_residual_rs": round(earmark_rs - notional, 2)}


def build_sweep_alert(entry: dict) -> dict:
    """A wealth_lock_ledger row dict -> the broadcast_alert payload for
    the sweep card. Pure function — tests assert on this exact shape."""
    amount = float(entry["sweep_rs"])
    units = entry.get("mock_units")
    qty = entry.get("order_qty")
    blocked = entry.get("sizing_blocked_reason")
    description = (f"🔒 PAPER SWEEP REQUIRED: Buy Rs.{amount:,.2f} of "
                   f"{entry['instrument']}")
    if qty:
        description += (f" — PAPER ORDER: {qty} unit(s) @ "
                        f"Rs.{entry['mock_price']:,.2f} "
                        f"(Rs.{entry.get('cash_residual_rs', 0):,.2f} "
                        "residual carries forward)")
    elif units is not None:
        description += f" (≈{units:.2f} units @ Rs.{entry['mock_price']:,.2f})"
    if blocked:
        # Say it out loud: an un-sized sweep is a fact about our data, not
        # a quiet no-op the owner should have to infer from a missing line.
        description += f"\n⚠ sizing skipped — {blocked}"
    return {
        "event": "wealth_sweep",
        "ticker": entry["instrument"],
        "date": (entry.get("ts") or _now_iso())[:10],
        "description": description,
        "sweep_rs": round(amount, 2),
        "trade_pnl": round(float(entry["trade_pnl"]), 2),
        "sweep_pct": SWEEP_PCT,
        "mock_units": units,
        "order_qty": qty,
        "cash_residual_rs": entry.get("cash_residual_rs"),
        "sizing_blocked_reason": blocked,
        "short_id": entry.get("journal_ref"),
    }


def record_sweep(conn, journal_ref: str, pnl_net: float,
                 price_fn=None, scrip_report_path=None) -> dict | None:
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

    # THE GATE (owner's Null-Honesty condition): price GOLDBEES only while
    # its security id is provably still GOLDBEES. Blocked = earmark-only,
    # never a guessed price and never a silent skip — the reason is stored.
    gate = goldbees_verified(report_path=scrip_report_path)
    blocked = None if gate["verified"] else gate["reason"]
    mock_price = None
    if not blocked:
        fetch = price_fn if price_fn is not None else default_price_fn
        try:
            raw = fetch(SWEEP_INSTRUMENT)
            mock_price = float(raw) if raw else None
        except Exception as e:
            print(f"  (wealth lock: price lookup failed, storing amount "
                  f"only: {e})")
    order = size_sweep_order(sweep_rs, mock_price)
    mock_units = (round(sweep_rs / mock_price, 4)
                  if mock_price and mock_price > 0 else None)
    row = {
        "ts": _now_iso(), "journal_ref": journal_ref, "trade_pnl": pnl,
        "sweep_rs": sweep_rs, "instrument": SWEEP_INSTRUMENT,
        "mock_price": mock_price, "mock_units": mock_units,
        "order_qty": order["qty"],
        "cash_residual_rs": order["cash_residual_rs"],
        "sizing_blocked_reason": blocked, "status": "logged",
    }
    conn.execute(
        "INSERT INTO wealth_lock_ledger (ts, journal_ref, trade_pnl, sweep_rs, "
        "instrument, mock_price, mock_units, order_qty, cash_residual_rs, "
        "sizing_blocked_reason, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["ts"], row["journal_ref"], row["trade_pnl"], row["sweep_rs"],
         row["instrument"], row["mock_price"], row["mock_units"],
         row["order_qty"], row["cash_residual_rs"],
         row["sizing_blocked_reason"], row["status"]))
    conn.commit()
    return row


def sweep_on_settlement(journal_ref: str, pnl_net: float, conn=None,
                        price_fn=None, notify: bool = True,
                        scrip_report_path=None) -> dict:
    """The post-trade hook portfolio_manager calls after settling a lock.
    Never raises; returns {"swept": bool, ...} either way. When a sweep
    is recorded and notify=True, the Discord card is dispatched through
    notifier.fire_broadcast (muzzled automatically under tests)."""
    try:
        owns = conn is None
        if conn is None:
            conn = brain_map.connect()
        row = record_sweep(conn, journal_ref, pnl_net, price_fn=price_fn,
                           scrip_report_path=scrip_report_path)
        if owns:
            conn.close()
        if row is None:
            return {"swept": False, "reason": "no profit to sweep or already swept"}
        payload = build_sweep_alert(row)
        if notify:
            from src.notifier import fire_broadcast
            fire_broadcast(payload)
        return {"swept": True, "sweep_rs": row["sweep_rs"],
                "instrument": row["instrument"],
                "order_qty": row.get("order_qty"),
                "sizing_blocked_reason": row.get("sizing_blocked_reason"),
                "payload": payload}
    except Exception as e:
        print(f"  (wealth lock unavailable — sweep skipped: {e})")
        return {"swept": False, "reason": str(e)}


def ledger_summary(conn) -> dict:
    """Totals for the CLI / EOD card: how much paper profit has been
    locked away so far."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(sweep_rs), 0), "
        "COALESCE(SUM(trade_pnl), 0), COALESCE(SUM(order_qty), 0), "
        "COALESCE(SUM(cash_residual_rs), 0), "
        "SUM(CASE WHEN sizing_blocked_reason IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM wealth_lock_ledger").fetchone()
    return {"sweeps": int(row[0]), "total_swept_rs": round(float(row[1]), 2),
            "total_winning_pnl_rs": round(float(row[2]), 2),
            "total_units": int(row[3]),
            "uninvested_residual_rs": round(float(row[4]), 2),
            # Precisely: rows the ID GATE refused to size. Pre-merge rows
            # are NOT counted here — they predate sizing entirely and read
            # as units 0, which is what they honestly are.
            "sweeps_blocked_by_id_gate": int(row[5] or 0),
            "instrument": SWEEP_INSTRUMENT,
            "id_status": goldbees_verified()}


if __name__ == "__main__":
    import json
    connection = brain_map.connect()
    print(json.dumps(ledger_summary(connection), indent=2))
    connection.close()
