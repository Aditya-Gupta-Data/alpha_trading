"""
src/staleness_guard.py — the generic data-freshness guard (Dept 1, Data)
=========================================================================

WHY THIS EXISTS (the 2026-08-05 finding). `data/sector_index_bars.json` was
20 days stale and still feeding a LIVE veto through
`analysis/sector_trend.is_sector_bullish` → `analysis/regime_filters.advise()`.
Nothing crashed, nothing logged, and every nightly ops card said ✅. A 50/200
SMA read on three-week-old bars is not wrong-loudly, it is wrong-quietly —
the worst failure mode this codebase has.

This module does NOT invent a doctrine. The house already had one, in four
places, and each of them is correct:

  * `equity_desk.TIERS_MAX_AGE_DAYS = 3`   — stale tiers ⇒ no NEW entries
  * `equity_desk.IDS_MAX_AGE_DAYS = 14`    — stale ids ⇒ unmarked, never guessed
  * `equity_entry_checks.LIQUIDITY_MAX_AGE_DAYS = 7` — stale tiers ⇒ FAIL-CLOSED
  * `market_snapshot.read(max_age_seconds=…)` — stale marks ⇒ None

What was missing was (a) a shared implementation, (b) a REGISTRY so an
artifact with no freshness check is visible as an omission rather than
invisible, and (c) a route from "this is stale" to the owner's phone.

THE CONTRACT
------------
An artifact is STALE when its age exceeds `tolerance × refresh_interval_hours`.
`refresh_interval_hours` is the cadence the producer is supposed to run at;
`tolerance` is how many missed runs we forgive before the guard fires. A daily
artifact with tolerance 3 goes stale after three missed days, not after one —
weekends, holidays and a single flaky night must not cry wolf.

FRESHNESS SIGNAL, in priority order:
  1. an `as_of` the caller passes in (the artifact's OWN content date — the
     most honest signal, and what the four existing checks use), else
  2. the file's mtime (O(1), no read; correct whenever the producer rewrites
     the whole file, which every producer in this repo does).

THE TWO POLICIES — direction matters, and getting it backwards is dangerous
--------------------------------------------------------------------------
"Fail open" and "fail safe" point in OPPOSITE directions depending on what the
consumer does with the data:

  policy="ignore"   The consumer is a RISK-REDUCING ADVISORY whose absence
                    returns the system to its documented baseline. Stale ⇒ the
                    consumer drops its opinion. Used by `regime_filters`'
                    sector veto: no verdict is strictly safer than a verdict
                    computed off 20-day-old bars, and "no verdict" is exactly
                    what that radar already does on any exception.

  policy="monitor"  ALERT ONLY. The guard never changes the consumer's
                    behaviour. Used wherever the consumer ALREADY has its own
                    correct check (the four above) — duplicating a fail-closed
                    gate with a self-disable would make it fail OPEN, i.e.
                    riskier. Monitoring makes the omission visible without
                    touching a working gate.

There is deliberately NO policy that bypasses a fail-closed risk gate on
staleness. If one is ever wanted it is an owner ruling, not a flag.

THE GUARD ITSELF FAILS SAFE, unlike most things here
----------------------------------------------------
Every other fail-open path in this codebase degrades toward "carry on". This
one degrades toward "assume stale" — an unreadable path, an unknown artifact
name, or any internal exception yields `state="stale"`. The precedent is
`sleep_phase._targets_the_real_brain_map`: a muzzle that fails open is not a
muzzle. Concretely: if the guard breaks, the sector veto switches OFF and the
proposer returns to its baseline behaviour. That is the same place any
exception in `_sector_bearish` already lands today, so a broken guard can
never be worse than no guard.

Pure stdlib. Every seam injectable (`now`, `root`, `as_of`) so the whole
surface tests offline with no clock and no real files.

CLI:  python3 -m src.staleness_guard [--json]
"""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOUR = 3600.0
DAY = 24 * HOUR

# Policies
IGNORE = "ignore"        # stale ⇒ dependent component drops its opinion
MONITOR = "monitor"      # stale ⇒ alert only, behaviour untouched

