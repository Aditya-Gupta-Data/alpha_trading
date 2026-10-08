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
from datetime import date, datetime, timedelta, timezone
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
BENCHMARKS_PATH = _DATA / "dashboard_benchmarks.json"
RECON_PATH = _LOGS / "recon.jsonl"
# THE ₹10L BASE (decisions #116/#117, owner 2026-09-25): the primary's
# COMPOUNDING figures (return, CAGR) start here. Before it the pool was reset
# to ₹2L (07-21 clean sheet) and topped up by ₹8L (08-07 16:41 injection) —
# capital moves, not trading. The CURVE shows the full history (#117) with
# those moves drawn as labelled markers (`capital_events`).
CURVE_EPOCH = "2026-08-07"
CAPITAL_EVENT_TYPES = ("clean_sheet", "capital_injection")
LIVE_ACCOUNTS = ("PAPER_2L_LIVE",)     # the live-quote arm (#120): marks itself
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


def benchmarks(base_rs: float, path=None, until: date = None) -> dict:
    """The Compounding chart's optional lines (decision #119) from
    `dashboard_benchmarks.json` (built on the VM, mirrored here) — Nifty 50
    and GOLDBEES rebased to `base_rs` at the epoch, plus a 7% daily-
    compounded FD. Missing file = FD only + a note per empty line."""
    from src.dashboard import benchmarks as bm
    raw = None
    try:
        raw = json.loads(Path(path or BENCHMARKS_PATH).read_text())
    except (OSError, ValueError):
        raw = None
    return bm.normalize(raw, base_rs, until or datetime.now(IST).date())


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


MARKET_OPEN_HM, MARKET_CLOSE_HM = (9, 15), (15, 30)     # market_loop's session (not imported: execution path)


def _market_seconds(start: datetime, end: datetime, cap: float) -> float:
    """Seconds of NSE session time (MARKET_OPEN_HM-MARKET_CLOSE_HM on a
    trading day) from `start` to `end`, naive IST — counted only until it
    passes `cap`, which is all a staleness check needs (a years-old stamp is
    never walked day by day). Nights, weekends and holidays add nothing: a
    15:29 mark is one market-minute old at 20:00 and at 09:15 the next
    session, sixteen at 09:30 — so the open's first push is not flagged for
    the overnight gap (review of Fix D), while a mark that stopped at 11:00
    still is in the evening."""
    from src import nse_calendar
    total, day = 0.0, start.date()
    while day <= end.date() and total <= cap:
        if nse_calendar.is_trading_day(day):
            lo = max(start, datetime(day.year, day.month, day.day, *MARKET_OPEN_HM))
            hi = min(end, datetime(day.year, day.month, day.day, *MARKET_CLOSE_HM))
            total += max(0.0, (hi - lo).total_seconds())
        day += timedelta(days=1)
    return total


def _captured_at(db_path):
    """When the brain_map copy being read was taken, naive IST: its file
    mtime. On the dashboard box that is the moment cron #33's
    mirror_snapshot made the copy (rsync -a keeps it); on a Mac pull, the
    pull; on a live file, its last commit. None when unreadable — staleness
    is then judged on the viewer's clock (the side that flags, not hides)."""
    try:
        return datetime.fromtimestamp(Path(db_path).stat().st_mtime, tz=IST).replace(tzinfo=None)
    except (OSError, ValueError, OverflowError, TypeError):
        return None


def _live_mark_stale(r, ref: datetime, max_age_s: float):
    """True when the row's mark — or the chain quotes it was priced on, which
    a failing chain door can leave hours old under a fresh mark time (audit
    F19) — is more than `max_age_s` of MARKET time old at `ref`; a row never
    marked ages from its open. None when no time parses (unknown, never
    guessed)."""
    stamps = []
    for k in (("last_mark_ts", "quote_ts") if r["last_mark_ts"] else ("opened_at",)):
        try:
            stamps.append(datetime.fromisoformat(str(r[k])).replace(tzinfo=None))
        except (TypeError, ValueError):
            continue
    if not stamps:
        return None
    return _market_seconds(min(stamps), ref, max_age_s) > max_age_s


