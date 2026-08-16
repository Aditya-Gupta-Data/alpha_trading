"""
src/ingestion/sandbox/macro_shocks_v2.py — El Niño and the election calendar
============================================================================

V2 RESEARCH SANDBOX, 2026-08-16. Two shock CLOCKS that the geo-exposure map
already declares sensitivity to (`shock_sensitivity: monsoon | election`),
turned into dated events the study can measure.

**ON NO EXECUTION PATH.** No cron, no live importer, a test enforces both.
Named `macro_shocks_v2` because `src/macro_shocks.py` already exists on the
V1 side (the War Playbook / crisis_playbook) — same word, different thing,
and one import of the wrong one would be a silent mess.

LANE 1 — EL NIÑO (ONI). NOAA/CPC publishes the Oceanic Niño Index free at
`www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt`: 3-month running SST
anomalies for the Niño-3.4 region, **1950 → present**, no auth, no key.
The convention this module uses is NOAA's own:

    ONI >= +0.5  El Niño       ONI <= -0.5  La Niña

and the monsoon-relevant read is the **JJA/JAS seasons**, because that is
the Indian southwest monsoon. An El Niño summer is the one that
historically coincides with deficient rainfall — that is the mechanism the
hypothesis rests on, and it is a CORRELATION in the climate record, not a
law.

LANE 2 — STATE ELECTIONS. `config/india_election_calendar.json` ships
**EMPTY BY DESIGN**, exactly like `config/global_indices.json`. Election
dates must come from the Election Commission of India, and this repo's
standing rule is that an unverified entry is worse than an absent one:
writing dates from memory would put fabricated timestamps into a research
record, which RULE 3 forbids outright. The loader, the schema and the study
path are built and tested; they return an honest zero until someone fills
the file from ECI.

WHAT THIS MODULE DOES NOT CLAIM
-------------------------------
It converts a climate index into dated events. It does NOT assert that
El Niño moves stocks — that is precisely what `event_study_simulator`
is for, and the first run of it is reported in the module docstring of
nothing, because the answer belongs in the ledger, not in a docstring
that will age.

CLI
    python3 -m src.ingestion.sandbox.macro_shocks_v2 --fetch-oni
    python3 -m src.ingestion.sandbox.macro_shocks_v2 --el-nino-seasons
    python3 -m src.ingestion.sandbox.macro_shocks_v2 --elections
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ONI_PATH = ROOT / "data" / "lake" / "macro" / "ONI.csv"
ELECTION_PATH = ROOT / "config" / "india_election_calendar.json"
GEO_PATH = ROOT / "config" / "geo_revenue_exposure.json"

ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
IST = timezone(timedelta(hours=5, minutes=30))

# NOAA's own thresholds. Not ours to tune — using a different cutoff and
# still calling the result "El Niño" would be quietly redefining the term.
EL_NINO_THRESHOLD = 0.5
LA_NINA_THRESHOLD = -0.5

# The Indian southwest monsoon is June-September. NOAA's seasons are
# 3-month running means labelled by initials, so JJA and JAS are the two
# that sit inside it.
MONSOON_SEASONS = ("JJA", "JAS")

# Month each NOAA season is centred on — used to date the event.
SEASON_CENTRE_MONTH = {
    "DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6,
    "JJA": 7, "JAS": 8, "ASO": 9, "SON": 10, "OND": 11, "NDJ": 12,
}


def fetch_oni(fetch_fn=None) -> str:
    """The raw ONI table as text. Injectable for offline tests."""
    if fetch_fn is not None:
        return fetch_fn(ONI_URL)
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(ONI_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_oni(text: str) -> list:
    """`SEAS YR TOTAL ANOM` rows -> [{season, year, sst, anom}].

    NULL-honest: a malformed row is skipped, not defaulted to zero. An ONI
    of 0.0 is a real neutral reading and must never be manufactured."""
    out = []
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[0] == "SEAS":
            continue
        season, year, total, anom = parts
        if season not in SEASON_CENTRE_MONTH:
            continue
        try:
            out.append({"season": season, "year": int(year),
                        "sst": float(total), "anom": float(anom)})
        except ValueError:
            continue
    return out


def save_oni(rows: list, path=None) -> Path:
    """Into the macro lake as a plain CSV, matching the other series there."""
    p = Path(path) if path else ONI_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,season,year,sst,anom"]
    for r in rows:
        month = SEASON_CENTRE_MONTH[r["season"]]
        lines.append(f"{r['year']}-{month:02d}-01,{r['season']},{r['year']},"
                     f"{r['sst']},{r['anom']}")
    p.write_text("\n".join(lines) + "\n")
    return p


def el_nino_seasons(rows: list, monsoon_only: bool = True,
                    threshold: float = EL_NINO_THRESHOLD) -> list:
    """Seasons at or above the El Niño threshold.

    `monsoon_only` keeps JJA/JAS, i.e. the seasons that actually overlap the
    Indian southwest monsoon. Anything else would be measuring a Pacific
    anomaly against an Indian crop it never touched."""
    out = []
    for r in rows:
        if monsoon_only and r["season"] not in MONSOON_SEASONS:
            continue
        if r["anom"] >= threshold:
            month = SEASON_CENTRE_MONTH[r["season"]]
            out.append({"date": f"{r['year']}-{month:02d}-01",
                        "season": r["season"], "year": r["year"],
                        "anom": r["anom"],
                        "strength": ("very_strong" if r["anom"] >= 2.0 else
                                     "strong" if r["anom"] >= 1.5 else
                                     "moderate" if r["anom"] >= 1.0 else
                                     "weak")})
    return out


def load_elections(path=None) -> list:
    """Historical state elections, or []. Ships EMPTY BY DESIGN.

    ECI is the only acceptable source. Writing dates from memory would put
    fabricated timestamps into a research record — RULE 3 forbids it, and a
    study built on invented dates is worse than no study."""
    try:
        raw = json.loads(Path(path or ELECTION_PATH).read_text())
    except (OSError, ValueError):
        return []
    return [e for e in (raw.get("elections") or [])
            if e.get("date") and e.get("state")]


def tickers_for_shock(shock: str, geo_path=None) -> list:
    """Every ticker whose geo map declares sensitivity to this shock —
    the join key the exposure schema was built around."""
    try:
        raw = json.loads(Path(geo_path or GEO_PATH).read_text())
    except (OSError, ValueError):
        return []
    return sorted(t for t, body in (raw.get("exposures") or {}).items()
                  if shock in (body.get("shock_sensitivity") or []))


def tickers_for_region(region_substr: str, geo_path=None) -> list:
    """Tickers exposed to a named state/country (substring, case-blind)."""
    try:
        raw = json.loads(Path(geo_path or GEO_PATH).read_text())
    except (OSError, ValueError):
        return []
    want = str(region_substr).lower()
    hits = []
    for t, body in (raw.get("exposures") or {}).items():
        for e in body.get("exposures") or []:
            if want in str(e.get("region", "")).lower():
                hits.append(t)
                break
    return sorted(hits)


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="El Niño + election shock clocks "
                                             "(research sandbox)")
    ap.add_argument("--fetch-oni", action="store_true")
    ap.add_argument("--el-nino-seasons", action="store_true")
    ap.add_argument("--elections", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if a.fetch_oni:
        rows = parse_oni(fetch_oni())
        p = save_oni(rows)
        print(f"ONI: {len(rows)} seasonal rows "
              f"({rows[0]['year']}→{rows[-1]['year']}) -> {p}")
    if a.el_nino_seasons:
        rows = parse_oni(ONI_PATH.read_text()) if ONI_PATH.exists() else []
        if not rows and ONI_PATH.exists():
            import csv
            rows = [{"season": r["season"], "year": int(r["year"]),
                     "sst": float(r["sst"]), "anom": float(r["anom"])}
                    for r in csv.DictReader(ONI_PATH.open())]
        hits = el_nino_seasons(rows)
        print(f"El Niño monsoon seasons (ONI >= {EL_NINO_THRESHOLD}): {len(hits)}")
        for h in hits[-12:]:
            print(f"  {h['date']}  {h['season']}  anom {h['anom']:+.2f}  "
                  f"{h['strength']}")
    if a.elections:
        els = load_elections()
        print(f"election calendar: {len(els)} dated entries"
              + ("" if els else " — EMPTY BY DESIGN, source from ECI"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
