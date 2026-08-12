"""
src/ingestion/cross_asset.py — MCX commodities + global indices (Dept 1)
=========================================================================

Level 1, 2026-08-05. Decision #2 always said MCX Commodities and Global
Indices were a PLANNED expansion the DhanHQ migration (#22) would make
free — "the same data layer, no new provider". This is that expansion's
first, deliberately small step: an EOD daily-bar tap into the lake, and
nothing else.

WHAT IT IS NOT, stated first because the boundary is the safety property:

  * It is NOT wired to any proposal, gate, sizing or exit path. Nothing
    in Dept 2/3 imports this module. It is capture-only, exactly as
    `chain_archiver` and `intraday_tracker` were on day one.
  * It does NOT touch the equity/options pipeline. It runs as its own
    cron job with its own log; the ONLY shared resource is the host-wide
    Dhan throttle in `dhan_client._throttle`, which every caller already
    goes through by construction.
  * It adds NO new provider, NO new credential, NO new HTTP client. Every
    call goes through `SafeDhanClient` — the one hardened market-data
    door (#48/#56) — so failures land in the same classified DH-9xx audit
    as everything else.

MARKET TIMINGS ARE DIFFERENT, AND THAT IS THE WHOLE PROBLEM. MCX trades
until 23:30/23:55 IST, NSE closes at 15:30, and the global indices keep
US hours; their holiday calendars share nothing. So this module refuses
to reason about "is the market open" at all — it asks for a date RANGE of
completed daily bars and takes whatever comes back. A silent day is a
holiday, an unlisted contract, or a segment the account is not entitled
to, and all three are the same honest outcome here: **a named skip, never
a fabricated bar, never a crash.** Per-instrument fail-open: one dead
symbol costs its own row.

SILENCE AND REFUSAL ARE DIFFERENT ANSWERS (2026-08-13). CA-404 means the
window came back genuinely EMPTY; **CA-401 means the door REFUSED and
said why** (a DH-9xx — expired token, entitlement, rate limit). Before
this split an expired token printed the CA-404 holiday line, so "the id
is still dead" and "MCX was shut" looked identical on the morning after
a roll — vague about exactly the thing you need sharp.

CONTRACT EXPIRY IS A KNOWN TRAP (inherited from `macro_tracker`, whose
`config/macro_securities.json` this module deliberately REUSES rather
than duplicating): MCX futures ids die with their contract. An expired id
does not error loudly, it just stops returning bars, so
`stale_instruments()` names any entry whose `_expiry` has passed and the
report says so out loud.

ROLL LOG — the ids are rolled BY HAND, on purpose. A replacement id
guessed from a naming pattern is the same bug in a new costume (ledger
Issues 14/15), so each roll is verified row-by-row against Dhan's public
scrip master and the reasoning lands in `_verified`.
  * 2026-08-05  GOLD_INDIA id 466583 (GOLD AUG FUT) expired. The tap ran
    for 8 days capturing CRUDE only.
  * 2026-08-13  rolled to 483079 (GOLD OCT FUT, exp 2026-10-05). 466583
    was by then ABSENT from the master — not repointed, gone. MCX GOLD is
    BI-MONTHLY (Feb/Apr/Jun/Aug/Oct/Dec), so there is no SEP contract and
    the 08-05→08-13 hole in the lake is real and unrecoverable.
  * NEXT DUE 2026-08-19 — CRUDE. Successor is 565899 (CRUDEOIL SEP FUT).

Writes date-partitioned lake rows (`lake.write_partition`), the same
layout every other clerk uses, so `lake.read_day("cross_asset", day)`
reads it with no new reader.

CLI:  python3 -m src.ingestion.cross_asset [--days N] [--dry-run] [--json]
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECURITIES_PATH = ROOT / "config" / "macro_securities.json"
GLOBAL_PATH = ROOT / "config" / "global_indices.json"
LEDGER_PATH = ROOT / "logs" / "cross_asset.jsonl"
DATASET = "cross_asset"

DEFAULT_LOOKBACK_DAYS = 7      # short: this runs daily and is idempotent

# The commodity legs we want, by the name they already carry in
# macro_securities.json. Keeping the SAME key names means one verified-id
# file serves both this clerk and the macro tracker — a second copy of an
# instrument id is a second thing to let rot (ledger Issues 14/15).
COMMODITY_KEYS = ("CRUDE", "GOLD_INDIA")


def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default if default is not None else {}


def load_instruments(securities_path=None, global_path=None) -> dict:
    """{name: {id, seg, inst, asset_class, _expiry?}} for everything we can
    legally price.

    Only entries carrying all of id/seg/inst are usable — the same rule
    `macro_tracker.load_securities` applies, for the same reason: a
    half-filled entry is a guess, and a guessed id silently prices the
    WRONG instrument. Underscore-prefixed keys (`_note`, `_verified`) are
    documentation, not instruments."""
    out = {}
    for name, e in (_load_json(securities_path or SECURITIES_PATH)).items():
        if name.startswith("_") or not isinstance(e, dict):
            continue
        if name in COMMODITY_KEYS and all(e.get(k) for k in ("id", "seg", "inst")):
            out[name] = {"id": str(e["id"]), "seg": str(e["seg"]),
                         "inst": str(e["inst"]), "asset_class": "commodity",
                         "_expiry": e.get("_expiry"),
                         "_symbol": e.get("_symbol")}
    for name, e in (_load_json(global_path or GLOBAL_PATH)).items():
        if name.startswith("_") or not isinstance(e, dict):
            continue
        if all(e.get(k) for k in ("id", "seg", "inst")):
            out[name] = {"id": str(e["id"]), "seg": str(e["seg"]),
                         "inst": str(e["inst"]), "asset_class": "global_index",
                         "_expiry": e.get("_expiry"),
                         "_symbol": e.get("_symbol")}
    return out


def stale_instruments(instruments: dict, today: date = None) -> list:
    """Names whose futures contract has already expired.

    An expired id is the quiet failure mode: Dhan does not shout, it just
    returns nothing, and a clerk that reports 'no bars' without saying
    'because the contract died' sends you looking in the wrong place."""
    today = today or date.today()
    dead = []
    for name, e in (instruments or {}).items():
        exp = e.get("_expiry")
        if not exp:
            continue
        try:
            if date.fromisoformat(str(exp)[:10]) < today:
                dead.append(name)
        except ValueError:
            continue
    return sorted(dead)


def _ledger(entry: dict, ledger_path=None) -> None:
    p = Path(ledger_path) if ledger_path else LEDGER_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(
                timespec="seconds"), **entry}) + "\n")
    except OSError:
        pass


class UpstreamRefused(Exception):
    """The door answered with a CLASSIFIED failure — not an empty window.

    Found 2026-08-13 while rolling GOLD_INDIA's id: with an expired local
    token, both legs reported `CA-404 no completed bars (holiday /
    unentitled / dead id)`. That reads as "the market was shut", so a dead
    TOKEN was indistinguishable from a dead CONTRACT and from a genuine
    MCX holiday — which is exactly the wrong thing to be vague about the
    day after someone rolls an id and wants to know whether it took.
    A DH-9xx is upstream telling us something; only silence is CA-404."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _default_fetch(instr: dict, start: str, end: str):
    """Daily OHLC via the ONE hardened door. Returns the payload dict, or
    None for an genuinely empty window; raises `UpstreamRefused` when the
    guard classified a failure. Never a second HTTP client (#48/#56)."""
    from src import dhan_client as dc
    from src.dhan_client import unwrap_payload
    from src.dhan_guard import SafeDhanClient
    safe = SafeDhanClient()
    client = dc._get_client()
    if client is None:
        raise UpstreamRefused("CA-401", "no Dhan client — token missing or "
                                        "unreadable, so nothing was asked")
    resp, err = safe._call("historical_daily_data",
                           client.historical_daily_data,
                           instr["id"], instr["seg"], instr["inst"],
                           start, end)
    if err is not None:
        raise UpstreamRefused("CA-401", str(err)[:200])
    payload = unwrap_payload(resp, inner_marker="timestamp")
    return payload if isinstance(payload, dict) else None


