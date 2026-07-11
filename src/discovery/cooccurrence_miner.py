"""
src/discovery/cooccurrence_miner.py — the frequent-itemset miner
================================================================

Phase 5 of docs/HOLY_GRAIL_PLAN.md (§8.1). The first miner: it looks across
every resolved outcome for TAG CO-OCCURRENCES that pay more than their
stratum's base rate — "golden_cross + fii-selling on a CALM-vol day" as a
cluster, not three hand-coded rules. It ENUMERATES; it never surfaces.
Everything it finds is registered as a CANDIDATE (src/validation/registry.py)
that must still clear the proving harness before any card cites it.

The honesty rails (the panel), all delegated to src/validation/stat_gates.py
so nothing is loosened inline:

  * REAL and SIMULATED corpora are mined SEPARATELY. A tag-set that only
    lights up in simulator replays is a different (weaker) hypothesis than
    one seen in real resolutions — never pooled. Corpus membership is the
    is_learnable_ref split (sim:/shadow:/trial:/placebo: -> sim corpus).
  * STRATIFIED base rate. An itemset's win-rate is tested against the
    blended base rate of the STRATA (underlying × vix-band) its own
    transactions live in — not the global rate. This is what stops the
    miner rediscovering the pipeline's own gates: "{vix:CALM}" can't look
    special because CALM transactions already carry a high stratum base
    rate, so observed ≈ expected there.
  * SUPPORT FLOOR. Below MIN_SUPPORT_ITEMSET occurrences an itemset is
    noise and is never even tested (configured_floors()).
  * FDR across the WHOLE batch. Every itemset that cleared the support
    floor is one hypothesis in the Benjamini-Hochberg denominator — you
    cannot correct for tests you didn't count.
  * auto: tags are excluded (is_minable_tag) — the system never mines its
    own validated hypotheses back into new ones (tautology guard).

Near-zero survivors on thin data is the CORRECT output, reported as such —
this miner is deliberately NOT wired into the nightly sleep phase yet
(panel: defer until enough live daily_context rows have accrued). It is
run/tested standalone.

Manual:  python3 -m src.discovery.cooccurrence_miner        (real corpus)
         python3 -m src.discovery.cooccurrence_miner sim    (sim corpus)
"""

import itertools
import json
from datetime import date

from src import daily_context as dc
from src.validation import registry as rg
from src.validation import stat_gates as sg

MAX_ITEMSET_LEN = 3
# A stratum needs at least this many transactions before its own win-rate
# is trusted as the base rate; thinner strata fall back to the global rate
# (a five-trade "CALM RELIANCE" cell is too noisy to be its own null).
MIN_STRATUM_N = 8


# ----------------------------------------------------- context -> tags

def context_tags(row) -> set:
    """One daily_context row -> the discrete tags a transaction inherits
    from its market day. NULL-honest (#50): an absent reading contributes
    NO tag, never a guessed-neutral one. All tags are `ctx:`-namespaced so
    they can never collide with an outcome's own event tags."""
    if row is None:
        return set()
    g = row.get if isinstance(row, dict) else (lambda k, d=None: row[k])
    tags = set()

    def sign_tag(key, name):
        v = g(key, None)
        if v is None:
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v > 0:
            tags.add(f"ctx:{name}:up")
        elif v < 0:
            tags.add(f"ctx:{name}:down")

    band = g("vix_band", None)
    if band:
        tags.add(f"ctx:vix:{band}")
    sign_tag("fii_net", "fii")
    sign_tag("dii_net", "dii")
    sign_tag("news_net", "news")
    sign_tag("macro_nifty_short", "macro_nifty")
    sign_tag("macro_bank_short", "macro_bank")

    buy, sell = g("deals_buy_legs", None), g("deals_sell_legs", None)
    if buy is not None and sell is not None:
        if buy > sell:
            tags.add("ctx:deals:net_buy")
        elif sell > buy:
            tags.add("ctx:deals:net_sell")

    dist = g("affinity_distribution", None)
    acc = g("affinity_accumulation", None)
    if dist is not None and acc is not None:
        if dist > acc:
            tags.add("ctx:affinity:distribution")
        elif acc > dist:
            tags.add("ctx:affinity:accumulation")

    return tags


# --------------------------------------------------- transactions

