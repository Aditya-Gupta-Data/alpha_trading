"""
src/human_pulse.py — the engagement tripwire for full paper autonomy
====================================================================

Phase 3 of docs/HOLY_GRAIL_PLAN.md (§6.6). Non-negotiable #2
(human-in-the-loop) is only real if the human is actually in the loop:
PAPER_AUTO_APPROVE (decision #53) plus an absent human is an unsupervised
autonomous system wearing a supervision costume. The tripwire:

    If the human has taken ZERO manual actions for N trading days while
    auto-approve is ON, NEW auto-approvals pause (fresh proposals stay
    PENDING_APPROVAL — which decision #31 already tracks hypothetically,
    so almost no learning data is lost) and one "brain is unsupervised"
    card fires. ANY human action re-arms full autonomy instantly.

What counts as a human action: any decide_pending call that is not the
auto-approve path (Discord button via the gateway, the --review-pending
CLI), recorded via `touch()`. State is one small JSON
(data/human_pulse.json, atomic write, hand-inspectable per #19).

Fail-OPEN on state problems in one deliberate direction: an unreadable
pulse file re-seeds as "seen now" — a broken file must not silently pause
the learning loop; the tripwire exists for the human-absent case, not the
disk-hiccup case. First run seeds "now" too (autonomy earns N days of
trust from deploy, not a day-one pause).

Config: `auto_approve_unsupervised_days` in config.json (default 3
trading days). Checked per call — no restart needed to tune it.
"""

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PULSE_PATH = ROOT / "data" / "human_pulse.json"
CONFIG_PATH = ROOT / "config.json"

DEFAULT_UNSUPERVISED_TRADING_DAYS = 3


def _threshold_days(config_path=None) -> int:
    path = Path(config_path) if config_path is not None else CONFIG_PATH
    try:
        raw = json.loads(path.read_text())
        return max(1, int(raw.get("auto_approve_unsupervised_days",
                                  DEFAULT_UNSUPERVISED_TRADING_DAYS)))
    except (OSError, ValueError, TypeError):
        return DEFAULT_UNSUPERVISED_TRADING_DAYS


def _read_state(path=None) -> dict | None:
    path = Path(path) if path is not None else PULSE_PATH
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _write_state(state: dict, path=None) -> None:
    path = Path(path) if path is not None else PULSE_PATH
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        print(f"  (human pulse: state write failed [{exc}])")
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def touch(source: str = "human", path=None, now: datetime = None) -> None:
    """Record a human action NOW. Called from every human decision path;
    clears any active pause + its one-shot alert flag. Never raises.
    Muzzled under tests unless the test passes its own path (the
    decision-#43 rule — suite runs never touch the live pulse)."""
    if path is None and (os.environ.get("PYTEST_CURRENT_TEST")
                         or os.environ.get("IS_TEST_ENV")):
        return
    now = now or datetime.now()
    _write_state({"last_human_action": now.isoformat(timespec="seconds"),
                  "source": source, "alerted_at": None}, path=path)


def trading_days_between(start: date, end: date) -> int:
    """Weekdays strictly after `start` up to and including `end`. (NSE
    holidays counted as trading days — the tripwire errs alert-early,
    which costs one card, never a missed absence.)"""
    if end <= start:
        return 0
    days, cursor = 0, start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def auto_approve_tripped(path=None, now: datetime = None,
                         config_path=None) -> bool:
    """True when new auto-approvals must PAUSE: no human action for more
    than the threshold of trading days. Seeds the pulse (returns False) on
    first run / unreadable state. Never raises."""
    now = now or datetime.now()
    state = _read_state(path)
    if not state or not state.get("last_human_action"):
        touch("seeded", path=path, now=now)
        return False
    try:
        last = datetime.fromisoformat(state["last_human_action"])
    except (ValueError, TypeError):
        touch("reseeded-corrupt", path=path, now=now)
        return False
    return (trading_days_between(last.date(), now.date())
            > _threshold_days(config_path))


def should_alert_once(path=None, now: datetime = None) -> bool:
    """True exactly once per pause episode: the caller sends the
    'unsupervised' card and this stamps alerted_at so repeat proposals
    during the same episode stay quiet. Never raises."""
    now = now or datetime.now()
    state = _read_state(path) or {}
    if state.get("alerted_at"):
        return False
    state["alerted_at"] = now.isoformat(timespec="seconds")
    _write_state(state, path=path)
    return True


UNSUPERVISED_CARD = (
    "🛑 **BRAIN UNSUPERVISED — auto-approve paused.**\n"
    "PAPER_AUTO_APPROVE is ON but no human action has been seen for over "
    "{days} trading day(s). New proposals will journal as PENDING_APPROVAL "
    "(still tracked hypothetically) until you take any action — approve or "
    "reject anything via `/pending` or the CLI and full autonomy resumes "
    "instantly. Human-in-the-loop is only real if the human is in the loop."
)


def unsupervised_card(config_path=None) -> str:
    return UNSUPERVISED_CARD.format(days=_threshold_days(config_path))
