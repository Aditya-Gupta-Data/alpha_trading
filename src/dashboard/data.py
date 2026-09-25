# MANUAL OFFLINE TOOL — data layer for the Streamlit showcase (decision #111).
"""
src/dashboard/data.py — READ-ONLY readers behind the showcase dashboard.

Every function here opens `brain_map.db` in SQLite read-only mode
(`file:...?mode=ro`, 5 s busy timeout) and swallows a locked/absent database
into an honest `{"error": ...}` instead of a crash — the live engine on the
VM may be writing the very file being read. JSON ledgers are read whole and
parsed line by line; a bad line is skipped, never invented. Nothing here
writes, quotes a broker, or imports an execution path.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

ROOT = Path(__file__).resolve().parents[2]
# ALPHA_DATA_DIR points the whole page at a mirror of the VM's files (see
# scripts/pull_dashboard_data.sh); default = this checkout's own data/ + logs/.
_MIRROR = os.environ.get("ALPHA_DATA_DIR")
_DATA = Path(_MIRROR) if _MIRROR else ROOT / "data"
_LOGS = Path(_MIRROR) if _MIRROR else ROOT / "logs"
DB_PATH = Path(os.environ.get("ALPHA_DB_PATH") or _DATA / "brain_map.db")
JOURNAL_PATH = Path(os.environ.get("ALPHA_JOURNAL_PATH") or _DATA / "journal.jsonl")
EQUITY_LEDGER_PATH = Path(os.environ.get("ALPHA_EQUITY_LEDGER") or _LOGS / "equity_shadow_journal.jsonl")
SNAPSHOT_PATH = _DATA / "market_snapshot.json"
RECON_PATH = _LOGS / "recon.jsonl"
# THE ₹10L BASE (decisions #116/#117, owner 2026-09-25): the primary's
# COMPOUNDING figures (return, CAGR) start here. Before it the pool was reset
# to ₹2L (07-21 clean sheet) and topped up by ₹8L (08-07 16:41 injection) —
# capital moves, not trading. The CURVE shows the full history (#117) with
# those moves drawn as labelled markers (`capital_events`).
CURVE_EPOCH = "2026-08-07"
CAPITAL_EVENT_TYPES = ("clean_sheet", "capital_injection")
STRATEGY_LABELS = {"bear_put_spread": "Bear Put", "bull_call_spread": "Bull Call",
                   "iron_condor": "Iron Condor", "iron_butterfly": "Iron Butterfly",
                   "equity_long": "Equity Long"}


def connect_ro(db_path=None, timeout: float = 5.0):
    """Read-only connection or None. Never raises."""
    p = Path(db_path or DB_PATH)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _q(conn, sql, args=()):
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.Error as exc:            # locked, missing table, etc.
        return {"error": f"{type(exc).__name__}: {exc}"}


def _jsonl(path) -> list:
    out = []
    try:
        for ln in Path(path).read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return out


# ------------------------------------------------------------- treasury
def cagr(start_capital: float, equity: float, days: float) -> float | None:
    """Annualised compound growth, percent: ((equity / start) ** (365 / days) − 1) × 100.
    None when fewer than one full day has elapsed or the inputs are not positive —
    annualising a few hours is noise, not a rate."""
    try:
        if days is None or days < 1 or start_capital <= 0 or equity <= 0:
            return None
        return round(((float(equity) / float(start_capital)) ** (365.0 / float(days)) - 1.0) * 100.0, 2)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def _days_since(iso_ts: str | None, now: datetime = None) -> float | None:
    if not iso_ts:
        return None
    try:
        t = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.now(IST)
    if t.tzinfo is None:
        t = t.replace(tzinfo=IST)                    # the VM writes naive IST
    return round((now - t).total_seconds() / 86400.0, 2)


def _run_epoch(conn) -> str | None:
    """The measurement epoch the firm MTM line also uses: the latest
    clean-sheet reset, else the account's birth."""
    row = _q(conn, "SELECT ts FROM account_events WHERE event_type = 'clean_sheet' ORDER BY ts DESC LIMIT 1")
    if isinstance(row, list) and row:
        return row[0]["ts"]
    row = _q(conn, "SELECT created_at FROM account_state WHERE id = 1")
    return row[0]["created_at"] if isinstance(row, list) and row else None


def _lakh(rs: float) -> str:
    v = rs / 100_000
    return f"₹{v:g}L" if abs(v - round(v)) < 1e-9 else f"₹{v:.2f}L"