def _live_positions(conn):
    """THE one reader of PAPER_2L_LIVE's own book (`paper_live_positions`,
    decision #120) on this page — the treasury card's unrealized P&L and
    staleness (audit F02, Fix D) and the open-trades table's live rows
    (audit F10) both read it. Every open or exiting row, oldest first; a
    `{"error": ...}` dict when the table cannot be read."""
    return _q(conn, "SELECT account_id, journal_ref, ticker, strategy, direction, expiry, lots, lot_size, "
                    "entry_mark_ps, max_profit_ps, max_loss_ps, opened_at, ratchet_peak_pct, "
                    "ratchet_lock_pct, last_mark_ts, last_mark_ps, last_profit_ps, last_capture_pct, "
                    "quote_ts, state FROM paper_live_positions WHERE state != 'closed' "
                    "ORDER BY opened_at, rowid")


def _stale_policy(now: datetime = None, captured_at: datetime = None) -> tuple:
    """(max_age_s, ref) for `_live_mark_stale`: 2 x LIVE_QUOTE_INTERVAL_SECONDS
    of market time (None = unknown, never guessed), judged as of the earlier
    of `now` and the copy's capture time (review of Fix D)."""
    try:
        from src.config import LIVE_QUOTE_INTERVAL_SECONDS
        max_age_s = 2.0 * float(LIVE_QUOTE_INTERVAL_SECONDS)
    except Exception:
        max_age_s = None                # unknown interval -> staleness unknown, never guessed
    ref = _as_naive_ist(now or datetime.now(IST))
    if captured_at is not None:
        ref = min(ref, _as_naive_ist(captured_at))
    return max_age_s, ref


def unrealized_by_account(conn, snapshot: dict = None, rows: list = None, now: datetime = None,
                          captured_at: datetime = None) -> dict:
    """{account: {unrealized_pnl, marked_positions, open_positions}} from the
    ENGINE'S published marks (`market_snapshot.json` — the same marks the
    Discord card's ladder reads first). Never a fresh quote. The primary sums
    the marked open positions; a shadow account scales each mark by its own
    lots / the primary's lots (the #102 settlement ratio). A position with no
    mark is COUNTED but not priced — `unrealized_pnl` is None when nothing
    is priced, never a guessed zero.

    The live-quote arm (#120) prices itself, so its account also carries
    `last_mark_ts` (its OLDEST open mark) and `mark_stale` (audit F02): True
    when any open row's mark is older than 2 x LIVE_QUOTE_INTERVAL_SECONDS
    (`mark_stale_after_s`) of market time — the arm abstains or holds in
    silence for days while its last mark keeps being added here as if it
    were current. None = unknown.

    The age is judged AS OF `mark_stale_as_of` = the earlier of `now` and
    `captured_at`, the moment this copy of the data was taken (treasury()
    passes the database file's mtime). The deployed page reads a mirror
    pushed every 15 minutes (cron #33); judged on the viewer's clock a
    healthy arm went stale for ~8 of every 15 minutes between pushes
    (review of Fix D). A dead loop is still caught: the pushes go on, the
    marks in them do not."""
    snap = _snapshot() if snapshot is None else snapshot
    marks = {m.get("short_id"): m for m in (snap or {}).get("marks") or [] if isinstance(m, dict)}
    live = _live_positions(conn)
    rows = open_trades(snapshot_marks=marks, live=live, now=now, captured_at=captured_at) \
        if rows is None else rows
    # the PRIMARY's rows only: the live arm's own rows (audit F10) are priced below
    rows = [r for r in rows if r.get("account", "PAPER_10L") == "PAPER_10L"]
    priced = [float(r["mtm_rs"]) for r in rows if r.get("mtm_rs") is not None]
    out = {"PAPER_10L": {"unrealized_pnl": round(sum(priced), 2) if priced else None,
                         "marked_positions": len(priced), "open_positions": len(rows)}}
    locks = _q(conn, "SELECT account_id, journal_ref, lots, primary_lots FROM paper_margin_locks "
                     "WHERE released_at IS NULL")
    for l in (locks if isinstance(locks, list) else []):
        a = out.setdefault(l["account_id"], {"unrealized_pnl": None, "marked_positions": 0,
                                            "open_positions": 0})
        if l["account_id"] in LIVE_ACCOUNTS:
            continue                    # priced below from its own crossed marks (#120)
        a["open_positions"] += 1
        m = marks.get(l["journal_ref"]) or {}
        if m.get("live_pnl_rs") is None or not l["primary_lots"]:
            continue
        pnl = float(m["live_pnl_rs"]) * float(l["lots"]) / float(l["primary_lots"])
        a["unrealized_pnl"] = round((a["unrealized_pnl"] or 0.0) + pnl, 2)
        a["marked_positions"] += 1
    # decision #120: the live-quote arm marks itself on crossed bid/ask —
    # its unrealized is last_profit_ps x qty from paper_live_positions.
    max_age_s, ref = _stale_policy(now, captured_at)
    flags, oldest = {}, {}
    for r in (live if isinstance(live, list) else []):
        a = out.setdefault(r["account_id"], {"unrealized_pnl": None, "marked_positions": 0,
                                            "open_positions": 0})
        a["open_positions"] += 1
        flags.setdefault(r["account_id"], []).append(
            _live_mark_stale(r, ref, max_age_s) if max_age_s is not None else None)
        if r["last_mark_ts"] and str(r["last_mark_ts"]) < oldest.get(r["account_id"], "~"):
            oldest[r["account_id"]] = str(r["last_mark_ts"])
        if r["last_profit_ps"] is None:
            continue
        a["unrealized_pnl"] = round((a["unrealized_pnl"] or 0.0)
                                    + float(r["last_profit_ps"]) * int(r["lots"]) * int(r["lot_size"]), 2)
        a["marked_positions"] += 1
    for acct in [k for k in out if k in LIVE_ACCOUNTS or k in flags]:
        f = flags.get(acct, [])
        out[acct]["last_mark_ts"] = oldest.get(acct)
        out[acct]["mark_stale"] = True if True in f else (None if None in f else False)
        out[acct]["mark_stale_as_of"] = ref.isoformat(timespec="seconds")
        out[acct]["mark_stale_after_s"] = max_age_s
    return out


