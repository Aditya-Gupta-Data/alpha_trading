"""
The generic data-freshness guard (`src/staleness_guard.py`, 2026-08-05).

Built after the SYSTEM_XRAY finding that `data/sector_index_bars.json` sat 20
days stale while feeding a LIVE bullish veto through
sector_trend -> regime_filters, with every nightly ops card reporting ✅.

Pinned here:
  * the age arithmetic — stale is `age > tolerance × refresh_interval`, and
    the boundary is not stale (a producer that runs exactly on time is fine);
  * the headline case — a mocked 20-day-old file returns "stale" AND produces
    an alert payload naming the disabled component;
  * fail-SAFE direction — missing file / unknown name / broken clock all
    yield stale, because a muzzle that fails open is not a muzzle;
  * `alert_payload` returns None on a clean scan, so the ops card stays
    byte-identical to its pre-guard form;
  * the registry's own invariants (every artifact has a consumer, a policy,
    and only the sector bars are allowed to self-disable a component).

Hermetic: tempdir files with hand-set mtimes, injected `now`. No repo data
file is read, no clock is trusted, nothing on the network.
"""
import json
import os
import time
from pathlib import Path

import pytest

from src import staleness_guard as SG

DAY = 24 * 3600.0
NOW = 1_800_000_000.0          # a fixed, arbitrary epoch — never time.time()


def _artifact_file(tmp_path: Path, rel: str, age_days: float) -> Path:
    """Write a registered artifact into a fake ROOT and age its mtime."""
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"stub": True}))
    stamp = NOW - age_days * DAY
    os.utime(p, (stamp, stamp))
    return p


# ------------------------------------------------------- the headline case

def test_twenty_day_old_file_is_stale_and_names_its_producer(tmp_path):
    """THE bug, reproduced: the exact artifact, the exact age.

    The reason used to end in "NO PRODUCER" because nothing refreshed this
    file at all. It has one since 2026-08-11 (the Mac auto-sync agent), so
    the assertion moved from "nobody refreshes it" to "name who does" —
    a stale artifact whose refresher is unnamed is the harder incident."""
    _artifact_file(tmp_path, "data/sector_index_bars.json", 20)
    v = SG.check("sector_index_bars", now=NOW, root=tmp_path)

    assert v["state"] == SG.STALE
    assert v["age_days"] == pytest.approx(20.0, abs=0.01)
    assert v["threshold_hours"] == 3 * 24          # 3 × its 24h cadence
    assert v["policy"] == SG.IGNORE                # this one self-disables
    assert v["signal"] == "mtime"
    # the reason must carry the age, the limit, AND who was supposed to refresh it
    assert "20.0 days old" in v["reason"]
    assert "mac_auto_sync.sh" in v["reason"]


def test_twenty_day_old_file_generates_the_alert_payload(tmp_path):
    _artifact_file(tmp_path, "data/sector_index_bars.json", 20)
    payload = SG.alert_payload([SG.check("sector_index_bars", now=NOW,
                                         root=tmp_path)])

    assert payload is not None
    assert payload["count"] == 1
    assert payload["disabled"] == 1                # a component went dark
    assert payload["names"] == ["sector_index_bars"]
    assert "STALE DATA" in payload["text"]
    assert "DISABLED" in payload["text"]
    # it must say WHICH component stopped voting, not just which file died
    assert "regime_filters" in payload["text"]


# --------------------------------------------------------- age arithmetic

def test_fresh_inside_the_tolerance_window(tmp_path):
    _artifact_file(tmp_path, "data/sector_index_bars.json", 2)
    v = SG.check("sector_index_bars", now=NOW, root=tmp_path)
    assert v["state"] == SG.FRESH
    assert "fresh" in v["reason"] and "limit" in v["reason"]