def capital_events(conn) -> list:
    """[{ts, kind, label, detail}] — the pool moves on the primary's
    `account_events` trail (a reset or an injection moves equity without a
    trade), oldest first, for the chart's markers. Labels are read from the
    event itself; an amount that cannot be parsed is left out, not guessed."""
    import re
    rows = _q(conn, "SELECT ts, event_type, detail FROM account_events WHERE event_type IN (?, ?) "
                    "ORDER BY ts, rowid", CAPITAL_EVENT_TYPES)
    out = []
    for r in (rows if isinstance(rows, list) else []):
        detail = str(r.get("detail") or "")
        if r["event_type"] == "capital_injection":
            m = re.match(r"\s*Rs\.(-?[\d,]+(?:\.\d+)?)", detail)
            amt = float(m.group(1).replace(",", "")) if m else None
            label = ("capital injection" if amt is None else
                     f"{_lakh(abs(amt))} capital {'injection' if amt >= 0 else 'withdrawal'}")
            short = ("Capital" if amt is None else f"{'+' if amt >= 0 else '−'}{_lakh(abs(amt))}")
        else:
            m = re.search(r"(\d+(?:\.\d+)?)L\s*->\s*(\d+(?:\.\d+)?)L", detail)
            label = (f"Pool reset ₹{m.group(1)}L → ₹{m.group(2)}L" if m else "Pool reset (clean sheet)")
            short = f"Reset → ₹{m.group(2)}L" if m else "Reset"
        out.append({"ts": r["ts"], "kind": r["event_type"], "label": label, "short": short,
                    "detail": detail[:240]})
    return out


def unrealized_by_account(conn, snapshot: dict = None, rows: list = None) -> dict:
    """{account: {unrealized_pnl, marked_positions, open_positions}} from the
    ENGINE'S published marks (`market_snapshot.json` — the same marks the
    Discord card's ladder reads first). Never a fresh quote. The primary sums
    the marked open positions; a shadow account scales each mark by its own
    lots / the primary's lots (the #102 settlement ratio). A position with no
    mark is COUNTED but not priced — `unrealized_pnl` is None when nothing
    is priced, never a guessed zero."""
    snap = _snapshot() if snapshot is None else snapshot
    marks = {m.get("short_id"): m for m in (snap or {}).get("marks") or [] if isinstance(m, dict)}
    rows = open_trades(snapshot_marks=marks) if rows is None else rows
    priced = [float(r["mtm_rs"]) for r in rows if r.get("mtm_rs") is not None]
    out = {"PAPER_10L": {"unrealized_pnl": round(sum(priced), 2) if priced else None,
                         "marked_positions": len(priced), "open_positions": len(rows)}}
    locks = _q(conn, "SELECT account_id, journal_ref, lots, primary_lots FROM paper_margin_locks "
                     "WHERE released_at IS NULL")
    for l in (locks if isinstance(locks, list) else []):
        a = out.setdefault(l["account_id"], {"unrealized_pnl": None, "marked_positions": 0,
                                            "open_positions": 0})
        a["open_positions"] += 1
        m = marks.get(l["journal_ref"]) or {}
        if m.get("live_pnl_rs") is None or not l["primary_lots"]:
            continue
        pnl = float(m["live_pnl_rs"]) * float(l["lots"]) / float(l["primary_lots"])
        a["unrealized_pnl"] = round((a["unrealized_pnl"] or 0.0) + pnl, 2)
        a["marked_positions"] += 1
    return out


def _snapshot() -> dict:
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
        return snap if isinstance(snap, dict) else {}
    except (OSError, ValueError):
        return {}


def _with_mtm(acct: dict, u: dict | None, as_of) -> dict:
    """True Net Equity = realized equity + unrealized (None when unpriced)."""
    u = u or {"unrealized_pnl": None, "marked_positions": 0, "open_positions": 0}
    acct.update(u, marks_as_of=as_of,
                net_equity=(round(acct["equity"] + u["unrealized_pnl"], 2)
                            if u["unrealized_pnl"] is not None else None))
    return acct


