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
GCLOUD_PATH = str(_CONFIG.get(
    "gcloud_path", "/opt/homebrew/share/google-cloud-sdk/bin/gcloud"))
