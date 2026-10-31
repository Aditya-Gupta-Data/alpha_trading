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
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
REPORT_PATH = ROOT / "data" / "scrip_reconciliation.json"
WANTED_PATH = ROOT / "config" / "scrip_wanted.json"
MACRO_SECURITIES_PATH = ROOT / "config" / "macro_securities.json"
ALERTS_LEDGER = ROOT / "logs" / "scrip_alerts.jsonl"
OUTAGE_LOG = ROOT / "logs" / "scrip_master.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# our SECURITY_ID_MAP segment -> the master's (exchange, segment) pair
# V1.3 (decision #106, 2026-09-23): MCX commodities join the map. In the
# master an MCX row is exchange "MCX", segment "M"; Dhan's API segment name
# for it is "MCX_COMM" (the same string `cross_asset` already uses).
SEGMENTS = {"NSE_EQ": ("NSE", "E"), "IDX_I": ("NSE", "I"),
            "MCX_COMM": ("MCX", "M")}

# The commodity underlyings the data pipelines follow (read-only capture —
# NO trading path opens on them; that is a separate numbered decision).
# Resolved by exact SM_SYMBOL_NAME on MCX FUTCOM rows, front-month contract.
COMMODITY_SYMBOLS = ("GOLD", "SILVER", "CRUDEOIL")

# Mapping between config/macro_securities.json keys and MCX SM_SYMBOL_NAME
MCX_COMMODITY_MAP = {
    "GOLD_INDIA": "GOLD",
    "SILVER": "SILVER",
    "CRUDE": "CRUDEOIL",
    "COPPER": "COPPER",
    "ALUMINIUM": "ALUMINIUM",
    "ZINC": "ZINC",
}
MCX_SYMBOL_TO_MACRO_KEY = {v: k for k, v in MCX_COMMODITY_MAP.items()}
MCX_FRONT_MONTH_KEYS = ("GOLD_INDIA", "SILVER", "CRUDE")

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


def check_one(ticker: str, instr: dict, master: dict, today=None) -> dict:
    """One mapped instrument -> its verdict row. Pure."""
    mapped_ticker = MCX_COMMODITY_MAP.get(ticker, ticker)
    base = _norm(mapped_ticker.replace(".NS", ""))
    seg = instr.get("seg") or instr.get("segment")
    inst = instr.get("inst") or instr.get("instrument")
    seg_pair = SEGMENTS.get(seg)
    entry = {"ticker": ticker, "id": instr.get("id"),
             "seg": seg, "inst": inst}
    if seg_pair is None:
        return {**entry, "verdict": "id_not_found",
                "detail": f"unknown segment {seg}"}
    row = master.get((seg_pair[0], seg_pair[1], str(instr.get("id"))))
    if row is None:
        return {**entry, "verdict": "id_not_found",
                "detail": "id absent from the scrip master "
                          "(delisted/withdrawn?)"}
    names = _names_of(row)
    series = (row.get("SEM_SERIES") or "").strip()
    found = (row.get("SEM_TRADING_SYMBOL") or "").strip()
    if base in names or _norm(ticker.replace(".NS", "")) in names:
        if today is not None and inst == "FUTCOM":
            today_iso = (today.isoformat() if hasattr(today, "isoformat") else str(today))[:10]
            exp = _expiry_iso(row)
            if exp and exp < today_iso:
                return {**entry, "verdict": "expired_contract",
                        "master_symbol": found, "series": series, "expiry": exp,
                        "detail": f"id {instr.get('id')} ({found}) expired on {exp}"}
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


def _expiry_iso(row: dict) -> str:
    return (row.get("SEM_EXPIRY_DATE") or "").strip()[:10]


def _today_iso(today=None) -> str:
    from datetime import date as _date
    if today is None:
        return _date.today().isoformat()
    if hasattr(today, "isoformat"):
        return today.isoformat()[:10]
    return str(today)[:10]