def treasury(db_path=None) -> dict:
    """{'PAPER_10L': {...}, 'PAPER_2L': {...}, 'error'?} — equity, realized,
    drawdown, locks per account, straight from the account tables."""
    conn = connect_ro(db_path)
    if conn is None:
        return {"error": f"database unavailable or locked: {db_path or DB_PATH}"}
    out = {}
    try:
        rows = _q(conn, "SELECT starting_capital, realized_pnl, peak_equity FROM account_state WHERE id = 1")
        if isinstance(rows, dict):
            return rows
        if rows:
            a = rows[0]
            eq = a["starting_capital"] + a["realized_pnl"]
            locks = _q(conn, "SELECT COALESCE(SUM(margin_rs),0) AS m, COUNT(*) AS n FROM margin_locks "
                             "WHERE released_at IS NULL")
            m = locks[0] if isinstance(locks, list) and locks else {"m": 0, "n": 0}
            peak = max(float(a["peak_equity"] or eq), eq)
            base = _q(conn, "SELECT ts, equity FROM equity_curve WHERE ts >= ? ORDER BY ts, rowid LIMIT 1",
                      (CURVE_EPOCH,))
            base = base[0] if isinstance(base, list) and base else None
            base_eq = float(base["equity"]) if base else float(a["starting_capital"])
            days = _days_since(base["ts"] if base else _run_epoch(conn))
            out["PAPER_10L"] = {"account_id": "PAPER_10L", "starting_capital": a["starting_capital"],
                                "realized_pnl": round(a["realized_pnl"], 2), "equity": round(eq, 2),
                                "peak_equity": peak,
                                "drawdown_pct": round((peak - eq) / peak * 100, 4) if peak else 0.0,
                                "locked_margin": round(float(m["m"]), 2), "open_locks": int(m["n"]),
                                "available_cash": round(eq - float(m["m"]), 2),
                                # compounding (#114, re-based by #116): from the ₹10L
                                # base = the first curve point on/after CURVE_EPOCH
                                "base_equity": round(base_eq, 2),
                                "base_ts": base["ts"] if base else None,
                                "days_elapsed": days,
                                "abs_return_pct": round((eq / base_eq - 1) * 100, 2) if base_eq else None,
                                "cagr_pct": cagr(base_eq, eq, days)}
        pa = _q(conn, "SELECT account_id, starting_capital, realized_pnl, peak_equity, created_at FROM paper_accounts")
        if isinstance(pa, list):
            for a in pa:
                acct = a["account_id"]
                eq = a["starting_capital"] + a["realized_pnl"]
                locks = _q(conn, "SELECT COALESCE(SUM(margin_rs),0) AS m, COUNT(*) AS n FROM paper_margin_locks "
                                 "WHERE account_id = ? AND released_at IS NULL", (acct,))
                m = locks[0] if isinstance(locks, list) and locks else {"m": 0, "n": 0}
                rej = _q(conn, "SELECT COUNT(*) AS n FROM paper_account_events WHERE account_id = ? AND "
                               "event_type IN ('margin_exhaustion','sizing_refused')", (acct,))
                peak = max(float(a["peak_equity"] or eq), eq)
                days = _days_since(a.get("created_at"))
                out[acct] = {"account_id": acct, "starting_capital": a["starting_capital"],
                             "realized_pnl": round(a["realized_pnl"], 2), "equity": round(eq, 2),
                             "peak_equity": peak,
                             "drawdown_pct": round((peak - eq) / peak * 100, 4) if peak else 0.0,
                             "locked_margin": round(float(m["m"]), 2), "open_locks": int(m["n"]),
                             "available_cash": round(eq - float(m["m"]), 2),
                             "rejections": int(rej[0]["n"]) if isinstance(rej, list) and rej else None,
                             "days_elapsed": days,
                             "abs_return_pct": round((eq / a["starting_capital"] - 1) * 100, 2) if a["starting_capital"] else None,
                             "cagr_pct": cagr(a["starting_capital"], eq, days)}
        curve = _q(conn, "SELECT ts, equity, drawdown_pct FROM equity_curve ORDER BY ts, rowid")
        out["equity_curve"] = curve if isinstance(curve, list) else []
        out["base_epoch"] = CURVE_EPOCH
        out["capital_events"] = capital_events(conn)
        # unrealized P&L + True Net Equity per account (engine snapshot marks)
        try:
            snap = _snapshot()
            u = unrealized_by_account(conn, snap)
        except Exception:
            snap, u = {}, {}
        for acct in [k for k in out if k.startswith("PAPER_")]:
            _with_mtm(out[acct], u.get(acct), snap.get("as_of"))
    finally:
        conn.close()
    return out


# ----------------------------------------------------------- open trades
def _snapshot_marks() -> dict:
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
        return {m.get("short_id"): m for m in snap.get("marks") or [] if isinstance(m, dict)}
    except (OSError, ValueError):
        return {}


