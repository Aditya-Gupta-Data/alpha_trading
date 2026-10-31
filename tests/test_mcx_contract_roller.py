"""Hermetic tests for the MCX Futures Contract Roller (Gold, Silver, Crude Oil)."""

import copy
import json
from datetime import date
from pathlib import Path

from src.ingestion import cross_asset, scrip_master as sm

ROOT = Path(__file__).resolve().parents[1]
MACRO_SECURITIES_FILE = ROOT / "config" / "macro_securities.json"

CSV_HEADER = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
    "SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SM_SYMBOL_NAME,"
    "SEM_SERIES,SEM_LOT_UNITS\n"
)


def _mcx_row(sec_id: str, symbol: str, custom: str, expiry: str,
             lot: str = "100.0", instr: str = "FUTCOM") -> str:
    trading = f"{symbol}-{expiry}-FUT"
    return (
        f"MCX,M,{sec_id},{instr},{trading},{custom},"
        f"{expiry} 23:30:00,{symbol},,{lot}\n"
    )


def _synthetic_mcx_csv() -> str:
    return (
        CSV_HEADER
        # GOLD standard (100g) bi-monthly ladder + lookalikes
        + _mcx_row("477176", "GOLD", "GOLD OCT FUT", "2026-10-05", "100.0")
        + _mcx_row("495213", "GOLD", "GOLD DEC FUT", "2026-12-04", "100.0")
        + _mcx_row("559933", "GOLD", "GOLD FEB FUT", "2027-02-05", "100.0")
        + _mcx_row("569424", "GOLD", "GOLD APR FUT", "2027-04-05", "100.0")
        + _mcx_row("990001", "GOLDM", "GOLDM NOV FUT", "2026-11-05", "10.0")
        + _mcx_row("990002", "GOLDPETAL", "GOLDPETAL NOV FUT", "2026-11-30", "1.0")
        + _mcx_row("990003", "GOLD", "GOLD NOV 78000 CE", "2026-11-25", "100.0", instr="OPTFUT")
        # SILVER standard (30kg) ladder + lookalikes
        + _mcx_row("464726", "SILVER", "SILVER SEP FUT", "2026-09-04", "30.0")
        + _mcx_row("495214", "SILVER", "SILVER DEC FUT", "2026-12-04", "30.0")
        + _mcx_row("564619", "SILVER", "SILVER MAR FUT", "2027-03-05", "30.0")
        + _mcx_row("572772", "SILVER", "SILVER MAY FUT", "2027-05-05", "30.0")
        + _mcx_row("990004", "SILVERM", "SILVERM NOV FUT", "2026-11-30", "5.0")
        # CRUDEOIL standard (100 bbl) monthly ladder + lookalike
        + _mcx_row("569900", "CRUDEOIL", "CRUDEOIL OCT FUT", "2026-10-19", "100.0")
        + _mcx_row("573422", "CRUDEOIL", "CRUDEOIL NOV FUT", "2026-11-19", "100.0")
        + _mcx_row("576264", "CRUDEOIL", "CRUDEOIL DEC FUT", "2026-12-18", "100.0")
        + _mcx_row("580111", "CRUDEOIL", "CRUDEOIL JAN FUT", "2027-01-19", "100.0")
        + _mcx_row("990005", "CRUDEOILM", "CRUDEOILM NOV FUT", "2026-11-19", "10.0")
        # Industrial metals (COPPER, ALUMINIUM, ZINC)
        + _mcx_row("574829", "COPPER", "COPPER OCT FUT", "2026-10-30", "2500.0")
        + _mcx_row("578634", "COPPER", "COPPER NOV FUT", "2026-11-30", "2500.0")
        + _mcx_row("581714", "COPPER", "COPPER DEC FUT", "2026-12-31", "2500.0")
        + _mcx_row("574828", "ALUMINIUM", "ALUMINIUM OCT FUT", "2026-10-30", "5000.0")
        + _mcx_row("578633", "ALUMINIUM", "ALUMINIUM NOV FUT", "2026-11-30", "5000.0")
        + _mcx_row("581712", "ALUMINIUM", "ALUMINIUM DEC FUT", "2026-12-31", "5000.0")
        + _mcx_row("574834", "ZINC", "ZINC OCT FUT", "2026-10-30", "5000.0")
        + _mcx_row("578638", "ZINC", "ZINC NOV FUT", "2026-11-30", "5000.0")
        + _mcx_row("581719", "ZINC", "ZINC DEC FUT", "2026-12-31", "5000.0")
    )