def build_transactions(conn, corpus: str = "real") -> list:
    """One transaction per resolved outcome in the chosen corpus:
    {items: frozenset(tags), win: bool, stratum: (ticker, vix_band)}.

    `items` = the outcome's own minable event tags UNION the ctx: tags of
    its market day (joined on the outcome date via daily_context). The
    corpus split is the self-poisoning guard: `real` keeps only learnable
    refs; `sim` keeps only the excluded (sim:/shadow:/trial:/placebo:)
    ones — the two are mined apart and never pooled. Read-only."""
    dc.ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT o.id, o.journal_ref, o.date, o.ticker, o.result, o.regime_vix,
               GROUP_CONCAT(DISTINCT e.tag) AS tags
        FROM outcomes o
        LEFT JOIN event_outcome_link l ON l.outcome_id = o.id
        LEFT JOIN events e ON e.id = l.event_id
        GROUP BY o.id
        """
    ).fetchall()

    # Preload the day-frames we need once.
    frames = {r["date"]: r for r in conn.execute(
        "SELECT * FROM daily_context")}

    txns = []
    for r in rows:
        ref = r["journal_ref"]
        learnable = sg.is_learnable_ref(ref)
        if corpus == "real" and not learnable:
            continue
        if corpus == "sim" and learnable:
            continue
        own = {t for t in (r["tags"] or "").split(",")
               if t and sg.is_minable_tag(t)}
        frame = frames.get(r["date"])
        band = (frame["vix_band"] if frame else None) or r["regime_vix"]
        items = own | context_tags(frame)
        if not items:
            continue
        txns.append({"items": frozenset(items),
                     "win": r["result"] == "win",
                     "stratum": (r["ticker"], band)})
    return txns


# --------------------------------------------------- apriori

def frequent_itemsets(transactions: list, min_support: int,
                      max_len: int = MAX_ITEMSET_LEN) -> dict:
    """Pure Apriori. Returns {frozenset(items): support} for every itemset
    (size 1..max_len) meeting min_support. Size-1 sets are kept only to
    generate larger ones — the miner emits candidates from size >= 2."""
    item_sets = [t["items"] for t in transactions]
    # L1
    counts = {}
    for items in item_sets:
        for it in items:
            counts[it] = counts.get(it, 0) + 1
    frequent = {frozenset([it]): c for it, c in counts.items()
                if c >= min_support}
    result = dict(frequent)
    k = 2
    while frequent and k <= max_len:
        prev = list(frequent)
        # Candidate generation: union pairs of frequent (k-1)-sets whose
        # union has size k, then prune any with an infrequent (k-1)-subset.
        candidates = set()
        for i in range(len(prev)):
            for j in range(i + 1, len(prev)):
                union = prev[i] | prev[j]
                if len(union) != k:
                    continue
                if all(frozenset(sub) in frequent
                       for sub in itertools.combinations(union, k - 1)):
                    candidates.add(union)
        # Count support.
        counts = {c: 0 for c in candidates}
        for items in item_sets:
            for c in candidates:
                if c <= items:
                    counts[c] += 1
        frequent = {c: n for c, n in counts.items() if n >= min_support}
        result.update(frequent)
        k += 1
    return result


# --------------------------------------------------- stratified null

def stratum_base_rates(transactions: list,
                       min_stratum_n: int = MIN_STRATUM_N) -> tuple:
    """(base_rate_by_stratum, global_rate). A stratum thinner than
    min_stratum_n is untrusted and maps to the global rate, so its members
    are tested against the pooled null rather than their own noisy cell."""
    n = len(transactions)
    global_rate = (sum(1 for t in transactions if t["win"]) / n) if n else 0.0
    agg = {}
    for t in transactions:
        w, c = agg.get(t["stratum"], (0, 0))
        agg[t["stratum"]] = (w + (1 if t["win"] else 0), c + 1)
    rates = {}
    for stratum, (wins, cnt) in agg.items():
        rates[stratum] = (wins / cnt) if cnt >= min_stratum_n else global_rate
    return rates, global_rate


def _expected_rate(txns_with_itemset: list, rates: dict,
                   global_rate: float) -> float:
    """The itemset's stratified null: mean base rate of the strata its own
    supporting transactions live in."""
    if not txns_with_itemset:
        return global_rate
    return sum(rates.get(t["stratum"], global_rate)
               for t in txns_with_itemset) / len(txns_with_itemset)


# --------------------------------------------------- the mine

def mine(transactions: list, min_support: int = None,
         fdr_q: float = None, max_len: int = MAX_ITEMSET_LEN) -> list:
    """The pure core: transactions -> surviving candidate itemsets.

    For every frequent itemset of size >= 2, test its observed win-count
    against its STRATIFIED expected win-count (exact binomial), then apply
    Benjamini-Hochberg across the whole tested batch. A survivor must also
    beat its null directionally (observed rate > expected) — a
    significantly-WORSE cluster is real information but not a BUY candidate.
    Returns a list of dicts sorted strongest-first; never raises."""
    floors = sg.configured_floors()
    min_support = floors["min_support_itemset"] if min_support is None else min_support
    fdr_q = floors["fdr_q"] if fdr_q is None else fdr_q
    if not transactions:
        return []

    freq = frequent_itemsets(transactions, min_support, max_len)
    rates, global_rate = stratum_base_rates(transactions)

    tested = []
    for itemset, support in freq.items():
        if len(itemset) < 2:
            continue
        supporting = [t for t in transactions if itemset <= t["items"]]
        n = len(supporting)
        wins = sum(1 for t in supporting if t["win"])
        expected = _expected_rate(supporting, rates, global_rate)
        p = sg.binomial_p_two_sided(wins, n, expected)
        tested.append({
            "tags": sorted(itemset), "support": support, "n": n,
            "wins": wins, "win_rate": wins / n if n else 0.0,
            "expected_rate": round(expected, 4), "p_value": p,
            "lift": (wins / n - expected) if n else 0.0,
        })

    if not tested:
        return []
    survives = sg.benjamini_hochberg([c["p_value"] for c in tested], q=fdr_q)
    out = []
    for cand, ok in zip(tested, survives):
        cand["bh_survives"] = ok
        if ok and cand["lift"] > 0:
            out.append(cand)
    out.sort(key=lambda c: (-c["lift"], c["p_value"]))
    return out


# --------------------------------------------------- registration

def _definition(cand: dict) -> dict:
    """The FROZEN predicate: just the sorted tag-set. Two mining runs that
    surface the same tag cluster mint the SAME pattern_id (idempotent
    re-discovery), so a dead cluster is never re-litigated."""
    return {"kind": "cooccurrence", "tags": cand["tags"]}


def register_survivors(conn, survivors: list, corpus: str,
                       run_id: str = "", window: str = "") -> list:
    """Register each surviving itemset as a CANDIDATE. Idempotent on the
    frozen definition — re-running the miner adds nothing new for clusters
    already known (including DEAD ones). Returns the register() results."""
    results = []
    for cand in survivors:
        desc = (f"[{corpus}] " + " + ".join(cand["tags"]) +
                f"  ({cand['wins']}/{cand['n']} = {cand['win_rate']:.0%} vs "
                f"base {cand['expected_rate']:.0%})")
        res = rg.register(
            conn, "cooccurrence", _definition(cand), description=desc,
            mining_run=run_id or f"cooccurrence:{corpus}",
            discovery_window=window, support_n=cand["support"],
            fdr_q=sg.configured_floors()["fdr_q"],
            insample_stats={"corpus": corpus, "n": cand["n"],
                            "wins": cand["wins"],
                            "win_rate": round(cand["win_rate"], 4),
                            "expected_rate": cand["expected_rate"],
                            "p_value": cand["p_value"],
                            "lift": round(cand["lift"], 4)})
        results.append({**res, "tags": cand["tags"]})
    return results


def run(conn=None, db_path=None, corpus: str = "real",
        today: date = None) -> dict:
    """Mine one corpus and register survivors. Reuses a caller conn or
    opens its own. Returns {corpus, transactions, tested_or_survivors,
    survivors, registered}. Never raises."""
    own = conn is None
    if conn is None:
        from src import brain_map
        conn = brain_map.connect(db_path)
    today = today or date.today()
    try:
        txns = build_transactions(conn, corpus=corpus)
        survivors = mine(txns)
        registered = register_survivors(
            conn, survivors, corpus,
            run_id=f"cooccurrence:{corpus}:{today.isoformat()}",
            window=f"..{today.isoformat()}")
        new = sum(1 for r in registered if r.get("created"))
    finally:
        if own:
            conn.close()
    summary = {"corpus": corpus, "transactions": len(txns),
               "survivors": len(survivors), "newly_registered": new,
               "registered": registered}
    print(f"  (cooccurrence miner [{corpus}]: {len(txns)} txns -> "
          f"{len(survivors)} survivor(s), {new} newly registered)")
    return summary


if __name__ == "__main__":
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else "real"
    print(json.dumps(run(corpus=which), indent=2, default=str))
