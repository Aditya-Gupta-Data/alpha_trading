# MANUAL OFFLINE TOOL — not on any cron line (decision #94: read-only sync;
# scheduling it is a separate owner decision). Run by hand:
#   python3 -m src.execution.recon_engine            # live, read-only
#   python3 -m src.execution.recon_engine --dry-run  # book side only, no broker call
"""
src/execution/recon_engine.py — THE READ-ONLY RECON ENGINE (decision #94, built
2026-09-23 under decision #104's M2 pre-requisite sprint).

WHAT IT IS. A one-way mirror. It reads the broker's book — Dhan Trading API
`GET /v2/positions`, `GET /v2/holdings`, `GET /v2/fundlimit` — and compares
it with what `brain_map.db` believes is open (active margin locks on both
paper accounts + FILLED OMS ENTRY tickets without a FILLED EXIT). Every
difference is a named `🔴 RECON MISMATCH` row: printed, appended to
`logs/recon.jsonl`, and fired through the one Discord door.

WHAT IT IS NOT. It never places, modifies or cancels anything. The ONLY HTTP
verb in this file is GET, `tests/test_recon_engine.py` fails the build if a
write verb, an order path or a placement SDK method ever appears here, and
Rule 7 (paper money only) is untouched: the desk still has no order path.

PARITY LOGIC, stated plainly.
  book side   = the paper book: one row per active lock / open ticket, keyed
                (underlying, account). While Rule 7 holds NOTHING in the book
                is expected at the broker, so a book-only row is INFO
                ("paper_only") — the expected state, reported not hidden.
  broker side = every non-zero net position and every holding, keyed by
                security id + trading symbol. A broker row that the book does
                not know is CRITICAL: money is at the exchange that no ledger
                here accounts for ("broker_position_unknown_to_book").
  funds       = reported as read (available / utilised / collateral); the
                paper treasury is not compared to it — different money.
  A broker call that fails, is muzzled (pytest), or has no token yields
  `broker_reachable = False` and ZERO mismatches — an absent read is never
  reported as parity (RULE 3: abstention, not a fabricated default).

Token: `token_provider.get_token()` — the live `.env` read every other Dhan
call uses; never minted here (#48). Rate limit: routed through
`dhan_client._throttle()` like every other Dhan call (#85).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "recon.jsonl"
BASE_URL = "https://api.dhan.co/v2"
TIMEOUT_SECONDS = 15

# The three READ endpoints and nothing else. (The test asserts this tuple is
# the module's whole endpoint surface.)
ENDPOINTS = ("/positions", "/holdings", "/fundlimit")

MISMATCH_TAG = "🔴 RECON MISMATCH"


# ------------------------------------------------------------------ broker
def _muzzled() -> bool:
    """Inside pytest the broker is never dialled (RULE 6, Issue 29)."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _headers() -> dict | None:
    from src import token_provider
    token = token_provider.get_token()
    cid = os.environ.get("DHAN_CLIENT_ID")
    if not token or not cid:
        return None
    return {"access-token": token, "client-id": cid, "Accept": "application/json"}


def _read(endpoint: str, headers: dict) -> tuple[object, str | None]:
    """(payload, error). GET only — by construction and by test."""
    assert endpoint in ENDPOINTS, endpoint
    try:
        import requests
        from src.dhan_client import _throttle
        _throttle()
        resp = requests.get(BASE_URL + endpoint, headers=headers,
                            timeout=TIMEOUT_SECONDS)
    except Exception as exc:
        return None, f"{endpoint}: {type(exc).__name__}: {exc}"
    try:
        body = resp.json()
    except Exception:
        body = None
    if resp.status_code >= 300:
        code = (body or {}).get("errorCode") if isinstance(body, dict) else None
        # DH-1111 = "no holdings" — Dhan reports an EMPTY book as an error.
        if code == "DH-1111" or "DH-1111" in (resp.text or ""):
            return [], None
        return None, f"{endpoint}: HTTP {resp.status_code} {code or ''} {(resp.text or '')[:160]}"
    return body, None