def _master():
    return sm.index_master(_synthetic_mcx_csv())


def test_lookup_commodities_identifies_front_month_and_rolls_on_expiry():
    master = _master()

    # Before Oct 5 expiry: GOLD Oct (477176), SILVER Dec (495214), CRUDEOIL Oct (569900)
    pre_oct = sm.lookup_commodities(
        ["GOLD", "SILVER", "CRUDEOIL"], master, today=date(2026, 10, 1)
    )
    assert pre_oct["GOLD"]["id"] == "477176"
    assert pre_oct["GOLD"]["expiry"] == "2026-10-05"
    assert "rolled_from" not in pre_oct["GOLD"]
    assert pre_oct["SILVER"]["id"] == "495214"
    assert pre_oct["CRUDEOIL"]["id"] == "569900"

    # On GOLD expiry day (2026-10-05): GOLD rolls to Dec (495213) and records rolled_from
    on_gold_exp = sm.lookup_commodities(
        ["GOLD", "SILVER", "CRUDEOIL"], master, today=date(2026, 10, 5)
    )
    assert on_gold_exp["GOLD"]["id"] == "495213"
    assert on_gold_exp["GOLD"]["expiry"] == "2026-12-04"
    assert on_gold_exp["GOLD"]["rolled_from"] == {
        "id": "477176",
        "expiry": "2026-10-05",
        "master_symbol": "GOLD-2026-10-05-FUT",
    }
    # Next after Dec is Feb 2027
    assert on_gold_exp["GOLD"]["next"][0]["id"] == "559933"

    # On CRUDEOIL Oct expiry day (2026-10-19): CRUDEOIL rolls to Nov (573422)
    on_crude_exp = sm.lookup_commodities(
        ["GOLD", "SILVER", "CRUDEOIL"], master, today=date(2026, 10, 19)
    )
    assert on_crude_exp["CRUDEOIL"]["id"] == "573422"
    assert on_crude_exp["CRUDEOIL"]["expiry"] == "2026-11-19"
    assert on_crude_exp["CRUDEOIL"]["rolled_from"]["id"] == "569900"

    # Lookalikes (GOLDM, GOLDPETAL, SILVERM, CRUDEOILM, OPTFUT) are never selected
    picked_ids = {v["id"] for v in on_crude_exp.values()}
    assert picked_ids.isdisjoint({"990001", "990002", "990003", "990004", "990005"})


def test_resolve_front_month_accepts_macro_keys_and_mcx_symbols():
    master = _master()

    # Using macro keys (GOLD_INDIA, SILVER, CRUDE) on 2026-11-01
    gold = sm.resolve_front_month("GOLD_INDIA", master=master, today=date(2026, 11, 1))
    silver = sm.resolve_front_month("SILVER", master=master, today=date(2026, 11, 1))
    crude = sm.resolve_front_month("CRUDE", master=master, today=date(2026, 11, 1))

    assert gold["id"] == "495213"
    assert gold["macro_key"] == "GOLD_INDIA"
    assert gold["mcx_symbol"] == "GOLD"
    assert gold["expiry"] == "2026-12-04"

    assert silver["id"] == "495214"
    assert silver["macro_key"] == "SILVER"
    assert silver["mcx_symbol"] == "SILVER"
    assert silver["expiry"] == "2026-12-04"

    assert crude["id"] == "573422"
    assert crude["macro_key"] == "CRUDE"
    assert crude["mcx_symbol"] == "CRUDEOIL"
    assert crude["expiry"] == "2026-11-19"


