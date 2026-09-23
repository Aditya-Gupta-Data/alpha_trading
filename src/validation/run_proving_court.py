"""
src/validation/run_proving_court.py — the nightly Department-5 job (decision #97)
=================================================================================

WHY (SYSTEM_BLUEPRINT.md §7, GAP 1). The court is the constitutional centre of
the desk — only it may grant a pattern the right to size or veto (#63) — and
it had never heard a case: nine candidates parked in CANDIDATE since 08-18,
zero organic shadow fires, zero placebos seeded, and every shadow strategy
shipped "on no cron" as the safe default. Every court input was a side effect
of some other job. This module is the job whose only purpose is to put cases
in front of the court, nightly, at 21:00 IST after the 20:30 ops sweep.

WHAT ONE RUN DOES, in order (every stage fails open and is named in the
summary, so a stage that produced nothing is a line, never silence):

  1. PROMOTE   every CANDIDATE older than TRIAL_AGE_DAYS -> TRIAL, through
               `registry.transition` (the only legal mutator). Placebos are
               promoted by the same rule — that is the point of a placebo.
  2. ENROL     the structural primitives (`strategies.glassbreaking`:
               falling_knife, early_breakout) as registered TRIAL hypotheses.
  3. SEED      one placebo batch per ISO week (`placebo.seed_batch`, tags drawn
               from the real `events` vocabulary) so the harness's false-
               discovery rate becomes a measured number, never a hope.
  4. FEED      the candidate universe = the five equity-option underlyings the
               chain archiver captures + every tier1 F&O name, bars from the
               VM's own bhavcopy lake (adjusted, decision #90), today's bar as
               the breakout "today", and the spread priced from the EOD chain
               archived at 15:40 (`lake chains/<slug>`, real quotes). A name
               without an archived chain yields a SIGNAL row with reason
               `no_chain_for_underlying` — counted, never priced off a guess.
  5. RUN       `glassbreaking.run` -> shadow ledger + `trial.record_shadow_fire`.
  6. GRADE     open shadow setups with the tracker's own resolvers
               (`glassbreaking.grade`) -> `trial.resolve_shadow`.
  7. SCORE     every non-DEAD hypothesis: n / wins / one-sided 95% Wilson LB
               against the structural null (`stat_gates.promotable`);
               `trial.evaluate_trial` runs — and may PROMOTE — only once a
               hypothesis has at least the configured floor of REAL
               resolutions, on windows split from `daily_context` dates.
  8. AUDIT     every placebo batch (`placebo.audit_batch`) -> realized FDR.
  9. PUBLISH   `data/proving_court.json` (what the CEO brief reads) and one
               line per run in `logs/proving_court.jsonl`.

AUTHORITY: none. This job registers, promotes within the registry's legal
map, and scores. It never touches the journal, a lock, the treasury or a
proposal. Everything it grades is shadow.

Pure-Python; every data door injectable (`bars_fn`, `chain_fn`, `fo`,
`conn`, `today`); the pytest muzzle (h4_shadow doctrine) refuses to open the
real brain_map from inside the suite.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = ROOT / "data" / "proving_court.json"
LEDGER_PATH = ROOT / "logs" / "proving_court.jsonl"
IST = timezone(timedelta(hours=5, minutes=30))

TRIAL_AGE_DAYS = 7            # a CANDIDATE this old is put on trial
PLACEBO_BATCH_SIZE = 10       # per ISO week
PLACEBO_MIN_TAG_POOL = 4
BARS_DAYS = 90                # sessions of bhavcopy per name
SCORECARD_TOP_N = 8


# ------------------------------------------------------------ stage 1: promote

def promote_aged_candidates(conn, today: date, age_days: int = TRIAL_AGE_DAYS) -> list:
    from src.validation import registry as rg
    cutoff = (today - timedelta(days=age_days)).isoformat()
    promoted = []
    for row in rg.list_by_status(conn, "CANDIDATE"):
        discovered = str(row.get("discovered_at") or "")[:10]
        if discovered and discovered <= cutoff:
            r = rg.transition(conn, row["pattern_id"], "TRIAL",
                              f"proving court: aged {age_days}d past discovery")
            if r.get("ok"):
                promoted.append(row["pattern_id"])
    return promoted


# ------------------------------------------------------------ stage 2: enrol

def enrol_primitives(conn) -> dict:
    from src.strategies import glassbreaking as gb
    return {p: gb.enrol_in_court(conn, p) for p in gb.STRUCTURES}


# ------------------------------------------------------------ stage 3: placebos

def placebo_batch_name(today: date) -> str:
    y, w, _ = today.isocalendar()
    return f"court-w{y}{w:02d}"


def _tag_pool(conn, limit: int = 400) -> list:
    try:
        rows = conn.execute("SELECT DISTINCT tag FROM events WHERE tag IS NOT NULL "
                            "AND tag != '' LIMIT ?", (limit,)).fetchall()
        return sorted({str(r[0]) for r in rows})
    except sqlite3.Error:
        return []


def seed_placebos(conn, today: date, count: int = PLACEBO_BATCH_SIZE,
                  rng=None, tag_pool: list = None) -> dict:
    from src.validation import placebo as pb
    batch = placebo_batch_name(today)
    pb.ensure_schema(conn)
    have = conn.execute("SELECT COUNT(*) FROM placebo_ledger WHERE batch = ?",
                        (batch,)).fetchone()[0]
    if have:
        return {"batch": batch, "seeded": 0, "existing": int(have), "skip": "already_seeded_this_week"}
    pool = tag_pool if tag_pool is not None else _tag_pool(conn)
    if len(pool) < PLACEBO_MIN_TAG_POOL:
        return {"batch": batch, "seeded": 0, "existing": 0,
                "skip": f"tag_pool_too_small ({len(pool)} < {PLACEBO_MIN_TAG_POOL})"}
    rng = rng or random.Random(f"{batch}|{len(pool)}")
    ids = pb.seed_batch(conn, batch, pool, rng, count=count)
    return {"batch": batch, "seeded": len(ids), "existing": 0, "skip": None}


# ------------------------------------------------------------ stage 4: feed

def _display_symbol(bare: str) -> str:
    """bhavcopy/F&O names are bare (RELIANCE); the chain archiver and the
    option-underlying tables key on RELIANCE.NS."""
    return f"{bare}.NS"


def _bar_dicts(bars: list) -> list:
    out = []
    for b in bars or []:
        out.append({"date": b.get("session") or b.get("date"), "open": b.get("open"),
                    "high": b.get("high"), "low": b.get("low"),
                    "close": b.get("close"), "volume": b.get("volume"),
                    "prev_close": b.get("prev_close")})
    return out


def default_bars_fn(symbols: list, days: int = BARS_DAYS) -> dict:
    from src.ingestion import bhavcopy_clerk as bc
    return bc.bars_for_many(symbols, days=days)


def lot_size_from_chain(oc: dict):
    """A tier-1 name's lot size is not in the repo's tables; Dhan's chain
    nodes carry it per leg (`lot_size` / `lotSize`). None when absent —
    the court then counts the name, never guesses a contract size."""
    for node in (oc or {}).values():
        for leg in (node or {}).values():
            if isinstance(leg, dict):
                for k in ("lot_size", "lotSize", "lot"):
                    try:
                        v = int(float(leg.get(k)))
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        pass
    return None


def chain_from_lake(symbol_display: str, day: str, today: date = None) -> dict | None:
    """The EOD chain the archiver captured at 15:40 -> the four legs a bull
    call spread needs: buy ATM, sell ATM + WING_STEPS steps, both priced by
    `_leg_fill` (#70 honest fill). None when the name has no archived chain
    or no usable expiry — never a synthetic premium."""
    from src import lake
    from src import options_proposer as op
    from src.ingestion import chain_archiver as ca
    slug = ca.UNDERLYINGS.get(symbol_display)
    if not slug:
        # decision #100: tier-1 names are archived under extension slugs
        slug = ca.extension_slug(str(symbol_display).split(".")[0])
    rows = [r for r in lake.read_day(f"chains/{slug}", day) if isinstance(r, dict) and r.get("oc")]
    if not rows:
        return None
    expiries = sorted({str(r.get("expiry")) for r in rows if r.get("expiry")})
    expiry = op.pick_expiry(expiries, today=today or date.fromisoformat(day),
                            underlying=symbol_display, horizon="short")
    if expiry is None:
        return None
    row = next(r for r in rows if str(r.get("expiry")) == expiry)
    chain = {"oc": row["oc"], "last_price": row.get("spot")}
    strikes = op._strikes(chain)
    spot = row.get("spot")
    if not strikes or spot is None:
        return None
    step = op._step(strikes) or 0.0
    atm = op._nearest_strike(strikes, float(spot))
    sell = op._nearest_strike(strikes, atm + op.WING_STEPS * step) if step else None
    if not sell or sell <= atm:
        return None
    buy_px, _ = op._leg_fill(chain, atm, "ce", "BUY")
    sell_px, _ = op._leg_fill(chain, sell, "ce", "SELL")
    if not buy_px or not sell_px:
        return None
    lot = (op.LOT_SIZES.get(symbol_display) or op.EQUITY_OPTION_UNDERLYINGS.get(symbol_display)
           or lot_size_from_chain(row.get("oc")))
    if not lot:
        return None
    return {"buy_strike": atm, "sell_strike": sell, "buy_premium": float(buy_px),
            "sell_premium": float(sell_px), "lot_size": int(lot), "expiry": expiry,
            "spot": float(spot), "chain_day": day}


def universe(fo: dict = None) -> list:
    from src import options_proposer as op
    names = {s.split(".")[0] for s in op.EQUITY_OPTION_UNDERLYINGS}
    for sym, row in ((fo or {}).get("symbols") or {}).items():
        if isinstance(row, dict) and row.get("tier") == "tier1":
            names.add(str(sym).upper())
    banned = {str(b).upper() for b in (fo or {}).get("banned") or []}
    return sorted(names - banned)


def build_candidates(day: str, fo: dict = None, bars_fn=None, chain_fn=None,
                     today: date = None) -> list:
    syms = universe(fo)
    bars_by = (bars_fn or default_bars_fn)(syms)
    chain_fn = chain_fn or chain_from_lake
    out = []
    for sym in syms:
        bars = _bar_dicts(bars_by.get(sym) or [])
        if len(bars) < 2:
            continue
        cand = {"symbol": _display_symbol(sym), "bare": sym, "bars": bars}
        last = bars[-1]
        if last.get("date") == day:
            cand["today"] = {"date": day, "open": last.get("open"),
                             "volume": last.get("volume")}
        try:
            cand["chain"] = chain_fn(cand["symbol"], day, today)
        except Exception as e:
            cand["chain"] = None
            cand["chain_error"] = f"{type(e).__name__}: {e}"
        out.append(cand)
    return out


# ------------------------------------------------------------ stage 6: grade

def open_shadow_setups(ledger_rows: list) -> list:
    done = {r.get("ref") for r in ledger_rows if r.get("event") == "outcome" and r.get("ref")}
    return [r for r in ledger_rows
            if r.get("accepted") and r.get("ref") and r["ref"] not in done]


def tuple_bars_fn(bars_fn=None):
    """(day, low, high, close) tuples for the tracker's resolvers, from
    the same bhavcopy lake."""
    def _f(symbol_display: str, start_iso: str):
        bare = symbol_display.split(".")[0]
        bars = (bars_fn or default_bars_fn)([bare]).get(bare) or []
        out = []
        for b in bars:
            d = b.get("session") or b.get("date")
            if d and d >= start_iso and None not in (b.get("low"), b.get("high"), b.get("close")):
                out.append((d, float(b["low"]), float(b["high"]), float(b["close"])))
        return out
    return _f


# ------------------------------------------------------------ stage 7: score

def _windows(conn) -> dict:
    from src.validation import trial
    try:
        days = [r[0] for r in conn.execute("SELECT date FROM daily_context ORDER BY date")]
    except sqlite3.Error:
        days = []
    return trial.split_windows(days)


def scorecards(conn, evaluate: bool = True) -> list:
    from src.validation import registry as rg, trial, placebo as pb
    from src.validation import stat_gates as sg
    null_rate = sg.breakeven_win_rate(1.5, 1.0)
    floors = sg.configured_floors()
    min_res = floors["min_resolutions"]
    windows = _windows(conn) if evaluate else None
    cards = []
    for status in ("TRIAL", "CANDIDATE", "INSUFFICIENT_N", "VALIDATED", "LIVE_ADVISORY"):
        for row in rg.list_by_status(conn, status):
            pid = row["pattern_id"]
            ev = trial.shadow_evidence(conn, pid)
            v = sg.promotable(ev["wins"], ev["n"], 0, 0, null_rate=null_rate,
                              min_resolutions=min_res)
            card = {"pattern_id": pid, "kind": row.get("kind"),
                    "description": (row.get("description") or "")[:80],
                    "status": status, "placebo": pb.is_placebo(conn, pid),
                    "n": ev["n"], "wins": ev["wins"],
                    "wilson_lb": round(float(v.get("wilson_lb") or 0.0), 4),
                    "null_rate": round(null_rate, 4), "min_n": min_res,
                    "promote": bool(v.get("promote")), "reason": v.get("reason")}
            if evaluate and ev["n"] >= min_res and status in ("TRIAL", "INSUFFICIENT_N", "CANDIDATE"):
                try:
                    verdict = trial.evaluate_trial(conn, pid, windows)
                    card["evaluated"] = True
                    card["final_status"] = verdict.get("final_status")
                except Exception as e:                       # fail open per card
                    card["evaluated"] = False
                    card["evaluate_error"] = f"{type(e).__name__}: {e}"
            cards.append(card)
    cards.sort(key=lambda c: (-c["n"], c["placebo"], c["description"]))
    return cards


def audit_placebos(conn) -> dict:
    from src.validation import placebo as pb
    pb.ensure_schema(conn)
    batches = [r[0] for r in conn.execute("SELECT DISTINCT batch FROM placebo_ledger")]
    for b in batches:
        pb.audit_batch(conn, b)
    return {"batches": len(batches), "fdr": pb.realized_fdr(conn)}


# ------------------------------------------------------------ the run

def _skip(summary: dict, stage: str, why: str) -> None:
    summary["skips"][stage] = why
    print(f"  proving court: {stage} skipped — {why}")


def run(today: date = None, conn=None, fo: dict = None, bars_fn=None,
        chain_fn=None, state_path=None, ledger_path=None,
        shadow_ledger_path=None, pool_rupees: float = 200_000.0,
        vix: float = None) -> dict:
    """One nightly pass. Returns the summary it also writes to
    data/proving_court.json. Never raises for a stage."""
    today = today or datetime.now(IST).date()
    day = today.isoformat()
    summary = {"date": day, "ran_at": datetime.now(IST).isoformat(timespec="seconds"),
               "skips": {}, "promoted": [], "enrolled": {}, "placebos": {},
               "feed": {}, "fires": 0, "graded": 0, "scorecards": [],
               "fdr": None}

    own = None
    if conn is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            # THE MUZZLE — the suite injects conn; the real brain is never opened here.
            _skip(summary, "all", "muzzled_under_pytest")
            return summary
        from src import brain_map
        own = conn = brain_map.connect()
    try:
        from src.validation import registry as rg, trial
        rg.ensure_schema(conn)
        trial.ensure_schema(conn)

        try:
            summary["promoted"] = promote_aged_candidates(conn, today)
        except Exception as e:
            _skip(summary, "promote", f"{type(e).__name__}: {e}")
        try:
            summary["enrolled"] = enrol_primitives(conn)
        except Exception as e:
            _skip(summary, "enrol", f"{type(e).__name__}: {e}")
        try:
            summary["placebos"] = seed_placebos(conn, today)
        except Exception as e:
            _skip(summary, "seed_placebos", f"{type(e).__name__}: {e}")

        from src.strategies import glassbreaking as gb
        if fo is None:
            from src.strategies.insolvency_short import load_fo
            fo = load_fo()
        sl = shadow_ledger_path or gb.SHADOW_LEDGER
        try:
            cands = build_candidates(day, fo=fo, bars_fn=bars_fn, chain_fn=chain_fn, today=today)
            setups = gb.run(day, cands, pool_rupees=pool_rupees, vix=vix, conn=conn,
                            ledger_path=sl, fo=fo)
            accepted = [s for s in setups if s.get("accepted")]
            summary["feed"] = {"symbols": len(cands),
                               "with_today_bar": sum(1 for c in cands if c.get("today")),
                               "with_chain": sum(1 for c in cands if c.get("chain")),
                               "signals": len(setups), "accepted": len(accepted),
                               "no_chain": sum(1 for s in setups
                                               if s.get("reason") == "no_chain_for_underlying"),
                               "rejections": sorted({str(s.get("reason")) for s in setups
                                                     if not s.get("accepted")})[:12]}
            summary["fires"] = len(accepted)
            if not cands:
                _skip(summary, "feed", "no bhavcopy bars for the universe")
        except Exception as e:
            _skip(summary, "feed", f"{type(e).__name__}: {e}")

        try:
            rows = gb.read_ledger(sl)
            graded = gb.grade(open_shadow_setups(rows), tuple_bars_fn(bars_fn),
                              today=today, conn=conn, ledger_path=sl)
            summary["graded"] = len(graded)
        except Exception as e:
            _skip(summary, "grade", f"{type(e).__name__}: {e}")

        try:
            cards = scorecards(conn)
            summary["scorecards"] = cards[:SCORECARD_TOP_N]
            summary["registry"] = {s: len(rg.list_by_status(conn, s)) for s in rg.STATES}
        except Exception as e:
            _skip(summary, "score", f"{type(e).__name__}: {e}")
        try:
            summary["fdr"] = audit_placebos(conn)
        except Exception as e:
            _skip(summary, "audit_placebos", f"{type(e).__name__}: {e}")
        # decision #109: the queued hypotheses (ToD window, Mansfield RS) —
        # scored nightly on the desk's own record, never on a live path.
        try:
            from src.validation import hypotheses as hyp
            summary["hypotheses"] = hyp.run_nightly(conn, today)
        except Exception as e:
            _skip(summary, "hypotheses", f"{type(e).__name__}: {e}")
    finally:
        if own is not None:
            own.close()

    _publish(summary, state_path, ledger_path)
    print(render_line(summary))
    return summary


def _publish(summary: dict, state_path=None, ledger_path=None) -> None:
    for path, mode, text in ((state_path or STATE_PATH, "w", json.dumps(summary, indent=2, default=str)),
                             (ledger_path or LEDGER_PATH, "a",
                              json.dumps({k: v for k, v in summary.items() if k != "scorecards"},
                                         default=str) + "\n")):
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, mode) as f:
                f.write(text)
        except OSError as e:
            print(f"  proving court: could not write {path}: {e}")


def render_line(s: dict) -> str:
    reg = s.get("registry") or {}
    fdr = ((s.get("fdr") or {}).get("fdr") or {})
    return (f"proving court {s.get('date')}: promoted {len(s.get('promoted') or [])} → TRIAL · "
            f"placebos seeded {((s.get('placebos') or {}).get('seeded'))} · "
            f"feed {((s.get('feed') or {}).get('symbols', 0))} names / "
            f"{((s.get('feed') or {}).get('signals', 0))} signals / {s.get('fires', 0)} fires · "
            f"graded {s.get('graded', 0)} · registry TRIAL {reg.get('TRIAL', 0)} "
            f"VALIDATED {reg.get('VALIDATED', 0)} · FDR {fdr.get('state', 'n/a')}"
            + (f" · skips {s['skips']}" if s.get("skips") else ""))


if __name__ == "__main__":
    run()
