"""
src/ingestion/sandbox/election_calendar.py — election dates, sourced not typed
==============================================================================

V2 RESEARCH SANDBOX, 2026-08-16. `config/india_election_calendar.json`
shipped empty because writing dates from memory would put fabricated
timestamps into a research record. This fills it from a **citable source**
instead: the Wikipedia API, one page per election, with the page title and
revision recorded on every row so any date can be traced back.

**ON NO EXECUTION PATH.** No cron, no live importer, a test enforces it.
Mac-lane by the same rule as every other crawler — it is a new host, and
the boundary doctrine keeps those off the VM's address.

WHY WIKIPEDIA AND NOT ECI DIRECTLY
-----------------------------------
ECI is the authority, but it publishes schedules as press-note PDFs
scattered across years with no stable machine-readable index. Wikipedia
keeps one page per election with a structured `election_date` infobox
field, reachable through a documented API, and — this is the part that
matters — **every row this writes carries `source_page` so it is
checkable.** It is a SECONDARY source and the file says so; a date that
matters should be confirmed against the ECI notification before anything
is concluded from it.

WHAT IT EXTRACTS, AND THE ONE JUDGEMENT IT MAKES
-------------------------------------------------
Indian assembly elections are usually MULTI-PHASE: "27 March – 29 April
2021" is one election polled over five weeks. This records the **LAST poll
date**, because that is when the campaign ends and the count becomes
imminent — the market event is the resolution, not the first phase.

`result_date` is left **null** unless the page states it. Counting day is
conventionally a few days after the final phase, but "conventionally" is
not a date, and inferring one would be exactly the fabrication this module
exists to avoid.

CLI
    python3 -m src.ingestion.sandbox.election_calendar --years 2019-2026
    python3 -m src.ingestion.sandbox.election_calendar --years 2019-2026 --apply
"""
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CALENDAR_PATH = ROOT / "config" / "india_election_calendar.json"

API = "https://en.wikipedia.org/w/api.php"
UA = "alpha-trading-research/1.0 (private research; no redistribution)"
THROTTLE_SECONDS = 0.6      # Wikipedia's API is generous; be polite anyway

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# "29 April 2021", "1 November 2023" — day-month-year, the Indian English
# convention every one of these pages uses.
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", re.I)
# Bare "27 March" inside a range that ends "... – 29 April 2021": the year
# is carried by the LAST token, so a leading fragment is resolved against it.
_DAYMONTH_RE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.I)


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _api(params, fetch_fn=None, attempts: int = 4):
    """One API call, with backoff on 429.

    The first run of this module threw HTTP 429 on 5 of 8 years because the
    throttle only covered PAGE fetches and left the category calls
    unthrottled — the fast path was the one that got rate-limited. Every
    call now goes through here, paced and retried."""
    if fetch_fn is not None:
        return fetch_fn(params)
    url = API + "?" + urllib.parse.urlencode(dict(params, format="json"))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for i in range(attempts):
        try:
            time.sleep(THROTTLE_SECONDS * (1 + i * 3))
            with urllib.request.urlopen(req, timeout=45, context=_ctx()) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code != 429:
                raise
        except Exception as exc:
            last = exc
    raise last


def election_pages(year: int, fetch_fn=None) -> list:
    """Every assembly-election page Wikipedia files under that year."""
    try:
        r = _api({"action": "query", "list": "categorymembers",
                  "cmtitle": f"Category:{year} State Assembly elections in India",
                  "cmlimit": "100"}, fetch_fn)
    except Exception as exc:
        print(f"  (election_calendar: {year} category failed [{exc}])")
        return []
    # BY-ELECTIONS EXCLUDED. A by-election fills a handful of seats and
    # does not change a state government; treating it as the same event
    # class as a full assembly poll would pad n with non-events, which is
    # the exact way a clustered study fools itself.
    return [m["title"] for m in
            r.get("query", {}).get("categorymembers", [])
            if not m["title"].startswith("Category:")
            and "by-election" not in m["title"].lower()]