def test_roll_mcx_contracts_with_master_and_offline_next_ladder(tmp_path):
    # Start with a stale October snapshot
    stale_cfg = {
        "_meta": "test config",
        "NIFTY": {"id": "13", "segment": "IDX_I", "instrument": "INDEX"},
        "GOLD_INDIA": {
            "id": "477176",
            "segment": "MCX_COMM",
            "instrument": "FUTCOM",
            "_contract": "GOLD 05OCT2026 FUT",
            "_expiry": "2026-10-05",
            "_next": [
                {"id": "495213", "expiry": "2026-12-04", "symbol": "GOLD DEC FUT"},
                {"id": "559933", "expiry": "2027-02-05", "symbol": "GOLD FEB FUT"},
            ],
        },
        "SILVER": {
            "id": "464726",
            "segment": "MCX_COMM",
            "instrument": "FUTCOM",
            "_contract": "SILVER 04SEP2026 FUT",
            "_expiry": "2026-09-04",
            "_next": [
                {"id": "495214", "expiry": "2026-12-04", "symbol": "SILVER DEC FUT"},
                {"id": "564619", "expiry": "2027-03-05", "symbol": "SILVER MAR FUT"},
            ],
        },
        "CRUDE": {
            "id": "569900",
            "segment": "MCX_COMM",
            "instrument": "FUTCOM",
            "_contract": "CRUDEOIL 19OCT2026 FUT",
            "_expiry": "2026-10-19",
            "_next": [
                {"id": "573422", "expiry": "2026-11-19", "symbol": "CRUDEOIL NOV FUT"},
                {"id": "576264", "expiry": "2026-12-18", "symbol": "CRUDEOIL DEC FUT"},
            ],
        },
    }
    sec_path = tmp_path / "macro_securities.json"
    sec_path.write_text(json.dumps(stale_cfg, indent=2))
    static_text = sec_path.read_text()
    state_path = tmp_path / "state" / "active_mcx_contracts.json"

    # 1. Roll offline (no master) using _next ladder as of 2026-11-01
    res_offline = sm.roll_mcx_contracts(
        securities_path=sec_path,
        state_path=state_path,
        master=None,
        today=date(2026, 11, 1),
        write=True,
    )
    assert set(res_offline["rolled"].keys()) == {"GOLD_INDIA", "SILVER", "CRUDE"}
    assert res_offline["unrollable"] == {}

    assert sec_path.read_text() == static_text  # the tracked config is never written
    disk_cfg = sm.load_effective_securities(sec_path, state_path)
    assert json.loads(state_path.read_text())["contracts"]["CRUDE"]["id"] == "573422"
    assert disk_cfg["GOLD_INDIA"]["id"] == "495213"
    assert disk_cfg["GOLD_INDIA"]["_expiry"] == "2026-12-04"
    assert disk_cfg["GOLD_INDIA"]["_previous"]["id"] == "477176"
    assert disk_cfg["SILVER"]["id"] == "495214"
    assert disk_cfg["SILVER"]["_expiry"] == "2026-12-04"
    assert disk_cfg["CRUDE"]["id"] == "573422"
    assert disk_cfg["CRUDE"]["_expiry"] == "2026-11-19"
    assert disk_cfg["CRUDE"]["_next"][0]["id"] == "576264"

    # 2. Advance to 2026-12-04 (expiry day of GOLD Dec & SILVER Dec, and after CRUDE Nov)
    # Using synthetic scrip master
    res_dec = sm.roll_mcx_contracts(
        securities_path=sec_path,
        state_path=state_path,
        master=_master(),
        today=date(2026, 12, 4),
        write=True,
    )
    assert set(res_dec["rolled"].keys()) == {"GOLD_INDIA", "SILVER", "CRUDE"}
    assert sec_path.read_text() == static_text
    disk_dec = sm.load_effective_securities(sec_path, state_path)
    assert disk_dec["GOLD_INDIA"]["id"] == "559933"
    assert disk_dec["GOLD_INDIA"]["_expiry"] == "2027-02-05"
    assert disk_dec["GOLD_INDIA"]["_previous"]["id"] == "495213"
    assert disk_dec["SILVER"]["id"] == "564619"
    assert disk_dec["SILVER"]["_expiry"] == "2027-03-05"
    assert disk_dec["CRUDE"]["id"] == "576264"
    assert disk_dec["CRUDE"]["_expiry"] == "2026-12-18"
    # Non-MCX entries remain untouched
    assert disk_dec["NIFTY"] == {"id": "13", "segment": "IDX_I", "instrument": "INDEX"}