def fetch_broker_book(headers: dict = None, read_fn=None) -> dict:
    """{"reachable": bool, "positions": [...], "holdings": [...], "funds": {...},
    "errors": [...]} — `read_fn(endpoint) -> (payload, error)` is the seam
    tests use; live it is `_read` with the live headers."""
    out = {"reachable": False, "positions": [], "holdings": [], "funds": {},
           "errors": []}
    if read_fn is None:
        if _muzzled():
            out["errors"].append("muzzled under pytest — broker not dialled")
            return out
        headers = headers or _headers()
        if headers is None:
            out["errors"].append("no DHAN_CLIENT_ID / access token — broker not dialled")
            return out
        read_fn = lambda ep: _read(ep, headers)          # noqa: E731
    raw = {}
    for ep in ENDPOINTS:
        payload, err = read_fn(ep)
        if err:
            out["errors"].append(err)
            continue
        raw[ep] = payload
    if "/positions" not in raw:
        return out            # no position read = no parity claim, ever
    out["reachable"] = True
    out["positions"] = [_norm_position(p) for p in _rows(raw["/positions"])
                        if _net_qty(p) != 0]
    out["holdings"] = [_norm_holding(h) for h in _rows(raw.get("/holdings", []))
                       if _to_int(h.get("totalQty") or h.get("availableQty")) != 0]
    out["funds"] = _norm_funds(raw.get("/fundlimit") or {})
    return out


def _rows(payload) -> list:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    return []


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _net_qty(p: dict) -> int:
    if p.get("netQty") is not None:
        return _to_int(p["netQty"])
    return _to_int(p.get("buyQty")) - _to_int(p.get("sellQty"))


def _norm_position(p: dict) -> dict:
    return {"security_id": str(p.get("securityId") or ""),
            "symbol": str(p.get("tradingSymbol") or ""),
            "segment": str(p.get("exchangeSegment") or ""),
            "product": str(p.get("productType") or ""),
            "net_qty": _net_qty(p),
            "avg_price": _to_float(p.get("buyAvg") if _net_qty(p) > 0 else p.get("sellAvg")),
            "unrealized": _to_float(p.get("unrealizedProfit"))}


def _norm_holding(h: dict) -> dict:
    return {"security_id": str(h.get("securityId") or ""),
            "symbol": str(h.get("tradingSymbol") or ""),
            "segment": str(h.get("exchange") or "NSE_EQ"),
            "qty": _to_int(h.get("totalQty") or h.get("availableQty")),
            "avg_price": _to_float(h.get("avgCostPrice"))}


def _norm_funds(f: dict) -> dict:
    if not isinstance(f, dict):
        return {}
    d = f.get("data") if isinstance(f.get("data"), dict) else f
    return {"available": _to_float(d.get("availabelBalance", d.get("availableBalance"))),
            "utilised": _to_float(d.get("utilizedAmount")),
            "collateral": _to_float(d.get("collateralAmount")),
            "withdrawable": _to_float(d.get("withdrawableBalance"))}