# States
FRESH = "fresh"
STALE = "stale"
MISSING = "missing"      # a subtype of stale for reporting; treated as stale


class Artifact:
    """One registered data file and what depends on it.

    refresh_interval_hours — the cadence the producer is supposed to run at.
    tolerance              — X: how many missed runs are forgiven. Stale when
                             age > tolerance * refresh_interval_hours.
    policy                 — IGNORE or MONITOR (see the module docstring).
    consumer               — plain-English: who reads this on a live path.
    producer               — the job that refreshes it, or None. **None is a
                             finding, not a blank** — it means nothing on any
                             schedule keeps this file alive.
    """

    def __init__(self, name, rel_path, refresh_interval_hours, tolerance,
                 policy, consumer, producer, note=""):
        self.name = name
        self.rel_path = rel_path
        self.refresh_interval_hours = float(refresh_interval_hours)
        self.tolerance = float(tolerance)
        self.policy = policy
        self.consumer = consumer
        self.producer = producer
        self.note = note

    @property
    def threshold_hours(self) -> float:
        return self.refresh_interval_hours * self.tolerance

    def path(self, root=None) -> Path:
        return (Path(root) if root else ROOT) / self.rel_path


# --------------------------------------------------------------------------
# THE REGISTRY — every artifact read on a live decision path.
#
# An artifact that is NOT here is not monitored. Adding a data file that a
# live decision reads without adding a row here is a review bug, the same way
# an undocumented module is (MODULES.md maintenance rule).
# --------------------------------------------------------------------------
REGISTRY = {
    a.name: a for a in [
        # ---- THE BUG THIS MODULE WAS BUILT FOR -----------------------------
        Artifact(
            name="sector_index_bars",
            rel_path="data/sector_index_bars.json",
            refresh_interval_hours=24, tolerance=3,
            policy=IGNORE,
            consumer="analysis.sector_trend.is_sector_bullish → "
                     "analysis.regime_filters._sector_bearish (LIVE bullish veto)",
            producer=None,
            note="NO PRODUCER EXISTS on any schedule — verified 2026-08-05 by "
                 "grep over src/, scripts/, archive/, research_archive/. The "
                 "file was written once (yfinance, 2026-07-16) and has never "
                 "been refreshed. Until a refresher exists this artifact is "
                 "permanently stale BY DESIGN and the sector veto stays off.",
        ),

        # ---- MONITOR-ONLY: these already have their own correct checks -----
        Artifact(
            name="fo_liquidity",
            rel_path="data/fo_liquidity.json",
            refresh_interval_hours=24, tolerance=7,
            policy=MONITOR,
            consumer="analysis.equity_entry_checks.liquidity_filter "
                     "(equity-option halt stack)",
            producer="Mac EOD chain — analysis.patience_basket --eod (19:15)",
            note="Has its own FAIL-CLOSED check (LIQUIDITY_MAX_AGE_DAYS=7). "
                 "Monitored, never overridden: self-disabling a fail-closed "
                 "risk gate would make it fail OPEN.",
        ),
        Artifact(
            name="darling_tiers",
            rel_path="data/darling_tiers.json",
            refresh_interval_hours=24, tolerance=3,
            policy=MONITOR,
            consumer="equity_desk (no NEW entries on a stale tier table)",
            producer="Mac EOD chain — analysis.patience_basket --eod (19:15)",
            note="Has its own check (equity_desk.TIERS_MAX_AGE_DAYS=3).",
        ),
        Artifact(
            name="darlings_levels",
            rel_path="data/darlings_levels.json",
            refresh_interval_hours=24, tolerance=3,
            policy=MONITOR,
            consumer="analysis.equity_entry_checks._extension (overextension "
                     "halt) + equity_shadow_proposer",
            producer="Mac EOD chain — analysis.dynamic_pricer (19:15)",
            note="No native freshness check of its own; gated indirectly by "
                 "the tier table's check. Monitored so the omission is "
                 "visible rather than assumed safe.",
        ),
        Artifact(
            name="darling_ids",
            rel_path="data/darling_ids.json",
            refresh_interval_hours=7 * 24, tolerance=2,
            policy=MONITOR,
            consumer="equity_desk quote ids (unquotable without them)",
            producer="Mac — ingestion.scrip_master (Sat 09:30)",
            note="Has its own check (equity_desk.IDS_MAX_AGE_DAYS=14).",
        ),
        Artifact(
            name="bulk_deals",
            rel_path="data/bulk_deals.json",
            refresh_interval_hours=24, tolerance=3,
            policy=MONITOR,
            consumer="analysis.regime_filters._distribution (LIVE bullish veto)",
            producer="VM cron — ingestion.deals_tracker (19:30)",
            note="Self-limiting by construction: smart_money_trend reads a "
                 "90-day point-in-time window, so an old ledger yields 'no "
                 "deals in window' → no veto. Monitored, behaviour untouched.",
        ),
        Artifact(
            name="macro_regime",
            rel_path="data/macro_regime.json",
            refresh_interval_hours=24, tolerance=3,
            policy=MONITOR,
            consumer="ceo_language / morning_brief (advisory sentence only — "
                     "zero execution authority, Rule 5)",
            producer="VM cron — analysis.macro_nightly (19:50)",
        ),
        Artifact(
            name="corporate_events",
            rel_path="data/lake/events",
            refresh_interval_hours=24, tolerance=4,
            policy=MONITOR,
            consumer="analysis.equity_entry_checks.corporate_risk_halt "
                     "(HARD equity entry halt on STRUCTURAL_RISK/LEGAL_RISK)",
            producer="VM cron — ingestion.corporate_events (19:25)",
            note="Wired 2026-08-05. The consumer FAILS CLOSED on a stale "
                 "feed by its own logic, so the policy here is MONITOR: the "
                 "guard reports, the halt decides. Directory artifact — "
                 "mtime is the newest partition's, which is what a "
                 "date-partitioned lake updates on each write.",
        ),
        Artifact(
            name="bars_cache",
            rel_path="data/bars_cache.json",
            refresh_interval_hours=24, tolerance=30,
            policy=MONITOR,
            consumer="regime backfill CLI, evolution, macro_shocks.crisis_playbook "
                     "(none on the live entry path)",
            producer=None,
            note="No producer on any schedule. Off the live decision path, so "
                 "monitored with a deliberately loose tolerance rather than "
                 "treated as an incident.",
        ),
    ]
}


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------
def _epoch(now) -> float:
    """`now` may be None (real clock), an epoch float, or a datetime."""
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    return now.timestamp()


