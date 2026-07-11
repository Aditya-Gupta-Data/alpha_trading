"""
src/daily_context.py — the Market Frame: one row per trading day
================================================================

Phase 2 of docs/HOLY_GRAIL_PLAN.md (§5.2). Cross-layer motifs ("entity
distribution + macro headwind + FII selling") are unminable while the
layers live in disjoint artifacts with no common time axis. This is the
cheapest unification: one denormalized `daily_context` row per day in
brain_map.db, NULL-honest everywhere (#50's rule — an absent reading is
recorded as NULL, never a guessed zero).

Columns are the frequently-queried aggregates; `payload` carries the full
JSON frame. Sources (ALL local reads, fail-open, as-of honest — a source
whose own as_of doesn't match the frame's day contributes NULLs):

  vix + band       the day's chain-archiver lake rows (captured at close)
  macro_*          the day's macro_daily lake snapshot (or injected matrix)
  news_*           the day's news_daily lake snapshot / live JSON
  deals_*          the day's deals_census lake row
  fii/dii nets     the day's flows lake row / live JSON
  affinity_*       data/entity_affinity.json group biases

Runs as no-LLM Sleep-Phase Task G (20:00 IST — after every EOD capture
job has landed its artifact), and `fold_lake(conn)` backfills frames for
every day the lake already holds. Event-explosion into brain_map `events`
rows is deliberately deferred to the miner phase (its link semantics get
documented in DATA_CONTRACT before any miner consumes them).

Manual:  python3 -m src.daily_context           (today's frame)
"""

import json
from datetime import date
from pathlib import Path

from src import lake

ROOT = Path(__file__).resolve().parent.parent


def ensure_schema(conn) -> None:
    """Additive daily_context table (#25 discipline). Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_context (
            date TEXT PRIMARY KEY,
            vix REAL, vix_band TEXT,
            macro_source TEXT,
            macro_nifty_short REAL, macro_nifty_medium REAL,
            macro_bank_short REAL, macro_bank_medium REAL,
            news_net INTEGER, news_fresh INTEGER,
            deals_rows INTEGER, deals_buy_legs INTEGER,
            deals_sell_legs INTEGER,
            affinity_distribution INTEGER, affinity_accumulation INTEGER,
            fii_net REAL, dii_net REAL,
            payload TEXT NOT NULL
        )
    """)
    conn.commit()


def _vix_band(vix):
    if vix is None:
        return None
    try:
        from src.regime import vix_band
        return vix_band(float(vix))
    except Exception:
        return None


def build_frame(day: str, macro: dict = None, news: dict = None,
                deals_census: dict = None, flows: dict = None,
                affinity: dict = None, vix=None) -> dict:
    """Pure assembly of one day's frame from already-loaded artifacts.
    Anything absent lands as None — never a fabricated neutral."""
    frame = {"date": day, "vix": vix, "vix_band": _vix_band(vix),
             "macro_source": None,
             "macro_nifty_short": None, "macro_nifty_medium": None,
             "macro_bank_short": None, "macro_bank_medium": None,
             "news_net": None, "news_fresh": None,
             "deals_rows": None, "deals_buy_legs": None,
             "deals_sell_legs": None,
             "affinity_distribution": None, "affinity_accumulation": None,
             "fii_net": None, "dii_net": None}

    if isinstance(macro, dict) and macro.get("source") not in (None, "none"):
        impact = macro.get("index_impact") or {}
        nifty, bank = impact.get("NIFTY 50") or {}, impact.get("NIFTY BANK") or {}
        frame.update(macro_source=macro.get("source"),
                     macro_nifty_short=nifty.get("SHORT"),
                     macro_nifty_medium=nifty.get("MEDIUM"),
                     macro_bank_short=bank.get("SHORT"),
                     macro_bank_medium=bank.get("MEDIUM"))

    if isinstance(news, dict) and isinstance(news.get("tickers"), dict):
        fresh = [t for t in news["tickers"].values()
                 if isinstance(t, dict) and not t.get("stale", True)
                 and t.get("sentiment_score") is not None]
        if fresh:
            frame["news_net"] = sum(t["sentiment_score"] for t in fresh)
            frame["news_fresh"] = len(fresh)

    if isinstance(deals_census, dict) and deals_census.get("as_of") == day:
        frame.update(deals_rows=deals_census.get("normalized"),
                     deals_buy_legs=deals_census.get("buy_legs"),
                     deals_sell_legs=deals_census.get("sell_legs"))

    if isinstance(affinity, dict):
        groups = affinity.get("groups") or {}
        biases = [g.get("net_bias") for g in groups.values()
                  if isinstance(g, dict)]
        if biases:
            frame["affinity_distribution"] = biases.count("distribution")
            frame["affinity_accumulation"] = biases.count("accumulation")

    if isinstance(flows, dict) and flows.get("as_of") == day:
        frame["fii_net"] = (flows.get("fii") or {}).get("net")
        frame["dii_net"] = (flows.get("dii") or {}).get("net")

    return frame


