"""
Loads system constants from config.json (project root) once at import time.

This runs unattended on the cloud VM (cron jobs, no one watching), so a
missing file or a typo'd key must fail loudly and clearly right away --
not silently fall back to a guessed default that quietly changes behavior.
"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_REQUIRED_KEYS = (
    "rsi_overbought",
    "rsi_oversold",
    "moving_average_fast",
    "moving_average_slow",
    "default_stop_loss_pct",
    "default_investment_size",
    "risk_level",
    "risk_levels",
    "take_profit_rr",
    "max_concurrent_positions",
    "alt_entry_pullback_pct",
    "plan_max_days",
    "tuner_min_samples",
    "tuner_weight_sensitivity",
    "tuner_weight_bounds",
)


def _load() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found at {_CONFIG_PATH} -- restore it in the "
            "project root before running."
        )
    with open(_CONFIG_PATH) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"config.json is not valid JSON: {e}") from e

    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise KeyError(f"config.json is missing required key(s): {', '.join(missing)}")

    if data["risk_level"] not in data["risk_levels"]:
        raise ValueError(
            f"config.json: risk_level '{data['risk_level']}' must be one of "
            f"{list(data['risk_levels'])}"
        )

    if (not isinstance(data["tuner_weight_bounds"], list)
            or len(data["tuner_weight_bounds"]) != 2):
        raise ValueError(
            "config.json: tuner_weight_bounds must be a [min, max] pair"
        )

    return data


_CONFIG = _load()

RSI_OVERBOUGHT = _CONFIG["rsi_overbought"]
RSI_OVERSOLD = _CONFIG["rsi_oversold"]
MOVING_AVERAGE_FAST = _CONFIG["moving_average_fast"]
MOVING_AVERAGE_SLOW = _CONFIG["moving_average_slow"]
DEFAULT_STOP_LOSS_PCT = _CONFIG["default_stop_loss_pct"]
DEFAULT_INVESTMENT_SIZE = _CONFIG["default_investment_size"]

# Phase 4B risk levers.
RISK_LEVEL = _CONFIG["risk_level"]
# % of total portfolio value risked per trade, set by the chosen risk level.
RISK_PER_TRADE_PCT = _CONFIG["risk_levels"][RISK_LEVEL]
TAKE_PROFIT_RR = _CONFIG["take_profit_rr"]
MAX_CONCURRENT_POSITIONS = _CONFIG["max_concurrent_positions"]
ALT_ENTRY_PULLBACK_PCT = _CONFIG["alt_entry_pullback_pct"]
# Phase 4C: a swing plan that hits neither stop nor target within this many
# days is closed out at the market ("time stop") so no plan dangles forever.
PLAN_MAX_DAYS = _CONFIG["plan_max_days"]

# Phase 4F: the learning-loop tuner won't adjust a signal archetype's weight
# until it has this many resolved plan outcomes behind it.
TUNER_MIN_SAMPLES = _CONFIG["tuner_min_samples"]
# How many weight-points a full +1.0 average R-multiple moves the weight by.
TUNER_WEIGHT_SENSITIVITY = _CONFIG["tuner_weight_sensitivity"]
# Weight is clamped to this [min, max] so no single archetype can swamp the
# forecast checklist.
TUNER_WEIGHT_BOUNDS = tuple(_CONFIG["tuner_weight_bounds"])

# Phase 5 options: % of portfolio value a single defined-risk spread's
# ABSOLUTE MAX LOSS may consume. Deliberately higher than the equity
# RISK_PER_TRADE_PCT: an equity stop can gap through (soft risk number),
# while a spread's max loss is a hard structural ceiling — and a single
# NIFTY lot-75 condor (~Rs.6k max loss) must be affordable on the
# Rs.1,00,000 paper book or no options trade could ever size above 0 lots.
# Optional key so older config.json copies (e.g. on the VM) keep working.
OPTIONS_RISK_PER_TRADE_PCT = float(_CONFIG.get("options_risk_per_trade_pct", 10.0))

# Equity desk (owner ruling 2026-07-20): the darling shadow book's paper
# capital slice, carved from the firm's 10L pool. Optional keys so older
# config.json copies keep working; the code default is DISABLED so a
# stale copy (e.g. the VM's) can never fund entries by accident.
EQUITY_DESK_ENABLED = bool(_CONFIG.get("equity_desk_enabled", False))
EQUITY_DESK_CAPITAL_RS = float(_CONFIG.get("equity_desk_capital_rs", 300000.0))
EQUITY_DESK_RISK_PER_TRADE_PCT = float(
    _CONFIG.get("equity_desk_risk_per_trade_pct", 1.0))
EQUITY_DESK_MAX_NOTIONAL_PCT = float(
    _CONFIG.get("equity_desk_max_notional_pct", 15.0))
# V1.2 SMART EXITS for the EQUITY DESK (decision #107, 2026-09-23): a funded
# long darling no longer exits at a static target; it rides an ATR trail —
# trail = highest_high_since_entry − ATR_MULT × ATR(ATR_N) on the
# underlying's DAILY bars, a one-way ratchet that only ever rises (floor =
# the plan's hard stop). Close/quote below the trail = exit through the OMS.
# The hard stop and the time stop still apply. `equity_trail_enabled: false`
# restores the static target; no bars (no id, feed down) = static target
# for that position, never a guess.
EQUITY_TRAIL_ENABLED = bool(_CONFIG.get("equity_trail_enabled", True))
EQUITY_TRAIL_ATR_MULT = float(_CONFIG.get("equity_trail_atr_mult", 3.0))
EQUITY_TRAIL_ATR_N = int(_CONFIG.get("equity_trail_atr_n", 14))
# VOLATILITY EDGE (decision #109, 2026-09-23): a short-vega structure (iron
# condor / butterfly) is only proposed when the underlying's 14-session
# realized volatility ranks high against its own 252-session range
# (`vol_rank.hv_rank`, 0-100). Below the floor the neutral proposal is
# REFUSED (VOL_RANK_GATE); missing history = named abstention, not a block.
VOL_RANK_GATE_ENABLED = bool(_CONFIG.get("vol_rank_gate_enabled", True))
VOL_RANK_MIN_NEUTRAL = float(_CONFIG.get("vol_rank_min_neutral", 50.0))
VOL_RANK_WINDOW = int(_CONFIG.get("vol_rank_window", 14))
VOL_RANK_LOOKBACK = int(_CONFIG.get("vol_rank_lookback", 252))
# CORRELATION GUARD (decision #109): at most this many OPEN directional
# spreads per thesis (bullish / bearish) across the whole book; the third
# is refused CORRELATION_GUARD_HIT by the exposure gate.
MAX_OPEN_PER_DIRECTION = int(_CONFIG.get("max_open_per_direction", 2))
# Macro expiry guard (decision #107): the CEO brief flags any macro /
# commodity contract id that has expired or expires within this many days.
MACRO_EXPIRY_WARN_DAYS = int(_CONFIG.get("macro_expiry_warn_days", 7))

# Firm treasury (owner Directive 1, 2026-07-20): dynamic capital routing
# between the desks. Optional keys; code default DISABLED — a stale config
# copy must never start moving capital on its own.
TREASURY_ENABLED = bool(_CONFIG.get("treasury_enabled", False))
TREASURY_EQUITY_MIN_PCT = float(_CONFIG.get("treasury_equity_min_pct", 15.0))
TREASURY_EQUITY_MAX_PCT = float(_CONFIG.get("treasury_equity_max_pct", 60.0))
TREASURY_DEADBAND_RS = float(_CONFIG.get("treasury_deadband_rs", 50000.0))
TREASURY_MAX_STEP_RS = float(_CONFIG.get("treasury_max_step_rs", 100000.0))
# Absolute path because the 19:15 cron's PATH is minimal (the standing
# three-unpinned-interpreter lesson applies to gcloud too).
def _resolve_gcloud(configured: str) -> str:
    """The configured absolute path when it exists (the Mac), else the first
    gcloud on a widened PATH (the Linux home node, decision #99) — still an
    absolute path once resolved, never a bare `gcloud` for a cron shell."""
    import os
    import shutil
    if configured and os.access(configured, os.X_OK):
        return configured
    extra = ("/usr/bin:/usr/local/bin:/snap/bin:/usr/lib/google-cloud-sdk/bin:"
             "/usr/local/google-cloud-sdk/bin:" + os.path.expanduser("~/google-cloud-sdk/bin")
             + ":/opt/homebrew/bin:/opt/homebrew/share/google-cloud-sdk/bin")
    found = shutil.which("gcloud", path=os.environ.get("PATH", "") + ":" + extra)
    return found or configured


GCLOUD_PATH = _resolve_gcloud(str(_CONFIG.get(
    "gcloud_path", "/opt/homebrew/share/google-cloud-sdk/bin/gcloud")))


def gcloud_env(env=None, executable=None) -> dict:
    """The environment `gcloud` must be invoked with from an unattended Mac
    process. **Pinning GCLOUD_PATH was one layer short of enough.**

    `gcloud` is a /bin/sh wrapper that then goes looking for a Python on
    PATH. Under cron's minimal PATH it finds macOS's own
    `/usr/bin/python3` — which is **3.9.6**, a version gcloud dropped:

        ERROR: gcloud failed to load. You are running gcloud with
        Python 3.9, which is no longer supported by gcloud. ... set the
        CLOUDSDK_PYTHON environment variable to point to it.

    Interactively it works (Homebrew/Framework pythons sit earlier on
    PATH), so this fails ONLY unattended — which is how the Mac→VM
    artifact ship ran dead from the day it shipped (2026-07-21) to
    2026-08-05 without a single visible error.

    Pins CLOUDSDK_PYTHON to the interpreter running us (`sys.executable`,
    the 3.14 framework python the cron line already names by absolute
    path). An explicitly-set CLOUDSDK_PYTHON in the environment is
    respected and never overwritten — the owner's choice outranks ours.
    """
    import os
    import sys
    out = dict(os.environ if env is None else env)
    out.setdefault("CLOUDSDK_PYTHON", executable or sys.executable)
    return out

# Adaptive sizing (owner Directive 2, 2026-07-20): the autopsy-driven
# sizing feedback loop. Optional key; code default OFF — a stale config
# copy must never start resizing trades on its own.
ADAPTIVE_SIZING_ENABLED = bool(_CONFIG.get("adaptive_sizing_enabled", False))

# V1.1 DYNAMIC POSITION SIZING (decision #106, 2026-09-23, architect
# directive). The static Rs.10,000 per-trade cap of decision #84
# (`max_risk_per_trade_rs`) is GONE — a stale config.json still carrying
# that key is ignored. The risk one trade may carry is a FRACTION of the
# specific account's total equity, evaluated per account
# (`position_sizing.fractional_lots`): PAPER_10L and PAPER_2L size the same
# structure on their own equity and come out with different lot counts.
# Config `risk_per_trade_pct`; code default 2.0 (architect's 2–5% band, the
# conservative end). Options: lots = max(1, floor(equity×pct/100 ÷
# max_loss_per_lot)), then the account's own margin wall. The equity desk
# keeps its own `equity_desk_risk_per_trade_pct` of DESK capital (no rupee
# cap any more either).
ACCOUNT_RISK_PER_TRADE_PCT = float(_CONFIG.get("risk_per_trade_pct", 2.0))
# Treasury rupee-granularity (pool-scale aware since the 2L clean sheet).
TREASURY_ROUND_RS = float(_CONFIG.get("treasury_round_rs", 5000.0))
# Directive 4 (#84): the daily Discord message budget. Code default OFF
# so a stale config copy never mutes anything unexpectedly.
DISCORD_BUDGET_ENABLED = bool(_CONFIG.get("discord_budget_enabled", False))
DISCORD_DAILY_BUDGET = int(_CONFIG.get("discord_daily_budget", 5))
# Phase M2 paper execution (decision #101, 2026-09-19): when ON, an APPROVED
# options entry is issued as an Order Ticket (strategy_router.issue) and
# filled by the PAPER venue (execution.paper_venue) instead of the legacy
# instant frictionless fill. Code default OFF — a stale config copy must
# never switch the desk's fill model on its own.
PAPER_VENUE_ENABLED = bool(_CONFIG.get("paper_venue_enabled", False))
# Phase M1.B — the DUAL PAPER TREASURY (decision #102, 2026-09-19): the
# Rs.2,00,000 stress-test account judged beside the primary pool on every
# signal (portfolio_manager.evaluate_shadow_accounts). Code default ON: it
# is a zero-authority shadow ledger in its own tables — it locks nothing
# in the primary account and changes no decision — and the architect's
# directive is that the 2L proof runs. `paper_2l_account_enabled: false`
# in config.json switches it off; the starting pool is a config value so
# the proof can be re-run at another scale by a numbered decision.
PAPER_2L_ACCOUNT_ENABLED = bool(_CONFIG.get("paper_2l_account_enabled", True))
# NO MID-TRADE STOP ON A DEFINED-RISK SPREAD (decision #105, 2026-09-23,
# architect mandate; #103 reversed "like it was never introduced", the #104
# thesis stop withdrawn the same day). The structure IS the stop: max loss is
# capped by construction and the Rs.10k per-trade cap bounds it further. A
# spread is held to the 65% profit take or the pre-expiry exit. There is no
# premium-drawdown knob and no underlying-break knob; do not add one here.
PAPER_2L_STARTING_CAPITAL_RS = float(_CONFIG.get("paper_2l_starting_capital_rs", 200000.0))
