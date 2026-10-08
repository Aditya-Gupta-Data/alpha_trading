"""Audit Chunk 4, Batch D (B1): decided_at is stamped; stale approvals refused only when the switch is set."""
from datetime import date
from src import options_proposer as op


def test_b1_stale_refusal_is_off_by_default_and_names_the_age_when_on(monkeypatch):
    row = {"short_id": "st000001", "date": "2026-10-01"}
    monkeypatch.setattr("src.config.STALE_APPROVAL_MAX_DAYS", None)
    assert op._stale_refusal(row, date(2026, 10, 9)) is None
    monkeypatch.setattr("src.config.STALE_APPROVAL_MAX_DAYS", 0)
    why = op._stale_refusal(row, date(2026, 10, 9))
    assert "8d ago" in why and "proposal-day premiums" in why
    assert op._stale_refusal(row, date(2026, 10, 1)) is None            # same session: fine
    assert op._stale_refusal({"date": "bad"}, date(2026, 10, 9)) is None  # unreadable date: abstain
    assert op.STALE_PROPOSAL in op.APPROVAL_REFUSALS
