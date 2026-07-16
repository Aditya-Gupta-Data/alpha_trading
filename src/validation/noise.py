"""
src/validation/noise.py — the false-discovery regression suite (Phase 4, §7.1)
==============================================================================

"This brain does not see faces in clouds", as a regression test. Feed the
FULL discovery pipeline — the real miners (src/discovery/cooccurrence_miner,
sequence_miner) -> the real registry (src/validation/registry) -> the real
trial gate (src/validation/trial + stat_gates.promotable) — pure-noise
histories, and assert the END-TO-END false-promotion rate stays within the
configured FDR budget. Every individual gate has its own unit tests; this
suite is the only place the COMPOSITION is proven: if someone loosens a
floor in config.json, swaps the Wilson lower bound for a point estimate, or
breaks the BH correction, the promoted count on noise jumps and this fails
loudly.

Noise model (deterministic per seed, `random.Random(seed)`):
  * A synthetic market history: weekday daily_context frames with plausible
    vix/fii/dii/news/deals readings, and outcomes carrying event tags —
    with WIN/LOSS LABELS DRAWN INDEPENDENTLY of every tag, at exactly the
    structural breakeven null rate (stat_gates.breakeven_win_rate). By
    construction NO pattern has an edge, so every promotion is a false one.
  * Trial-stage evidence: fabricated resolved shadow fires in the
    out-of-discovery validation window, again at exactly the null rate.
  * `forced_candidates` extra patterns are registered per seed even when
    the miners (correctly) surface nothing — so the TRIAL gate is always
    exercised, not just the miner gate; a leak in either shows up.

Positive controls (a suite that promotes nothing proves nothing):
  * plant_edge(): outcomes carrying a planted tag-pair win ~95% — the miner
    MUST register it and, with genuinely winning shadow evidence, the trial
    MUST promote it. Sees real edges; ignores fake ones.

Isolation (non-negotiable): every run works on its own throwaway SQLite
database (tempfile, brain_map-schema'd via brain_map.connect) and
_assert_not_production refuses to run against data/brain_map.db even if
handed such a conn deliberately. No lake writes, no journal, no network.

CI: 25-seed smoke via tests/test_noise_injection.py.
Nightly: python3 -m src.validation.noise --seeds 500   (exit 1 on breach)
"""

import argparse
import json
import math
import os
import random
import tempfile
from datetime import date, timedelta

from src import brain_map
from src import daily_context as dc
from src.validation import registry as rg
from src.validation import stat_gates as sg
from src.validation import trial

# The synthetic tag vocabulary (all minable — no auto: prefix). Drawn from
# per-outcome; labels never look at them, so any association is chance.
EVENT_TAGS = ("golden_cross", "rsi_oversold", "breakout", "hammer",
              "gap_up", "news_positive", "high_oi", "fii_selling",
              "volume_spike", "support_bounce")
TICKERS = ("NIFTY 50", "NIFTY BANK", "RELIANCE.NS")

# Corpus shape: enough transactions that the support floor (12) is
# reachable and BH has a real batch to correct across — small enough that
# a 25-seed CI smoke stays in seconds.
DAYS = 90
OUTCOMES_PER_DAY = 3


# ---------------------------------------------------------------- guard

def _assert_not_production(conn) -> None:
    """Refuse to run against the real brain_map.db. The harness only ever
    hands out throwaway conns; this is the belt AND suspenders."""
    prod = str(brain_map.DEFAULT_DB_PATH.resolve())
    for _, name, file in conn.execute("PRAGMA database_list"):
        if file and os.path.realpath(file) == prod:
            raise RuntimeError(
                "noise harness pointed at the PRODUCTION brain_map.db — "
                "refusing to write. Use the harness's own temp conn.")


def _temp_conn(tmpdir: str):
    """A fully brain_map-schema'd throwaway DB (registry/trial/daily_context
    schemas layered on top). Lives in tmpdir; deleted with it."""
    path = os.path.join(tmpdir, "noise_brain.db")
    conn = brain_map.connect(path)   # applies _SCHEMA + additive columns
    _assert_not_production(conn)
    dc.ensure_schema(conn)
    rg.ensure_schema(conn)
    trial.ensure_schema(conn)
    return conn


# ------------------------------------------------------------- generators

