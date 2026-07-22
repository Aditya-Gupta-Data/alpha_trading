"""
src/ingestion/scrip_master.py — the scrip-master reconciliation clerk
=====================================================================

The Dept-1 gap Fable review #2 wrote down and left open (ARCHITECTURE.md:
"Until that job exists, every deploy must re-verify IDs by hand"). This
is that job.

THE RISK IT CLOSES: `dhan_client.SECURITY_ID_MAP` hard-codes broker
security IDs. Those IDs silently ROT — a company delists, demerges, or is
renamed, and the id we hold either vanishes or, far worse, keeps
resolving to a DIFFERENT instrument. Nothing crashes; we simply price the
wrong stock. It has bitten twice, both times caught by a human at deploy
time (ledger Issues 14/15: LTIM delisted, TATAMOTORS demerged into
TMPV/TMCV).

THE SOURCE: Dhan publishes its full scrip master as a public static CSV
(~27MB, no credentials, no session, not NSE — so this carries none of the
NSE ban risk that makes the other clerks Mac-only; this one may run
anywhere). One GET, no throttle needed: it is a CDN file, and we fetch it
weekly, not per-symbol.

WHAT IT CHECKS, per mapped instrument:
  * does our (segment, id) still EXIST in the master?
  * does that row's symbol still MATCH the ticker we filed it under?
  * for equities, what SERIES is it now (EQ/BE/…)? A series move is not a
    mismatch but it IS reportable — it changes what the bhavcopy clerk
    keeps.
Verdicts: `ok` | `symbol_mismatch` (the dangerous one — a live id pointing
at another instrument) | `id_not_found` (delisted/withdrawn).

WANTED LIST (`config/scrip_wanted.json`, optional): symbols we do NOT have
an id for but want one — GOLDBEES is the standing example, and the
wealth-flywheel merge is explicitly blocked until its id is
scrip-master-verified. The clerk looks each up and reports the candidate
rows, so the answer arrives without anyone hand-searching a 27MB file.

HONESTY RULES (the whole point — a reconciler that lies is worse than
none):
  * a fetch/parse failure is an OUTAGE, never "all clear". `status`
    distinguishes `verified` from `unavailable`; nothing downstream may
    read an unavailable run as a pass.
  * the raw 27MB CSV is deliberately NOT archived (re-fetchable upstream,
    and weekly copies would bloat the lake). The REPORT is the artifact.

Cards: mismatches are exactly the "needs human review" class the owner
ruled must reach Discord in real time — ONE card per NEW problem, keyed
by (ticker, verdict, id) in an append-only ledger so a known-broken name
alerts once, not weekly (the exposure-gate convention: the ledger IS the
memory). A failed send does NOT mark items seen, so they re-announce when
Discord is back.

Outages: logs/scrip_master.jsonl   SM-404 no file | SM-408 timeout |
                                   SM-500 unexpected
Report:  data/scrip_reconciliation.json

CLI:  python3 -m src.ingestion.scrip_master [--json] [--quiet]
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REPORT_PATH = ROOT / "data" / "scrip_reconciliation.json"
WANTED_PATH = ROOT / "config" / "scrip_wanted.json"
ALERTS_LEDGER = ROOT / "logs" / "scrip_alerts.jsonl"
OUTAGE_LOG = ROOT / "logs" / "scrip_master.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# our SECURITY_ID_MAP segment -> the master's (exchange, segment) pair
SEGMENTS = {"NSE_EQ": ("NSE", "E"), "IDX_I": ("NSE", "I")}

# the master's own name columns, most specific first
NAME_COLS = ("SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "SM_SYMBOL_NAME")


def _now_iso() -> str:
    return datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")


def _log_outage(code: str, detail: str, log_path=None) -> None:
    path = Path(log_path) if log_path else OUTAGE_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": _now_iso(), "code": code,
                                 "detail": str(detail)[:300]}) + "\n")
    except OSError:
        pass


def _norm(s) -> str:
    """Compare names case/space/punctuation-blind: the master writes
    'Nifty 50' where we key 'NIFTY 50', and 'BANKNIFTY' for 'NIFTY BANK'."""
    return "".join(ch for ch in str(s or "").upper() if ch.isalnum())


def fetch_master(fetch_fn=None) -> str:
    """The raw master CSV as text. Injectable for offline tests."""
    if fetch_fn is not None:
        return fetch_fn(MASTER_URL)
    import ssl
    import urllib.request
    # certifi, like every other clerk: the Mac's framework Python carries
    # no system CA bundle, and an unverified context is never the answer.
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(
        MASTER_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; "
                                           "alpha-trading scrip clerk)"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def index_master(csv_text: str) -> dict:
    """{(exch, seg, id): row} — the lookup the reconciler walks."""
    out = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        key = (row.get("SEM_EXM_EXCH_ID"), row.get("SEM_SEGMENT"),
               row.get("SEM_SMST_SECURITY_ID"))
        if all(key):
            out[key] = row
    return out


def _names_of(row: dict) -> set:
    return {_norm(row.get(c)) for c in NAME_COLS if row.get(c)}


def check_one(ticker: str, instr: dict, master: dict) -> dict:
    """One mapped instrument -> its verdict row. Pure."""
    base = _norm(ticker.replace(".NS", ""))
    seg_pair = SEGMENTS.get(instr.get("seg"))
    entry = {"ticker": ticker, "id": instr.get("id"),
             "seg": instr.get("seg"), "inst": instr.get("inst")}
    if seg_pair is None:
        return {**entry, "verdict": "id_not_found",
                "detail": f"unknown segment {instr.get('seg')}"}
    row = master.get((seg_pair[0], seg_pair[1], str(instr.get("id"))))
    if row is None:
        return {**entry, "verdict": "id_not_found",
                "detail": "id absent from the scrip master "
                          "(delisted/withdrawn?)"}
    names = _names_of(row)
    series = (row.get("SEM_SERIES") or "").strip()
    found = (row.get("SEM_TRADING_SYMBOL") or "").strip()
    if base in names:
        return {**entry, "verdict": "ok", "master_symbol": found,
                "series": series}
    return {**entry, "verdict": "symbol_mismatch", "master_symbol": found,
            "series": series,
            "detail": f"id {instr.get('id')} now trades as '{found}' "
                      f"— we file it as '{ticker}'"}


def lookup_wanted(symbols: list, master: dict) -> dict:
    """{symbol: [candidate rows]} for ids we don't hold yet (GOLDBEES et
    al). Equity segment only; exact name match, never a fuzzy guess — a
    wrong id here is the exact failure this module exists to prevent."""
    want = {_norm(s): s for s in symbols or []}
    found = {s: [] for s in want.values()}
    if not want:
        return found
    for (exch, seg, sid), row in master.items():
        if seg != "E" or exch != "NSE":
            continue
        hit = want.get(_norm(row.get("SEM_TRADING_SYMBOL")))
        if hit:
            found[hit].append({"id": sid, "seg": "NSE_EQ",
                               "symbol": (row.get("SEM_TRADING_SYMBOL")
                                          or "").strip(),
                               "name": (row.get("SM_SYMBOL_NAME")
                                        or "").strip(),
                               "series": (row.get("SEM_SERIES")
                                          or "").strip()})
    return found


def reconcile(id_map: dict, master: dict, wanted: list = None) -> dict:
    """Every mapped instrument judged, plus the wanted-list lookups."""
    rows = [check_one(t, i, master) for t, i in sorted(id_map.items())]
    problems = [r for r in rows if r["verdict"] != "ok"]
    return {
        "as_of": _now_iso(), "status": "verified",
        "master_rows": len(master), "checked": len(rows),
        "ok": len(rows) - len(problems),
        "problems": problems,
        "counts": {v: sum(1 for r in rows if r["verdict"] == v)
                   for v in ("ok", "symbol_mismatch", "id_not_found")},
        "wanted": lookup_wanted(wanted or [], master),
        "rows": rows,
    }


def _load_wanted(path=None) -> list:
    try:
        data = json.loads(Path(path or WANTED_PATH).read_text())
    except (OSError, ValueError):
        return []
    return data.get("wanted") or []


def notify_problems(report: dict, ledger_path=None, notify_fn=None) -> int:
    """ONE Discord card for NEW problems only (owner directive: review
    flags never sit log-only). De-duped by (ticker, verdict, id) — a
    known-broken name alerts once, not every week. A failed send does NOT
    mark items seen. Fail-open; returns how many were announced."""
    problems = report.get("problems") or []
    if not problems:
        return 0
    path = Path(ledger_path) if ledger_path is not None else ALERTS_LEDGER
    try:
        seen = set()
        if path.exists():
            for raw in path.read_text().splitlines():
                try:
                    seen.add(json.loads(raw)["key"])
                except (ValueError, KeyError):
                    continue
        new = [(f"{p['ticker']}|{p['verdict']}|{p['id']}", p)
               for p in problems]
        new = [(k, p) for k, p in new if k not in seen]
        if not new:
            return 0
        lines = [f"🪪 **Scrip master: {len(new)} security id(s) need human "
                 "review** — a mapped id no longer matches its ticker, so "
                 "we may be PRICING THE WRONG INSTRUMENT. Nothing "
                 "auto-corrects (a guessed id is the same bug again); fix "
                 "`dhan_client.SECURITY_ID_MAP` by hand."]
        for _, p in new[:8]:
            lines.append(f"• {p['ticker']} (id {p['id']}): {p['verdict']}"
                         + (f" — {p['detail']}" if p.get("detail") else ""))
        if len(new) > 8:
            lines.append(f"…and {len(new) - 8} more — see "
                         "data/scrip_reconciliation.json.")
        if notify_fn is None:
            from src.notifier import fire_broadcast
            notify_fn = lambda text: fire_broadcast({"text": text})  # noqa: E731
        notify_fn("\n".join(lines))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for key, p in new:
                fh.write(json.dumps({"key": key, "as_of": report["as_of"],
                                     "ticker": p["ticker"],
                                     "verdict": p["verdict"]}) + "\n")
        return len(new)
    except Exception as exc:
        print(f"  (scrip master: review notify skipped [{exc}])")
        return 0


def run(fetch_fn=None, id_map=None, wanted=None, write: bool = True,
        notify: bool = True, notify_fn=None, report_path=None,
        ledger_path=None, wanted_path=None) -> dict:
    """The cron seam: fetch -> index -> reconcile -> card -> report.
    A fetch/parse failure returns status='unavailable' — NEVER a pass."""
    if id_map is None:
        from src.dhan_client import SECURITY_ID_MAP
        id_map = SECURITY_ID_MAP
    if wanted is None:
        wanted = _load_wanted(wanted_path)
    try:
        master = index_master(fetch_master(fetch_fn))
        if not master:
            raise ValueError("scrip master parsed to zero rows")
    except Exception as exc:
        code = "SM-408" if "timed out" in str(exc).lower() else "SM-500"
        if "404" in str(exc):
            code = "SM-404"
        _log_outage(code, exc)
        report = {"as_of": _now_iso(), "status": "unavailable",
                  "code": code, "detail": str(exc)[:300],
                  "checked": 0, "problems": [],
                  "note": "NOT a pass — the master could not be read; "
                          "ids remain unverified this run."}
        if write:
            _write(report, report_path)
        return report

    report = reconcile(id_map, master, wanted)
    if notify:
        report["announced"] = notify_problems(report, ledger_path, notify_fn)
    if write:
        _write(report, report_path)
    return report


def _write(report: dict, report_path=None) -> None:
    try:
        out = Path(report_path) if report_path else REPORT_PATH
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1))
    except OSError as exc:
        print(f"  (scrip master: report write skipped [{exc}])")


if __name__ == "__main__":
    import sys

    rep = run(notify="--quiet" not in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=1))
        sys.exit(0)
    if rep["status"] != "verified":
        print(f"scrip master UNAVAILABLE [{rep.get('code')}]: "
              f"{rep.get('detail')}")
        print("ids remain UNVERIFIED this run — this is not a pass.")
        sys.exit(1)
    c = rep["counts"]
    print(f"scrip reconciliation {rep['as_of']} — {rep['master_rows']:,} "
          f"master rows")
    print(f"  checked {rep['checked']}: {c['ok']} ok, "
          f"{c['symbol_mismatch']} symbol mismatch, "
          f"{c['id_not_found']} id not found")
    for p in rep["problems"]:
        print(f"  ⚠ {p['ticker']:<16} id {p['id']:<9} {p['verdict']}"
              + (f" — {p['detail']}" if p.get("detail") else ""))
    for sym, hits in (rep.get("wanted") or {}).items():
        if hits:
            for h in hits:
                print(f"  wanted {sym}: id {h['id']} "
                      f"({h['symbol']} / {h['name']} [{h['series']}])")
        else:
            print(f"  wanted {sym}: NOT FOUND in the NSE equity segment")
    if rep.get("announced"):
        print(f"  ({rep['announced']} new problem(s) announced on Discord)")


# ----------------------------------------- darling ids (decision #83)

DARLING_IDS_PATH = ROOT / "data" / "darling_ids.json"
TIERS_PATH_FOR_IDS = ROOT / "data" / "darling_tiers.json"


def _darling_symbols(tiers_path=None) -> list:
    """Every symbol in the tier table — the id universe the VM desk may
    ever need to quote."""
    import json as _json
    p = Path(tiers_path) if tiers_path else TIERS_PATH_FOR_IDS
    try:
        tiers = _json.loads(p.read_text()).get("tiers") or {}
    except (OSError, ValueError):
        return []
    return sorted({r.get("symbol") for rows in tiers.values()
                   for r in rows if r.get("symbol")})


def build_darling_ids(symbols=None, fetch_fn=None, out_path=None,
                      tiers_path=None) -> dict:
    """The VM desk's quote ids (decision #83): darlings are non-F&O names
    outside SECURITY_ID_MAP, so their ids come from Dhan's PUBLIC scrip
    master — exact name match only, EQ series preferred, anything
    ambiguous lands in `unresolved` and stays UNQUOTABLE (#78: a guessed
    id silently prices the wrong instrument). Built ON THE MAC (27MB
    fetch), shipped nightly; the VM refuses ids older than its own
    freshness gate."""
    import json as _json
    symbols = symbols if symbols is not None else _darling_symbols(tiers_path)
    master = index_master(fetch_master(fetch_fn))
    found = lookup_wanted(symbols, master)
    ids, unresolved = {}, {}
    for sym, cands in found.items():
        eq = [c for c in cands if c.get("series") == "EQ"] or cands
        if len(eq) == 1:
            ids[sym] = {"id": eq[0]["id"],
                        "master_symbol": eq[0]["symbol"],
                        "series": eq[0]["series"]}
        else:
            unresolved[sym] = f"{len(cands)} candidate(s) in the master"
    out = {"built_at": _now_iso(), "count": len(ids),
           "ids": ids, "unresolved": unresolved}
    p = Path(out_path) if out_path else DARLING_IDS_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(out, indent=1))
    except OSError:
        pass
    return out


def ensure_darling_ids(max_age_days: int = 7, fetch_fn=None,
                       out_path=None, tiers_path=None) -> bool:
    """Weekly-refresh guard around the heavy fetch: rebuild only when the
    artifact is absent or older than `max_age_days`. A failed fetch keeps
    the previous file (the VM's own staleness gate judges it) and returns
    False — never a crash in the Mac evening chain."""
    import json as _json
    from datetime import datetime as _dt
    p = Path(out_path) if out_path else DARLING_IDS_PATH
    try:
        built = _dt.fromisoformat(_json.loads(p.read_text())["built_at"])
        if (_dt.now(built.tzinfo) - built).days <= max_age_days:
            return True
    except Exception:
        pass
    try:
        out = build_darling_ids(fetch_fn=fetch_fn, out_path=out_path,
                                tiers_path=tiers_path)
        print(f"  (darling ids rebuilt: {out['count']} resolved, "
              f"{len(out['unresolved'])} unresolved)")
        return True
    except Exception as exc:
        print(f"  (darling ids rebuild failed — keeping prior file: {exc})")
        return False
