"""
src/confluence/evidence.py — the Evidence Snapshot substrate
============================================================

Phase 2 of docs/HOLY_GRAIL_PLAN.md, the piece every other fusion idea is
blocked on. Today only regime and pattern tags are captured at proposal
time (decision #50); what macro, news, smart money, and flows were saying
at that moment is lost forever — so per-layer reliability can never be
learned retroactively. This module fixes the capture side: one canonical
Evidence record per layer, one snapshot builder, one persistence table.

THE CONTRACT (frozen like FEATURE_NAMES, decision #44's discipline):

    Evidence = {
        "layer":     "technical" | "news" | "macro" | "affinity" |
                     "flows" | "vix_regime",
        "direction": -1.0..+1.0   (signed stance; 0.0 only when abstained
                                   or genuinely neutral),
        "strength":  0.0..1.0     (how emphatic the layer is),
        "stance":    short human word ("bullish", "distribution", ...),
        "detail":    one provenance sentence a human can audit,
        "abstained": bool         (True = the layer had NOTHING to say —
                                   an explicit abstention, never a guessed
                                   neutral; #50's NULL-honesty rule),
    }

    Snapshot = {"as_of", "ticker", "days_to_results", "layers": [Evidence]}

Advisory-capture ONLY: nothing here scores, gates, or proposes — the
snapshot rides the journal entry (additive key, like created_at, decision
#52) and lands in an additive `evidence_snapshots` table keyed by
journal_ref at resolution time. Fail-open everywhere: any layer's reader
failing yields that layer's abstention, never an exception.

Manual check:  python3 -m src.confluence.evidence TICKER.NS
"""

import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

LAYERS = ("technical", "news", "macro", "affinity", "flows", "vix_regime")


def _evidence(layer, direction=0.0, strength=0.0, stance="abstain",
              detail="", abstained=False) -> dict:
    """Clamped, schema-true Evidence record."""
    return {
        "layer": layer,
        "direction": max(-1.0, min(1.0, round(float(direction), 3))),
        "strength": max(0.0, min(1.0, round(float(strength), 3))),
        "stance": str(stance),
        "detail": str(detail)[:200],
        "abstained": bool(abstained),
    }


def _abstain(layer, why) -> dict:
    return _evidence(layer, 0.0, 0.0, "abstain", why, abstained=True)


# ------------------------------------------------------------- adapters
# Each adapter: pure function of its layer's already-loaded artifact ->
# one Evidence record. Missing/stale/unknown input -> explicit abstention.

def technical_evidence(analysis: dict) -> dict:
    """From suggestions.analyze()'s {uptrend, fresh_cross, rsi, ...}."""
    if not isinstance(analysis, dict) or analysis.get("uptrend") is None:
        return _abstain("technical", "no analysis (insufficient history)")
    direction = 1.0 if analysis["uptrend"] else -1.0
    strength, notes = 0.4, ["uptrend" if analysis["uptrend"] else "downtrend"]
    if analysis.get("fresh_cross"):
        strength += 0.4
        notes.append("fresh cross")
    rsi = analysis.get("rsi")
    if rsi is not None:
        if rsi <= 30 or rsi >= 70:
            strength += 0.2
        notes.append(f"RSI {rsi:.0f}")
    stance = "bullish" if direction > 0 else "bearish"
    return _evidence("technical", direction, strength, stance,
                     "SMA 50/200 " + ", ".join(notes))


def news_evidence(entry: dict) -> dict:
    """From one data/news_sentiment.json ticker entry."""
    if not isinstance(entry, dict) or entry.get("stale", True):
        return _abstain("news", "no fresh sentiment read")
    score = entry.get("sentiment_score")
    if score is None or score == 0:
        return _abstain("news", "neutral/absent sentiment")
    direction = max(-1.0, min(1.0, score / 5.0))
    stance = "positive" if direction > 0 else "negative"
    return _evidence("news", direction, abs(direction), stance,
                     f"{stance} news — {entry.get('headline_focus', '')} "
                     f"({score:+d}/5)")


# Ticker -> the index whose macro impact proxies it. Single-name mapping
# is deliberately coarse until the sector map (Phase 3) lands: bank names
# ride NIFTY BANK, everything else NIFTY 50.
_BANK_HINTS = ("BANK", "FIN", "HDFC", "ICICI", "KOTAK", "SBIN", "AXIS")


def _macro_index_for(ticker: str) -> str:
    t = str(ticker or "").upper()
    if "NIFTY BANK" in t or (t.endswith(".NS")
                             and any(h in t for h in _BANK_HINTS)):
        return "NIFTY BANK"
    return "NIFTY 50"