def test_unrollable_when_all_contracts_expired_never_guesses():
    # Decision #78: never guess an unverified id when the ladder is exhausted
    exhausted_cfg = {
        "CRUDE": {
            "id": "569900",
            "segment": "MCX_COMM",
            "instrument": "FUTCOM",
            "_expiry": "2026-10-19",
            "_next": [
                {"id": "573422", "expiry": "2026-11-19", "symbol": "CRUDEOIL NOV FUT"}
            ],
        }
    }
    res = sm.roll_mcx_contracts(
        securities=copy.deepcopy(exhausted_cfg),
        master=None,
        today=date(2028, 1, 1),
        keys=["CRUDE"],
        write=False,
    )
    assert res["rolled"] == {}
    assert "CRUDE" in res["unrollable"]
    assert res["securities"]["CRUDE"]["id"] == "569900"
    assert sm.resolve_front_month(
        "CRUDE", securities=exhausted_cfg, master=None, today=date(2028, 1, 1)
    ) is None


def test_check_one_resolves_mcx_alias_and_flags_expired_contract():
    master = _master()
    # Active contract for GOLD_INDIA (alias -> GOLD)
    ok_res = sm.check_one(
        "GOLD_INDIA",
        {"id": "495213", "segment": "MCX_COMM", "instrument": "FUTCOM"},
        master,
        today=date(2026, 11, 1),
    )
    assert ok_res["verdict"] == "ok"

    # Expired contract for CRUDE (alias -> CRUDEOIL)
    exp_res = sm.check_one(
        "CRUDE",
        {"id": "569900", "segment": "MCX_COMM", "instrument": "FUTCOM"},
        master,
        today=date(2026, 11, 1),
    )
    assert exp_res["verdict"] == "expired_contract"
    assert "2026-10-19" in exp_res["detail"]


def test_repo_macro_securities_has_active_mcx_front_months_and_zero_stale_refusals():
    # Verify the repo's config/macro_securities.json is active as of 2026-11-01
    cfg = json.loads(MACRO_SECURITIES_FILE.read_text())
    today = date(2026, 11, 1)

    for key in ("GOLD_INDIA", "SILVER", "CRUDE", "COPPER", "ALUMINIUM", "ZINC"):
        entry = cfg[key]
        assert entry["seg"] == "MCX_COMM"
        assert entry["inst"] == "FUTCOM"
        assert entry["_auto_roll"] is True
        assert entry["_expiry"] > today.isoformat(), f"{key} expiry {entry['_expiry']} not > {today}"
        assert isinstance(entry.get("_next"), list) and len(entry["_next"]) >= 1
        assert entry["_next"][0]["expiry"] > entry["_expiry"]

    # Verify cross_asset.stale_instruments reports zero stale contracts
    instruments = cross_asset.load_instruments(MACRO_SECURITIES_FILE)
    stale = cross_asset.stale_instruments(instruments, today=today)
    assert stale == []


