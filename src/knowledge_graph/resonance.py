"""
src/knowledge_graph/resonance.py — portfolio resonance & risk mitigation
========================================================================

Phase 7's cross-referencing brain: takes a parsed news signal
(src/ingestion/news_parser frame, optional) plus the multi-horizon macro
matrix (src/ingestion/macro_tracker) and evaluates them against every OPEN
paper position in data/journal.jsonl. For each position it answers one
question — does the incoming macro/news flow fight the trade, validate
it, or neither? — and emits one explicit advisory payload:

    CONFLICT    market dynamics actively oppose the trade -> advisory to
                review for an immediate exit / cut-loss; if the LONG
                horizon still favors the thesis, a roll-to-further-expiry
                advisory rides along (escape the near-term squeeze, keep
                the structural view).
    RESONANCE   macro vectors validate the trade -> advisory to extend
                targets, plus (for spreads) a concrete strike-roll
                suggestion computed from the position's own legs.
    NEUTRAL     no material overlap.

How the math works, per position:
  * direction: bear_* spreads -1, bull_* spreads +1, equity swings +1
    (long-only book), non-directional structures (condor/straddle/...) 0.
  * per-horizon market bias = clamp(macro index impact + EVENT_WEIGHT x
    event bias x confidence). Index positions read their own impact row;
    single stocks inherit the NIFTY 50 row damped by STOCK_INDEX_BETA.
    A macro-entity event (CRUDE/USDINR/GOLD_*) is translated onto the
    underlying through the same INDEX_IMPACT_WEIGHTS the tracker uses.
  * the three horizons are blended by the position's OWN clock — days to
    expiry decide whether the SHORT, MEDIUM, or LONG read dominates — and
    the blend times direction is the alignment score the verdict
    thresholds cut on.

SAFETY GUARANTEES (the reason this lives in its own package):
  * ADVISORY ONLY — no order, no journal write, no auto-exit. Payloads
    go back to the caller; `log_advisories` appends them to a plain
    side-file in logs/ that nothing in the execution loop reads.
  * Journal access is a pure file stream (journal.read_all / injected
    entries) and brain_map.db is opened strictly read-only
    (`file:...?mode=ro`) — zero lock contention with the main loop, ever.

Manual check:  python3 -m src.knowledge_graph.resonance
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import brain_map, journal, positions
from src.ingestion.macro_tracker import (HORIZONS, IMPACT_INDEXES,
                                         INDEX_IMPACT_WEIGHTS, MACRO_METRICS)
from src.ingestion.news_parser import canonicalize_entity

ROOT = Path(__file__).resolve().parent.parent.parent
ADVISORY_LOG_PATH = ROOT / "logs" / "resonance_advisories.jsonl"

IST = timezone(timedelta(hours=5, minutes=30))

# ------------------------------------------------------ system parameters

# |alignment| at or beyond these cuts the verdict; between them is NEUTRAL.
CONFLICT_THRESHOLD = 0.25
RESONANCE_THRESHOLD = 0.25

# A parsed event is one headline; the macro matrix is the whole tape.
# Weight the event below parity so a single headline can tilt but not
# single-handedly overrule a full macro read.
EVENT_WEIGHT = 0.75

# Single stocks inherit the broad-market (NIFTY 50) macro impact damped by
# a beta proxy — macro flow moves the index harder than any one name.
STOCK_INDEX_BETA = 0.7

# Minimum long-horizon agreement for the "thesis intact, roll the expiry"
# advisory to attach to a CONFLICT verdict.
ALIGN_EPS = 0.05

# How the three horizons blend, keyed by the position's remaining life:
# a spread dying this week lives on the SHORT read; a far-dated one on
# the structural read. Equity swings are time-stopped in days (see
# config plan_max_days), so they sit near the short end.
_EXPIRY_PROFILES = (
    (10, {"SHORT": 0.60, "MEDIUM": 0.30, "LONG": 0.10}),
    (35, {"SHORT": 0.30, "MEDIUM": 0.50, "LONG": 0.20}),
)
_FAR_PROFILE = {"SHORT": 0.20, "MEDIUM": 0.40, "LONG": 0.40}
_EQUITY_PROFILE = {"SHORT": 0.50, "MEDIUM": 0.35, "LONG": 0.15}

# Strategy-name fragments that mean "no directional thesis to resonate
# with" — verdict is always NEUTRAL for these.
_NON_DIRECTIONAL = ("condor", "straddle", "strangle", "butterfly", "calendar")


# ------------------------------------------------------ graph safety guard

def _read_only_connection(db_path=None) -> sqlite3.Connection:
    """A strictly read-only SQLite handle to the Brain Map. mode=ro makes
    the OS enforce what this module promises: no write, no lock that the
    main execution loop could ever collide with. Raises sqlite3.Error if
    the file doesn't exist (read-only mode never creates)."""
    db_path = Path(db_path) if db_path is not None else brain_map.DEFAULT_DB_PATH
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _graph_context(tags, db_path=None, conn=None) -> dict | None:
    """Historical color from the knowledge graph, read-only: across every
    outcome linked to an event carrying any of `tags`, how often did the
    trade pay? {tags, count, win_rate} or None when the graph is absent,
    empty for these tags, or unreadable — enrichment only, never load-
    bearing, never raising.

    Pass an open read-only `conn` to reuse it across calls (the portfolio
    sweep queries once per DISTINCT strategy, not once per position);
    without one, a connection is opened and closed per call."""
    tags = sorted({brain_map._normalize_tag(t) for t in tags or [] if t}
                  - {""})
    if not tags:
        return None
    owns = conn is None
    if owns:
        try:
            conn = _read_only_connection(db_path)
        except sqlite3.Error:
            return None
    try:
        marks = ",".join("?" * len(tags))
        rows = conn.execute(
            f"""SELECT DISTINCT o.id, o.result
                  FROM outcomes o
                  JOIN event_outcome_link l ON l.outcome_id = o.id
                  JOIN events e ON e.id = l.event_id
                 WHERE e.tag IN ({marks})""", tags).fetchall()
    except sqlite3.Error:
        return None
    finally:
        if owns:
            conn.close()
    if not rows:
        return None
    wins = sum(1 for r in rows if r["result"] == "win")
    return {"tags": tags, "count": len(rows),
            "win_rate": round(wins / len(rows), 3)}


