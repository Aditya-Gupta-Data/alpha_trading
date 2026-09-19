"""
src/reporting/markdown_ledger.py — the LIVE TRADE BOOK, as a markdown file
==========================================================================

`python3 -m src.reporting.markdown_ledger` renders `docs/LIVE_TRADE_BOOK.md`:
one page the owner can open on the Mac every morning and read the whole
paper book without touching the database — the account line, every OPEN
position with its locked margin and age, and the last 50 RESOLVED trades
with P&L, R-multiple and the tracker's own verdict.

WHERE THE NUMBERS COME FROM (each one already written by the trading path;
nothing here is recomputed from prices):

  * account line      — `account_state` / `equity_curve` in brain_map.db,
                        the same rows `eod_summary.drawdown_line` reads.
  * options positions — `data/journal.jsonl` (`decision == "approved"`,
                        `spread` present). OPEN = `outcome` is null.
                        RESOLVED = the tracker stamped an `outcome`;
                        `hypothetical` (#31 shadow) outcomes are excluded,
                        as `performance.py` excludes them.
  * equity positions  — `logs/equity_shadow_journal.jsonl` event pairs
                        (entry/exit sharing an `id`). ONLY `PAPER_CAPITAL`
                        rows with `funding.funded` are trades; the
                        zero-capital telemetry shadows are counted in a
                        footnote and never tabled — no rupees were at risk.
  * margin locked     — `margin_locks` (journal_ref = short_id, or
                        `eqd:<id>` for the desk); the journal's own
                        `spread.margin.total_margin` / `funding.notional`
                        only as a fallback when the ledger has no row.
  * equity P&L        — `margin_locks.pnl_net` as the desk settled it;
                        qty × (exit − entry) only when no lock row exists.

HONESTY RULES: a value the ledgers never recorded renders as `—`, never as
0. The DB is opened read-only (`mode=ro`); a missing table or file degrades
to an empty section with a note, never an exception. Appending to any
ledger from here is impossible by construction — nothing is imported from
the tracker, the desk, or the treasury.

The file is regenerable runtime data (gitignored, like `data/`). It is
written on the VM after the 16:30 CEO brief (cron #32) and pulled down to
the Mac by `scripts/mac_auto_sync.sh` (`firm_treasury.vm_pull_file`).
"""
import json
import socket
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_JOURNAL = ROOT / "data" / "journal.jsonl"
DEFAULT_EQUITY_JOURNAL = ROOT / "logs" / "equity_shadow_journal.jsonl"
DEFAULT_DB_PATH = ROOT / "data" / "brain_map.db"
DEFAULT_OUT = ROOT / "docs" / "LIVE_TRADE_BOOK.md"

IST = timezone(timedelta(hours=5, minutes=30))
RESOLVED_LIMIT = 50
EQD_PREFIX = "eqd:"

STRATEGY_LABELS = {
    "bear_put_spread": "Bear Put Spread",
    "bull_call_spread": "Bull Call Spread",
    "bull_put_spread": "Bull Put Spread",
    "bear_call_spread": "Bear Call Spread",
    "iron_condor": "Iron Condor",
    "long_straddle": "Long Straddle",
    "long_strangle": "Long Strangle",
    "block_vwap_pullback": "Equity Block-VWAP Pullback",
    "darling_ripe": "Equity Darling (legacy ripe)",
    "darling_buy": "Equity Darling Buy",
}

EQUITY_REASON_LABELS = {
    "stop_loss": "Stop Hit",
    "target": "Target Hit",
    "time_stop": "Time Stop",
    "fundamental_break": "Fundamental Break",
    "strong_sell_tier": "Strong-Sell Tier Exit",
}


# ------------------------------------------------------------------ readers

