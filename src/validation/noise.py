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


# ==========================================================================
# v2 — block-permuted bars through the REAL simulator (the price path)
# ==========================================================================
#
# v1 proves the TAG path can't see faces in clouds; v2 proves the PRICE
# path can't see trends in coin flips. The bars themselves are the noise:
# a fixed base series is circular-BLOCK-permuted per seed (short-range
# autocorrelation / vol texture preserved inside blocks, date-aligned
# structure destroyed — the same null philosophy as
# stat_gates.block_permutation_null), then the REAL src.simulator
# .run_simulation proposes and resolves trades on it. Window A's organic
# sim outcomes are mined (corpus="sim"); every candidate is then judged on
# window B's ORGANIC evidence against its FAMILY's own window-B base rate.
#
# The v2-specific regression: the sim corpus wins far above 50% doing
# nothing clever (decision #65 measured ~79% — structural generosity of
# defined-risk spreads + synthetic pricing, not edge). Judged against a
# breakeven/50% null, every noise pattern would ride that straight to
# VALIDATED; judged against its family's own measured window-B rate, it
# can't. This suite is what makes reintroducing that mistake loud.
#
# Real-evidence note (locked policy): promotable() requires >= 1 REAL
# resolution, so each pattern's matched window-B sim outcomes are mirrored
# as resolved shadow fires — the real stratum, still 100% organic
# simulator output on noise bars; nothing is fabricated at a chosen rate.

BAR_DAYS = 380            # 201 warmup + ~2 windows + resolution buffer
BAR_BLOCK = 5             # block length in trading days
_BASE_BAR_SEED = 990001   # ONE fixed base series; per-seed noise = its
                          # block arrangement (identical return marginals
                          # across seeds — differences are pure order)
BAR_UNDERLYINGS = ("NIFTY 50", "NIFTY BANK")