# ------------------------------------------------------------- position math

def _position_direction(pos: dict) -> int:
    """-1 bearish thesis, +1 bullish, 0 non-directional/unknown."""
    strategy = str(pos.get("strategy") or "").lower()
    if any(frag in strategy for frag in _NON_DIRECTIONAL):
        return 0
    if "bear" in strategy:
        return -1
    if "bull" in strategy:
        return 1
    if pos.get("kind") == "equity":
        return 1   # the swing book is long-only by design
    return 0


def _underlying_bucket(ticker) -> str | None:
    """A position's ticker -> the same canonical vocabulary events use,
    so 'NIFTY'/'NIFTY 50'/'^NSEI' and 'TCS'/'TCS.NS' all collide."""
    return canonicalize_entity(ticker)


def _days_to_expiry(expiry, today: date) -> int | None:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - today).days
    except (TypeError, ValueError):
        return None


def _blend_weights(pos: dict, today: date) -> tuple:
    """(weights dict, days_to_expiry) — which horizon owns this position."""
    if pos.get("kind") != "spread":
        return dict(_EQUITY_PROFILE), None
    days = _days_to_expiry(pos.get("expiry"), today)
    if days is None:
        return dict(_EQUITY_PROFILE), None
    for limit, profile in _EXPIRY_PROFILES:
        if days <= limit:
            return dict(profile), days
    return dict(_FAR_PROFILE), days


def _macro_scores(bucket: str, macro_matrix) -> dict:
    """Per-horizon macro bias for this underlying, from the matrix's
    index_impact block. Missing matrix/blocks contribute zero."""
    zeros = {h: 0.0 for h in HORIZONS}
    if not isinstance(macro_matrix, dict) or bucket is None:
        return zeros
    impact = macro_matrix.get("index_impact") or {}
    row = impact.get(bucket)
    damp = 1.0
    if row is None:
        row = impact.get("NIFTY 50")   # stocks ride broad-market flow…
        damp = STOCK_INDEX_BETA        # …at less than index beta
    if not isinstance(row, dict):
        return zeros
    out = {}
    for h in HORIZONS:
        try:
            out[h] = float(row.get(h, 0.0)) * damp
        except (TypeError, ValueError):
            out[h] = 0.0
    return out