def _read_jsonl(path) -> list:
    """Every parseable line; a missing file or a junk line is fewer rows,
    never an exception (the eod_summary / knowledge_graph_logger rule)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_account(db_path=None) -> dict:
    """Account line + margin-lock map, read-only. Every key is None / {}
    when the DB or a table is absent, so the renderer can say so."""
    path = Path(db_path or DEFAULT_DB_PATH)
    acct = {"account": None, "curve": None, "max_drawdown_pct": None,
            "locks": {}, "db_present": path.exists()}
    if not path.exists():
        return acct
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return acct
    try:
        for key, sql in (
            ("account", "SELECT starting_capital, realized_pnl, peak_equity "
                        "FROM account_state WHERE id = 1"),
            ("curve", "SELECT ts, equity, peak_equity, drawdown_pct FROM "
                      "equity_curve ORDER BY ts DESC LIMIT 1"),
            ("max_drawdown_pct", "SELECT MAX(drawdown_pct) FROM equity_curve"),
        ):
            try:
                row = conn.execute(sql).fetchone()
            except sqlite3.Error:
                row = None
            if row is None:
                continue
            if key == "account":
                acct[key] = {"starting_capital": row[0], "realized_pnl": row[1],
                             "peak_equity": row[2]}
            elif key == "curve":
                acct[key] = {"ts": row[0], "equity": row[1],
                             "peak_equity": row[2], "drawdown_pct": row[3]}
            else:
                acct[key] = row[0]
        try:
            for ref, margin, locked_at, released_at, pnl in conn.execute(
                    "SELECT journal_ref, margin_rs, locked_at, released_at, "
                    "pnl_net FROM margin_locks"):
                acct["locks"][ref] = {"margin_rs": margin, "locked_at": locked_at,
                                      "released_at": released_at, "pnl_net": pnl}
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return acct


# ------------------------------------------------------------------ shaping

def _label(raw, table=STRATEGY_LABELS) -> str:
    if not raw:
        return "—"
    return table.get(raw, str(raw).replace("_", " ").title())


def _date_of(stamp) -> str:
    """'2026-09-10T13:00:37+05:30' -> '2026-09-10'; '' stays ''."""
    if not stamp:
        return ""
    return str(stamp).partition("T")[0]


def _days_between(start: str, end: str):
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def options_rows(entries: list, locks: dict, today: str) -> tuple:
    """(open, resolved) for the options desk. Rejected proposals and #31
    hypothetical outcomes never enter either list."""
    open_rows, resolved = [], []
    for e in entries:
        if e.get("decision") != "approved" or not e.get("spread"):
            continue
        spread = e["spread"]
        outcome = e.get("outcome")
        sid = e.get("short_id") or ""
        entry_date = _date_of(e.get("created_at")) or e.get("date") or ""
        strategy = _label(spread.get("strategy"))
        if outcome is None:
            lock = locks.get(sid)
            margin = (lock["margin_rs"] if lock and lock.get("released_at") is None
                      else (spread.get("margin") or {}).get("total_margin"))
            open_rows.append({
                "id": sid, "desk": "Options", "ticker": e.get("ticker") or "—",
                "strategy": strategy, "entry_date": entry_date,
                "margin": margin, "margin_from_ledger": bool(lock),
                "days_held": _days_between(entry_date, today),
            })
        elif not outcome.get("hypothetical"):
            resolved.append({
                "id": sid, "desk": "Options", "ticker": e.get("ticker") or "—",
                "strategy": strategy, "entry_date": entry_date,
                "exit_date": outcome.get("exit_date") or outcome.get("checked") or "",
                "pnl": outcome.get("pnl_rs"), "r": outcome.get("r_multiple"),
                "verdict": outcome.get("verdict") or _label(outcome.get("resolution")),
            })
    return open_rows, resolved


def equity_rows(events: list, locks: dict, today: str) -> tuple:
    """(open, resolved, telemetry_open, telemetry_resolved) for the equity
    desk. Only funded PAPER_CAPITAL rows are trades; the rest are counted."""
    entries, exits = {}, {}
    for ev in events:
        ev_id = ev.get("id")
        if not ev_id:
            continue
        if ev.get("event") == "entry":
            entries.setdefault(ev_id, ev)
        elif ev.get("event") == "exit":
            exits[ev_id] = ev
    open_rows, resolved = [], []
    tele_open = tele_resolved = 0
    for ev_id, en in entries.items():
        ex = exits.get(ev_id)
        funding = en.get("funding") or {}
        funded = bool(funding.get("funded")) and en.get("mode") == "PAPER_CAPITAL"
        if not funded:
            if ex:
                tele_resolved += 1
            else:
                tele_open += 1
            continue
        action = en.get("kya_kara_action") or {}
        strategy = _label((en.get("kyu_trigger") or {}).get("setup") or "equity_shadow")
        entry_date = _date_of(en.get("ts")) or en.get("as_of") or ""
        lock = locks.get(funding.get("lock_ref") or (EQD_PREFIX + ev_id))
        if ex is None:
            margin = (lock["margin_rs"] if lock and lock.get("released_at") is None
                      else funding.get("notional"))
            open_rows.append({
                "id": ev_id, "desk": "Equity", "ticker": en.get("ticker") or "—",
                "strategy": strategy, "entry_date": entry_date,
                "margin": margin, "margin_from_ledger": bool(lock),
                "days_held": _days_between(entry_date, today),
            })
            continue
        autopsy = ex.get("kya_sikha_autopsy") or {}
        pnl = lock.get("pnl_net") if lock else None
        if pnl is None:
            qty, ep, xp = funding.get("qty"), action.get("entry_price"), ex.get("exit_price")
            if qty and ep is not None and xp is not None:
                pnl = round(float(qty) * (float(xp) - float(ep)), 2)
        verdict = " · ".join(x for x in (
            autopsy.get("category"), _label(ex.get("reason"), EQUITY_REASON_LABELS)
            if ex.get("reason") else "") if x)
        resolved.append({
            "id": ev_id, "desk": "Equity", "ticker": en.get("ticker") or "—",
            "strategy": strategy, "entry_date": entry_date,
            "exit_date": _date_of(ex.get("ts")), "pnl": pnl,
            "r": autopsy.get("r_multiple"), "verdict": verdict or "—",
        })
    return open_rows, resolved, tele_open, tele_resolved


# ---------------------------------------------------------------- rendering

def _rs(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    return f"-₹{-x:,.0f}" if x < 0 else f"₹{x:,.0f}"


def _r(x) -> str:
    return "—" if x is None else f"{float(x):+.2f}R"


def _cell(x) -> str:
    """Markdown-safe cell: pipes would split the row."""
    return str(x if x not in (None, "") else "—").replace("|", "／")


def _table(headers: list, rows: list) -> list:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines


def render(journal_path=None, equity_path=None, db_path=None, today=None,
           now=None) -> str:
    """The whole page as one string. Pure over its inputs; every section
    fails open to a note rather than an exception."""
    today = today or datetime.now(IST).date().isoformat()
    now = now or datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    acct = read_account(db_path)
    locks = acct["locks"]
    entries = _read_jsonl(journal_path or DEFAULT_JOURNAL)
    events = _read_jsonl(equity_path or DEFAULT_EQUITY_JOURNAL)

    opt_open, opt_res = options_rows(entries, locks, today)
    eq_open, eq_res, tele_open, tele_res = equity_rows(events, locks, today)
    open_rows = sorted(opt_open + eq_open, key=lambda r: (r["entry_date"], r["id"]))
    resolved = sorted(opt_res + eq_res,
                      key=lambda r: (r["exit_date"], r["entry_date"], r["id"]),
                      reverse=True)
    recent = resolved[:RESOLVED_LIMIT]

    out = ["# LIVE TRADE BOOK — paper portfolio", "",
           f"_Generated {now} on `{socket.gethostname()}` · "
           "read-only render of `data/journal.jsonl`, "
           "`logs/equity_shadow_journal.jsonl` and `brain_map.db`. "
           "Paper money only — no broker exists in this system (Rule 7)._", ""]

    # --- account line ---------------------------------------------------
    out += ["## Account", ""]
    a, c = acct["account"], acct["curve"]
    if a is None:
        out.append("_No `account_state` row readable"
                   + ("" if acct["db_present"] else " (brain_map.db absent)")
                   + " — account figures withheld._")
    else:
        equity = float(a["starting_capital"]) + float(a["realized_pnl"])
        open_locked = sum(float(l["margin_rs"]) for l in locks.values()
                          if l.get("released_at") is None)
        dd_now = float(c["drawdown_pct"]) if c and c.get("drawdown_pct") is not None else None
        mdd = acct["max_drawdown_pct"]
        out += _table(["Metric", "Value"], [
            ("Starting capital", _rs(a["starting_capital"])),
            ("Realized P&L (ledger)", _rs(a["realized_pnl"])),
            ("Equity", _rs(equity)),
            ("Peak equity", _rs(a["peak_equity"])),
            ("Drawdown now", "—" if dd_now is None else f"-{dd_now:.2f}%"
                             + (" (at peak)" if dd_now <= 0.0001 else "")),
            ("Max drawdown (curve)", "—" if mdd is None else f"-{float(mdd):.2f}%"),
            ("Margin locked (open)", _rs(open_locked)),
            ("Available cash", _rs(equity - open_locked)),
            ("Open trades", f"{len(open_rows)} ({len(opt_open)} options, {len(eq_open)} equity)"),
        ])
        if c and c.get("ts"):
            out += ["", f"_Curve last written {c['ts']}._"]
    out.append("")

    # --- open trades ----------------------------------------------------
    out += ["## OPEN TRADES", ""]
    if open_rows:
        out += _table(["Ticker", "Desk", "Strategy", "Entry Date", "Margin Locked",
                       "Days Held", "ID"],
                      [(r["ticker"], r["desk"], r["strategy"], r["entry_date"],
                        _rs(r["margin"]) + ("" if r["margin_from_ledger"] else "†"),
                        r["days_held"], r["id"]) for r in open_rows])
        if any(not r["margin_from_ledger"] for r in open_rows):
            out += ["", "_† no `margin_locks` row — figure is the journal's own "
                        "margin/notional, not a ledger lock._"]
    else:
        out.append("_No open positions._")
    out.append("")

    # --- resolved -------------------------------------------------------
    out += [f"## RECENTLY RESOLVED TRADES (last {len(recent)} of {len(resolved)})", ""]
    if recent:
        out += _table(["Ticker", "Desk", "Strategy", "Exit Date", "P&L", "R-Multiple",
                       "Verdict"],
                      [(r["ticker"], r["desk"], r["strategy"], r["exit_date"],
                        _rs(r["pnl"]), _r(r["r"]), r["verdict"]) for r in recent])
        with_pnl = [r for r in recent if r["pnl"] is not None]
        with_r = [r for r in recent if r["r"] is not None]
        wins = sum(1 for r in with_pnl if float(r["pnl"]) > 0)
        line = (f"_Shown rows: net P&L {_rs(sum(float(r['pnl']) for r in with_pnl))} "
                f"over {len(with_pnl)} with a rupee outcome · "
                f"{wins}W/{len(with_pnl) - wins}L")
        if with_r:
            line += (f" · avg {sum(float(r['r']) for r in with_r) / len(with_r):+.2f}R "
                     f"over {len(with_r)} R-stamped")
        out += ["", line + "._"]
    else:
        out.append("_No resolved trades yet._")
    out.append("")

    # --- reconciliation -------------------------------------------------
    # A lock still open for a position the ledgers say is closed (or never
    # tabled) is money the book cannot spend. Found on the first VM render
    # 2026-09-19: eqd:3fedfeeb (TCS.NS) exited 08-xx, lock never released.
    open_refs = {r["id"] for r in opt_open} | {EQD_PREFIX + r["id"] for r in eq_open}
    orphans = sorted((ref, l) for ref, l in locks.items()
                     if l.get("released_at") is None and ref not in open_refs)
    if orphans:
        out += ["## ⚠️ Locks without an open position", "",
                "_Margin still locked in `margin_locks` for a ref no open row "
                "carries — an orphan. `python3 -m src.equity_desk --sweep` "
                "reconciles desk (`eqd:`) locks; an options orphan needs a look "
                "at the tracker._", ""]
        out += _table(["Lock ref", "Margin", "Locked at"],
                      [(ref, _rs(l["margin_rs"]), l.get("locked_at")) for ref, l in orphans])
        out.append("")

    # --- footnotes ------------------------------------------------------
    out += ["## Notes", "",
            f"- Equity telemetry shadows (zero capital, never tabled): "
            f"{tele_open} open, {tele_res} resolved.",
            "- `—` means the ledger never recorded that value. It is not zero.",
            "- Options P&L is NET of frictions as the tracker stamped it; "
            "equity P&L is `margin_locks.pnl_net` as the desk settled it.",
            "- Hypothetical (#31 shadow) options outcomes and rejected proposals "
            "are excluded, exactly as `performance.py` excludes them.", ""]
    return "\n".join(out)


def write(out_path=None, **kw) -> Path:
    """Render and write atomically (tmp + replace) so a reader on the Mac
    never sees a half-written page."""
    out = Path(out_path or DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = render(**kw)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Render docs/LIVE_TRADE_BOOK.md (read-only).")
    ap.add_argument("--out", default=None, help=f"output path (default {DEFAULT_OUT})")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args(argv)
    try:
        if args.stdout:
            print(render())
            return 0
        path = write(args.out)
        print(f"[markdown_ledger] wrote {path}")
        return 0
    except Exception as exc:  # fail open: a broken render must not fail the cron chain
        print(f"[markdown_ledger] FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