def _as_naive_ist(t: datetime) -> datetime:
    """An aware time converted to IST, then naive — the engine stamps its
    marks in naive IST; a naive time is taken as IST already."""
    return t.astimezone(IST).replace(tzinfo=None) if t.tzinfo else t


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


def treasury(db_path=None, now: datetime = None) -> dict:
    """{'PAPER_10L': {...}, 'PAPER_2L': {...}, 'error'?} — equity, realized,
    drawdown, locks per account, straight from the account tables. `now`
    defaults to the wall clock (tests inject it)."""
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
            # decision #119: E0 = CONTRIBUTED capital (₹2L reset + ₹8L top-up =
            # account_state.starting_capital, which inject_capital moves and
            # nothing else does) — NOT the post-injection equity, which would
            # bury the ₹2L era's trading profit inside the principal. Days run
            # from the last capital move on/after the epoch.
            inj = _q(conn, "SELECT ts FROM account_events WHERE event_type = 'capital_injection' "
                           "AND ts >= ? ORDER BY ts DESC LIMIT 1", (CURVE_EPOCH,))
            base_ts = (inj[0]["ts"] if isinstance(inj, list) and inj else None)
            if base_ts is None:
                first = _q(conn, "SELECT ts FROM equity_curve WHERE ts >= ? ORDER BY ts, rowid LIMIT 1",
                           (CURVE_EPOCH,))
                base_ts = first[0]["ts"] if isinstance(first, list) and first else None
            base_eq = float(a["starting_capital"])
            days = _days_since(base_ts or _run_epoch(conn))
            out["PAPER_10L"] = {"account_id": "PAPER_10L", "starting_capital": a["starting_capital"],
                                "realized_pnl": round(a["realized_pnl"], 2), "equity": round(eq, 2),
                                "peak_equity": peak,
                                "drawdown_pct": round((peak - eq) / peak * 100, 4) if peak else 0.0,
                                "locked_margin": round(float(m["m"]), 2), "open_locks": int(m["n"]),
                                "available_cash": round(eq - float(m["m"]), 2),
                                # compounding (#114, re-based by #116): from the ₹10L
                                # base = the first curve point on/after CURVE_EPOCH
                                "base_equity": round(base_eq, 2),
                                "base_ts": base_ts,
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
        # decision #119: passive-alternative lines, rebased to the contributed
        # capital on the epoch; read from the mirrored file, never a quote.
        try:
            base_rs = float(out["PAPER_10L"]["starting_capital"]) if "PAPER_10L" in out else 1_000_000.0
            out["benchmarks"] = benchmarks(base_rs)
        except Exception as exc:
            out["benchmarks"] = {"error": str(exc)}
        # unrealized P&L + True Net Equity per account (engine snapshot marks)
        try:
            snap = _snapshot()
            # the live arm's mark is judged as of when THIS copy was taken
            # (review of Fix D: the box's 15-min mirror), not the viewer's clock
            u = unrealized_by_account(conn, snap, now=now, captured_at=_captured_at(db_path or DB_PATH))
        except Exception:
            snap, u = {}, {}
        for acct in [k for k in out if k.startswith("PAPER_")]:
            # the live-quote arm is NOT priced from the snapshot: its marks are
            # as of its own oldest open mark (audit F02 — it used to borrow the
            # snapshot's fresh timestamp for a mark that could be days old)
            as_of = (u.get(acct) or {}).get("last_mark_ts") if acct in LIVE_ACCOUNTS else snap.get("as_of")
            _with_mtm(out[acct], u.get(acct), as_of)
    finally:
        conn.close()
    return out


# ------------------------------------------------------------ brain map
GRAPH_VIZ_PATH = _DATA / "graph_viz.html"


def brain_map_html(db_path=None) -> dict:
    """The Brain Map — the knowledge graph's causal edges (src/graph_viz.py:
    one self-contained page, inline JS force layout, no network) — for the
    dashboard to embed (Architect 2026-10-09). Rendered LIVE from this
    page's brain_map.db through a read-only connection, so it is as fresh as
    the mirror; `data/graph_viz.html` (the tool's manual output, written by
    nothing on a schedule) is used only when the database cannot be read.
    {"html", "stats", "source"} or {"error"}. Read-only."""
    conn = connect_ro(db_path)
    if conn is not None:
        try:
            from src import graph_viz
            g = graph_viz.build_graph_json(conn)
            return {"html": graph_viz.render_html(g), "stats": g.get("stats") or {},
                    "source": f"live from {Path(db_path or DB_PATH).name}"}
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
    else:
        err = f"database unavailable or locked: {db_path or DB_PATH}"
    try:
        html = GRAPH_VIZ_PATH.read_text()
        mtime = datetime.fromtimestamp(GRAPH_VIZ_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        return {"html": html, "stats": {}, "source": f"{GRAPH_VIZ_PATH.name} written {mtime} ({err})"}
    except OSError:
        return {"error": err}


# ------------------------------------------------- equity history (graph)
EQUITY_ACCOUNTS = ("PAPER_10L", "PAPER_2L", "PAPER_2L_ROT", "PAPER_2L_LIVE")
EQUITY_WINDOWS = ("Today", "1W", "1M", "YTD", "All time")


def _capital_bases(conn) -> tuple:
    """PAPER_10L's CONTRIBUTED capital over time, from its own capital
    events: [(ts, base_rs)] in order, plus the base in force before the
    first event; (None, ...) for any event whose amount cannot be parsed —
    the % line then stops there (abstain, never guess). A clean sheet reads
    '10L->2L' (the base becomes 2L); an injection reads '(200,000.00 ->
    1,000,000.00 base'."""
    import re
    moves, first_base = [], None
    for e in capital_events(conn):
        d = e.get("detail") or ""
        if e["kind"] == "capital_injection":
            m = re.search(r"\(([\d,]+(?:\.\d+)?)\s*->\s*([\d,]+(?:\.\d+)?)\s*base", d)
            before, after = ((float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
                             if m else (None, None))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)L\s*->\s*(\d+(?:\.\d+)?)L", d)
            before, after = ((float(m.group(1)) * 100_000, float(m.group(2)) * 100_000) if m else (None, None))
        if first_base is None and not moves:
            first_base = before
        moves.append((e["ts"], after))
    return first_base, moves


def _base_at(ts: str, first_base, moves, current, strictly_before: bool = False):
    """The contributed base in force at `ts` (a move stamped AT `ts` counts,
    unless `strictly_before`)."""
    base = first_base if moves else current
    for mts, after in moves:
        if mts < ts or (mts == ts and not strictly_before):
            base = after
    return base


def equity_history(db_path=None) -> dict:
    """The multi-portfolio equity graph's data (owner request 2026-10-09),
    each point as % return on the account's CONTRIBUTED capital at that time:
      "realized" — a point at every settlement (`equity_curve` for PAPER_10L,
                   `paper_equity_curve` for the shadows) plus each account's
                   starting point; PAPER_10L's base follows its capital moves
                   (the 21 Jul clean sheet to ₹2L, the 7 Aug injection) — so
                   a clean sheet is a visible break, an injection is not;
      "net"      — True Net Equity (realized + open positions' marks) as
                   recorded every 15 min on the VM by src.equity_history
                   (from 2026-10-09; empty before its first run).
    {"realized": [{account, ts, equity, pct}], "net": [...], "capital_events":
    [...], "notes": [...]} or {"error": ...}. Read-only."""
    conn = connect_ro(db_path)
    if conn is None:
        return {"error": f"database unavailable or locked: {db_path or DB_PATH}"}
    out = {"realized": [], "net": [], "capital_events": [], "notes": []}
    try:
        def pct(eq, base):
            return round((float(eq) / float(base) - 1) * 100, 4) if base else None

        st = _q(conn, "SELECT starting_capital FROM account_state WHERE id = 1")
        cur10 = float(st[0]["starting_capital"]) if isinstance(st, list) and st else None
        first_base, moves = _capital_bases(conn)
        out["capital_events"] = capital_events(conn)
        rows = _q(conn, "SELECT ts, equity FROM equity_curve ORDER BY ts, rowid")
        stopped, last_pct = False, None
        for r in (rows if isinstance(rows, list) else []):
            base = _base_at(r["ts"], first_base, moves, cur10)
            if base is not None and any(mts == r["ts"] for mts, _ in moves):
                # a point stamped at the very instant of a capital move can be
                # the state on either side of it (the 21 Jul clean sheet's
                # point still holds the pre-reset equity; the 7 Aug
                # injection's already holds the post-injection one): it takes
                # the base that continues its own line
                before = _base_at(r["ts"], first_base, moves, cur10, strictly_before=True)
                if before and last_pct is not None and \
                        abs(pct(r["equity"], before) - last_pct) < abs(pct(r["equity"], base) - last_pct):
                    base = before
            if base is None:
                if not stopped:
                    out["notes"].append(f"PAPER_10L's realized line stops at {r['ts']}: a capital move's "
                                        "amount could not be read, so its % base is unknown")
                    stopped = True
                continue
            last_pct = pct(r["equity"], base)
            out["realized"].append({"account": "PAPER_10L", "ts": r["ts"], "equity": r["equity"],
                                    "pct": last_pct})
        accts = _q(conn, "SELECT account_id, starting_capital, created_at FROM paper_accounts")
        caps = {a["account_id"]: a for a in (accts if isinstance(accts, list) else [])}
        for acct, a in sorted(caps.items()):
            if a.get("created_at"):
                out["realized"].append({"account": acct, "ts": a["created_at"], "equity": a["starting_capital"],
                                        "pct": 0.0})
        pts = _q(conn, "SELECT account_id, ts, equity FROM paper_equity_curve ORDER BY ts, rowid")
        for r in (pts if isinstance(pts, list) else []):
            a = caps.get(r["account_id"])
            if a is None:
                continue
            out["realized"].append({"account": r["account_id"], "ts": r["ts"], "equity": r["equity"],
                                    "pct": pct(r["equity"], a["starting_capital"])})
        net = _q(conn, "SELECT account_id, ts, net_equity, starting_capital FROM net_equity_history "
                       "ORDER BY ts, account_id")
        if isinstance(net, list):
            out["net"] = [{"account": r["account_id"], "ts": r["ts"], "equity": r["net_equity"],
                           "pct": pct(r["net_equity"], r["starting_capital"])} for r in net]
        else:
            out["notes"].append("no true-net-equity history yet (recorded every 15 min on the VM from 2026-10-09)")
        out["realized"].sort(key=lambda x: (x["account"], x["ts"]))
    finally:
        conn.close()
    return out


def window_start(choice: str, now: datetime = None):
    """The first instant (naive IST ISO) a timeframe shows; None = All time."""
    now = _as_naive_ist(now or datetime.now(IST))
    if choice == "Today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif choice == "1W":
        start = now - timedelta(days=7)
    elif choice == "1M":
        start = now - timedelta(days=30)
    elif choice == "YTD":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return None
    return start.isoformat(timespec="seconds")


def extend_to_now(points: list, now: datetime = None) -> list:
    """REALIZED lines only: each account's last settled point restated at
    `now` — realized equity cannot move until the next settlement, so the
    line runs to the present instead of stopping at an old trade (a quiet
    account looked dead). Never for true net equity: that is sampled, and
    a restated sample would be a fabricated reading. Pure."""
    now_iso = _as_naive_ist(now or datetime.now(IST)).isoformat(timespec="seconds")
    last = {}
    for p in points:
        if p["account"] not in last or p["ts"] >= last[p["account"]]["ts"]:
            last[p["account"]] = p
    return list(points) + [dict(p, ts=now_iso) for p in last.values() if p["ts"] < now_iso]


def in_window(points: list, start) -> list:
    """The points inside the window, each account's line CARRIED IN: its last
    point before the window is restated at the window's start, so a line
    that did not move inside the window still shows where it stands. Pure."""
    if start is None:
        return list(points)
    out, last_before = [], {}
    for p in points:
        if p["ts"] < start:
            last_before[p["account"]] = p
        else:
            out.append(p)
    return [dict(p, ts=start) for p in last_before.values()] + out


# ----------------------------------------------------------- open trades
def _snapshot_marks() -> dict:
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
        return {m.get("short_id"): m for m in snap.get("marks") or [] if isinstance(m, dict)}
    except (OSError, ValueError):
        return {}


# A verdict under which the account HOLDS the trade: an approval, or the
# live arm's `already_open` (a re-approval that found its position already
# recorded — L3 residual (c): it was missing from the table until it closed).
HOLDING_VERDICTS = ("approved", "already_open")
DIRECTIONAL = ("bear_put_spread", "bull_call_spread")


def _exit_rule(strategy, armed: bool, lock) -> str:
    return ("armed → lock %s%%" % lock if armed
            else ("unarmed" if strategy in DIRECTIONAL else "static 65% take"))


def _read_live(db_path=None) -> tuple:
    """(rows | {"error"}, captured_at) from a read-only connection — the
    open-trades table's own read of `_live_positions` when the caller did
    not pass one."""
    conn = connect_ro(db_path)
    if conn is None:
        return {"error": f"database unavailable or locked: {db_path or DB_PATH}"}, None
    try:
        return _live_positions(conn), _captured_at(db_path or DB_PATH)
    finally:
        conn.close()


def _live_row(r: dict, settled: dict, max_age_s, ref) -> dict:
    """Audit F10: one PAPER_2L_LIVE position on its OWN figures (#120) —
    its last crossed mark, capture, ratchet peak/lock, the time of the
    quotes behind that mark and the Fix D staleness flag — never the
    primary's model mark or the journal's ratchet block."""
    qty = int(r["lots"] or 0) * int(r["lot_size"] or 0)
    lock = r["ratchet_lock_pct"]
    o = settled.get(r["journal_ref"])
    note = "live arm's own position — crossed bid/ask marks (#120)"
    if o is not None:
        # the primary's settlement leaves the live arm's position and lock
        # open (release_shadow_locks): it stays a row of its own here
        note += (f"; the primary settled ({o.get('resolution') or 'resolved'}"
                 + (f", {str(o['settled_at'])[:10]}" if o.get("settled_at") else "")
                 + ") — this position is still open")
    if r["state"] == "exiting":
        note += "; exit in progress"
    # its own id (the React desk keys rows by `id`): the primary's row of the
    # same trade carries the bare ref
    return {"id": f"{r['account_id']}:{r['journal_ref']}", "journal_ref": r["journal_ref"],
            "account": r["account_id"], "symbol": r["ticker"],
            "strategy": STRATEGY_LABELS.get(r["strategy"], r["strategy"]), "direction": r["direction"],
            "accounts": r["account_id"], "lots": r["lots"], "entered": str(r["opened_at"] or "")[:10] or None,
            "expiry": r["expiry"],
            "max_loss_rs": round(float(r["max_loss_ps"] or 0) * qty, 2),
            "mtm_rs": round(float(r["last_profit_ps"]) * qty, 2) if r["last_profit_ps"] is not None else None,
            "capture_pct": r["last_capture_pct"],
            "ratchet_peak_pct": r["ratchet_peak_pct"], "ratchet_lock_pct": lock,
            "ratchet": _exit_rule(r["strategy"], lock is not None, lock),
            "sizing": None, "note": note, "state": r["state"],
            "primary_settled": o is not None,
            "last_mark_ts": r["last_mark_ts"],
            "mark_stale": _live_mark_stale(r, ref, max_age_s) if max_age_s is not None else None}


def open_trades(journal_path=None, equity_ledger_path=None, snapshot_marks: dict = None,
                db_path=None, live=None, now: datetime = None, captured_at: datetime = None) -> list:
    """One row per open position: symbol, strategy, account(s), lots/qty,
    entry, MTM (engine snapshot when present — never a fresh quote here),
    and the profit-ratchet state (peak / lock / armed) for directional
    spreads. Every row names its `account` (the book it is priced on).

    Audit F10 (2026-10-06): PAPER_2L_LIVE marks itself on crossed quotes
    (#120), so its position is a row of its OWN from `paper_live_positions`
    (`_live_positions`, read-only: `live` when the caller already read it,
    else `db_path` / DB_PATH) — its last mark, capture, ratchet peak/lock,
    `last_mark_ts` and the Fix D `mark_stale` flag — and is not listed on
    the primary's row, whose figures are the model's. It stays as an open
    row after the primary settles (the arm settles itself). When its book
    cannot be read, the arm stays named on the primary's row as before
    (never silently dropped). The model-priced shadows (PAPER_2L, ROT)
    keep sharing the primary's row (#102: they settle at its P&L)."""
    marks = _snapshot_marks() if snapshot_marks is None else snapshot_marks
    if live is None:
        live, read_at = _read_live(db_path)
        captured_at = captured_at or read_at
    # Lows residual B2 (F10): a READABLE live book is the truth for the live
    # arm — it is a holder only through its own open/exiting rows, never
    # through the journal verdict (a closed position, an 'already_open'
    # stamp over a closed row, a missed journal stamp or a fill not yet
    # recorded all left it listed on the primary's row on the primary's
    # figures). Only an UNREADABLE book falls back to the journal verdict,
    # and that row then says so.
    book_readable = isinstance(live, list)
    live = live if book_readable else []                 # unreadable: no live row is invented
    shown = {(r["account_id"], r["journal_ref"]) for r in live}
    max_age_s, ref = _stale_policy(now, captured_at)
    rows, settled = [], {}
    for e in _jsonl(journal_path or JOURNAL_PATH):
        s = e.get("spread")
        if s and e.get("outcome") is not None:
            settled[e.get("short_id")] = e["outcome"]
        if not s or e.get("decision") != "approved" or e.get("outcome") is not None:
            continue
        r = e.get("ratchet") or {}
        m = marks.get(e.get("short_id")) or {}
        accounts = ["PAPER_10L"] + [a for a, v in (e.get("accounts") or {}).items()
                                    if (v or {}).get("status") in HOLDING_VERDICTS
                                    and (a, e.get("short_id")) not in shown
                                    and not (book_readable and a in LIVE_ACCOUNTS)]
        live_named = [a for a in accounts if a in LIVE_ACCOUNTS]
        rows.append({"id": e.get("short_id"), "account": "PAPER_10L", "symbol": e.get("ticker"),
                     "strategy": STRATEGY_LABELS.get(s.get("strategy"), s.get("strategy")),
                     "direction": s.get("direction"), "accounts": ", ".join(accounts),
                     "lots": s.get("lots"), "entered": e.get("date"), "expiry": s.get("expiry"),
                     "max_loss_rs": round(float(s.get("max_loss") or 0) * int(s.get("lots") or 1), 2),
                     "mtm_rs": m.get("live_pnl_rs"), "capture_pct": m.get("capture_pct"),
                     "ratchet_peak_pct": r.get("peak_capture_pct"),
                     "ratchet_lock_pct": r.get("locked_pct"),
                     "ratchet": _exit_rule(s.get("strategy"), bool(r.get("armed")), r.get("locked_pct")),
                     "sizing": ((e.get("sizing") or {}).get("reason") or None),
                     **({"note": f"live book unreadable — {', '.join(live_named)} named from the journal "
                                 "verdict, on the primary's figures"} if live_named else {})})
    rows += [_live_row(r, settled, max_age_s, ref) for r in live]
    entries = {}
    from src.knowledge_graph_logger import apply_corrections       # funding_revoked (#123)
    for x in apply_corrections(_jsonl(equity_ledger_path or EQUITY_LEDGER_PATH)):
        if x.get("event") == "entry" and (x.get("funding") or {}).get("funded"):
            entries[x.get("id")] = x
        elif x.get("event") == "exit":
            entries.pop(x.get("id"), None)
    for x in entries.values():
        a = x.get("kya_kara_action") or {}
        rows.append({"id": (x.get("funding") or {}).get("lock_ref") or f"eqd:{x.get('id')}",
                     "account": "PAPER_10L",
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