def _weekdays(start: date, n: int) -> list:
    """n consecutive weekdays from start (ISO strings)."""
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def synth_corpus(conn, rng: random.Random, days: list,
                 base_rate: float, planted: dict = None) -> dict:
    """Populate the temp DB with a synthetic history: one daily_context
    frame per day, OUTCOMES_PER_DAY tagged outcomes per day. Labels are
    Bernoulli(base_rate) INDEPENDENT of tags — pure noise — except for
    outcomes carrying the `planted` tag-set, which win at planted
    ["win_rate"] (the positive control). Returns {outcomes, planted_n}."""
    _assert_not_production(conn)
    planted_tags = list((planted or {}).get("tags") or [])
    planted_rate = (planted or {}).get("win_rate", 0.95)
    planted_every = (planted or {}).get("every", 9)   # ~1 in 9 outcomes

    made = planted_n = 0
    for di, day in enumerate(days):
        vix = round(rng.uniform(11.0, 19.5), 2)
        conn.execute(
            "INSERT OR REPLACE INTO daily_context (date, vix, vix_band, "
            "fii_net, dii_net, news_net, deals_buy_legs, deals_sell_legs, "
            "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, vix, ("low" if vix < 13 else "mid" if vix <= 16 else "high"),
             round(rng.uniform(-2000, 2000), 1), round(rng.uniform(-2000, 2000), 1),
             rng.randint(-4, 4), rng.randint(0, 9), rng.randint(0, 9), "{}"))
        for k in range(OUTCOMES_PER_DAY):
            idx = di * OUTCOMES_PER_DAY + k
            is_planted = bool(planted_tags) and idx % planted_every == 0
            tags = (list(planted_tags) if is_planted
                    else rng.sample(EVENT_TAGS, rng.randint(1, 3)))
            rate = planted_rate if is_planted else base_rate
            win = rng.random() < rate
            ticker = TICKERS[idx % len(TICKERS)]
            cur = conn.execute(
                "INSERT INTO outcomes (journal_ref, date, ticker, r_multiple, "
                "result) VALUES (?, ?, ?, ?, ?)",
                (f"noise-{idx:05d}", day, ticker,
                 round(1.5 if win else -1.0, 2), "win" if win else "loss"))
            oid = cur.lastrowid
            for t in tags:
                ecur = conn.execute(
                    "INSERT INTO events (date, ticker, event_type, tag, "
                    "source) VALUES (?, ?, 'noise', ?, 'noise')",
                    (day, ticker, t))
                conn.execute(
                    "INSERT INTO event_outcome_link (event_id, outcome_id) "
                    "VALUES (?, ?)", (ecur.lastrowid, oid))
            made += 1
            planted_n += 1 if is_planted else 0
    conn.commit()
    return {"outcomes": made, "planted_n": planted_n}


def _fabricate_shadow_evidence(conn, rng: random.Random, pattern_id: str,
                               windows: dict, val_days: list,
                               n: int, win_rate: float) -> None:
    """n resolved shadow fires inside the validation window at `win_rate`
    (the noise runs pass the structural null here — a promotion off these
    is a pure false positive)."""
    for i in range(n):
        day = val_days[rng.randrange(len(val_days))]
        ticker = TICKERS[i % len(TICKERS)]
        ref = trial.record_shadow_fire(
            conn, pattern_id, day, f"{ticker}#{i}")["ref"]
        win = rng.random() < win_rate
        trial.resolve_shadow(conn, ref, "win" if win else "loss",
                             1.5 if win else -1.0, day)


# ------------------------------------------------------------- one seed

def run_noise_seed(seed: int, forced_candidates: int = 6,
                   planted: dict = None,
                   planted_shadow_win_rate: float = None) -> dict:
    """One full mine -> register -> trial -> promote pass over pure noise
    (plus the optional planted edge). Isolated: everything happens in a
    throwaway temp DB. Returns the seed's counts + per-pattern verdicts."""
    rng = random.Random(seed)
    null_rate = sg.breakeven_win_rate(1.5, 1.0)      # the structural null
    floors = sg.configured_floors()
    n_shadow = floors["min_resolutions"] + 3         # enough to ask the LB

    with tempfile.TemporaryDirectory(prefix="noise_seed_") as tmp:
        conn = _temp_conn(tmp)
        try:
            days = _weekdays(date(2026, 1, 5), DAYS)
            synth = synth_corpus(conn, rng, days, base_rate=null_rate,
                                 planted=planted)

            # ---- MINE (the real miners, real corpus split) ----
            from src.discovery import cooccurrence_miner, sequence_miner
            co = cooccurrence_miner.run(conn=conn, corpus="real",
                                        today=date.fromisoformat(days[-1]))
            sq = sequence_miner.run(conn=conn, corpus="real",
                                    today=date.fromisoformat(days[-1]))
            mined_ids = [r["pattern_id"] for r in
                         (co.get("registered") or []) +
                         (sq.get("registered") or [])]

            # ---- FORCE extra candidates so the trial gate is always
            # exercised even when BH (correctly) lets nothing through ----
            forced_ids = []
            for i in range(forced_candidates):
                pair = sorted(rng.sample(EVENT_TAGS, 2))
                res = rg.register(conn, "cooccurrence",
                                  {"kind": "cooccurrence", "tags": pair,
                                   "forced": i},
                                  description=f"[noise forced] {pair}",
                                  mining_run=f"noise:forced:{seed}")
                forced_ids.append(res["pattern_id"])

            # ---- TRIAL every candidate on null-rate shadow evidence ----
            windows = trial.split_windows(days)
            val_days = [d for d in days if trial.in_validation(d, windows)]
            promoted, verdicts = [], []
            planted_id = None
            for pid in mined_ids + forced_ids:
                row = rg.get(conn, pid)
                is_planted_pattern = bool(
                    planted and
                    set(planted["tags"]) <= set(
                        json.loads(row["definition"]).get("tags") or []))
                if is_planted_pattern and planted_id is None:
                    planted_id = pid
                rate = (planted_shadow_win_rate
                        if (is_planted_pattern and
                            planted_shadow_win_rate is not None)
                        else null_rate)
                _fabricate_shadow_evidence(conn, rng, pid, windows, val_days,
                                           n=n_shadow, win_rate=rate)
                verdict = trial.evaluate_trial(conn, pid, windows)
                verdicts.append({"pattern_id": pid,
                                 "final_status": verdict["final_status"],
                                 "planted": is_planted_pattern,
                                 "reason": verdict["reason"]})
                if (verdict["final_status"] in rg.CITABLE_STATES
                        and not is_planted_pattern):
                    promoted.append(pid)
        finally:
            conn.close()

    return {"seed": seed, "outcomes": synth["outcomes"],
            "planted_n": synth["planted_n"],
            "mined_candidates": len(mined_ids),
            "forced_candidates": len(forced_ids),
            "candidates": len(mined_ids) + len(forced_ids),
            "promoted": len(promoted), "promoted_ids": promoted,
            "planted_id": planted_id, "verdicts": verdicts}