def test_the_threshold_boundary_is_not_stale(tmp_path):
    """A producer that runs exactly at the limit is on time, not late —
    `age > threshold`, never `>=`. Pins the comparison against drift."""
    _artifact_file(tmp_path, "data/sector_index_bars.json", 3)
    assert SG.check("sector_index_bars", now=NOW, root=tmp_path)["state"] == SG.FRESH
    _artifact_file(tmp_path, "data/sector_index_bars.json", 3.001)
    assert SG.check("sector_index_bars", now=NOW, root=tmp_path)["state"] == SG.STALE


def test_tolerance_is_per_artifact_not_global(tmp_path):
    """darling_ids is WEEKLY with tolerance 2 = 14 days; the same 10-day age
    that would kill a daily artifact leaves it fresh."""
    _artifact_file(tmp_path, "data/darling_ids.json", 10)
    _artifact_file(tmp_path, "data/darling_tiers.json", 10)
    assert SG.check("darling_ids", now=NOW, root=tmp_path)["state"] == SG.FRESH
    assert SG.check("darling_tiers", now=NOW, root=tmp_path)["state"] == SG.STALE


def test_a_future_mtime_is_never_a_negative_age(tmp_path):
    """Clock skew between the Mac (writer) and the VM (reader) must not
    produce a nonsense age — it clamps at 0 and reads fresh."""
    _artifact_file(tmp_path, "data/darling_tiers.json", -5)
    v = SG.check("darling_tiers", now=NOW, root=tmp_path)
    assert v["age_hours"] == 0.0
    assert v["state"] == SG.FRESH


# ------------------------------------------------- content date beats mtime

def test_explicit_as_of_overrides_mtime(tmp_path):
    """A file rewritten today whose CONTENT is three weeks old is stale —
    the artifact's own as_of is the more honest signal when a caller has it."""
    _artifact_file(tmp_path, "data/darling_tiers.json", 0)
    from datetime import datetime
    old = datetime.fromtimestamp(NOW - 21 * DAY).isoformat()
    v = SG.check("darling_tiers", now=NOW, as_of=old, root=tmp_path)
    assert v["state"] == SG.STALE
    assert v["signal"] == "as_of"


def test_a_bare_date_string_as_of_is_accepted(tmp_path):
    _artifact_file(tmp_path, "data/darling_tiers.json", 0)
    from datetime import datetime
    day = datetime.fromtimestamp(NOW - 30 * DAY).date().isoformat()
    v = SG.check("darling_tiers", now=NOW, as_of=day, root=tmp_path)
    assert v["state"] == SG.STALE and v["signal"] == "as_of"


def test_an_unparseable_as_of_falls_back_to_mtime_never_invents_a_date(tmp_path):
    _artifact_file(tmp_path, "data/darling_tiers.json", 1)
    v = SG.check("darling_tiers", now=NOW, as_of="not-a-date", root=tmp_path)
    assert v["signal"] == "mtime"
    assert v["state"] == SG.FRESH


# ------------------------------------------------------- FAIL-SAFE direction

def test_a_missing_file_is_stale_not_fresh(tmp_path):
    v = SG.check("sector_index_bars", now=NOW, root=tmp_path)
    assert v["state"] == SG.MISSING
    assert SG.is_stale("sector_index_bars", now=NOW, root=tmp_path) is True


def test_an_unregistered_artifact_is_assumed_stale(tmp_path):
    """Fail-SAFE, deliberately unlike the rest of the codebase: an artifact
    nobody registered is not evidence of freshness."""
    v = SG.check("something_nobody_registered", now=NOW, root=tmp_path)
    assert v["state"] == SG.STALE
    assert "not in the staleness registry" in v["reason"]
    assert SG.is_stale("something_nobody_registered", now=NOW, root=tmp_path)


def test_an_exploding_clock_is_stale_not_fresh(tmp_path):
    _artifact_file(tmp_path, "data/sector_index_bars.json", 0)

    class Boom:
        def timestamp(self):
            raise RuntimeError("clock is on fire")

    v = SG.check("sector_index_bars", now=Boom(), root=tmp_path)
    assert v["state"] == SG.STALE
    assert "fail-safe" in v["reason"]