# -------------------------------------------------------------------- book
def read_paper_book(conn: sqlite3.Connection = None, db_path=None) -> list:
    """What THIS desk believes is open, from brain_map.db, read-only:
    active margin locks (primary + shadow accounts) and FILLED OMS ENTRY
    tickets that have no FILLED EXIT. One row per (underlying, account)."""
    owns = conn is None
    if owns:
        from src.brain_map import DEFAULT_DB_PATH
        conn = sqlite3.connect(str(db_path or DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = {}
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "margin_locks" in tables:
            for r in conn.execute("SELECT journal_ref, margin_rs, locked_at FROM "
                                  "margin_locks WHERE released_at IS NULL"):
                key = (r["journal_ref"], "PAPER_10L")
                rows[key] = {"ref": r["journal_ref"], "account": "PAPER_10L",
                             "underlying": None, "margin_rs": r["margin_rs"],
                             "since": r["locked_at"], "source": "margin_lock"}
        if "paper_margin_locks" in tables:
            for r in conn.execute("SELECT journal_ref, account_id, margin_rs, locked_at "
                                  "FROM paper_margin_locks WHERE released_at IS NULL"):
                key = (r["journal_ref"], r["account_id"])
                rows[key] = {"ref": r["journal_ref"], "account": r["account_id"],
                             "underlying": None, "margin_rs": r["margin_rs"],
                             "since": r["locked_at"], "source": "paper_margin_lock"}
        if "trade_tickets" in tables:
            cols = {c[1] for c in conn.execute("PRAGMA table_info(trade_tickets)")}
            kind = "kind" if "kind" in cols else "'ENTRY' AS kind"
            acct = "account_id" if "account_id" in cols else "'PAPER_10L' AS account_id"
            tickets = [dict(r) for r in conn.execute(
                f"SELECT ticket_id, journal_ref, underlying, status, {kind}, {acct} "
                f"FROM trade_tickets")]
            exited = {(t["journal_ref"], t["account_id"]) for t in tickets
                      if t["kind"] == "EXIT" and t["status"] == "FILLED"}
            for t in tickets:
                if t["kind"] != "ENTRY" or t["status"] != "FILLED":
                    continue
                key = (t["journal_ref"], t["account_id"])
                if key in exited:
                    continue
                row = rows.setdefault(key, {"ref": t["journal_ref"],
                                            "account": t["account_id"],
                                            "margin_rs": None, "since": None,
                                            "source": "oms_ticket"})
                row["underlying"] = t["underlying"]
                row["ticket_id"] = t["ticket_id"]
    finally:
        if owns:
            conn.close()
    return sorted(rows.values(), key=lambda r: (r["account"], r["ref"]))


# ------------------------------------------------------------------ parity
def compare(broker: dict, book: list, now: str = None) -> dict:
    """The parity verdict. `mismatches` are the CRITICAL rows; `paper_only`
    the expected-while-Rule-7-holds rows; nothing is inferred when the
    broker was not read."""
    now = now or datetime.now().isoformat(timespec="seconds")
    result = {"ts": now, "broker_reachable": bool(broker.get("reachable")),
              "broker_positions": len(broker.get("positions") or []),
              "broker_holdings": len(broker.get("holdings") or []),
              "book_rows": len(book), "funds": broker.get("funds") or {},
              "errors": list(broker.get("errors") or []),
              "mismatches": [], "paper_only": []}
    if not result["broker_reachable"]:
        result["verdict"] = "unknown"      # never "in parity" on a failed read
        return result
    book_names = {_canon(r.get("underlying")) for r in book if r.get("underlying")}
    for p in broker.get("positions") or []:
        if _canon(p["symbol"]) not in book_names:
            result["mismatches"].append({
                "kind": "broker_position_unknown_to_book", "severity": "CRITICAL",
                "symbol": p["symbol"], "security_id": p["security_id"],
                "segment": p["segment"], "net_qty": p["net_qty"],
                "detail": (f"{MISMATCH_TAG}: broker holds {p['net_qty']:+d} "
                           f"{p['symbol']} ({p['segment']}) that no paper lock "
                           f"or ticket accounts for")})
        else:
            # Paper never sends an order, so a broker position on a book name
            # is STILL not ours: sizes cannot be reconciled until Rule 7 lifts.
            result["mismatches"].append({
                "kind": "broker_position_on_book_name_but_paper_never_ordered",
                "severity": "CRITICAL", "symbol": p["symbol"],
                "security_id": p["security_id"], "segment": p["segment"],
                "net_qty": p["net_qty"],
                "detail": (f"{MISMATCH_TAG}: broker holds {p['net_qty']:+d} "
                           f"{p['symbol']} — the paper book has a position on "
                           f"this name but never placed an order")})
    for h in broker.get("holdings") or []:
        result["mismatches"].append({
            "kind": "broker_holding_unknown_to_book", "severity": "CRITICAL",
            "symbol": h["symbol"], "security_id": h["security_id"],
            "segment": h["segment"], "net_qty": h["qty"],
            "detail": (f"{MISMATCH_TAG}: demat holds {h['qty']} {h['symbol']} "
                       f"— the paper book never buys delivery")})
    for r in book:
        result["paper_only"].append({
            "kind": "paper_only", "severity": "INFO", "ref": r["ref"],
            "account": r["account"], "underlying": r.get("underlying"),
            "margin_rs": r.get("margin_rs"), "source": r["source"],
            "detail": (f"paper {r['account']} {r['ref']} "
                       f"({r.get('underlying') or 'underlying n/a'}) — expected "
                       f"absent at the broker while Rule 7 holds")})
    result["verdict"] = "mismatch" if result["mismatches"] else "parity"
    return result


def _canon(name) -> str:
    return str(name or "").upper().replace(".NS", "").replace(" ", "").replace("-", "")


# ------------------------------------------------------------------- doors
def _log(result: dict, path: Path = None) -> None:
    p = path or LOG_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(result, default=str) + "\n")
    except Exception as exc:
        print(f"  (recon: log write failed: {exc})")


def _announce(result: dict) -> None:
    """CRITICAL mismatches go through the one Discord door, fail-open."""
    if not result.get("mismatches"):
        return
    try:
        from src.notifier import fire_broadcast
        lines = [m["detail"] for m in result["mismatches"][:10]]
        if len(result["mismatches"]) > 10:
            lines.append(f"…and {len(result['mismatches']) - 10} more")
        fire_broadcast({"event": "recon_mismatch", "ticker": "🔴 RECON",
                        "date": result["ts"][:10],
                        "description": (f"{MISMATCH_TAG} — {len(result['mismatches'])} "
                                        f"broker row(s) the paper book cannot explain"),
                        "fields": [{"name": "Rows", "value": "\n".join(lines),
                                    "inline": False}]})
    except Exception as exc:
        print(f"  (recon: announce failed: {exc})")


def run(conn=None, db_path=None, read_fn=None, log_path=None,
        announce: bool = True, dry_run: bool = False) -> dict:
    """One reconciliation pass. Read-only on both sides."""
    book = read_paper_book(conn=conn, db_path=db_path)
    broker = ({"reachable": False, "positions": [], "holdings": [], "funds": {},
               "errors": ["dry run — broker not dialled"]}
              if dry_run else fetch_broker_book(read_fn=read_fn))
    result = compare(broker, book)
    _log(result, log_path)
    if announce:
        _announce(result)
    return result


def render(result: dict) -> str:
    head = (f"RECON {result['ts']} — verdict {result['verdict'].upper()} · broker "
            f"{'reachable' if result['broker_reachable'] else 'NOT READ'} · "
            f"broker positions {result['broker_positions']} · holdings "
            f"{result['broker_holdings']} · paper rows {result['book_rows']}")
    lines = [head]
    f = result.get("funds") or {}
    if f:
        lines.append(f"funds: available {f.get('available')} · utilised "
                     f"{f.get('utilised')} · collateral {f.get('collateral')}")
    for e in result.get("errors") or []:
        lines.append(f"  ! {e}")
    for m in result.get("mismatches") or []:
        lines.append(f"  {m['detail']}")
    for r in result.get("paper_only") or []:
        lines.append(f"  · {r['detail']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    result = run(dry_run="--dry-run" in argv, announce="--quiet" not in argv)
    print(render(result))
    return 2 if result["verdict"] == "mismatch" else 0


if __name__ == "__main__":
    raise SystemExit(main())
