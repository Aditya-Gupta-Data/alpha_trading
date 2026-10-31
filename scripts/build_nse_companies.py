# MANUAL OFFLINE TOOL — rebuilds config/nse_companies.json (decision #175) from Dhan's public scrip master. Not on any cron.
# Every NSE EQ/BE equity becomes a GraphRAG company name the shadow graph can resolve; learning only, never the trading universe.
#   python3 scripts/build_nse_companies.py [--master FILE]     (downloads the ~35 MB CSV when --master is not given)
import argparse
import csv
import json
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "nse_companies.json"
URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
# Tickers that are plain economic words: as a node id they would swallow the commodity / sector / theme of the same name.
GENERIC_TICKERS = {
    "ADVANCE", "AEROPLANE", "AFFORDABLE", "ALKALI", "ALPHA", "BETA", "CHEMICAL", "CLEAN", "CONSUMER", "CROWN", "DEEP",
    "DEFENCE", "DIVIDEND", "DOLLAR", "ENERGY", "FOCUS", "GATEWAY", "GLOBAL", "GLOBE", "GRAPHITE", "HEALTHY", "INFRA",
    "IT", "LANDMARK", "LIQUID", "MARINE", "METAL", "MOMENTUM", "OIL", "PREMIER", "PRIORITY", "PRUDENT", "RACE", "RAIN",
    "RELIABLE", "RETAIL", "ROUTE", "SIGMA", "SILVER", "SPECIALITY", "STAR", "SUPREME", "TAKE", "TECH", "TOTAL", "VALUE",
    "WEALTH",
}
_SUFFIX = re.compile(r"\s*(\(i\)|\bltd\.?|\blimited)\s*$", re.I)


def _clean(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    while _SUFFIX.search(name):
        name = _SUFFIX.sub("", name).strip()
    return name


def build(master: Path) -> dict:
    out = {}
    with open(master, encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if r["EXCH_ID"] != "NSE" or r["SEGMENT"] != "E" or r["SERIES"] not in ("EQ", "BE"):
                continue
            sym = r["UNDERLYING_SYMBOL"].strip().upper()
            names = {_clean(r["DISPLAY_NAME"]), _clean(r["SYMBOL_NAME"])} - {""}
            if not sym or sym in GENERIC_TICKERS or any("etf" in n.lower() for n in names) or sym.endswith("BEES"):
                continue
            aliases = set()
            for n in names:
                aliases |= {n, f"{n} Ltd", f"{n} Limited"}
            out[sym] = sorted({x.lower(): x for x in sorted(aliases)}.values())
    return dict(sorted(out.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master")
    a = ap.parse_args()
    if a.master:
        master = Path(a.master)
    else:
        master = Path(tempfile.mkdtemp()) / "master.csv"
        subprocess.run(["curl", "-sL", "-m", "180", "-o", str(master), URL], check=True)
    companies = build(master)
    OUT.write_text(json.dumps({
        "_comment": "GraphRAG learning list (decision #175): NSE EQ/BE equities from Dhan's public scrip master, ticker -> name "
                    "aliases. Seeded into the SHADOW graph only, in_universe=0 — never the trading universe. Rebuild with "
                    "scripts/build_nse_companies.py; generic-word tickers and ETFs are left out on purpose.",
        "built_on": date.today().isoformat(),
        "companies": companies,
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(companies)} companies to {OUT}")


if __name__ == "__main__":
    main()