def test_is_stale_is_true_for_every_uncertain_case(tmp_path):
    assert SG.is_stale("sector_index_bars", now=NOW, root=tmp_path) is True   # missing
    assert SG.is_stale("nope", now=NOW, root=tmp_path) is True                # unknown
    _artifact_file(tmp_path, "data/sector_index_bars.json", 0)
    assert SG.is_stale("sector_index_bars", now=NOW, root=tmp_path) is False  # fresh


# ------------------------------------------------------------ scan + payload

def test_a_clean_scan_adds_nothing_to_the_card(tmp_path):
    """The byte-identical rule: when everything is fresh the ops card must be
    exactly what it was before this module existed."""
    for art in SG.REGISTRY.values():
        _artifact_file(tmp_path, art.rel_path, 0)
    verdicts = SG.scan(now=NOW, root=tmp_path)
    assert all(v["state"] == SG.FRESH for v in verdicts)
    assert SG.alert_payload(verdicts) is None
    assert SG.alert_payload([]) is None
    assert SG.alert_payload(None) is None


def test_scan_covers_the_whole_registry_and_a_named_subset(tmp_path):
    full = SG.scan(now=NOW, root=tmp_path)
    assert len(full) == len(SG.REGISTRY)
    assert [v["name"] for v in full] == list(SG.REGISTRY)
    subset = SG.scan(names=["darling_tiers"], now=NOW, root=tmp_path)
    assert [v["name"] for v in subset] == ["darling_tiers"]


def test_monitor_artifacts_alert_but_are_not_counted_as_disabled(tmp_path):
    """A stale fail-closed gate must be REPORTED without being switched off —
    self-disabling one would make it fail OPEN, i.e. riskier."""
    for art in SG.REGISTRY.values():
        _artifact_file(tmp_path, art.rel_path, 0)
    _artifact_file(tmp_path, "data/fo_liquidity.json", 60)
    payload = SG.alert_payload(SG.scan(now=NOW, root=tmp_path))
    assert payload["count"] == 1
    assert payload["disabled"] == 0
    assert "monitor" in payload["text"]
    assert "DISABLED" not in payload["text"]


# ------------------------------------------------------- registry invariants

def test_every_registered_artifact_declares_its_consumer_and_policy():
    for name, art in SG.REGISTRY.items():
        assert art.policy in (SG.IGNORE, SG.MONITOR), name
        assert art.consumer, name
        assert art.refresh_interval_hours > 0, name
        assert art.tolerance >= 1, name


def test_only_the_sector_bars_may_self_disable_a_component():
    """An anti-drift lock. Extending `ignore` to a fail-closed risk gate is
    the one change that would turn this safety device into a hazard; it must
    be a deliberate edit to this test, never a quiet registry line."""
    disabling = [n for n, a in SG.REGISTRY.items() if a.policy == SG.IGNORE]
    assert disabling == ["sector_index_bars"]


def test_artifacts_with_no_producer_are_declared_not_blank():
    """`producer=None` is a FINDING (nothing refreshes this), so it must be
    accompanied by a note that says so — never left as an empty field."""
    for name, art in SG.REGISTRY.items():
        if art.producer is None:
            assert art.note, f"{name}: producer=None needs an explanatory note"
            assert "PRODUCER" in art.note.upper()


def test_the_real_registry_paths_are_inside_the_repo():
    for name, art in SG.REGISTRY.items():
        assert not Path(art.rel_path).is_absolute(), name
        assert art.path().is_relative_to(SG.ROOT), name


# ------------------------------------------------------------------- the CLI

def test_cli_runs_and_prints_without_touching_the_network(capsys):
    assert SG.main([]) == 0
    out = capsys.readouterr().out
    assert "sector_index_bars" in out
    assert SG.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)
