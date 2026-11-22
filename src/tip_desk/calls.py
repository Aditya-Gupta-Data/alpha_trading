"""
Arm 2, record-and-grade only (decision #189 addendum): every DIRECTION the tipster calls — including the ones
Arm 1 rejects (naked sells, rollovers, stock names outside our option universe) — is logged with its paste time,
NSE symbol, inferred direction and the spot at paste time, then graded on the bhavcopy close 1 and 5 sessions
later. Nothing here trades or sizes; it only answers "do his calls have edge?" before any execution is wired.

    python -m src.tip_desk.calls --grade      # nightly 20:40 on the VM (after the 19:15 bhavcopy)
    python -m src.tip_desk.calls --report     # hit rate and mean signed return per horizon

Direction from the option side: CE/CALL buy, PE/PUT sell or writer, futures buy, LONG → bullish; PE/PUT buy,
CE/CALL sell or writer, futures sell, SHORT → bearish. A bare "sell" is NOT a call (it is usually an exit), past
moves ("all flying", "running") are not calls, and a text that points both ways is skipped.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from src.tip_desk import IST, now_ist, store
from src.tip_desk.parser import INDEX_ALIASES, normalize

HORIZONS = (1, 5)
SYMBOL_ALIASES = {"ANGEL ONE": "ANGELONE", "ANGLE ONE": "ANGELONE", "ANGEL": "ANGELONE", "CG POWER": "CGPOWER",
                  "BANK OF BARODA": "BANKBARODA", "HDFC BANK": "HDFCBANK", "ICICI BANK": "ICICIBANK",
                  "AXIS BANK": "AXISBANK", "KOTAK BANK": "KOTAKBANK", "TATA MOTORS": "TATAMOTORS",
                  "TATA STEEL": "TATASTEEL", "TATA POWER": "TATAPOWER", "ADANI ENT": "ADANIENT"}
NOT_SYMBOLS = {"BUY", "SELL", "SELLER", "SELLERS", "CE", "PE", "CALL", "CALLS", "PUT", "PUTS", "FUT", "FUTURES",
               "SL", "TGT", "TARGET", "NOW", "ALL", "ONE", "CAN", "COULD", "BE", "DONE", "SAME", "STRIKE", "NEXT",
               "MONTH", "TODAY", "TODAYS", "WEEK", "EXPIRY", "ENTRY", "EXIT", "BOOK", "BOOKING", "LONG", "SHORT",
               "TRADE", "OPTION", "INDEX", "ROLLOVER", "WRITE", "WRITER", "LIMIT", "ORDER", "ORDERS", "PLACE",
               "ABOVE", "BELOW", "NEAR", "AT", "THE", "AND", "FOR", "IN", "ON", "TO", "OF", "IS", "IT", "GOOD"}
BULL_RE = re.compile(r"\b(?:CE|CALLS?)\s+(?:BUY|BUYING)\b|\b(?:BUY|BUYING)\s+(?:\w+\s+){0,4}?(?:\d+\s*)?(?:CE|CALLS?)\b|"
                     r"\b(?:PE|PUTS?)\s+(?:SELL|SELLING|SELLERS?|WRITE|WRITERS?|WRITING)\b|"
                     r"\b(?:SELL|WRITE)\s+(?:\w+\s+){0,4}?(?:\d+\s*)?(?:PE|PUTS?)\b|"
                     r"\bFUT(?:URES?)?S?\s+BUY\b|\bBUY\s+(?:\w+\s+){0,3}?FUT(?:URES?)?S?\b|\bGO\s+LONG\b|\bLONG\b")
BEAR_RE = re.compile(r"\b(?:PE|PUTS?)\s+(?:BUY|BUYING)\b|\b(?:BUY|BUYING)\s+(?:\w+\s+){0,4}?(?:\d+\s*)?(?:PE|PUTS?)\b|"
                     r"\b(?:CE|CALLS?)\s+(?:SELL|SELLING|SELLERS?|WRITE|WRITERS?|WRITING)\b|"
                     r"\b(?:SELL|WRITE)\s+(?:\w+\s+){0,4}?(?:\d+\s*)?(?:CE|CALLS?)\b|"
                     r"\bFUT(?:URES?)?S?\s+SELL\b|\bSELL\s+(?:\w+\s+){0,3}?FUT(?:URES?)?S?\b|\bSHORT\b")
SCHEMA = """
CREATE TABLE IF NOT EXISTS tip_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    group_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,
    basis        TEXT,
    arm1_kind    TEXT,
    spot         REAL,
    spot_source  TEXT,
    raw_text     TEXT,
    UNIQUE (group_id, symbol)
);
CREATE TABLE IF NOT EXISTS tip_call_grades (
    call_id        INTEGER NOT NULL,
    horizon        INTEGER NOT NULL,
    entry_px       REAL NOT NULL,
    entry_basis    TEXT NOT NULL,
    exit_session   TEXT NOT NULL,
    exit_close     REAL NOT NULL,
    ret_pct        REAL NOT NULL,
    signed_ret_pct REAL NOT NULL,
    hit            INTEGER NOT NULL,
    graded_at      TEXT NOT NULL,
    PRIMARY KEY (call_id, horizon)
);
CREATE TRIGGER IF NOT EXISTS tip_calls_no_update BEFORE UPDATE ON tip_calls
BEGIN SELECT RAISE(ABORT, 'tip_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS tip_calls_no_delete BEFORE DELETE ON tip_calls
BEGIN SELECT RAISE(ABORT, 'tip_calls is append-only'); END;
CREATE TRIGGER IF NOT EXISTS tip_call_grades_no_update BEFORE UPDATE ON tip_call_grades
BEGIN SELECT RAISE(ABORT, 'tip_call_grades is append-only'); END;
CREATE TRIGGER IF NOT EXISTS tip_call_grades_no_delete BEFORE DELETE ON tip_call_grades
BEGIN SELECT RAISE(ABORT, 'tip_call_grades is append-only'); END;
"""
_OBJECTS = {"tip_calls", "tip_call_grades", "tip_calls_no_update", "tip_calls_no_delete",
            "tip_call_grades_no_update", "tip_call_grades_no_delete"}


def ensure_schema(conn) -> None:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')")}
    if not _OBJECTS <= have:
        conn.executescript(SCHEMA)
        conn.commit()


# ------------------------------------------------------------ extraction (pure)
def symbols_in(text: str, known: set) -> list:
    """NSE symbols named in the text, in order: index aliases, the alias table, then words (and word pairs
    joined) found in `known` (the bhavcopy universe)."""
    t = normalize(text)
    found = []

    def add(sym):
        if sym and sym not in found:
            found.append(sym)
    for pattern, canon in INDEX_ALIASES:
        if canon in ("SENSEX", "BANKEX"):
            continue
        if re.search(rf"\b(?:{pattern})\b", t):
            add(canon)
            t = re.sub(rf"\b(?:{pattern})\b", " ", t)
    for alias, sym in sorted(SYMBOL_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            add(sym)
            t = re.sub(rf"\b{re.escape(alias)}\b", " ", t)
    words = [w for w in re.findall(r"[A-Z][A-Z0-9&\-]*", t)]
    for i, w in enumerate(words):
        if i + 1 < len(words) and (w + words[i + 1]) in known and w + words[i + 1] not in NOT_SYMBOLS:
            add(w + words[i + 1])
        elif w in known and w not in NOT_SYMBOLS and len(w) >= 2:
            add(w)
    return found


def direction_of(text: str):
    """('bullish' | 'bearish', matched phrase) or (None, why)."""
    t = normalize(text)
    bull, bear = BULL_RE.search(t), BEAR_RE.search(t)
    if bull and bear:
        return None, "points both ways"
    if bull:
        return "bullish", bull.group(0)
    if bear:
        return "bearish", bear.group(0)
    return None, "no direction"


def direction_from_trade(parsed: dict):
    """An Arm-1 trade's direction from its own structure (a spread's SELL leg must not read as bearish)."""
    s, legs = parsed.get("structure"), parsed.get("legs") or []
    if s == "bull_call_spread" or (s == "long" and legs and legs[0]["option_type"] == "CE"):
        return "bullish", f"{s} {legs[0]['option_type'] if legs else ''}".strip()
    if s == "bear_put_spread" or (s == "long" and legs and legs[0]["option_type"] == "PE"):
        return "bearish", f"{s} {legs[0]['option_type'] if legs else ''}".strip()
    return None, "no direction"


def extract(text: str, known: set, parsed: dict = None) -> list:
    """[{symbol, direction, basis}] — one per symbol named, all with the text's one direction."""
    from src.tip_desk import TRADE_KINDS
    if parsed and parsed.get("kind") in TRADE_KINDS:
        direction, basis = direction_from_trade(parsed)
    else:
        direction, basis = direction_of(text)
    if direction is None:
        return []
    return [{"symbol": s, "direction": direction, "basis": basis} for s in symbols_in(text, known)]