def bars_from_payload(payload: dict, name: str, asset_class: str) -> list:
    """Dhan's parallel arrays -> one row per COMPLETED daily bar.

    NULL-honest and length-honest: a row is emitted only when every one of
    open/high/low/close is present and numeric for that index. A partial
    bar is dropped, never zero-filled — a fabricated OHLC in a lake that
    later feeds a feature layer is worse than a hole."""
    if not isinstance(payload, dict):
        return []
    stamps = payload.get("timestamp") or []
    o, h, l, c = (payload.get(k) or [] for k in ("open", "high", "low", "close"))
    vol = payload.get("volume") or []
    n = min(len(stamps), len(o), len(h), len(l), len(c))
    rows = []
    for i in range(n):
        try:
            day = datetime.fromtimestamp(int(stamps[i])).date().isoformat()
            row = {"day": day, "name": name, "asset_class": asset_class,
                   "open": float(o[i]), "high": float(h[i]),
                   "low": float(l[i]), "close": float(c[i])}
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        try:
            row["volume"] = float(vol[i]) if i < len(vol) else None
        except (TypeError, ValueError):
            row["volume"] = None
        rows.append(row)
    return rows


def run(days: int = DEFAULT_LOOKBACK_DAYS, today: date = None,
        fetch_fn=None, instruments: dict = None, lake_root=None,
        ledger_path=None, dry_run: bool = False,
        securities_path=None, global_path=None) -> dict:
    """One EOD pass. Returns an honest report; never raises.

    Idempotent by the lake's own partition semantics: re-running the same
    day rewrites that day's partition with the same rows."""
    from src import lake
    today = today or date.today()
    instruments = (instruments if instruments is not None
                   else load_instruments(securities_path, global_path))
    fetch_fn = fetch_fn or _default_fetch
    start = (today - timedelta(days=max(1, days))).isoformat()
    end = today.isoformat()

    expired = stale_instruments(instruments, today)
    ok, skipped, by_day = [], [], {}

    for name, instr in sorted(instruments.items()):
        if name in expired:
            skipped.append({"name": name, "code": "CA-410",
                            "detail": f"contract expired {instr.get('_expiry')}"
                                      " — re-verify a fresh id from the scrip"
                                      " master"})
            continue
        try:
            payload = fetch_fn(instr, start, end)
        except UpstreamRefused as exc:              # upstream SAID something
            skipped.append({"name": name, "code": exc.code,
                            "detail": exc.detail})
            continue
        except Exception as exc:                    # per-instrument fail-open
            skipped.append({"name": name, "code": "CA-500",
                            "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})
            continue
        rows = bars_from_payload(payload, name, instr["asset_class"])
        if not rows:
            # A holiday, an unlisted contract, or a segment this account is
            # not entitled to. All three are the same honest outcome and
            # NONE of them is an error worth waking anyone for.
            skipped.append({"name": name, "code": "CA-404",
                            "detail": "no completed bars in window "
                                      "(holiday / unentitled / dead id)"})
            continue
        ok.append({"name": name, "asset_class": instr["asset_class"],
                   "bars": len(rows), "last": rows[-1]["day"]})
        for r in rows:
            by_day.setdefault(r["day"], []).append(r)

    written = 0
    if by_day and not dry_run:
        for day, rows in sorted(by_day.items()):
            try:
                lake.write_partition(DATASET, day, rows, root=lake_root)
                written += len(rows)
            except Exception as exc:
                skipped.append({"name": f"lake:{day}", "code": "CA-500",
                                "detail": str(exc)[:160]})
    for s in skipped:
        _ledger(s, ledger_path)

    return {"instruments": len(instruments), "ok": ok, "skipped": skipped,
            "expired": expired, "days_written": len(by_day),
            "rows_written": written, "dry_run": bool(dry_run)}


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="MCX + global-index EOD tap")
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    rep = run(days=args.days, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        for r in rep["ok"]:
            print(f"  ✅ {r['name']:<12} {r['asset_class']:<13} "
                  f"{r['bars']:>3} bars, last {r['last']}")
        for s in rep["skipped"]:
            print(f"  ⚠️  {s['name']:<12} {s['code']} {s['detail']}")
        print(f"cross-asset: {len(rep['ok'])}/{rep['instruments']} instruments, "
              f"{rep['rows_written']} row(s) across {rep['days_written']} day(s)"
              + (" [dry-run]" if rep["dry_run"] else ""))
    # Zero instruments configured is a SETUP problem worth a non-zero exit;
    # zero bars on a holiday is not.
    return 0 if rep["instruments"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
