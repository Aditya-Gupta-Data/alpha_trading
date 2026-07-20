"""
src/ingestion/fo_bhavcopy.py — F&O daily bundle intake (Dept 1 clerk)
=====================================================================

The F&O tranche's grain supply, DROP-FOLDER FIRST (the flows_backfill
precedent): the owner downloads NSE's "Reports-Daily-Multiple.zip" (the
daily F&O reports bundle) and this clerk ingests it — no scraping
needed, the exchange hands the file over the counter. First real bundle
delivered by the owner 2026-07-20 (36 files, 17-Jul session + 20-Jul
pre-open).

What it keeps per trading day (data/lake/fo_bhavcopy/YYYY-MM-DD/):
  fo.csv      the F&O bhavcopy: INSTRUMENT x SYMBOL x EXPIRY rows with
              OPEN_INT, traded value/qty/contracts (FUTIDX/FUTSTK/
              OPTIDX/OPTSTK)
  secban.csv  the day's F&O security BAN list (MWPL breaches — the
              exchange's own "too crowded" flag)
  fovolt.csv  per-underlying daily/annualised volatility

`liquidity_snapshot()` then answers the liquidity_filter's question:
per underlying, total stock-OPTIONS traded value + OI, futures OI, the
ban flag, and a documented TIER:
  tier1  top LIQ_TIER1_N underlyings by stock-options traded value,
         not banned — the ONLY tier the equity-options path may touch
  tier2  next LIQ_TIER2_N — watch, never trade v1
  illiquid / banned — everything else
Output: data/fo_liquidity.json (read by equity_entry_checks).

NULL-honest: NSE pads numbers with zeros ('000000002006910') and mixes
' -' — parsed or None, never guessed. MAC-ONLY like every clerk.

CLI:  python3 -m src.ingestion.fo_bhavcopy --ingest ~/Downloads/Reports-Daily-Multiple.zip
      python3 -m src.ingestion.fo_bhavcopy --snapshot
"""
import csv
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FO_LAKE = ROOT / "data" / "lake" / "fo_bhavcopy"
LIQUIDITY_PATH = ROOT / "data" / "fo_liquidity.json"

IST = timezone(timedelta(hours=5, minutes=30))
LIQ_TIER1_N = 25
LIQ_TIER2_N = 35