# ------------------------------------------------------------ prices (VM)
_KNOWN = {"file": None, "symbols": set()}


def known_symbols(lake_dir=None) -> set:
    """Every EQ/BE symbol in the newest bhavcopy day file (empty when the lake is missing or under test)."""
    from src.equity_desk import market_data_muzzled
    from src.ingestion import bhavcopy_clerk as bc
    if lake_dir is None and market_data_muzzled():
        return set()
    root = Path(lake_dir) if lake_dir else bc.BHAVCOPY_LAKE
    files = sorted(root.glob("????-??-??.csv")) if root.is_dir() else []
    if not files:
        return set()
    if _KNOWN["file"] != str(files[-1]):
        try:
            _KNOWN.update(file=str(files[-1]), symbols=set(bc.parse_bhavcopy(files[-1].read_text())))
        except OSError:
            return set()
    return _KNOWN["symbols"]


def spot_now(symbol: str):
    """(price, source) or (None, why). Fixed-list names by ticker; others by the darling/tier-1 F&O id map."""
    from src import dhan_client
    from src.equity_desk import market_data_muzzled, security_id_for
    if market_data_muzzled():
        return None, "muzzled (test)"
    key = symbol if symbol.startswith("NIFTY") else f"{symbol}.NS"
    if dhan_client._resolve(key) is not None:
        px = dhan_client.get_live_price(key)
        return (px, "dhan_ltp") if px else (None, "no LTP")
    sid = security_id_for(symbol)
    if sid is None:
        return None, f"no Dhan id for {symbol} (not on the fixed list or in data/darling_ids.json)"
    px = dhan_client.get_live_price_by_id(sid)
    return (px, "dhan_ltp_by_id") if px else (None, "no LTP")


