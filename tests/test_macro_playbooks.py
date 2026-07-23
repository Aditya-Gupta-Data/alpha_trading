"""
M3 playbook tables, fully offline: window return math matches hand
computation, excess is vs the NIFTY benchmark, aggregates state n and
name their episodes, pre-index-history episodes get no sector legs
(named, never fabricated), and dry-run writes nothing.
"""
import json

from src.analysis import macro_playbooks as PB


def _write_series(lake, key, rows):
    lake.mkdir(parents=True, exist_ok=True)
    body = "date,value\n" + "".join(
        f"{d},{'' if v is None else v}\n" for d, v in rows)
    (lake / f"{key}.csv").write_text(body)


def _sessions(n, start_day=1):
    """n consecutive July-2026 'sessions' (calendar days serve fine)."""
    return [f"2026-07-{d:02d}" for d in range(start_day, start_day + n)]


def _lake(tmp_path):
    """NIFTY flat at 100 -> P1 return 0; BANK rises 100->110 by T+10;
    IT falls 100->90; PHARMA has a hole at the window edge but usable
    sessions inside."""
    lake = tmp_path / "macro"
    days = _sessions(15)
    _write_series(lake, "NIFTY", [(d, 100.0) for d in days])
    _write_series(lake, "NIFTY_BANK",
                  [(d, 100.0 + i) for i, d in enumerate(days)])
    _write_series(lake, "NIFTY_IT",
                  [(d, 100.0 - 0.9 * i) for i, d in enumerate(days)])
    _write_series(lake, "NIFTY_PHARMA",
                  [(d, None if i == 0 else 100.0 + 0.5 * i)
                   for i, d in enumerate(days)])
    return lake


def test_episode_phase_returns_hand_checked(tmp_path):
    lake = _lake(tmp_path)
    phases, bench = PB.episode_phase_returns("2026-07-01", lake_dir=lake)
    assert bench["P1_shock"] == 0.0                    # flat benchmark
    p1 = phases["P1_shock"]
    assert abs(p1["NIFTY_BANK"]["abs"] - 0.10) < 1e-9  # 100 -> 110
    assert abs(p1["NIFTY_BANK"]["excess"] - 0.10) < 1e-9
    assert p1["NIFTY_IT"]["abs"] < 0                   # 100 -> 91
    # PHARMA's T+0 close is a hole; the window snaps to the first
    # usable session inside it and still yields a return
    assert "NIFTY_PHARMA" in p1


def test_pre_history_episode_has_no_sector_legs(tmp_path):
    lake = _lake(tmp_path)
    phases, bench = PB.episode_phase_returns("2019-01-01", lake_dir=lake)
    assert phases == {} and bench == {}


def _templates(tmp_path, members, horizon="shock", aid="A1"):
    doc = {"built_at": "t0",
           "episodes": [{"name": n, "anchor": a} for n, a in members],
           "horizons": {horizon: {
               "episodes": [{"name": n, "anchor": a, "included": True}
                            for n, a in members],
               "archetypes": [{"id": aid,
                               "members": [n for n, _ in members],
                               "medoid": members[0][0]}]}}}
    p = tmp_path / "templates.json"
    p.write_text(json.dumps(doc))
    return p


def test_build_playbooks_aggregates_with_named_n(tmp_path):
    lake = _lake(tmp_path)
    tpl = _templates(tmp_path, [("ep_new", "2026-07-01"),
                                ("ep_old", "2019-01-01")])
    out_path = tmp_path / "playbooks.json"
    doc = PB.build_playbooks(templates_path=tpl, lake_dir=lake,
                             out_path=out_path)
    assert doc["episodes_without_sector_legs"] == ["ep_old"]
    cell = doc["table"]["A1"]["P1_shock"]["NIFTY_BANK"]
    assert cell["n"] == 1                       # n STATED, tiny and honest
    assert list(cell["episodes"]) == ["ep_new"]
    assert cell["hit_rate"] == 1.0
    assert abs(cell["median_excess"] - 0.10) < 1e-9
    # a sector with zero usable episodes has NO cell at all
    assert "NIFTY_METAL" not in doc["table"]["A1"]["P1_shock"]
    assert json.loads(out_path.read_text())["table"] == doc["table"]


def test_aggregate_median_and_hitrate():
    row = PB._aggregate([("a", 0.10), ("b", -0.02), ("c", 0.04)])
    assert row["n"] == 3
    assert row["median_excess"] == 0.04
    assert abs(row["hit_rate"] - 2 / 3) < 1e-3
    assert row["min_excess"] == -0.02 and row["max_excess"] == 0.10


def test_dry_run_writes_nothing(tmp_path):
    lake = _lake(tmp_path)
    tpl = _templates(tmp_path, [("ep_new", "2026-07-01")])
    out_path = tmp_path / "playbooks.json"
    PB.build_playbooks(templates_path=tpl, lake_dir=lake,
                       out_path=out_path, dry_run=True)
    assert not out_path.exists()


def test_slow_burn_archetypes_read_on_the_slow_clock(tmp_path):
    """An S-family's table must use S1/S2/S3 windows, never P1/P2/P3 —
    the horizon-aware clock (owner directive 2026-07-23)."""
    lake = tmp_path / "macro"
    days = _sessions(15)          # enough for S1's inward-snapping edges
    _write_series(lake, "NIFTY", [(d, 100.0) for d in days])
    _write_series(lake, "NIFTY_FMCG",
                  [(d, 100.0 + i) for i, d in enumerate(days)])
    tpl = _templates(tmp_path, [("nino", "2026-07-01")],
                     horizon="slow_burn", aid="S1")
    doc = PB.build_playbooks(templates_path=tpl, lake_dir=lake,
                             out_path=tmp_path / "pb.json", dry_run=True)
    phases = set(doc["table"].get("S1", {}))
    assert phases and phases <= {"S1_buildup", "S2_peak_effect",
                                 "S3_unwind"}
    assert "P1_shock" not in doc["table"].get("S1", {})