def generate_base_bars(n: int = BAR_DAYS, anchor: float = 22000.0,
                       seed: int = _BASE_BAR_SEED,
                       drift_cycle: int = 120) -> list:
    """The deterministic base series: alternating up/down drift regimes
    (so SMA50/200 crosses — hence proposals — exist to permute) + gaussian
    noise, as (date, low, high, close) 4-tuples on a weekday axis."""
    rng = random.Random(seed)
    days = _weekdays(date(2025, 1, 6), n)
    bars, close = [], float(anchor)
    for i, day in enumerate(days):
        drift = 0.0012 if (i // drift_cycle) % 2 == 0 else -0.0012
        close = max(1000.0, close * (1.0 + drift + rng.gauss(0, 0.008)))
        lo = close * (1.0 - abs(rng.gauss(0, 0.004)))
        hi = close * (1.0 + abs(rng.gauss(0, 0.004)))
        bars.append((day, round(min(lo, close), 2), round(max(hi, close), 2),
                     round(close, 2)))
    return bars


def block_permute_bars(bars: list, seed: int, block: int = BAR_BLOCK) -> list:
    """The per-seed null: keep each bar's SHAPE (return, low/close,
    high/close) intact, shuffle BLOCKS of consecutive shapes, recompound
    from the anchor onto the ORIGINAL date axis. Marginals (the multiset
    of returns) are preserved exactly; the date-aligned structure any
    pattern could have exploited is destroyed."""
    rng = random.Random(seed)
    days = [b[0] for b in bars]
    shapes, prev = [], bars[0][3]
    for _, lo, hi, c in bars[1:]:
        shapes.append((c / prev, lo / c, hi / c))
        prev = c
    blocks = [shapes[i:i + block] for i in range(0, len(shapes), block)]
    order = list(range(len(blocks)))
    rng.shuffle(order)
    shuffled = [shape for bi in order for shape in blocks[bi]]
    # Closes stay UNROUNDED so the return multiset survives the round-trip
    # exactly (rounding while compounding perturbs marginals ~1e-7 — the
    # simulator consumes floats either way).
    out, close = [bars[0]], float(bars[0][3])
    for day, (ret, lo_r, hi_r) in zip(days[1:], shuffled):
        close *= ret
        out.append((day, close * lo_r, close * hi_r, close))
    return out


def generate_vix_by_date(days: list, seed: int) -> dict:
    """A mean-reverting VIX path inside 11..19.5 — realistic enough that
    the proposer's IV routing (condor band 13-16, hard gate >16) sees all
    its regimes. Shuffled independently of the bars, so any VIX-price
    alignment is destroyed (part of the null)."""
    rng = random.Random(seed * 7 + 3)
    v, out = 14.0, {}
    for d in days:
        v = min(19.5, max(11.0, v + rng.gauss(0, 0.6) + 0.15 * (14.0 - v)))
        out[d] = round(v, 2)
    return out


def _matched_sim_evidence(conn, tags: list, windows: dict) -> list:
    """Window-B sim outcomes whose linked event tags cover the pattern's
    tag-set (ctx: tags joined from the day's frame, mirroring the miner's
    transaction items). Keyed by simulated_trades.proposed_on — market
    date. Returns the matched rows [{ref, proposed_on, ticker, strategy,
    result}, ...]."""
    from src.discovery.cooccurrence_miner import context_tags
    frames = {r["date"]: r for r in conn.execute("SELECT * FROM daily_context")}
    rows = conn.execute(
        """
        SELECT s.journal_ref AS ref, s.proposed_on, s.strategy, s.result,
               o.ticker, o.date AS outcome_date,
               GROUP_CONCAT(DISTINCT e.tag) AS tags
        FROM simulated_trades s
        JOIN outcomes o ON o.journal_ref = s.journal_ref
        LEFT JOIN event_outcome_link l ON l.outcome_id = o.id
        LEFT JOIN events e ON e.id = l.event_id
        GROUP BY s.journal_ref
        """).fetchall()
    wanted, matched = set(tags), []
    for r in rows:
        if not trial.in_validation(r["proposed_on"], windows):
            continue
        items = {t for t in (r["tags"] or "").split(",") if t}
        items |= context_tags(frames.get(r["outcome_date"]))
        if wanted <= items:
            matched.append({"ref": r["ref"], "proposed_on": r["proposed_on"],
                            "ticker": r["ticker"], "strategy": r["strategy"],
                            "result": r["result"]})
    return matched


def _family_rate(conn, windows: dict, strategy: str = None) -> dict:
    """The matched family's own window-B base rate — THE null a pattern
    must beat (never 50%/breakeven: the sim corpus wins ~79% doing nothing
    clever, decision #65). No strategy tag -> the global window-B rate."""
    ev = trial.sim_evidence_in_window(conn, windows, strategy=strategy)
    return {"n": ev["n"], "wins": ev["wins"],
            "rate": (ev["wins"] / ev["n"]) if ev["n"] else None}


def run_bars_seed(seed: int, forced_candidates: int = 4) -> dict:
    """One full price-path pass: permute bars -> run the REAL simulator
    over window A -> mine the sim corpus -> (force extra candidates) ->
    run the simulator over window B -> judge every candidate's ORGANIC
    matched evidence against its family's window-B base rate. Isolated in
    a throwaway temp DB like v1. Returns the seed's counts."""
    rng = random.Random(seed)
    base = generate_base_bars()
    strategies = set()

    with tempfile.TemporaryDirectory(prefix="noise_bars_") as tmp:
        conn = _temp_conn(tmp)
        try:
            bars_by_u = {u: block_permute_bars(base, seed * 31 + i)
                         for i, u in enumerate(BAR_UNDERLYINGS)}
            all_days = [b[0] for b in base]
            vix_by_date = generate_vix_by_date(all_days, seed)
            # Frames for every bar day: the miner's ctx: tag surface + the
            # outcome-date join both stay realistic.
            for day in all_days:
                v = vix_by_date[day]
                conn.execute(
                    "INSERT OR REPLACE INTO daily_context (date, vix, "
                    "vix_band, fii_net, dii_net, news_net, deals_buy_legs, "
                    "deals_sell_legs, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (day, v, ("low" if v < 13 else "mid" if v <= 16 else "high"),
                     round(rng.uniform(-2000, 2000), 1),
                     round(rng.uniform(-2000, 2000), 1),
                     rng.randint(-4, 4), rng.randint(0, 9), rng.randint(0, 9),
                     "{}"))
            conn.commit()

            # Sim range = everything after the 201-close warmup, minus a
            # resolution buffer at the end (positions must be able to
            # resolve inside the bar series).
            sim_days = all_days[201:-40]
            windows = trial.split_windows(sim_days)

            from src.simulator import run_simulation
            # ---- window A (discovery): organic trades, then mine ----
            a = run_simulation(sim_days[0], windows["discovery_end"],
                               BAR_UNDERLYINGS, conn=conn,
                               bars_by_underlying=bars_by_u,
                               vix_by_date=vix_by_date)
            from src.discovery import cooccurrence_miner, sequence_miner
            co = cooccurrence_miner.run(conn=conn, corpus="sim",
                                        today=date.fromisoformat(sim_days[-1]))
            sq = sequence_miner.run(conn=conn, corpus="sim",
                                    today=date.fromisoformat(sim_days[-1]))
            mined_ids = [r["pattern_id"] for r in
                         (co.get("registered") or []) +
                         (sq.get("registered") or [])]

            # ---- forced candidates: CO-OCCURRING window-A (strategy, view)
            # pairs — real tag-sets sim outcomes actually carry, so matched
            # window-B evidence exists and the trial gate is exercised ----
            strategies = {r["strategy"] for r in conn.execute(
                "SELECT DISTINCT strategy FROM simulated_trades")}
            combos = [dict(r) for r in conn.execute(
                "SELECT DISTINCT strategy, view FROM simulated_trades")]
            forced_ids = []
            for i in range(min(forced_candidates, 2 * len(combos))):
                c = combos[i % len(combos)]
                # first pass: {strategy, view} pairs; second: {strategy} alone
                tags = (sorted({c["strategy"], c["view"] or c["strategy"]})
                        if i < len(combos) else [c["strategy"]])
                res = rg.register(conn, "cooccurrence",
                                  {"kind": "cooccurrence", "tags": tags,
                                   "forced": i},
                                  description=f"[bars-noise forced] {tags}",
                                  mining_run=f"noise:bars:{seed}")
                forced_ids.append(res["pattern_id"])

            # ---- window B (validation): organic out-of-sample trades ----
            b = run_simulation(windows["validation_start"], sim_days[-1],
                               BAR_UNDERLYINGS, conn=conn,
                               bars_by_underlying=bars_by_u,
                               vix_by_date=vix_by_date)

            promoted, verdicts = [], []
            for pid in mined_ids + forced_ids:
                row = rg.get(conn, pid)
                tags = list(json.loads(row["definition"]).get("tags") or [])
                matched = _matched_sim_evidence(conn, tags, windows)
                strat = next((t for t in tags if t in strategies), None)
                family = _family_rate(conn, windows, strategy=strat)
                if family["rate"] is None or not matched:
                    verdicts.append({"pattern_id": pid,
                                     "final_status": row["status"],
                                     "reason": "no window-B family baseline "
                                               "or no matched evidence"})
                    continue
                # Mirror the ORGANIC matched outcomes as the real stratum
                # (locked policy needs >= 1 real resolution) — outcomes are
                # the simulator's own, never drawn at a chosen rate.
                for m in matched:
                    ref = trial.record_shadow_fire(
                        conn, pid, m["proposed_on"],
                        f"{m['ticker']}#{m['ref']}")["ref"]
                    trial.resolve_shadow(conn, ref, m["result"],
                                         1.0 if m["result"] == "win" else -1.0,
                                         m["proposed_on"])
                verdict = trial.evaluate_trial(conn, pid, windows,
                                               base_rate=family["rate"])
                verdicts.append({"pattern_id": pid,
                                 "final_status": verdict["final_status"],
                                 "matched_n": len(matched),
                                 "family": family,
                                 "reason": verdict["reason"]})
                if verdict["final_status"] in rg.CITABLE_STATES:
                    promoted.append(pid)
        finally:
            conn.close()

    return {"seed": seed,
            "resolved_a": a.get("resolved", a.get("recorded", 0)),
            "resolved_b": b.get("resolved", b.get("recorded", 0)),
            "mined_candidates": len(mined_ids),
            "forced_candidates": len(forced_ids),
            "candidates": len(mined_ids) + len(forced_ids),
            "promoted": len(promoted), "promoted_ids": promoted,
            "verdicts": verdicts}