def test_build_darling_ids_auto_rolls_macro_securities(tmp_path):
    stale_sec = json.loads(MACRO_SECURITIES_FILE.read_text())
    stale_sec["CRUDE"]["id"] = "569900"
    stale_sec["CRUDE"]["_expiry"] = "2026-10-19"
    sec_path = tmp_path / "macro_securities.json"
    sec_path.write_text(json.dumps(stale_sec, indent=2))
    out_path = tmp_path / "darling_ids.json"

    out = sm.build_darling_ids(
        symbols=[],
        fetch_fn=lambda _url: _synthetic_mcx_csv(),
        out_path=out_path,
        securities_path=sec_path,
        today=date(2026, 11, 1),
    )
    assert out["commodities"]["CRUDEOIL"]["id"] == "573422"
    assert out["commodities"]["GOLD"]["id"] == "495213"
    assert out["commodities"]["SILVER"]["id"] == "495214"
    assert "CRUDE" in out["mcx_roll"]["rolled"]

    assert json.loads(sec_path.read_text())["CRUDE"]["id"] == "569900"
    state = json.loads(sm.ACTIVE_MCX_STATE_PATH.read_text())["contracts"]
    assert state["CRUDE"]["id"] == "573422"
    assert state["CRUDE"]["_expiry"] == "2026-11-19"
    assert cross_asset.load_instruments(sec_path)["CRUDE"]["id"] == "573422"


def _write_state(path, contracts):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"as_of": "2026-11-01", "contracts": contracts}))


def test_state_file_is_read_first_and_static_config_is_the_fallback(tmp_path):
    sec_path = tmp_path / "macro_securities.json"
    sec_path.write_text(json.dumps({
        "CRUDE": {"id": "569900", "seg": "MCX_COMM", "inst": "FUTCOM",
                  "_expiry": "2026-10-19", "_next": []},
        "GOLD_INDIA": {"id": "495213", "seg": "MCX_COMM", "inst": "FUTCOM",
                       "_expiry": "2026-12-04", "_next": []}}))
    state_path = tmp_path / "state" / "active_mcx_contracts.json"
    # No state file yet: the static config is the answer.
    assert sm.load_effective_securities(sec_path, state_path)["CRUDE"]["id"] == "569900"
    _write_state(state_path, {"CRUDE": {"id": "573422", "seg": "MCX_COMM", "inst": "FUTCOM",
                                        "_expiry": "2026-11-19", "_next": []}})
    eff = sm.load_effective_securities(sec_path, state_path)
    assert eff["CRUDE"]["id"] == "573422"
    assert eff["GOLD_INDIA"]["id"] == "495213"
    got = sm.resolve_front_month("CRUDE", securities_path=sec_path, state_path=state_path,
                                 today=date(2026, 11, 1))
    assert got["id"] == "573422" and got["rolled"] is False


def test_state_older_than_a_hand_rolled_config_is_ignored(tmp_path):
    sec_path = tmp_path / "macro_securities.json"
    sec_path.write_text(json.dumps({"CRUDE": {"id": "576264", "seg": "MCX_COMM",
                                              "inst": "FUTCOM", "_expiry": "2026-12-18"}}))
    state_path = tmp_path / "state" / "active_mcx_contracts.json"
    _write_state(state_path, {"CRUDE": {"id": "573422", "seg": "MCX_COMM", "inst": "FUTCOM",
                                        "_expiry": "2026-11-19"}})
    assert sm.load_effective_securities(sec_path, state_path)["CRUDE"]["id"] == "576264"


def test_run_writes_state_not_the_tracked_config(tmp_path):
    sec_path = tmp_path / "macro_securities.json"
    sec_path.write_text(json.dumps({"CRUDE": {"id": "569900", "seg": "MCX_COMM",
                                              "inst": "FUTCOM", "_expiry": "2026-10-19",
                                              "_next": []}}))
    before = sec_path.read_text()
    report = sm.run(fetch_fn=lambda _url: _synthetic_mcx_csv(), id_map={}, wanted=[],
                    notify=False, report_path=tmp_path / "report.json",
                    securities_path=sec_path, today=date(2026, 11, 1))
    assert sec_path.read_text() == before
    assert report["mcx_roll"]["rolled"]["CRUDE"]["to_id"] == "573422"
    state = json.loads(sm.ACTIVE_MCX_STATE_PATH.read_text())
    assert state["contracts"]["CRUDE"]["id"] == "573422"


def test_state_file_is_gitignored():
    import subprocess
    rel = sm.ROOT.joinpath("data", "state", "active_mcx_contracts.json").relative_to(ROOT)
    r = subprocess.run(["git", "check-ignore", "-q", str(rel)], cwd=ROOT)
    assert r.returncode == 0