def _as_of_epoch(as_of) -> float | None:
    """A caller-supplied content date → epoch. Accepts date/datetime or an
    ISO string. Anything unparseable returns None so we fall back to mtime
    rather than inventing a timestamp."""
    if as_of is None:
        return None
    try:
        if isinstance(as_of, str):
            from datetime import date, datetime
            try:
                return datetime.fromisoformat(as_of).timestamp()
            except ValueError:
                d = date.fromisoformat(as_of)
                return datetime(d.year, d.month, d.day).timestamp()
        if hasattr(as_of, "timestamp"):
            return as_of.timestamp()
        from datetime import datetime
        return datetime(as_of.year, as_of.month, as_of.day).timestamp()
    except Exception:
        return None


def check(name: str, now=None, as_of=None, root=None) -> dict:
    """Freshness verdict for one registered artifact.

    Returns {name, state, age_hours, age_days, threshold_hours, policy,
             consumer, producer, reason, signal}. NEVER raises: any internal
             failure yields state=STALE (fail-safe — see the module docstring).
    """
    art = REGISTRY.get(name)
    if art is None:
        return {"name": name, "state": STALE, "age_hours": None,
                "age_days": None, "threshold_hours": None,
                "policy": IGNORE, "consumer": None, "producer": None,
                "signal": "unregistered",
                "reason": f"'{name}' is not in the staleness registry — "
                          "assuming stale (fail-safe)"}

    base = {"name": art.name, "policy": art.policy,
            "consumer": art.consumer, "producer": art.producer,
            "threshold_hours": art.threshold_hours}
    try:
        stamp = _as_of_epoch(as_of)
        signal = "as_of"
        if stamp is None:
            p = art.path(root)
            if not p.exists():
                return {**base, "state": MISSING, "age_hours": None,
                        "age_days": None, "signal": "absent",
                        "reason": f"{art.rel_path} does not exist"}
            stamp = os.path.getmtime(p)
            signal = "mtime"

        age_h = max(0.0, (_epoch(now) - stamp) / HOUR)
        stale = age_h > art.threshold_hours
        if stale:
            missed = art.producer or "NO PRODUCER on any schedule"
            reason = (f"{art.rel_path} is {age_h / 24:.1f} days old "
                      f"(limit {art.threshold_hours / 24:.1f} days = "
                      f"{art.tolerance:g}× its {art.refresh_interval_hours:g}h "
                      f"cadence); producer: {missed}")
        else:
            # State the limit even when fresh: "fresh (27 days old)" reads as
            # an endorsement unless the reader can see the tolerance it passed.
            reason = (f"{art.rel_path} fresh ({age_h / 24:.1f} days old, "
                      f"limit {art.threshold_hours / 24:.1f})")
        return {**base, "state": STALE if stale else FRESH,
                "age_hours": round(age_h, 2), "age_days": round(age_h / 24, 2),
                "signal": signal, "reason": reason}
    except Exception as exc:                       # pragma: no cover - guard
        return {**base, "state": STALE, "age_hours": None, "age_days": None,
                "signal": "error",
                "reason": f"staleness check failed [{exc}] — assuming stale "
                          "(fail-safe)"}


