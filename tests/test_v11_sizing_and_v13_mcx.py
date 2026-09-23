"""Decision #106 (2026-09-23): V1.1 fixed-fractional sizing per account and
V1.3 MCX commodity data pipelines (read-only)."""
import json
from datetime import date

from src import position_sizing as ps
from src.ingestion import chain_archiver as ca, scrip_master as SM


# ------------------------------------------------------------ V1.1 sizing
def test_fractional_lots_is_floor_of_capacity_over_max_loss():
    r = ps.fractional_lots(1_088_751.63, 5000.0, 2.0)
    assert r["risk_capacity_rs"] == 21775.03 and r["by_risk"] == 4 and r["lots"] == 4
    assert r["risk_at_lots_rs"] == 20000.0 and not r["floor_applied"] and r["reason"] == "sized"


def test_the_two_accounts_size_the_same_structure_independently():
    ten = ps.fractional_lots(1_000_000.0, 5000.0, 2.0, margin_per_lot=20000.0, available_cash=800_000.0)
    two = ps.fractional_lots(200_000.0, 5000.0, 2.0, margin_per_lot=20000.0, available_cash=180_000.0)
    assert ten["lots"] == 4 and two["lots"] == 1          # 20k//5k vs 4k//5k -> floor
    assert not ten["floor_applied"] and two["floor_applied"]
    assert "1-lot floor" in two["reason"] and "Rs.4,000" in two["reason"]


def test_min_one_lot_but_margin_is_the_hard_wall():
    r = ps.fractional_lots(200_000.0, 9000.0, 2.0, margin_per_lot=250_000.0, available_cash=200_000.0)
    assert r["lots"] == 0 and r["by_margin"] == 0 and "exceeds liquid cash" in r["reason"]
    r = ps.fractional_lots(200_000.0, 9000.0, 2.0, margin_per_lot=100_000.0, available_cash=200_000.0)
    assert r["lots"] == 1 and r["floor_applied"]
    assert ps.fractional_lots(200_000.0, 0.0, 2.0)["lots"] == 0     # unmeasurable loss


def test_risk_pct_is_config_driven_and_the_rupee_cap_is_gone():
    import src.config as cfg
    from src import options_proposer as op, portfolio_manager as pm, equity_desk as eqd
    assert cfg.ACCOUNT_RISK_PER_TRADE_PCT == 2.0
    for mod in (cfg, op, pm, eqd):
        assert not hasattr(mod, "MAX_RISK_PER_TRADE_RS")
    assert json.load(open(cfg.__file__.replace("src/config.py", "config.json")))["risk_per_trade_pct"] == 2.0
    assert ps.fractional_lots(1_000_000.0, 5000.0, 5.0)["lots"] == 10   # 5% band end


# ------------------------------------------------------------- V1.3 MCX
HEADER = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
          "SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_LOT_UNITS,SM_SYMBOL_NAME,SEM_SERIES")
ROWS = [
    ("MCX", "M", "466583", "FUTCOM", "GOLD-05Aug2026-FUT", "GOLD AUG FUT", "2026-08-05 23:30:00", "1.0", "GOLD", ""),
    ("MCX", "M", "483079", "FUTCOM", "GOLD-05Oct2026-FUT", "GOLD OCT FUT", "2026-10-05 23:30:00", "1.0", "GOLD", ""),
    ("MCX", "M", "495213", "FUTCOM", "GOLD-04Dec2026-FUT", "GOLD DEC FUT", "2026-12-04 23:30:00", "1.0", "GOLD", ""),
    ("MCX", "M", "900001", "OPTFUT", "GOLD-30Oct2026-90000-CE", "GOLD 30 OCT 90000 CALL", "2026-10-30 23:30:00", "1.0", "GOLD", ""),
    ("MCX", "M", "569900", "FUTCOM", "CRUDEOIL-19Oct2026-FUT", "CRUDEOIL OCT FUT", "2026-10-19 23:30:00", "1.0", "CRUDEOIL", ""),
    ("MCX", "M", "999999", "FUTCOM", "GOLDM-05Oct2026-FUT", "GOLDM OCT FUT", "2026-10-05 23:30:00", "1.0", "GOLDM", ""),
    ("NSE", "E", "11536", "EQUITY", "TCS", "Tata Consultancy", "", "1", "TCS", "EQ"),
]


def _master(rows=ROWS):
    return SM.index_master("\n".join([HEADER] + [",".join(r) for r in rows]) + "\n")


def test_lookup_commodities_picks_the_front_month_never_an_expired_or_lookalike_contract():
    out = SM.lookup_commodities(["GOLD", "SILVER", "CRUDEOIL"], _master(), today=date(2026, 9, 23))
    assert set(out) == {"GOLD", "CRUDEOIL"}                  # SILVER absent: no live contract, not invented
    g = out["GOLD"]
    assert g["id"] == "483079" and g["seg"] == "MCX_COMM" and g["inst"] == "FUTCOM"
    assert g["expiry"] == "2026-10-05" and g["master_symbol"] == "GOLD-05Oct2026-FUT"
    assert [n["id"] for n in g["next"]] == ["495213"]        # never 466583 (expired), never GOLDM
    assert g["option_expiries"] == ["2026-10-30"]
    assert out["CRUDEOIL"]["id"] == "569900"
    assert SM.SEGMENTS["MCX_COMM"] == ("MCX", "M")