def macro_evidence(matrix: dict, ticker: str) -> dict:
    """From macro_tracker.build_macro_matrix() for the ticker's proxy
    index. SHORT and MEDIUM horizons blended 60/40 — the swing window."""
    if not isinstance(matrix, dict) or matrix.get("source") in (None, "none"):
        return _abstain("macro", "macro matrix unavailable")
    index = _macro_index_for(ticker)
    impact = (matrix.get("index_impact") or {}).get(index) or {}
    short, medium = impact.get("SHORT"), impact.get("MEDIUM")
    if short is None and medium is None:
        return _abstain("macro", f"no {index} impact computed")
    blended = 0.6 * (short or 0.0) + 0.4 * (medium or 0.0)
    if abs(blended) < 0.05:
        return _evidence("macro", 0.0, 0.0, "neutral",
                         f"{index} macro bias flat "
                         f"(S {short}, M {medium})")
    stance = "tailwind" if blended > 0 else "headwind"
    return _evidence("macro", blended, abs(blended), stance,
                     f"{index} bias S {short} / M {medium} "
                     f"[{matrix.get('source')}]")


def affinity_evidence(readmodel: dict, ticker: str,
                      groups: dict = None) -> dict:
    """From data/entity_affinity.json for the ticker's promoter group.
    DISTRIBUTION = linked smart money unloading = bearish tell."""
    if groups is None:
        try:
            from src.knowledge_graph.entity_affinity import load_entity_groups
            groups = load_entity_groups()
        except Exception:
            groups = {"ticker_to_group": {}}
    grp = groups["ticker_to_group"].get(str(ticker or "").upper())
    if not grp:
        return _abstain("affinity", "ticker not in any tracked group")
    data = ((readmodel or {}).get("groups") or {}).get(grp)
    if not data:
        return _abstain("affinity", f"no affinity data for {grp}")
    bias = data.get("net_bias")
    movers = len([e for e in data.get("linked_entities", [])
                  if e.get("recent_direction") in ("accumulating",
                                                   "distributing")])
    if bias == "distribution":
        return _evidence("affinity", -1.0, min(1.0, 0.5 + 0.25 * movers),
                         "distribution",
                         f"{grp}: {movers} linked entity(ies) net selling")
    if bias == "accumulation":
        return _evidence("affinity", 1.0, min(1.0, 0.5 + 0.25 * movers),
                         "accumulation",
                         f"{grp}: {movers} linked entity(ies) net buying")
    return _abstain("affinity", f"{grp} net bias {bias or 'unknown'}")


def flows_evidence(flows: dict) -> dict:
    """From data/fii_dii_flows.json. FII net is the risk-appetite tell;
    scaled so ±3,000 cr saturates. DII net rides in the detail line."""
    fii = ((flows or {}).get("fii") or {}).get("net")
    if fii is None:
        return _abstain("flows", "no FII/DII read")
    direction = max(-1.0, min(1.0, fii / 3000.0))
    dii = ((flows or {}).get("dii") or {}).get("net")
    stance = ("fii_buying" if direction > 0.05 else
              "fii_selling" if direction < -0.05 else "neutral")
    return _evidence("flows", direction, abs(direction), stance,
                     f"FII net {fii:+.0f} cr, DII net "
                     f"{dii:+.0f} cr" if dii is not None
                     else f"FII net {fii:+.0f} cr")


def vix_evidence(vix) -> dict:
    """VIX regime as evidence: high VIX is a defined-risk headwind for the
    bullish playbook (and the #42 gate's vocabulary). Band edges from
    src/regime.py's locked 13/16 boundaries."""
    if vix is None:
        return _abstain("vix_regime", "VIX unavailable (regime unknown)")
    try:
        v = float(vix)
    except (TypeError, ValueError):
        return _abstain("vix_regime", "VIX unreadable")
    if v > 16:
        return _evidence("vix_regime", -1.0, min(1.0, (v - 16) / 8 + 0.5),
                         "high_vix", f"VIX {v:.1f} > 16 — breakout risk")
    if v < 13:
        return _evidence("vix_regime", 0.5, 0.4, "low_vix",
                         f"VIX {v:.1f} < 13 — calm regime")
    return _evidence("vix_regime", 0.0, 0.2, "mid_vix",
                     f"VIX {v:.1f} in 13-16 band")


# ------------------------------------------------------------- snapshot