def record(conn, group_id: str, text: str, arm1_kind: str, known: set = None, spot_fn=None, now=None,
           parsed: dict = None) -> list:
    """Log each (group, symbol) call once; later joined texts of the same group add only new symbols."""
    ensure_schema(conn)
    known = known_symbols() if known is None else known
    now = now or now_ist()
    out = []
    for c in extract(text, known, parsed):
        if conn.execute("SELECT 1 FROM tip_calls WHERE group_id = ? AND symbol = ?",
                        (str(group_id), c["symbol"])).fetchone():
            continue
        px, src = (spot_fn or spot_now)(c["symbol"])
        conn.execute("INSERT OR IGNORE INTO tip_calls (ts, group_id, symbol, direction, basis, arm1_kind, spot, "
                     "spot_source, raw_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (now.isoformat(timespec="seconds"), str(group_id), c["symbol"], c["direction"], c["basis"],
                      arm1_kind, px, src, str(text)[:2000]))
        out.append(dict(c, spot=px, spot_source=src))
    conn.commit()
    return out


# ------------------------------------------------------------ grading
def default_closes(symbol: str) -> list:
    """[(session ISO, close)] oldest first: the bhavcopy lake for stocks, Dhan daily bars for an index."""
    if symbol.startswith("NIFTY"):
        from src import dhan_client
        from src.equity_desk import market_data_muzzled
        if market_data_muzzled():
            return []
        return [(b["date"], float(b["close"])) for b in dhan_client.get_daily_ohlc(symbol, days=60) if b.get("close")]
    from src.ingestion import bhavcopy_clerk as bc
    return [(b["session"], float(b["close"])) for b in bc.bars_for(symbol, days=60) if b.get("close")]


def grade_one(call: dict, closes: list, horizon: int):
    """One grade row, or None while the horizon has not closed. Entry = the spot at paste time, else the paste
    day's close (basis named). Exit = the close `horizon` sessions after the paste day."""
    paste_day = call["ts"][:10]
    after = [(d, c) for d, c in closes if d > paste_day]
    if len(after) < horizon:
        return None
    entry, basis = call.get("spot"), "spot_at_paste"
    if not entry:
        same = [c for d, c in closes if d == paste_day]
        if not same:
            return None
        entry, basis = same[-1], "paste_day_close"
    exit_day, exit_close = after[horizon - 1]
    ret = (exit_close / float(entry) - 1.0) * 100.0
    signed = ret if call["direction"] == "bullish" else -ret
    return {"call_id": call["id"], "horizon": horizon, "entry_px": round(float(entry), 2), "entry_basis": basis,
            "exit_session": exit_day, "exit_close": exit_close, "ret_pct": round(ret, 3),
            "signed_ret_pct": round(signed, 3), "hit": 1 if signed > 0 else 0}