def test_build_darling_ids_carries_a_separate_commodities_block(tmp_path):
    csv = "\n".join([HEADER] + [",".join(r) for r in ROWS]) + "\n"
    out = SM.build_darling_ids(symbols=["TCS"], fetch_fn=lambda u: csv,
                               out_path=tmp_path / "ids.json",
                               commodity_symbols=["GOLD", "SILVER"])
    assert out["ids"] == {"TCS": {"id": "11536", "master_symbol": "TCS", "series": "EQ"}}
    assert out["commodities"]["GOLD"]["id"] == "483079" and "SILVER" not in out["commodities"]
    assert out["commodity_unresolved"] == {"SILVER": "no live MCX FUTCOM contract in the master"}
    on_disk = json.loads((tmp_path / "ids.json").read_text())
    assert "GOLD" not in on_disk["ids"]                       # equity quoting can never see a future


def test_archiver_commodity_extension_reads_the_block_and_slugs_apart(tmp_path):
    p = tmp_path / "ids.json"
    p.write_text(json.dumps({"ids": {}, "commodities": {
        "GOLD": {"id": "483079", "seg": "MCX_COMM", "expiry": "2026-10-05"},
        "CRUDEOIL": {"id": "569900", "seg": "MCX_COMM", "expiry": "2026-10-19"},
        "BROKEN": {"seg": "MCX_COMM"}},
        "commodity_unresolved": {"SILVER": "no live MCX FUTCOM contract in the master"}}))
    ext = ca.commodity_extension(p)
    assert ext["names"]["GOLD (MCX)"] == {"slug": "mcx_gold", "security_id": "483079",
                                         "segment": "MCX_COMM", "symbol": "GOLD", "expiry": "2026-10-05"}
    assert set(ext["names"]) == {"GOLD (MCX)", "CRUDEOIL (MCX)"}
    assert ("BROKEN", "no_scrip_master_id") in ext["skipped"]
    assert ("SILVER", "no live MCX FUTCOM contract in the master") in ext["skipped"]
    assert not {v["slug"] for v in ext["names"].values()} & set(ca.UNDERLYINGS.values())
    assert ca.commodity_extension(tmp_path / "nope.json")["names"] == {}


def test_archiver_run_captures_mcx_by_id_into_its_own_lake_slug(tmp_path):
    from src import lake
    p = tmp_path / "ids.json"
    p.write_text(json.dumps({"ids": {}, "commodities": {"GOLD": {"id": "483079", "seg": "MCX_COMM"}}}))
    fo = tmp_path / "fo.json"; fo.write_text(json.dumps({"symbols": {}, "banned": []}))
    calls = []
    def expiry_by_id(sid, seg):
        calls.append(("expiry", sid, seg)); return ["2026-10-30", "2026-11-27", "2026-12-31"]
    def chain_by_id(sid, e, seg):
        calls.append(("chain", sid, e, seg)); return {"last_price": 90000.0, "oc": {"90000": {}}}
    summary = ca.run(today=date(2026, 9, 23), lake_root=tmp_path / "lake",
                     expiry_fn=lambda u: [], chain_fn=lambda u, e: None,
                     spot_fn=lambda u: None, vix_fn=lambda: 11.0, sleep_fn=lambda s: None,
                     fo_path=fo, ids_path=p, expiry_by_id_fn=expiry_by_id,
                     chain_by_id_fn=chain_by_id, spot_by_id_fn=lambda sid, seg: 90000.0)
    assert summary["commodities"]["captured"] == {"GOLD (MCX)": 2}      # COMMODITY_MAX_EXPIRIES
    assert ("expiry", "483079", "MCX_COMM") in calls
    assert ("chain", "483079", "2026-10-30", "MCX_COMM") in calls
    rows = lake.read_day("chains/mcx_gold", "2026-09-23", root=tmp_path / "lake")
    assert len(rows) == 2 and rows[0]["underlying"] == "GOLD (MCX)"


def test_cross_asset_follows_silver_and_the_verified_ids_are_live():
    from src.ingestion import cross_asset as xa
    assert "SILVER" in xa.COMMODITY_KEYS
    inst = xa.load_instruments()
    for k in ("CRUDE", "GOLD_INDIA", "SILVER"):
        assert inst[k]["seg"] == "MCX_COMM" and inst[k]["inst"] == "FUTCOM"
    assert xa.stale_instruments({k: inst[k] for k in ("CRUDE", "GOLD_INDIA", "SILVER")},
                                today=date(2026, 9, 23)) == []