_MONTH_ABBR = ("", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _infer_mcx_display_symbol(mcx_symbol: str, expiry_iso: str,
                              fallback: str = "") -> str:
    """Human-readable '<SYMBOL> <MON> FUT' label for macro_securities.json."""
    if fallback and " " in fallback and fallback.upper().endswith("FUT"):
        return fallback.strip()
    try:
        m = int(str(expiry_iso)[5:7])
        if 1 <= m <= 12:
            return f"{mcx_symbol.upper()} {_MONTH_ABBR[m]} FUT"
    except (ValueError, IndexError):
        pass
    return fallback.strip() or f"{mcx_symbol.upper()} FUT"


def lookup_commodities(symbols: list, master: dict, today=None,
                       roll_on_expiry: bool = True) -> dict:
    """{symbol: {id, seg, inst, expiry, master_symbol, lot_units, next: [...],
    option_expiries: [...]}} for MCX commodity underlyings (V1.3). The id is
    the FRONT-MONTH futures contract (`FUTCOM`, earliest active expiry) —
    that is the instrument Dhan quotes bars and option chains against.
    When `roll_on_expiry=True` (default) and the nearest contract expires
    on `today`, it seamlessly rolls to the next month's contract (if one is
    listed in the master) so callers never hit a dying contract on expiry day.
    `next` carries the following contracts so a roll is a lookup, not a
    re-derivation (the CA-410 expired-id trap in cross_asset).
    `option_expiries` lists the OPTFUT expiries the master holds, so the
    archiver knows there is a chain to ask for. Exact name match on
    SM_SYMBOL_NAME only — a guessed id silently prices the wrong metal (#78).
    A symbol with no live contract is absent from the result, never
    invented."""
    today_s = _today_iso(today)
    want = {_norm(s): s for s in symbols or []}
    futs, opts = {s: [] for s in want.values()}, {s: set() for s in want.values()}
    for (exch, seg, sid), row in master.items():
        if exch != "MCX" or seg != "M":
            continue
        hit = want.get(_norm(row.get("SM_SYMBOL_NAME")))
        if not hit:
            continue
        inst = (row.get("SEM_INSTRUMENT_NAME") or "").strip()
        exp = _expiry_iso(row)
        if inst == "FUTCOM" and exp >= today_s:
            futs[hit].append({"id": str(sid), "expiry": exp,
                              "master_symbol": (row.get("SEM_TRADING_SYMBOL") or "").strip(),
                              "custom_symbol": (row.get("SEM_CUSTOM_SYMBOL") or "").strip(),
                              "lot_units": (row.get("SEM_LOT_UNITS") or "").strip()})
        elif inst == "OPTFUT" and exp >= today_s:
            opts[hit].add(exp)
    out = {}
    for sym, rows in futs.items():
        if not rows:
            continue
        rows.sort(key=lambda r: (r["expiry"], r["id"]))
        rolled_from = None
        if roll_on_expiry and len(rows) > 1 and rows[0]["expiry"] <= today_s:
            later_idx = next((i for i, r in enumerate(rows) if r["expiry"] > today_s), None)
            if later_idx is not None and later_idx > 0:
                rolled_from = {"id": rows[0]["id"], "expiry": rows[0]["expiry"],
                               "master_symbol": rows[0]["master_symbol"]}
                front = rows[later_idx]
                next_rows = rows[later_idx + 1 : later_idx + 3]
            else:
                front = rows[0]
                next_rows = rows[1:3]
        else:
            front = rows[0]
            next_rows = rows[1:3]
        entry = {"id": front["id"], "seg": "MCX_COMM", "inst": "FUTCOM",
                 "expiry": front["expiry"], "master_symbol": front["master_symbol"],
                 "custom_symbol": front["custom_symbol"],
                 "lot_units": front["lot_units"],
                 "next": [{"id": r["id"], "expiry": r["expiry"],
                           "master_symbol": r["master_symbol"]} for r in next_rows],
                 "option_expiries": sorted(opts[sym])[:4]}
        if rolled_from is not None:
            entry["rolled_from"] = rolled_from
        out[sym] = entry
    return out


def _normalize_mcx_key_and_symbol(symbol_or_key: str) -> tuple:
    """Return (macro_key, mcx_symbol) for any MCX commodity alias."""
    raw = str(symbol_or_key or "").strip().upper()
    if raw in MCX_COMMODITY_MAP:
        return raw, MCX_COMMODITY_MAP[raw]
    if raw in MCX_SYMBOL_TO_MACRO_KEY:
        return MCX_SYMBOL_TO_MACRO_KEY[raw], raw
    return raw, raw


def _load_macro_securities(path=None) -> dict:
    p = Path(path) if path is not None else MACRO_SECURITIES_PATH
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def resolve_front_month(symbol_or_key: str, master: dict = None,
                        securities: dict = None, securities_path=None,
                        today=None, roll_on_expiry: bool = True) -> dict | None:
    """Resolve the active front-month MCX futures contract for a commodity
    (`GOLD` / `GOLD_INDIA`, `SILVER`, `CRUDEOIL` / `CRUDE`, etc.).

    1. When `master` is supplied and contains live FUTCOM rows for the
       symbol, resolves from the master (rolling automatically onto the next
       month if the front contract expires on or before `today`).
    2. Otherwise falls back to `securities` (`config/macro_securities.json`):
       if the current `_expiry` is still active, returns the current contract;
       if expired (or expiring today with `roll_on_expiry=True`), rolls onto
       the first unexpired contract in `_next`.
    3. Never guesses or returns an expired contract (#78): returns `None` if
       no unexpired contract is available in either `master` or `_next`.
    """
    today_s = _today_iso(today)
    macro_key, mcx_sym = _normalize_mcx_key_and_symbol(symbol_or_key)
    sec_doc = (securities if securities is not None
               else _load_macro_securities(securities_path))
    cur = sec_doc.get(macro_key) if isinstance(sec_doc, dict) else None
    cur = cur if isinstance(cur, dict) else {}
    cur_id = str(cur.get("id") or "")
    cur_exp = str(cur.get("_expiry") or "")[:10]
    cur_sym = str(cur.get("_symbol") or "")

    if master:
        resolved = lookup_commodities([mcx_sym], master, today=today_s,
                                      roll_on_expiry=roll_on_expiry).get(mcx_sym)
        if resolved:
            new_id = str(resolved["id"])
            new_exp = str(resolved["expiry"])[:10]
            disp_sym = _infer_mcx_display_symbol(
                mcx_sym, new_exp,
                resolved.get("custom_symbol") or resolved.get("master_symbol") or "")
            next_ladder = [
                {"id": str(n["id"]), "expiry": str(n["expiry"])[:10],
                 "symbol": _infer_mcx_display_symbol(mcx_sym, n["expiry"], n.get("master_symbol", ""))}
                for n in (resolved.get("next") or [])
            ]
            rolled = bool((cur_id and cur_id != new_id) or resolved.get("rolled_from"))
            rolled_from = None
            if cur_id and cur_id != new_id:
                rolled_from = {"id": cur_id, "expiry": cur_exp, "symbol": cur_sym}
            elif resolved.get("rolled_from"):
                rf = resolved["rolled_from"]
                rolled_from = {"id": rf["id"], "expiry": rf["expiry"],
                               "symbol": rf.get("master_symbol", "")}
            return {
                "key": macro_key, "macro_key": macro_key, "mcx_symbol": mcx_sym,
                "id": new_id, "seg": "MCX_COMM", "inst": "FUTCOM",
                "expiry": new_exp, "_expiry": new_exp,
                "_symbol": disp_sym, "master_symbol": resolved.get("master_symbol", disp_sym),
                "_next": next_ladder, "next": resolved.get("next", []),
                "rolled": rolled, "rolled_from": rolled_from,
                "source": "master",
            }

    if not cur or not cur_id or not cur_exp:
        return None

    next_rungs = [
        r for r in (cur.get("_next") or [])
        if isinstance(r, dict) and r.get("id") and r.get("expiry")
    ]
    next_rungs.sort(key=lambda r: (str(r["expiry"])[:10], str(r["id"])))
    valid_next = [
        r for r in next_rungs
        if (str(r["expiry"])[:10] > today_s if roll_on_expiry
            else str(r["expiry"])[:10] >= today_s)
    ]

    is_expired = (cur_exp < today_s) or (roll_on_expiry and cur_exp <= today_s and bool(valid_next))
    if not is_expired and cur_exp >= today_s:
        return {
            "key": macro_key, "macro_key": macro_key, "mcx_symbol": mcx_sym,
            "id": cur_id, "seg": str(cur.get("seg") or "MCX_COMM"),
            "inst": str(cur.get("inst") or "FUTCOM"),
            "expiry": cur_exp, "_expiry": cur_exp,
            "_symbol": cur_sym or _infer_mcx_display_symbol(mcx_sym, cur_exp),
            "_next": valid_next, "next": valid_next,
            "rolled": False, "rolled_from": None,
            "source": "current",
        }

    if not valid_next:
        return None

    chosen = valid_next[0]
    remaining = valid_next[1:]
    new_id = str(chosen["id"])
    new_exp = str(chosen["expiry"])[:10]
    new_sym = (str(chosen.get("symbol") or chosen.get("custom_symbol") or "").strip()
               or _infer_mcx_display_symbol(mcx_sym, new_exp, str(chosen.get("master_symbol") or "")))
    return {
        "key": macro_key, "macro_key": macro_key, "mcx_symbol": mcx_sym,
        "id": new_id, "seg": str(cur.get("seg") or "MCX_COMM"),
        "inst": str(cur.get("inst") or "FUTCOM"),
        "expiry": new_exp, "_expiry": new_exp,
        "_symbol": new_sym,
        "_next": remaining, "next": remaining,
        "rolled": True,
        "rolled_from": {"id": cur_id, "expiry": cur_exp, "symbol": cur_sym},
        "source": "next_ladder",
    }


def roll_mcx_contracts(securities: dict = None, master: dict = None,
                       today=None, keys=None, securities_path=None,
                       write: bool = False, roll_on_expiry: bool = True) -> dict:
    """Identify the active front-month contracts for MCX commodities (Gold,
    Silver, Crude Oil, and industrial metals) and roll any expired contract
    onto the next month's contract (via the scrip master when available, or
    the verified `_next` ladder in `config/macro_securities.json`).

    Prevents stale-contract refusals (`CA-410` / `stale_instruments`) without
    ever guessing an unverified security ID (#78).
    """
    import copy
    today_s = _today_iso(today)
    raw_doc = (copy.deepcopy(securities) if securities is not None
               else _load_macro_securities(securities_path))
    if not isinstance(raw_doc, dict):
        raw_doc = {}

    if keys is not None:
        target_keys = [_normalize_mcx_key_and_symbol(k)[0] for k in keys]
    else:
        target_keys = [
            k for k, v in raw_doc.items()
            if not str(k).startswith("_") and isinstance(v, dict)
            and (v.get("seg") == "MCX_COMM" or k in MCX_COMMODITY_MAP)
        ]
        if master:
            for mk in MCX_FRONT_MONTH_KEYS:
                if mk not in target_keys:
                    target_keys.append(mk)

    rolled, active, unrollable = {}, {}, {}
    for macro_key in target_keys:
        _, mcx_sym = _normalize_mcx_key_and_symbol(macro_key)
        cur = raw_doc.get(macro_key)
        cur = cur if isinstance(cur, dict) else {}
        old_id = str(cur.get("id") or "")
        old_exp = str(cur.get("_expiry") or "")[:10]
        old_sym = str(cur.get("_symbol") or "")

        res = resolve_front_month(
            macro_key, master=master, securities=raw_doc,
            today=today_s, roll_on_expiry=roll_on_expiry,
        )
        if res is None:
            if old_id or macro_key in raw_doc:
                unrollable[macro_key] = (
                    f"expired on {old_exp or 'unknown'} and no unexpired "
                    "contract in master or _next ladder"
                )
            continue

        updated = dict(cur)
        updated["id"] = res["id"]
        updated["seg"] = res["seg"]
        updated["inst"] = res["inst"]
        updated["_mcx_symbol"] = mcx_sym
        updated["_auto_roll"] = True
        updated["_symbol"] = res["_symbol"]
        updated["_expiry"] = res["_expiry"]
        updated["_next"] = [
            {
                "id": str(n["id"]),
                "expiry": str(n["expiry"])[:10],
                "symbol": (str(n.get("symbol") or "").strip()
                           or _infer_mcx_display_symbol(mcx_sym, n["expiry"], str(n.get("master_symbol") or ""))),
            }
            for n in (res.get("_next") or [])
        ]

        if res["rolled"]:
            rf = res.get("rolled_from") or {"id": old_id, "expiry": old_exp, "symbol": old_sym}
            updated["_previous"] = {
                "id": str(rf.get("id") or old_id),
                "symbol": str(rf.get("symbol") or old_sym),
                "expiry": str(rf.get("expiry") or old_exp),
                "rolled_on": today_s,
            }
            rolled[macro_key] = {
                "from_id": str(rf.get("id") or old_id),
                "from_expiry": str(rf.get("expiry") or old_exp),
                "from_symbol": str(rf.get("symbol") or old_sym),
                "to_id": res["id"],
                "to_expiry": res["_expiry"],
                "to_symbol": res["_symbol"],
                "source": res["source"],
            }
        raw_doc[macro_key] = updated
        active[macro_key] = {
            "id": res["id"],
            "expiry": res["_expiry"],
            "symbol": res["_symbol"],
            "mcx_symbol": mcx_sym,
            "source": res["source"],
        }

    if write:
        out_p = Path(securities_path) if securities_path is not None else MACRO_SECURITIES_PATH
        try:
            out_p.parent.mkdir(parents=True, exist_ok=True)
            tmp_p = out_p.with_suffix(".json.tmp")
            tmp_p.write_text(json.dumps(raw_doc, indent=1) + "\n")
            tmp_p.replace(out_p)
        except OSError as exc:
            print(f"  (scrip master: macro_securities write skipped [{exc}])")

    return {
        "as_of": today_s,
        "rolled": rolled,
        "active": active,
        "unrollable": unrollable,
        "securities": raw_doc,
    }


roll_macro_securities = roll_mcx_contracts


def reconcile(id_map: dict, master: dict, wanted: list = None,
              today=None) -> dict:
    """Every mapped instrument judged, plus the wanted-list lookups."""
    rows = [check_one(t, i, master, today=today) for t, i in sorted(id_map.items())]
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
            notify_fn = lambda text: fire_broadcast({"event": "scrip_review", "text": text})  # noqa: E731
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
        ledger_path=None, wanted_path=None, securities_path=None,
        today=None, roll_commodities: bool = True) -> dict:
    """The cron seam: fetch -> index -> reconcile -> roll MCX -> card -> report.
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

    report = reconcile(id_map, master, wanted, today=today)
    if roll_commodities:
        write_sec = bool(
            write and (securities_path is not None
                       or not os.environ.get("PYTEST_CURRENT_TEST"))
        )
        report["mcx_roll"] = roll_mcx_contracts(
            master=master,
            today=today,
            securities_path=securities_path,
            write=write_sec,
        )
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


def _open_position_symbols(journal_path=None) -> set:
    """Symbols the desk currently HOLDS, tier table or not.

    Added 2026-08-07. The tier table is a screen — it churns, and a name
    that drops out of it keeps its open position. Those orphans were
    still quoted every cycle and every quote failed `no_security_id`,
    which is a real hole (an unpriced holding is an unmarked holding)
    wearing the costume of log spam: 7 names, ~9 lines a cycle. An id is
    needed for as long as we hold the thing, so the id universe is the
    union of what we screen and what we own."""
    try:
        from src import knowledge_graph_logger as kg
        return {t.replace(".NS", "") for t in
                kg.open_positions(path=journal_path) if t}
    except Exception:
        return set()


def _already_resolved_symbols(out_path=None) -> set:
    """Symbols the previous build already resolved.

    CARRY-FORWARD, added 2026-08-07, and the durable half of the fix. The
    build runs on the MAC off the Mac's tier table; the open book lives on
    the VM. So a name the VM holds but the Mac's screen has dropped was
    silently deleted from the map on the next weekly rebuild — which is
    how five held positions ended up unquotable. Carrying prior symbols
    into the lookup universe keeps them, and because every symbol is
    re-looked-up in that run's master, a delisted or renamed id still
    falls out honestly into `unresolved` rather than being carried blind."""
    import json as _json
    p = Path(out_path) if out_path else DARLING_IDS_PATH
    try:
        return set((_json.loads(p.read_text()).get("ids") or {}).keys())
    except (OSError, ValueError):
        return set()


def _darling_symbols(tiers_path=None, journal_path=None,
                     out_path=None, fo_path=None) -> list:
    """The id universe the VM desk may ever need to quote: every symbol in
    the tier table, plus every open position, plus everything a previous
    build already resolved."""
    import json as _json
    p = Path(tiers_path) if tiers_path else TIERS_PATH_FOR_IDS
    try:
        tiers = _json.loads(p.read_text()).get("tiers") or {}
    except (OSError, ValueError):
        tiers = {}
    screened = {r.get("symbol") for rows in tiers.values()
                for r in rows if r.get("symbol")}
    return sorted(screened | _open_position_symbols(journal_path)
                  | _already_resolved_symbols(out_path)
                  | _tier1_fo_symbols(fo_path))


def _tier1_fo_symbols(fo_path=None) -> set:
    """decision #101 (2026-09-19): every tier1 F&O name joins the id
    universe so the chain archiver's tier-1 extension (#100) can capture
    all of them, not only the ones that happen to be darlings. Banned
    names are left out; a missing/unreadable file contributes nothing."""
    import json as _json
    p = Path(fo_path) if fo_path else ROOT / "data" / "fo_liquidity.json"
    try:
        fo = _json.loads(p.read_text())
    except (OSError, ValueError):
        return set()
    banned = {str(b).upper() for b in fo.get("banned") or []}
    return {str(sym).upper() for sym, row in (fo.get("symbols") or {}).items()
            if isinstance(row, dict) and row.get("tier") == "tier1"
            and str(sym).upper() not in banned}


def build_darling_ids(symbols=None, fetch_fn=None, out_path=None,
                      tiers_path=None, journal_path=None,
                      commodity_symbols=None, today=None,
                      securities_path=None,
                      roll_commodities: bool = True) -> dict:
    """The VM desk's quote ids (decision #83): darlings are non-F&O names
    outside SECURITY_ID_MAP, so their ids come from Dhan's PUBLIC scrip
    master — exact name match only, EQ series preferred, anything
    ambiguous lands in `unresolved` and stays UNQUOTABLE (#78: a guessed
    id silently prices the wrong instrument). Built ON THE MAC (27MB
    fetch), shipped nightly; the VM refuses ids older than its own
    freshness gate."""
    import json as _json
    symbols = (symbols if symbols is not None
               else _darling_symbols(tiers_path, journal_path, out_path))
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
    # V1.3: the MCX commodity block rides in the SAME artifact under its own
    # key — `ids` stays NSE-equity-only, so nothing that quotes a darling by
    # `ids[sym]` can ever be handed a futures contract by mistake.
    commodities = lookup_commodities(list(commodity_symbols
                                          if commodity_symbols is not None
                                          else COMMODITY_SYMBOLS), master, today=today)
    commodity_unresolved = {s: "no live MCX FUTCOM contract in the master"
                            for s in (commodity_symbols if commodity_symbols is not None
                                      else COMMODITY_SYMBOLS) if s not in commodities}
    mcx_roll = None
    if roll_commodities:
        write_sec = bool(
            securities_path is not None
            or not os.environ.get("PYTEST_CURRENT_TEST")
        )
        mcx_roll = roll_mcx_contracts(
            master=master,
            today=today,
            securities_path=securities_path,
            write=write_sec,
        )
    out = {"built_at": _now_iso(), "count": len(ids),
           "ids": ids, "unresolved": unresolved,
           "commodities": commodities,
           "commodity_unresolved": commodity_unresolved}
    if mcx_roll is not None:
        out["mcx_roll"] = mcx_roll
    p = Path(out_path) if out_path else DARLING_IDS_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(out, indent=1))
    except OSError:
        pass
    return out


def ensure_darling_ids(max_age_days: int = 7, fetch_fn=None,
                       out_path=None, tiers_path=None,
                       journal_path=None) -> bool:
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
                                tiers_path=tiers_path,
                                journal_path=journal_path)
        print(f"  (darling ids rebuilt: {out['count']} resolved, "
              f"{len(out['unresolved'])} unresolved)")
        return True
    except Exception as exc:
        print(f"  (darling ids rebuild failed — keeping prior file: {exc})")
        return False
