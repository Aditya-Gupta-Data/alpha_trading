"""
src/strategies/cross_asset_gold_crude.py — Gold/Crude cross-asset recipe (B5 CROSS_ASSET), TRIAL only
=====================================================================================================

A PLACEHOLDER, pre-registered thesis: when the Macro Regime Engine declares a
regime whose best analog is a pre-registered inflationary episode, the recipe
would lean long MCX GOLD (`GOLD_INDIA`) and long MCX CRUDEOIL (`CRUDE`; build-up / peak
only, flat in the unwind). It reads the declared state in data/macro_regime.json
(DTW fingerprint match per horizon) and nothing else.

Authority: NONE. Registers into the Department-5 registry and parks at TRIAL;
`evaluate` returns a shadow stance only. Not reachable from any cron (Rule 5),
no order path, no sizing. Promotion past TRIAL is a Department-5 decision on
forward out-of-sample evidence, never a code change.
"""
from src.buckets import CROSS_ASSET_FAMILY
from src.ingestion import scrip_master
from src.validation import registry

KIND = "cross_asset_recipe"
DEFAULT_STATUS = "TRIAL"

# A-priori inflation analogs from config/macro_episodes.yaml, frozen into the spec.
INFLATION_EPISODES = (
    "cpi_rate_shock_2022",
    "ukraine_invasion",
    "rbi_hike_2010_11",
    "rbi_hike_2013_14",
    "rbi_hike_cycle_2022_23",
    "el_nino_2009_10",
    "el_nino_2014_16",
    "el_nino_2023_24",
)

RECIPE = {
    "name": "gold_crude_inflation_tilt",
    "version": 1,
    "bucket": CROSS_ASSET_FAMILY,
    "instruments": ["GOLD_INDIA", "CRUDE"],
    "regime_source": "macro_regime.declare",
    "horizons": ["slow_burn", "shock"],
    "inflation_episodes": sorted(INFLATION_EPISODES),
    "stance": {
        "GOLD_INDIA": {"default": "long"},
        "CRUDE": {"default": "long", "flat_in_phases": ["S3_unwind", "P3_resolution"]},
    },
}


def contracts(securities: dict = None, today=None) -> dict:
    """Front-month MCX contract per instrument via the roller; None when nothing unexpired."""
    return {key: scrip_master.resolve_front_month(key, securities=securities, today=today)
            for key in RECIPE["instruments"]}


def inflation_read(state: dict) -> dict | None:
    """First declared horizon whose best analog is an inflation episode, or None."""
    horizons = (state or {}).get("horizons") or {}
    for hz in RECIPE["horizons"]:
        v = horizons.get(hz) or {}
        best = v.get("best") or {}
        if v.get("declared") and best.get("best_episode") in INFLATION_EPISODES:
            return {"horizon": hz, "phase": v.get("phase"), "archetype": best.get("archetype"),
                    "episode": best["best_episode"], "similarity": best.get("similarity")}
    return None


def evaluate(state: dict) -> dict:
    """Shadow stance for the current regime state; abstains with a named reason."""
    out = {"recipe": RECIPE["name"], "bucket": CROSS_ASSET_FAMILY,
           "status": DEFAULT_STATUS, "authority": "NONE", "stance": None}
    if not state:
        return {**out, "fires": False, "reason": "regime_state_unavailable"}
    read = inflation_read(state)
    if read is None:
        return {**out, "fires": False, "reason": "no_inflation_regime_declared"}
    stance = {}
    for sym, rule in RECIPE["stance"].items():
        stance[sym] = "flat" if read["phase"] in rule.get("flat_in_phases", ()) else rule["default"]
    return {**out, "fires": True, "reason": "inflation_regime_declared",
            "regime": read, "stance": stance}


def register_trial(conn) -> dict:
    """Register the frozen recipe and park it at TRIAL; idempotent, never moves a later status."""
    reg = registry.register(
        conn, kind=KIND, definition=RECIPE,
        description="Gold/Crude long tilt in declared inflation regimes (B5 CROSS_ASSET placeholder)",
        mining_run="a_priori:cross_asset_gold_crude")
    if reg["status"] == "CANDIDATE":
        t = registry.transition(conn, reg["pattern_id"], DEFAULT_STATUS,
                                "pre-registered a-priori thesis; forward OOS trial required")
        return {**reg, "status": t["status"]}
    return reg