def open_trades(journal_path=None, equity_ledger_path=None, snapshot_marks: dict = None) -> list:
    """One row per open position: symbol, strategy, account(s), lots/qty,
    entry, MTM (engine snapshot when present — never a fresh quote here),
    and the profit-ratchet state (peak / lock / armed) for directional
    spreads."""
    marks = _snapshot_marks() if snapshot_marks is None else snapshot_marks
    rows = []
    for e in _jsonl(journal_path or JOURNAL_PATH):
        s = e.get("spread")
        if not s or e.get("decision") != "approved" or e.get("outcome") is not None:
            continue
        r = e.get("ratchet") or {}
        m = marks.get(e.get("short_id")) or {}
        accounts = ["PAPER_10L"] + [a for a, v in (e.get("accounts") or {}).items()
                                    if (v or {}).get("status") == "approved"]
        rows.append({"id": e.get("short_id"), "symbol": e.get("ticker"),
                     "strategy": STRATEGY_LABELS.get(s.get("strategy"), s.get("strategy")),
                     "direction": s.get("direction"), "accounts": ", ".join(accounts),
                     "lots": s.get("lots"), "entered": e.get("date"), "expiry": s.get("expiry"),
                     "max_loss_rs": round(float(s.get("max_loss") or 0) * int(s.get("lots") or 1), 2),
                     "mtm_rs": m.get("live_pnl_rs"), "capture_pct": m.get("capture_pct"),
                     "ratchet_peak_pct": r.get("peak_capture_pct"),
                     "ratchet_lock_pct": r.get("locked_pct"),
                     "ratchet": ("armed → lock %s%%" % r.get("locked_pct") if r.get("armed")
                                 else ("unarmed" if s.get("strategy") in ("bear_put_spread", "bull_call_spread")
                                       else "static 65% take")),
                     "sizing": ((e.get("sizing") or {}).get("reason") or None)})
    entries = {}
    for x in _jsonl(equity_ledger_path or EQUITY_LEDGER_PATH):
        if x.get("event") == "entry" and (x.get("funding") or {}).get("funded"):
            entries[x.get("id")] = x
        elif x.get("event") == "exit":
            entries.pop(x.get("id"), None)
    for x in entries.values():
        a = x.get("kya_kara_action") or {}
        rows.append({"id": (x.get("funding") or {}).get("lock_ref") or f"eqd:{x.get('id')}",
                     "symbol": x.get("ticker"), "strategy": "Equity Long", "direction": "bullish",
                     "accounts": "PAPER_10L", "lots": (x.get("funding") or {}).get("qty"),
                     "entered": x.get("as_of"), "expiry": None,
                     "max_loss_rs": (round((float(a["entry_price"]) - float(a["stop"])) * int((x.get("funding") or {}).get("qty") or 0), 2)
                                     if a.get("entry_price") is not None and a.get("stop") is not None else None),
                     "mtm_rs": None, "capture_pct": None, "ratchet_peak_pct": None, "ratchet_lock_pct": None,
                     "ratchet": "ATR trail (3×ATR14)", "sizing": None})
    return rows


# ------------------------------------------------------------- recon
def latest_recon(path=None) -> dict | None:
    rows = _jsonl(path or RECON_PATH)
    return rows[-1] if rows else None


def recon_history(path=None, n: int = 30) -> list:
    rows = _jsonl(path or RECON_PATH)
    return [{"ts": r.get("ts"), "verdict": r.get("verdict"), "broker_positions": r.get("broker_positions"),
             "book_rows": r.get("book_rows"), "mismatches": len(r.get("mismatches") or [])}
            for r in rows[-n:]]


# --------------------------------------------------------- audit events
def audit_events(db_path=None, n: int = 60) -> list:
    """The latest account + shadow-account events, newest first."""
    conn = connect_ro(db_path)
    if conn is None:
        return [{"ts": None, "account": "-", "event_type": "database unavailable or locked", "detail": ""}]
    try:
        a = _q(conn, "SELECT ts, 'PAPER_10L' AS account, event_type, detail FROM account_events "
                     "ORDER BY ts DESC LIMIT ?", (n,))
        b = _q(conn, "SELECT ts, account_id AS account, event_type, "
                     "COALESCE(journal_ref, '') || ' ' || COALESCE(detail, '') AS detail "
                     "FROM paper_account_events ORDER BY ts DESC LIMIT ?", (n,))
    finally:
        conn.close()
    rows = (a if isinstance(a, list) else []) + (b if isinstance(b, list) else [])
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return rows[:n]


def recent_outcomes(journal_path=None, n: int = 25) -> list:
    rows = []
    for e in _jsonl(journal_path or JOURNAL_PATH):
        o = e.get("outcome") or {}
        if not o or e.get("decision") != "approved":
            continue
        rows.append({"settled": str(o.get("settled_at") or o.get("exit_date") or "")[:16],
                     "symbol": e.get("ticker"),
                     "strategy": STRATEGY_LABELS.get((e.get("spread") or {}).get("strategy"),
                                                     (e.get("spread") or {}).get("strategy")),
                     "resolution": o.get("resolution"), "pnl_rs": o.get("pnl_rs"),
                     "r_multiple": o.get("r_multiple"),
                     "ticket": (o.get("execution") or {}).get("ticket_id")})
    rows.sort(key=lambda r: r["settled"], reverse=True)
    return rows[:n]


def freshness() -> dict:
    """When each source was last written — the honest 'as of' line."""
    out = {}
    for name, p in (("brain_map.db", DB_PATH), ("journal", JOURNAL_PATH),
                    ("market_snapshot", SNAPSHOT_PATH), ("recon", RECON_PATH)):
        try:
            out[name] = datetime.fromtimestamp(Path(p).stat().st_mtime, tz=IST).isoformat(timespec="minutes")
        except OSError:
            out[name] = None
    return out