def record_frame(conn, frame: dict) -> bool:
    """Upsert one day's frame (latest write wins — a sleep-phase re-run
    with fuller artifacts refreshes the row). Never raises."""
    if not isinstance(frame, dict) or not frame.get("date"):
        return False
    try:
        ensure_schema(conn)
        cols = ("date", "vix", "vix_band", "macro_source",
                "macro_nifty_short", "macro_nifty_medium",
                "macro_bank_short", "macro_bank_medium",
                "news_net", "news_fresh", "deals_rows", "deals_buy_legs",
                "deals_sell_legs", "affinity_distribution",
                "affinity_accumulation", "fii_net", "dii_net")
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
        conn.execute(
            f"INSERT INTO daily_context ({', '.join(cols)}, payload) "
            f"VALUES ({', '.join('?' * len(cols))}, ?) "
            f"ON CONFLICT (date) DO UPDATE SET {sets}, "
            "payload = excluded.payload",
            tuple(frame.get(c) for c in cols) + (json.dumps(frame),))
        conn.commit()
        return True
    except Exception as exc:
        print(f"  (daily context: record failed for "
              f"{frame.get('date')} [{exc}])")
        return False


def _day_sources_from_lake(day: str, lake_root=None) -> dict:
    """Everything the lake holds for one day, NULL where absent."""
    def first(dataset):
        rows = lake.read_day(dataset, day, root=lake_root)
        return rows[0] if rows else None
    vix = None
    for slug in ("nifty", "banknifty"):
        rows = lake.read_day(f"chains/{slug}", day, root=lake_root)
        if rows and rows[0].get("vix") is not None:
            vix = rows[0]["vix"]
            break
    return {"macro": first("macro_daily"), "news": first("news_daily"),
            "deals_census": first("deals_census"), "flows": first("flows"),
            "vix": vix}


def fold_lake(conn, lake_root=None, affinity: dict = None) -> int:
    """Backfill/refresh frames for every day ANY lake dataset holds.
    Idempotent (upsert). Returns frames written. The affinity read-model
    is current-state only (its history lives in the graph, not per-day
    snapshots), so it only enriches TODAY's frame — historical frames get
    NULL affinity columns, honestly."""
    days = set()
    for ds in ("macro_daily", "news_daily", "deals_census", "flows",
               "chains/nifty", "chains/banknifty"):
        days.update(lake.list_days(ds, root=lake_root))
    today_iso = date.today().isoformat()
    written = 0
    for day in sorted(days):
        src = _day_sources_from_lake(day, lake_root)
        frame = build_frame(day, macro=src["macro"], news=src["news"],
                            deals_census=src["deals_census"],
                            flows=src["flows"], vix=src["vix"],
                            affinity=affinity if day == today_iso else None)
        if record_frame(conn, frame):
            written += 1
    return written


def run_for_today(conn, today: date = None, lake_root=None) -> dict:
    """The Sleep-Phase Task G entry: record today's frame from the day's
    captured artifacts (+ live JSONs where the lake copy hasn't landed).
    Never raises."""
    today = today or date.today()
    day = today.isoformat()
    src = _day_sources_from_lake(day, lake_root)
    # Live-JSON fallbacks for artifacts whose lake copy is absent (e.g.
    # the first days before every cron has run).
    try:
        if src["flows"] is None:
            from src.ingestion.flows_tracker import load_flows
            flows = load_flows()
            src["flows"] = flows if flows.get("as_of") == day else None
    except Exception:
        pass
    affinity = None
    try:
        from src.knowledge_graph.entity_affinity import AFFINITY_PATH
        if AFFINITY_PATH.exists():
            affinity = json.loads(AFFINITY_PATH.read_text())
    except Exception:
        affinity = None
    frame = build_frame(day, macro=src["macro"], news=src["news"],
                        deals_census=src["deals_census"], flows=src["flows"],
                        vix=src["vix"], affinity=affinity)
    ok = record_frame(conn, frame)
    filled = sum(1 for k, v in frame.items()
                 if v is not None and k != "date")
    print(f"  (daily context: {day} — {filled} field(s) filled, "
          f"{'recorded' if ok else 'FAILED'})")
    return {"date": day, "recorded": ok, "fields_filled": filled}


if __name__ == "__main__":
    from src import brain_map
    conn = brain_map.connect()
    try:
        print(json.dumps(run_for_today(conn), indent=2))
    finally:
        conn.close()