def _num(v):
    s = str(v or "").strip().lstrip("0") or ("0" if str(v).strip("0 ") == ""
                                             else "")
    s = str(v or "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _day_from_name(name: str):
    """'fo17072026.csv' / 'fo_secban_20072026.csv' -> date."""
    m = re.search(r"(\d{2})(\d{2})(\d{4})", name)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return datetime(int(y), int(mo), int(d)).date()
    except ValueError:
        return None


def ingest_bundle(zip_path, lake_dir=None) -> dict:
    """NSE Reports-Daily-Multiple.zip -> per-day lake folders. Never
    raises; returns what landed. Nested foDDMMYYYY.zip is opened for the
    real bhavcopy csv inside."""
    lake = Path(lake_dir) if lake_dir else FO_LAKE
    landed = {}
    try:
        outer = zipfile.ZipFile(zip_path)
    except Exception as e:
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"}

    def _save(day, kind, data: bytes):
        d = lake / day.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{kind}.csv").write_bytes(data)
        landed.setdefault(day.isoformat(), []).append(kind)

    for name in outer.namelist():
        base = name.rsplit("/", 1)[-1]
        low = base.lower()
        try:
            if re.fullmatch(r"fo\d{8}\.zip", low):
                inner = zipfile.ZipFile(io.BytesIO(outer.read(name)))
                for iname in inner.namelist():
                    if re.fullmatch(r"fo\d{8}\.csv", iname.lower()):
                        day = _day_from_name(iname)
                        if day:
                            _save(day, "fo", inner.read(iname))
            elif low.startswith("fo_secban"):
                day = _day_from_name(low)
                if day:
                    _save(day, "secban", outer.read(name))
            elif low.startswith("fovolt"):
                day = _day_from_name(low)
                if day:
                    _save(day, "fovolt", outer.read(name))
        except Exception:
            continue                      # one bad member never kills intake
    return {"status": "ok", "landed": landed}


def parse_fo_csv(text: str) -> dict:
    """fo bhavcopy csv -> per-symbol aggregates:
    {SYM: {opt_oi, opt_val, fut_oi, fut_val}} (stock F&O only)."""
    out = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        r = {(k or "").strip().rstrip("*"): (v or "").strip()
             for k, v in row.items() if k}
        inst, sym = r.get("INSTRUMENT", ""), r.get("SYMBOL", "").upper()
        if not sym or inst not in ("OPTSTK", "FUTSTK"):
            continue
        oi, val = _num(r.get("OPEN_INT")), _num(r.get("TRD_VAL"))
        slot = out.setdefault(sym, {"opt_oi": 0.0, "opt_val": 0.0,
                                    "fut_oi": 0.0, "fut_val": 0.0})
        key = "opt" if inst == "OPTSTK" else "fut"
        if oi is not None:
            slot[f"{key}_oi"] += oi
        if val is not None:
            slot[f"{key}_val"] += val
    return out


def parse_secban(text: str) -> list:
    """Ban csv -> [SYMBOL, ...]. Format: 'sr,SYMBOL' rows after a header
    line; NULL-honest on shape surprises."""
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        cand = parts[-1] if parts else ""
        if cand and cand.upper() == cand and cand.isalnum() \
                and not cand.isdigit() and "SECURIT" not in cand:
            out.append(cand)
    return out


def liquidity_snapshot(lake_dir=None, out_path=None,
                       write: bool = True) -> dict:
    """Newest lake day -> the liquidity tiers file the halt stack reads."""
    lake = Path(lake_dir) if lake_dir else FO_LAKE
    days = sorted([p for p in lake.iterdir() if p.is_dir()]) \
        if lake.is_dir() else []
    # newest day that actually HAS a bhavcopy — pre-open days carry only
    # the ban list (the 20-Jul-Monday lesson: secban lands before fo.csv)
    fo_days = [d for d in days if (d / "fo.csv").exists()]
    if not fo_days:
        return {"status": "no_data"}
    day = fo_days[-1]
    fo_file = day / "fo.csv"
    per_symbol = parse_fo_csv(fo_file.read_text(errors="replace"))
    banned = []
    for d in reversed(days):              # newest ban list anywhere in lake
        sb = d / "secban.csv"
        if sb.exists():
            banned = parse_secban(sb.read_text(errors="replace"))
            break
    ranked = sorted(per_symbol,
                    key=lambda s: per_symbol[s]["opt_val"], reverse=True)
    symbols = {}
    for i, sym in enumerate(ranked, 1):
        if sym in banned:
            tier = "banned"
        elif i <= LIQ_TIER1_N:
            tier = "tier1"
        elif i <= LIQ_TIER1_N + LIQ_TIER2_N:
            tier = "tier2"
        else:
            tier = "illiquid"
        symbols[sym] = {**{k: round(v, 2)
                           for k, v in per_symbol[sym].items()},
                        "rank": i, "tier": tier}
    snap = {"as_of": day.name, "generated_at":
            datetime.now(IST).replace(tzinfo=None)
                             .isoformat(timespec="seconds"),
            "tier_rule": f"tier1 = top {LIQ_TIER1_N} by stock-options "
                         "traded value, not banned; only tier1 is "
                         "option-tradeable",
            "banned": banned, "symbols": symbols}
    if write:
        path = Path(out_path) if out_path else LIQUIDITY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap, indent=1))
    return snap


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest", type=str, default=None)
    ap.add_argument("--snapshot", action="store_true")
    args = ap.parse_args()
    if args.ingest:
        print(json.dumps(ingest_bundle(args.ingest), indent=1))
    if args.snapshot or not args.ingest:
        s = liquidity_snapshot()
        if s.get("status") == "no_data":
            print("no fo lake data yet — --ingest a bundle first")
        else:
            t1 = [x for x, v in s["symbols"].items() if v["tier"] == "tier1"]
            print(f"as_of {s['as_of']} | {len(s['symbols'])} F&O stocks | "
                  f"banned {s['banned']}")
            print("tier1 (option-tradeable):", " ".join(t1))