def _event_scores(bucket: str, parsed_event) -> dict:
    """Per-horizon bias this ONE event contributes to this underlying:
    full strength on a direct entity match, translated through the
    tracker's impact weights when the event targets a macro variable,
    zero otherwise. Strength = directional_bias x confidence_score,
    landing only on the event's own horizon."""
    zeros = {h: 0.0 for h in HORIZONS}
    if not isinstance(parsed_event, dict) or bucket is None:
        return zeros
    entity = canonicalize_entity(parsed_event.get("target_entity"))
    horizon = parsed_event.get("horizon_impact")
    if entity is None or horizon not in HORIZONS:
        return zeros
    try:
        strength = (float(parsed_event.get("directional_bias", 0.0))
                    * float(parsed_event.get("confidence_score", 0.0)))
    except (TypeError, ValueError):
        return zeros
    if strength == 0.0:
        return zeros
    scores = dict(zeros)
    if entity == bucket:
        scores[horizon] = strength
    elif entity in MACRO_METRICS:
        weights = INDEX_IMPACT_WEIGHTS.get(entity, {})
        if bucket in IMPACT_INDEXES:
            weight = weights.get(bucket, 0.0)
        else:
            weight = weights.get("NIFTY 50", 0.0) * STOCK_INDEX_BETA
        scores[horizon] = strength * weight
    return scores


def _suggest_strike_roll(legs, direction: int) -> dict | None:
    """For a directional spread in RESONANCE: shift every leg one full
    spread-width WITH the confirmed move (down for bearish, up for
    bullish) so the structure keeps chasing the validated thesis. Pure
    arithmetic on the position's own legs; None when strikes are
    missing/degenerate."""
    strikes = []
    for leg in legs or []:
        try:
            strikes.append(float(leg.get("strike")))
        except (TypeError, ValueError):
            return None
    if len(set(strikes)) < 2 or direction == 0:
        return None
    width = max(strikes) - min(strikes)
    shift = width if direction > 0 else -width
    return {"from_strikes": strikes,
            "to_strikes": [round(s + shift, 2) for s in strikes],
            "strike_width": round(width, 2)}


# ------------------------------------------------------------- the verdict

def _advisory_for(pos: dict, verdict: str, alignment: float,
                  combined: dict, direction: int, days_left,
                  legs) -> tuple:
    """(advisory text, actions list, suggested_adjustment dict|None)."""
    label = f"{pos.get('strategy') or pos.get('kind')} on {pos.get('ticker')}"
    if direction == 0:
        return (f"NEUTRAL: {label} is non-directional — directional "
                "resonance does not apply.", [], None)
    if verdict == "CONFLICT":
        text = (f"CONFLICT: macro/news flow actively opposes {label} "
                f"(alignment {alignment:+.2f}). Review for an immediate "
                "exit / cut-loss — the position is fighting the tape.")
        actions = ["EXIT_ADVISORY"]
        adjustment = None
        if pos.get("kind") == "spread" and combined["LONG"] * direction > ALIGN_EPS:
            actions.append("ROLL_EXPIRY_ADVISORY")
            adjustment = {
                "type": "roll_expiry",
                "current_expiry": pos.get("expiry"),
                "hint": ("the LONG-horizon read still favors this thesis — "
                         "consider closing here and re-establishing at a "
                         "further expiry instead of holding through the "
                         "near-term squeeze"),
            }
            text += (" Structural (LONG) trend still agrees with the "
                     "thesis: a roll to a further expiry is the "
                     "alternative to a flat exit.")
        return text, actions, adjustment
    if verdict == "RESONANCE":
        text = (f"RESONANCE: macro vectors validate {label} (alignment "
                f"{alignment:+.2f}). Consider extending targets while the "
                "flow supports the move.")
        actions = ["EXTEND_TARGET_ADVISORY"]
        adjustment = None
        if pos.get("kind") == "spread":
            roll = _suggest_strike_roll(legs, direction)
            if roll is not None:
                actions.append("ROLL_STRIKE_ADVISORY")
                adjustment = {"type": "roll_strikes", **roll}
                slower = (combined["MEDIUM"] + combined["LONG"]) * direction
                if days_left is not None and slower > combined["SHORT"] * direction:
                    adjustment["expiry_hint"] = (
                        "MEDIUM/LONG horizons carry the move — a further "
                        "expiry captures more of it than the current one")
                text += (" Strike roll one width with the move: "
                         f"{roll['from_strikes']} -> {roll['to_strikes']}.")
        return text, actions, adjustment
    return (f"NEUTRAL: no material overlap between the incoming flow and "
            f"{label} (alignment {alignment:+.2f}).", [], None)