def false_promotion_rate_bars(seeds: int = 4, start_seed: int = 0,
                              forced_candidates: int = 4) -> dict:
    """v2 aggregate: same binomial-bound assertion as v1, over the price
    path. Also reports total organic resolutions so a degenerate simulator
    (zero trades on permuted bars) fails the suite loudly instead of
    passing vacuously."""
    q = sg.configured_floors()["fdr_q"]
    total_candidates = total_promoted = total_resolved = 0
    per_seed = []
    for s in range(start_seed, start_seed + seeds):
        r = run_bars_seed(s, forced_candidates=forced_candidates)
        total_candidates += r["candidates"]
        total_promoted += r["promoted"]
        total_resolved += r["resolved_a"] + r["resolved_b"]
        per_seed.append({"seed": s, "candidates": r["candidates"],
                         "promoted": r["promoted"],
                         "resolved": r["resolved_a"] + r["resolved_b"]})
    bound = binom_upper_bound(total_candidates, q)
    return {"mode": "bars", "seeds": seeds, "fdr_q": q,
            "total_candidates": total_candidates,
            "total_promoted": total_promoted,
            "total_resolved_trades": total_resolved,
            "empirical_rate": (round(total_promoted / total_candidates, 4)
                               if total_candidates else 0.0),
            "bound": bound,
            "ok": total_promoted <= bound and total_resolved > 0,
            "per_seed": per_seed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="End-to-end false-discovery regression on pure noise")
    parser.add_argument("--seeds", type=int, default=None,
                        help="independent noise seeds (defaults: labels 25, "
                             "bars 4; nightly: 500 / 50)")
    parser.add_argument("--start-seed", type=int, default=0)
    parser.add_argument("--mode", choices=("labels", "bars"),
                        default="labels",
                        help="labels = v1 tag path; bars = v2 price path "
                             "(block-permuted bars through run_simulation)")
    args = parser.parse_args()

    if args.mode == "bars":
        result = false_promotion_rate_bars(
            seeds=args.seeds if args.seeds is not None else 4,
            start_seed=args.start_seed)
        print(json.dumps(result, indent=2))
        print(f"\n{'PASS' if result['ok'] else 'FAIL'} — "
              f"{result['total_promoted']}/{result['total_candidates']} "
              f"noise promotions on the PRICE path (bound {result['bound']} "
              f"at q={result['fdr_q']}); "
              f"{result['total_resolved_trades']} organic sim resolutions")
        raise SystemExit(0 if result["ok"] else 1)

    control = plant_edge()
    result = false_promotion_rate(
        seeds=args.seeds if args.seeds is not None else 25,
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
