"""
src/ceo_language.py — plain-English rendering for the CEO-facing Discord
cards (Directive 2, docs/ceo_view_discord_design.md).

ONE place decides whether a macro/strategy claim is earned before it reaches
a sentence a human reads on a phone. Two honesty gates, both enforced here so
no other call site can accidentally skip them:

  1. An analog/episode name is spoken ONLY when the regime engine itself
     declared a match (`horizons[hz]["declared"]` — already gated on
     SIM_FLOOR/ANALOG_FLOOR/MIN_ANALOGS in `analysis.macro_regime`).
  2. A strategy is called more than an in-sample preference ONLY when the
     Stage-B forward scoreboard has graduated that (archetype, phase,
     strategy) cell to FORWARD_CONFIRMED. Everything else — including the
     common case today, ACCUMULATING — says so honestly and states that
     nothing acts on it automatically.

The Macro Regime Engine has ZERO execution authority (CLAUDE.md Rule 5), so
these sentences never say a strategy is "executing" — only that the playbook
"favours" or "prefers" it. Pure functions over already-loaded/injected data;
no capital, no writes, no new engine.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_REASON_TEXT = {
    "no_comparable_match": "no historical episode is a comparable match",
    "empty_current_window": "not enough recent market data to read yet",
}


def _humanize_episode(name: str) -> str:
    return " ".join(w.capitalize() for w in str(name or "").split("_"))


def _reason_sentence(reason: str) -> str:
    if reason in _REASON_TEXT:
        return _REASON_TEXT[reason]
    if reason and reason.startswith("cache_"):
        return "the overnight computation was skipped (cache miss) — abstaining rather than guessing"
    if reason and reason.startswith("best "):
        return f"the closest historical match is too weak to call ({reason})"
    if reason and reason.startswith("analogs "):
        return f"too few comparable episodes agree yet ({reason})"
    return reason or "no verdict yet"


def _load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _scoreboard_status(scoreboard_doc, archetype, phase, strategy_id):
    """FORWARD_CONFIRMED / FORWARD_CONTRADICTED / INCONCLUSIVE /
    ACCUMULATING / None (cell not tracked at all — same honesty as
    ACCUMULATING, said as "no live record yet")."""
    if not scoreboard_doc:
        return None
    cells = ((scoreboard_doc.get("table") or {}).get(archetype) or {}).get(phase) or []
    for cell in cells:
        if cell.get("strategy_id") == strategy_id:
            return cell.get("status")
    return None


def macro_regime_sentence(regime_doc=None, scoreboard_doc=None,
                          regime_path=None, scoreboard_path=None,
                          horizon="shock", max_strategies=2) -> str:
    """The one macro sentence for the CEO cards. Never guesses, never
    says "executing" — see module docstring for the two honesty gates."""
    if regime_doc is None:
        regime_doc = _load_json(regime_path or
                                (ROOT / "data" / "macro_regime.json"))
    if not regime_doc:
        return "🌍 Macro regime: not available yet."

    verdict = (regime_doc.get("horizons") or {}).get(horizon)
    if not verdict:
        return "🌍 Macro regime: not available yet."

    if not verdict.get("declared"):
        return (f"🌍 Macro regime: still accumulating — "
                f"{_reason_sentence(verdict.get('reason'))}.")

    best = verdict.get("best") or {}
    episode = _humanize_episode(best.get("best_episode"))
    n = best.get("analog_count")
    sim = best.get("similarity")
    lead = (f"🌍 Macro regime matches **{episode}** conditions "
            f"({n} comparable episode{'s' if n != 1 else ''}, "
            f"{sim:.0%} pattern similarity)." if sim is not None else
            f"🌍 Macro regime matches **{episode}** conditions.")

    if scoreboard_doc is None:
        scoreboard_doc = _load_json(scoreboard_path or
                                    (ROOT / "data" / "strategy_scoreboard.json"))
    strategies = ((verdict.get("strategy_slice") or {}).get("strategies") or [])
    archetype = best.get("archetype")
    phase = verdict.get("phase")

    confirmed, preferred = [], []
    for s in strategies[:max_strategies]:
        status = _scoreboard_status(scoreboard_doc, archetype, phase,
                                    s.get("strategy_id"))
        label = s.get("name") or s.get("strategy_id") or "unnamed"
        if status == "FORWARD_CONFIRMED":
            confirmed.append(label)
        else:
            preferred.append(label)

    parts = [lead]
    if confirmed:
        parts.append(f"Forward-confirmed live: {', '.join(confirmed)}.")
    if preferred:
        parts.append(
            f"The playbook favours {', '.join(preferred)} on this read — "
            "in-sample only, forward evidence still accumulating. Advisory "
            "only: nothing acts on this automatically (no capital is wired "
            "to the macro engine's output).")
    return " ".join(parts)


def book_summary_sentence(active_total: int, daily_pnl: float,
                          net_delta: float) -> str:
    """Turn the EOD card's own already-computed numbers into one narrated
    line — no new data, just plain English over what the card already
    knows."""
    if active_total == 0:
        book = "no open positions"
    else:
        book = f"{active_total} open position{'s' if active_total != 1 else ''}"

    if daily_pnl > 0:
        pnl = f"up Rs.{daily_pnl:,.0f} today"
    elif daily_pnl < 0:
        pnl = f"down Rs.{abs(daily_pnl):,.0f} today"
    else:
        pnl = "flat today"

    if net_delta > 0:
        lean = "leaning long"
    elif net_delta < 0:
        lean = "leaning short"
    else:
        lean = "flat directionally"

    return f"{book}, {pnl}, book is {lean}."
