"""
src/ingestion/sandbox/credit_monitor.py — corporate credit stress, without a terminal
=====================================================================================

V2 RESEARCH SANDBOX, 2026-08-11. The thesis: **credit moves before equity.**
A rating downgrade, a debenture trustee's default notice or a sudden bond
issuance to refinance is visible days-to-weeks before the equity market
finishes repricing it.

**ON NO EXECUTION PATH.** No cron, no importer outside this package, and a
test enforces it. Capture-only, exactly as `chain_archiver` and
`cross_asset` were on day one.

HOW IT PROXIES CREDIT STRESS WITHOUT A BLOOMBERG TERMINAL
---------------------------------------------------------
We have no bond terminal and will not have one. But **the disclosure itself
is public and we already collect it**: every rating action on a listed
company must be filed with the exchange, and `ingestion/corporate_events`
has been capturing those filings since 2019 into
`data/lake/events/date=…` — 2,612 partitions of `{symbol, subject, flags,
attachment}`.

So the primary lane is **not a scraper at all: it is a re-read of a lake we
already own.** That is the difference between a design that works on Monday
and one that needs three new credentials first.

Three lanes, in order of how much they cost us:

  1. **EVENTS LAKE (free, already captured, 7 years deep).** Classify each
     filing's `subject` against the vocabulary below. This is the whole
     backbone: rating actions, default/delay notices, debenture trustee
     communications, and fresh NCD issuance all arrive here as ordinary
     corporate announcements.
  2. **RATING-AGENCY PAGES (not implemented — a decision, not an omission).**
     CRISIL/ICRA/CARE publish rating rationales on their own sites. That is
     a NEW crawler against a NEW host, and this repo's boundary doctrine
     keeps crawlers off the VM's address and out of `src/` proper. Wire it
     only if lane 1 proves the signal is worth the risk.
  3. **YIELD DATA (deliberately out of scope).** Corporate bond yields need
     a paid feed. `data/lake/macro/` already carries the sovereign/global
     series; the SPREAD that would matter needs the corporate leg we do not
     have. Stated so nobody assumes it is covered.

WHAT IT REFUSES TO DO
---------------------
It does not score, rank or infer a "credit health" number. A filing that
says `Downgrade` is a FACT; deciding it means the equity will fall is a
Department 5 judgement gated on evidence, and this module has none yet. It
classifies and counts. Nothing more.

Classification is keyword-based and therefore **coarse and honest about it**:
`_classify` returns None for anything it does not recognise, and the report
carries an `unclassified_sample` so the vocabulary can be improved from real
misses rather than guessed at.

CLI
    python3 -m src.ingestion.sandbox.credit_monitor --days 90
    python3 -m src.ingestion.sandbox.credit_monitor --from 2019-01-01 --to 2026-08-15 --json
    python3 -m src.ingestion.sandbox.credit_monitor --days 365 --out data/lake/credit_events.jsonl
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVENTS_DIR = ROOT / "data" / "lake" / "events"
IST = timezone(timedelta(hours=5, minutes=30))

# Ordered: the first pattern that matches wins, and the ordering encodes
# severity — a "default" filing that also says "rating" is a default.
CREDIT_PATTERNS = (
    ("DEFAULT", r"\bdefault\b|delay in payment|failure to pay|"
                r"non[- ]payment|payment obligation"),
    ("DOWNGRADE", r"\bdowngrad|\brating revis|revision in (credit )?rating|"
                  r"rating action"),
    ("UPGRADE", r"\bupgrad"),
    ("WATCH_NEGATIVE", r"credit watch|rating watch|negative outlook|"
                       r"outlook revised"),
    # TRUSTEE must be tested BEFORE ISSUANCE: "Debenture Trustee
    # Communication" contains the word `debenture` and would otherwise be
    # filed as routine financing. A trustee communication is the opposite —
    # the trustee speaks up when something is going wrong. Caught by a test.
    ("TRUSTEE", r"debenture trustee|trustee communication"),
    ("ISSUANCE", r"\bncd\b|non[- ]convertible debenture|bond issu|"
                 r"debenture|commercial paper|debt securities|"
                 r"fund rais.*(debt|debenture|ncd)"),
    ("PLEDGE", r"pledge|encumbrance|invocation of pledge"),
)
_COMPILED = [(tag, re.compile(pat, re.I)) for tag, pat in CREDIT_PATTERNS]

# Severity ordering for the report; DEFAULT first because it is the one that
# is never routine.
SEVERITY = ("DEFAULT", "DOWNGRADE", "WATCH_NEGATIVE", "TRUSTEE", "PLEDGE",
            "ISSUANCE", "UPGRADE")


def classify(subject: str):
    """The credit category of one filing headline, or None.

    None is a real answer, not a failure: most corporate filings are board
    meetings and record dates, and pretending otherwise would flood the
    report with noise."""
    text = str(subject or "")
    for tag, rx in _COMPILED:
        if rx.search(text):
            return tag
    return None


def _partition_days(events_dir: Path, start: str, end: str) -> list:
    if not events_dir.is_dir():
        return []
    days = [p.name[5:] for p in events_dir.iterdir() if p.name.startswith("date=")]
    return sorted(d for d in days if start <= d <= end)


def scan(start: str, end: str, events_dir=None, read_day_fn=None) -> dict:
    """Every credit-flavoured filing in the window, classified.

    Returns {events, unclassified_sample, days_scanned}. Never raises: one
    unreadable partition costs that day, not the run."""
    events_dir = Path(events_dir) if events_dir else EVENTS_DIR
    if read_day_fn is None:
        from src import lake

        def read_day_fn(day):
            return lake.read_day("events", day, root=events_dir.parent.parent)

    events, unclassified, scanned = [], [], 0
    for day in _partition_days(events_dir, start, end):
        try:
            rows = read_day_fn(day) or []
        except Exception:
            continue
        scanned += 1
        for r in rows:
            subject = str(r.get("subject") or "")
            tag = classify(subject)
            if tag:
                events.append({"date": r.get("as_of") or day,
                               "symbol": r.get("symbol"),
                               "ticker": r.get("ticker"),
                               "category": tag,
                               "subject": subject,
                               "attachment": r.get("attachment")})
            elif len(unclassified) < 25:
                unclassified.append(subject[:120])
    return {"events": events, "unclassified_sample": unclassified,
            "days_scanned": scanned}


def report(start: str, end: str, events_dir=None, read_day_fn=None,
           out_path=None) -> dict:
    """The counted view. Writes the raw rows only when `out_path` is given —
    a research tool that writes by default is a research tool that pollutes
    the lake."""
    found = scan(start, end, events_dir=events_dir, read_day_fn=read_day_fn)
    events = found["events"]
    by_cat, by_symbol = {}, {}
    for e in events:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
        by_symbol.setdefault(e["symbol"], []).append(e["category"])
    stressed = {s: c for s, c in
                ((s, [x for x in cats
                      if x in ("DEFAULT", "DOWNGRADE", "WATCH_NEGATIVE")])
                 for s, cats in by_symbol.items()) if c}
    rep = {
        "from": start, "to": end,
        "days_scanned": found["days_scanned"],
        "credit_filings": len(events),
        "by_category": {k: by_cat.get(k, 0) for k in SEVERITY if by_cat.get(k)},
        "distinct_symbols": len(by_symbol),
        "symbols_with_negative_credit_news": dict(sorted(
            stressed.items(), key=lambda kv: -len(kv[1]))[:25]),
        "unclassified_sample": found["unclassified_sample"],
        "written_to": None,
        "note": ("Classification is KEYWORD-based and coarse. It counts "
                 "filings; it does not score credit health, and nothing "
                 "downstream reads this. Rating-agency sites and corporate "
                 "bond yields are deliberately NOT sources — see the module "
                 "docstring for why."),
    }
    if out_path and events:
        p = Path(out_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                for e in events:
                    fh.write(json.dumps(dict(
                        e, captured_at=datetime.now(IST).isoformat(
                            timespec="seconds"))) + "\n")
            rep["written_to"] = str(p)
        except OSError as exc:
            rep["written_to"] = f"write failed: {exc}"
    return rep


def render_lines(rep: dict) -> list:
    lines = [f"credit monitor {rep['from']} → {rep['to']}  "
             f"({rep['days_scanned']} day partitions scanned)",
             f"  {rep['credit_filings']} credit-flavoured filing(s) across "
             f"{rep['distinct_symbols']} symbol(s)"]
    if not rep["credit_filings"]:
        lines.append("  nothing matched the credit vocabulary in this window")
        return lines
    for cat, n in rep["by_category"].items():
        lines.append(f"    {cat:<16} {n}")
    neg = rep["symbols_with_negative_credit_news"]
    if neg:
        lines.append("  negative credit news:")
        for sym, cats in list(neg.items())[:10]:
            lines.append(f"    {sym:<14} {', '.join(cats)}")
    return lines


def main(argv=None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(
        description="Credit stress from filings we already capture "
                    "(research sandbox; reads the events lake, no new feed)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--out", help="append the raw rows to this jsonl")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)

    end = a.end or date.today().isoformat()
    start = a.start or (date.fromisoformat(end)
                        - timedelta(days=a.days)).isoformat()
    rep = report(start, end, out_path=a.out)
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        for line in render_lines(rep):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