def page_wikitext(title: str, fetch_fn=None) -> str:
    try:
        r = _api({"action": "query", "prop": "revisions", "rvprop": "content",
                  "rvslots": "main", "titles": title}, fetch_fn)
        page = list(r["query"]["pages"].values())[0]
        return page["revisions"][0]["slots"]["main"]["*"]
    except Exception:
        return ""


def last_poll_date(election_date_field: str):
    """The LAST poll date in a multi-phase election string, ISO, or None.

    "27 March – 29 April 2021" → 2021-04-29. The final phase is the market
    event: that is when campaigning ends and the count becomes imminent."""
    text = str(election_date_field or "")
    full = [(int(d), MONTHS[m.lower()], int(y))
            for d, m, y in _DATE_RE.findall(text)]
    if not full:
        return None
    # A leading "27 March" with no year belongs to the year of the last
    # fully-qualified date; resolve those so a range's true end wins.
    last_year = full[-1][2]
    candidates = list(full)
    for d, m in _DAYMONTH_RE.findall(text):
        candidates.append((int(d), MONTHS[m.lower()], last_year))
    try:
        best = max(date(y, m, d) for d, m, y in candidates)
    except ValueError:
        return None
    return best.isoformat()


def parse_election(title: str, wikitext: str) -> dict | None:
    """One page → one calendar row, or None when no date is parseable."""
    m = re.search(r"\|\s*election_date\s*=\s*([^\n]{0,300})", wikitext)
    if not m:
        return None
    poll = last_poll_date(m.group(1))
    if not poll:
        return None
    state = re.sub(r"^\d{4}\s+", "", title)
    state = re.sub(r"\s+Legislative Assembly election.*$", "", state).strip()
    res = re.search(r"\|\s*(?:results_date|counting_date)\s*=\s*([^\n]{0,120})",
                    wikitext)
    return {
        "date": poll,
        "result_date": last_poll_date(res.group(1)) if res else None,
        "state": state,
        "kind": "state_assembly",
        "source": f"Wikipedia: {title}",
        "source_page": title,
        "verified_against_eci": False,
    }


def collect(years, fetch_fn=None, sleep_fn=time.sleep) -> list:
    rows, seen = [], set()
    for y in years:
        for title in election_pages(y, fetch_fn):
            if title in seen:
                continue
            seen.add(title)
            row = parse_election(title, page_wikitext(title, fetch_fn))
            if row:
                rows.append(row)
    return sorted(rows, key=lambda r: r["date"])


def apply_rows(rows: list, path=None) -> dict:
    p = Path(path or CALENDAR_PATH)
    raw = json.loads(p.read_text())
    raw["elections"] = rows
    raw["_sourced"] = {
        "run_on": date.today().isoformat(),
        "count": len(rows),
        "source": "Wikipedia API (en.wikipedia.org/w/api.php), one page per "
                  "election; `source_page` on every row",
        "caveat": ("SECONDARY SOURCE. Wikipedia is machine-readable where "
                   "ECI is scattered press-note PDFs, but it is not the "
                   "authority. `verified_against_eci` is False on every row "
                   "until someone checks it. `date` is the LAST poll date "
                   "of a multi-phase election; `result_date` is null unless "
                   "the page stated one — counting day is conventionally a "
                   "few days later, and 'conventionally' is not a date."),
    }
    p.write_text(json.dumps(raw, indent=1) + "\n")
    return {"written": len(rows), "path": str(p)}


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Source Indian assembly-election "
                                             "dates from the Wikipedia API")
    ap.add_argument("--years", default="2019-2026")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    lo, _, hi = a.years.partition("-")
    rows = collect(range(int(lo), int(hi or lo) + 1))
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"  {r['date']}  {r['state']:<28} ({r['source_page']})")
        print(f"{len(rows)} election(s) sourced")
    if a.apply:
        print(json.dumps(apply_rows(rows), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