# ------------------------------------------------------------ aggregation

def binom_upper_bound(n: int, p: float, alpha: float = 0.01) -> int:
    """Smallest k with P(Binomial(n,p) <= k) >= 1 - alpha — the promotion
    count a leak-free pipeline should virtually never exceed if its true
    per-candidate false-promotion rate were AT the budget p. Log-space
    (lgamma) so large n never underflows. Pure stdlib."""
    if n <= 0:
        return 0
    log_p, log_q = math.log(p), math.log(1.0 - p)
    cdf = 0.0
    for k in range(n + 1):
        log_pmf = (math.lgamma(n + 1) - math.lgamma(k + 1)
                   - math.lgamma(n - k + 1) + k * log_p + (n - k) * log_q)
        cdf += math.exp(log_pmf)
        if cdf >= 1.0 - alpha:
            return k
    return n


def false_promotion_rate(seeds: int = 25, start_seed: int = 0,
                         forced_candidates: int = 6) -> dict:
    """The aggregate regression: run `seeds` independent noise seeds and
    compare total promotions against the binomial upper bound at the ACTIVE
    fdr budget (configured_floors()['fdr_q'] — so a config loosening is
    tested at the loosened value it actually runs with). Returns the
    verdict dict; `ok` False = the pipeline promoted more noise than its
    own budget allows."""
    q = sg.configured_floors()["fdr_q"]
    total_candidates = total_promoted = total_mined = 0
    per_seed = []
    for s in range(start_seed, start_seed + seeds):
        r = run_noise_seed(s, forced_candidates=forced_candidates)
        total_candidates += r["candidates"]
        total_promoted += r["promoted"]
        total_mined += r["mined_candidates"]
        per_seed.append({"seed": s, "candidates": r["candidates"],
                         "promoted": r["promoted"]})
    bound = binom_upper_bound(total_candidates, q)
    return {"seeds": seeds, "fdr_q": q,
            "total_candidates": total_candidates,
            "total_mined_candidates": total_mined,
            "total_promoted": total_promoted,
            "empirical_rate": (round(total_promoted / total_candidates, 4)
                               if total_candidates else 0.0),
            "bound": bound, "ok": total_promoted <= bound,
            "per_seed": per_seed}


def plant_edge() -> dict:
    """The positive control, full chain: a genuinely predictive tag-pair
    must be MINED into the registry AND PROMOTED off genuinely winning
    shadow evidence. Returns {mined, promoted, ...}; both must be True —
    a pipeline that rejects everything also fails this suite."""
    planted = {"tags": ["planted:sig_a", "planted:sig_b"],
               "win_rate": 0.95, "every": 9}
    r = run_noise_seed(seed=424242, forced_candidates=0, planted=planted,
                       planted_shadow_win_rate=0.9)
    planted_verdicts = [v for v in r["verdicts"] if v["planted"]]
    mined = r["planted_id"] is not None
    promoted = any(v["final_status"] in rg.CITABLE_STATES
                   for v in planted_verdicts)
    return {"mined": mined, "promoted": promoted,
            "planted_id": r["planted_id"], "verdicts": planted_verdicts,
            "mined_candidates": r["mined_candidates"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="End-to-end false-discovery regression on pure noise")
    parser.add_argument("--seeds", type=int, default=25,
                        help="independent noise seeds to run (nightly: 500)")
    parser.add_argument("--start-seed", type=int, default=0)
    args = parser.parse_args()

    control = plant_edge()
    result = false_promotion_rate(seeds=args.seeds,
                                  start_seed=args.start_seed)
    result["positive_control"] = {"mined": control["mined"],
                                  "promoted": control["promoted"]}
    print(json.dumps(result, indent=2))
    ok = (result["ok"] and control["mined"] and control["promoted"])
    print(f"\n{'PASS' if ok else 'FAIL'} — "
          f"{result['total_promoted']}/{result['total_candidates']} noise "
          f"promotions (bound {result['bound']} at q={result['fdr_q']}); "
          f"planted edge mined={control['mined']} "
          f"promoted={control['promoted']}")
    raise SystemExit(0 if ok else 1)