def _calls(conn) -> list:
    cur = conn.execute("SELECT * FROM tip_calls ORDER BY id")
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def grade(conn, closes_fn=None, now=None) -> list:
    ensure_schema(conn)
    now = now or now_ist()
    done = {(r[0], r[1]) for r in conn.execute("SELECT call_id, horizon FROM tip_call_grades")}
    cache, written = {}, []
    for call in _calls(conn):
        todo = [h for h in HORIZONS if (call["id"], h) not in done]
        if not todo:
            continue
        if call["symbol"] not in cache:
            try:
                cache[call["symbol"]] = (closes_fn or default_closes)(call["symbol"])
            except Exception as exc:
                print(f"  (tip calls: no closes for {call['symbol']}: {exc})")
                cache[call["symbol"]] = []
        upto = now.date().isoformat()
        for h in todo:
            g = grade_one(call, [(d, c) for d, c in cache[call["symbol"]] if d <= upto], h)
            if g is None:
                continue
            conn.execute("INSERT OR IGNORE INTO tip_call_grades (call_id, horizon, entry_px, entry_basis, "
                         "exit_session, exit_close, ret_pct, signed_ret_pct, hit, graded_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (g["call_id"], h, g["entry_px"], g["entry_basis"], g["exit_session"], g["exit_close"],
                          g["ret_pct"], g["signed_ret_pct"], g["hit"], now.isoformat(timespec="seconds")))
            written.append(g)
    conn.commit()
    return written


def report(conn) -> dict:
    """Per horizon: n, hit rate, mean signed return; split Arm-1-tradeable vs rest; plus the base rate (how often
    the named stock simply ROSE over the same window — a bearish-only caller in a falling market looks good)."""
    ensure_schema(conn)
    rows = conn.execute("SELECT g.horizon, g.hit, g.signed_ret_pct, g.ret_pct, c.arm1_kind, c.direction "
                        "FROM tip_call_grades g JOIN tip_calls c ON c.id = g.call_id").fetchall()
    out = {}
    for h in HORIZONS:
        sel = [r for r in rows if r[0] == h]

        def stats(rs):
            n = len(rs)
            return {"n": n, "hit_rate": round(sum(r[1] for r in rs) / n, 3) if n else None,
                    "mean_signed_ret_pct": round(sum(r[2] for r in rs) / n, 3) if n else None}
        arm1 = [r for r in sel if r[4] in ("OPTION_BUY", "DEFINED_SPREAD")]
        rest = [r for r in sel if r[4] not in ("OPTION_BUY", "DEFINED_SPREAD")]
        out[f"{h}D"] = dict(stats(sel), arm1_tradeable=stats(arm1), not_arm1=stats(rest),
                            bullish=sum(1 for r in sel if r[5] == "bullish"),
                            bearish=sum(1 for r in sel if r[5] == "bearish"),
                            base_rate_rose=round(sum(1 for r in sel if r[3] > 0) / len(sel), 3) if sel else None)
    out["calls_logged"] = conn.execute("SELECT COUNT(*) FROM tip_calls").fetchone()[0]
    return out


def report_text(rep: dict) -> str:
    lines = [f"📒 Tipster calls — {rep['calls_logged']} logged"]
    for h in HORIZONS:
        r = rep[f"{h}D"]
        if not r["n"]:
            lines.append(f"{h}D: nothing graded yet")
            continue
        lines.append(f"{h}D: n={r['n']} ({r['bullish']} bull / {r['bearish']} bear) · hit {r['hit_rate']:.0%} · "
                     f"mean {r['mean_signed_ret_pct']:+.2f}% · stock simply rose {r['base_rate_rose']:.0%} · "
                     f"Arm-1 tips n={r['arm1_tradeable']['n']}, the rest n={r['not_arm1']['n']}")
    if all((rep[f"{h}D"]["n"] or 0) < 20 for h in HORIZONS):
        lines.append("_n < 20 at every horizon — too few to call it edge either way._")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Grade the tipster's direction calls (1D / 5D)")
    ap.add_argument("--grade", action="store_true", help="grade every call whose horizon has closed")
    ap.add_argument("--report", action="store_true", help="print the hit-rate report")
    ap.add_argument("--notify", action="store_true", help="post the report card when --grade wrote new grades")
    args = ap.parse_args(argv)
    from src import brain_map
    conn = brain_map.connect()
    try:
        new = grade(conn) if args.grade else []
        rep = report(conn)
        if args.grade:
            print(f"[tip_calls] graded {len(new)} new (call, horizon) row(s)")
        if args.report or args.grade:
            print(json.dumps(rep, indent=2))
        if args.notify and new:
            from src.notifier import fire_broadcast
            fire_broadcast({"event": "tip_calls_report", "ticker": "TIP_CALLS", "date": date.today().isoformat(),
                            "text": report_text(rep)})
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