def build_evidence_snapshot(ticker: str, today: date = None,
                            analysis: dict = None, news_entry: dict = None,
                            macro_matrix: dict = None,
                            affinity_readmodel: dict = None,
                            flows: dict = None, vix=None,
                            earnings_calendar: dict = None,
                            load_missing: bool = False) -> dict:
    """One capture of what EVERY layer said about `ticker` right now.

    Pure by default: layers not passed in are recorded as abstained (the
    caller captures what it actually consulted — what the human saw and
    what the system learns from stay byte-identical). With
    load_missing=True, absent layers are read from their standard local
    artifacts (never live network fetches — reading a file is cheap and
    honest; minting new data at stamp time is not)."""
    today = today or date.today()
    if load_missing:
        try:
            if news_entry is None:
                from src.forecast import load_news
                news_entry = load_news().get(ticker)
        except Exception:
            pass
        try:
            if affinity_readmodel is None:
                from src.knowledge_graph.entity_affinity import AFFINITY_PATH
                if AFFINITY_PATH.exists():
                    affinity_readmodel = json.loads(AFFINITY_PATH.read_text())
        except Exception:
            pass
        try:
            if flows is None:
                from src.ingestion.flows_tracker import load_flows
                flows = load_flows() or None
        except Exception:
            pass
        try:
            if earnings_calendar is None:
                from src.ingestion.earnings_calendar import load_calendar
                earnings_calendar = load_calendar() or None
        except Exception:
            pass

    layers = [
        technical_evidence(analysis),
        news_evidence(news_entry),
        macro_evidence(macro_matrix, ticker),
        affinity_evidence(affinity_readmodel, ticker),
        flows_evidence(flows),
        vix_evidence(vix),
    ]
    days_to_results = None
    try:
        if earnings_calendar is not None:
            from src.ingestion.earnings_calendar import days_to_results as dtr
            days_to_results = dtr(ticker, today, earnings_calendar)
    except Exception:
        days_to_results = None
    return {
        "as_of": today.isoformat(),
        "ticker": ticker,
        "days_to_results": days_to_results,
        "layers": layers,
    }


def summarize(snapshot: dict) -> str:
    """One human line per non-abstaining layer — the proposal-card /
    /analyze rendering. Abstentions collapse to a count (card-bloat rule)."""
    lines, abstained = [], 0
    for ev in (snapshot or {}).get("layers", []):
        if ev.get("abstained"):
            abstained += 1
            continue
        arrow = "↑" if ev["direction"] > 0 else ("↓" if ev["direction"] < 0
                                                 else "→")
        lines.append(f"{ev['layer']} {arrow} {ev['stance']} "
                     f"({ev['strength']:.0%}): {ev['detail']}")
    if snapshot.get("days_to_results") is not None:
        lines.append(f"results in {snapshot['days_to_results']}d")
    if abstained:
        lines.append(f"({abstained} layer(s) abstained)")
    return "\n".join(lines) if lines else "(no evidence captured)"


# ------------------------------------------------------------- persistence

def ensure_schema(conn) -> None:
    """Additive evidence_snapshots table in brain_map.db (#25 discipline)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS evidence_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            journal_ref TEXT NOT NULL UNIQUE,
            as_of TEXT NOT NULL,
            ticker TEXT NOT NULL,
            days_to_results INTEGER,
            payload TEXT NOT NULL
        )
    """)
    conn.commit()


def persist_snapshot(conn, journal_ref: str, snapshot: dict) -> bool:
    """Store one snapshot keyed by its trade's journal_ref (idempotent —
    first capture wins; a resolution-time re-stamp never overwrites what
    the proposal actually saw). Never raises."""
    if not journal_ref or not isinstance(snapshot, dict):
        return False
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO evidence_snapshots (journal_ref, as_of, ticker, "
            "days_to_results, payload) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (journal_ref) DO NOTHING",
            (journal_ref, snapshot.get("as_of") or "",
             snapshot.get("ticker") or "",
             snapshot.get("days_to_results"),
             json.dumps(snapshot)))
        conn.commit()
        return True
    except Exception as exc:
        print(f"  (evidence: persist failed for {journal_ref} [{exc}])")
        return False


def load_snapshot(conn, journal_ref: str) -> dict | None:
    """The stored snapshot for one trade, or None. Never raises."""
    try:
        ensure_schema(conn)
        row = conn.execute("SELECT payload FROM evidence_snapshots "
                           "WHERE journal_ref = ?", (journal_ref,)).fetchone()
        return json.loads(row["payload"]) if row else None
    except Exception:
        return None


if __name__ == "__main__":
    import sys as _sys
    _ticker = _sys.argv[1] if len(_sys.argv) > 1 else "NIFTY 50"
    snap = build_evidence_snapshot(_ticker, load_missing=True)
    print(json.dumps(snap, indent=2))
    print("\n" + summarize(snap))