def evaluate_portfolio_resonance(parsed_event, macro_matrix,
                                 entries: list = None, today: date = None,
                                 db_path=None) -> list:
    """The Phase 7 core: one advisory payload per OPEN paper position.

    `parsed_event` is a news_parser frame or None (macro-only sweep);
    `macro_matrix` is macro_tracker.build_macro_matrix() output or None;
    `entries`/`today` are injectable for offline tests (default: a plain
    file-stream read of data/journal.jsonl — no DB, no locks); `db_path`
    points the read-only graph enrichment somewhere else in tests.

    Returns a list of dicts, newest position first (positions.active_
    positions order), each carrying verdict / alignment / per-horizon
    scores / advisory text / actions / suggested_adjustment. Read-only
    top to bottom: this function writes NOTHING."""
    today = today or datetime.now(IST).date()
    if entries is None:
        entries = journal.read_all()
    open_positions = positions.active_positions(entries=entries, today=today)

    # Legs live on the raw journal entry, not the positions view — keep a
    # side lookup so strike-roll advisories can do real arithmetic.
    legs_by_id = {e.get("short_id"): (e.get("spread") or {}).get("legs")
                  for e in entries
                  if e.get("spread") and e.get("short_id")}

    event_tag = (parsed_event or {}).get("event_classification")
    # One read-only graph connection for the whole sweep, and one query
    # per DISTINCT strategy (positions sharing a strategy produce the
    # identical tag-set) — not one connection + query per position.
    graph_conn = None
    try:
        graph_conn = _read_only_connection(db_path)
    except sqlite3.Error:
        graph_conn = None
    context_by_strategy: dict = {}
    payloads = []
    for pos in open_positions:
        bucket = _underlying_bucket(pos.get("ticker"))
        direction = _position_direction(pos)
        weights, days_left = _blend_weights(pos, today)
        macro = _macro_scores(bucket, macro_matrix)
        event = _event_scores(bucket, parsed_event)
        combined = {h: max(-1.0, min(1.0, macro[h] + EVENT_WEIGHT * event[h]))
                    for h in HORIZONS}
        alignment = round(sum(weights[h] * combined[h] for h in HORIZONS)
                          * direction, 3)

        if direction == 0:
            verdict = "NEUTRAL"
        elif alignment <= -CONFLICT_THRESHOLD:
            verdict = "CONFLICT"
        elif alignment >= RESONANCE_THRESHOLD:
            verdict = "RESONANCE"
        else:
            verdict = "NEUTRAL"

        legs = legs_by_id.get(pos.get("trade_id"))
        advisory, actions, adjustment = _advisory_for(
            pos, verdict, alignment, combined, direction, days_left, legs)

        strategy_key = pos.get("strategy")
        if strategy_key not in context_by_strategy:
            context_by_strategy[strategy_key] = (
                _graph_context([event_tag, strategy_key], conn=graph_conn)
                if graph_conn is not None else None)
        context = context_by_strategy[strategy_key]
        if context is not None:
            advisory += (f" (Graph: {context['count']} linked outcome(s) "
                         f"for {'/'.join(context['tags'])}, win rate "
                         f"{context['win_rate']:.0%}.)")

        payloads.append({
            "trade_id": pos.get("trade_id"),
            "ticker": pos.get("ticker"),
            "strategy": pos.get("strategy"),
            "kind": pos.get("kind"),
            "direction": direction,
            "days_to_expiry": days_left,
            "verdict": verdict,
            "alignment": alignment,
            "horizon_scores": {h: round(combined[h], 3) for h in HORIZONS},
            "blend_weights": weights,
            "advisory": advisory,
            "actions": actions,
            "suggested_adjustment": adjustment,
            "graph_context": context,
            "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        })
    if graph_conn is not None:
        graph_conn.close()
    return payloads


# --------------------------------------------------------------- side-file

def log_advisories(payloads: list, path=None) -> Path:
    """Append advisory payloads to the resonance side-file (one JSON line
    each) — a plain append to logs/, read by humans and dashboards only,
    never by the execution loop. Returns the path written."""
    path = Path(path) if path is not None else ADVISORY_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for payload in payloads or []:
            f.write(json.dumps(payload) + "\n")
    return path


if __name__ == "__main__":
    # Manual sweep: current macro matrix vs the live paper book, advisory
    # lines printed and appended to logs/ — still zero writes anywhere else.
    from src.ingestion.macro_tracker import build_macro_matrix
    matrix = build_macro_matrix()
    results = evaluate_portfolio_resonance(None, matrix)
    if not results:
        print("No open paper positions to evaluate.")
    for r in results:
        print(f"[{r['verdict']:>9}] {r['ticker']} {r['strategy']} "
              f"(align {r['alignment']:+.2f}) — {r['advisory']}")
    if results:
        print(f"\nLogged {len(results)} advisory line(s) to "
              f"{log_advisories(results)}")