def is_stale(name: str, now=None, as_of=None, root=None) -> bool:
    """The one-liner a consumer calls. True for stale, missing, unregistered
    and broken — every uncertain case, on purpose."""
    return check(name, now=now, as_of=as_of, root=root)["state"] != FRESH


def scan(names=None, now=None, root=None) -> list:
    """Every registered artifact (or a named subset), registry order."""
    keys = names if names is not None else list(REGISTRY)
    return [check(k, now=now, root=root) for k in keys]


# --------------------------------------------------------------------------
# The alert — appended to the nightly Discord health ping by ops_monitor
# --------------------------------------------------------------------------
def alert_payload(verdicts: list) -> dict | None:
    """Turn a scan into the health card's stale-data block, or None when
    everything is fresh (a clean scan must add NOTHING to the card — the
    byte-identical rule).

    `disabled` counts the components the guard actually switched off; that
    number is the one the owner needs, because it means the engine is
    running with a radar down."""
    stale = [v for v in (verdicts or []) if v.get("state") != FRESH]
    if not stale:
        return None
    disabled = [v for v in stale if v.get("policy") == IGNORE]
    lines = []
    for v in stale:
        mark = "🔴 DISABLED" if v.get("policy") == IGNORE else "⚠️ monitor"
        lines.append(f"• {mark} `{v['name']}` — {v['reason']}")
        if v.get("policy") == IGNORE and v.get("consumer"):
            lines.append(f"    ↳ off: {v['consumer']}")
    # The headline states only what is true. A monitor-only scan must NOT
    # claim a component went dark, or the one alert that matters stops
    # standing out from the ones that don't.
    if disabled:
        head = (f"🚨 **STALE DATA — {len(stale)} artifact(s), "
                f"{len(disabled)} component(s) SELF-DISABLED**")
    else:
        head = (f"⚠️ **STALE DATA — {len(stale)} artifact(s) "
                "(monitor only, nothing disabled)**")
    return {"count": len(stale), "disabled": len(disabled),
            "names": [v["name"] for v in stale],
            "text": head + "\n" + "\n".join(lines)}


def main(argv=None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    verdicts = scan()
    if "--json" in argv:
        print(json.dumps(verdicts, indent=2))
    else:
        for v in verdicts:
            icon = "✅" if v["state"] == FRESH else "🔴"
            print(f"{icon} {v['name']:<20} {v['state']:<8} {v['reason']}")
        payload = alert_payload(verdicts)
        print("\n" + (payload["text"] if payload else "all artifacts fresh"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
