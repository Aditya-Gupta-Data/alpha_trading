"""Hermetic tests for scripts/triage_transcripts.py (Pillar 4 semantic friction counter)."""
from __future__ import annotations

import csv
import json

import scripts.triage_transcripts as tt

TRANSCRIPT = (
    "Good evening and welcome to the Q2 call.\n\n"
    "We saw inventory buildup in the trade channel during the quarter.\n\n"
    "Container freight delays added two weeks to our export cycle.\n\n"
    "We took a price hike of 3% in September across the portfolio.\n\n"
    "We see no supply chain disruption at present."
)


def _empty(**over):
    counts = dict.fromkeys(tt.CONCEPTS, 0)
    evidence = {k: [] for k in tt.CONCEPTS}
    for k, quotes in over.items():
        counts[k] = len(quotes)
        evidence[k] = quotes
    return {"counts": counts, "evidence": evidence}


def _good_reply():
    return _empty(
        inventory_buildup=["We saw inventory buildup in the trade channel during the quarter."],
        freight_delays=["Container freight delays added two weeks to our export cycle."],
        price_hikes=["We took a price hike of 3% in September across the portfolio."],
    )


def _triage(tmp_path, llm, **kw):
    return tt.SemanticFrictionTriage(
        transcripts_dir=tmp_path / "transcripts", llm=llm, model="mock:router",
        lake_dir=tmp_path / "lake", log_path=tmp_path / "alt_data.jsonl", **kw,
    )


def _write_txt(tmp_path, name="TCS_2026-10-09_Q2FY27.txt", text=TRANSCRIPT):
    d = tmp_path / "transcripts"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _rows(tmp_path):
    with (tmp_path / "lake" / "semantic_friction.csv").open() as fh:
        return list(csv.DictReader(fh))


def test_prompt_is_strict_counter_with_exact_schema():
    for k in tt.CONCEPTS:
        assert k in tt.SYSTEM_PROMPT
    assert "do NOT judge" in tt.SYSTEM_PROMPT
    assert "ONLY one JSON object" in tt.SYSTEM_PROMPT


def test_txt_transcript_counts_land_in_csv_with_key_columns(tmp_path):
    _write_txt(tmp_path)
    calls = []

    def llm(system, user):
        calls.append((system, user))
        return _good_reply()

    out = _triage(tmp_path, llm).run()
    assert out["status"] == "ok" and out["rows_added"] == 1
    assert calls[0][0] == tt.SYSTEM_PROMPT and "inventory buildup" in calls[0][1]
    (row,) = _rows(tmp_path)
    assert (row["ticker"], row["date"], row["quarter"]) == ("TCS", "2026-10-09", "Q2FY27")
    assert row["inventory_buildup"] == "1" and row["freight_delays"] == "1" and row["price_hikes"] == "1"
    assert row["supply_chain_disruption"] == "0" and row["demand_softening"] == "0"
    assert row["model"] == "mock:router" and row["prompt_version"] == tt.PROMPT_VERSION


def test_hallucinated_quotes_and_inflated_counts_are_not_counted(tmp_path):
    _write_txt(tmp_path)
    reply = _good_reply()
    reply["counts"]["demand_softening"] = 4
    reply["evidence"]["demand_softening"] = ["Demand softened sharply in rural markets this quarter."]
    reply["counts"]["inventory_buildup"] = 2
    reply["evidence"]["inventory_buildup"] *= 2
    _triage(tmp_path, lambda s, u: reply).run()
    (row,) = _rows(tmp_path)
    assert row["demand_softening"] == "0"
    assert row["inventory_buildup"] == "1"
    assert row["llm_claimed_total"] == str(2 + 1 + 1 + 0 + 4)


def test_non_json_reply_is_retried_then_accepted(tmp_path):
    _write_txt(tmp_path)
    replies = iter([None, {"counts": "lots"}, _good_reply()])
    users = []

    def llm(system, user):
        users.append(user)
        return next(replies)

    out = _triage(tmp_path, llm).run()
    assert out["rows_added"] == 1 and len(users) == 3
    assert tt.RETRY_SUFFIX in users[1]
    log = [json.loads(ln) for ln in (tmp_path / "alt_data.jsonl").read_text().splitlines()]
    assert [e["code"] for e in log] == ["BAD-JSON", "BAD-JSON"]


def test_transcript_skipped_when_every_attempt_fails_and_later_ones_still_run(tmp_path):
    _write_txt(tmp_path, "AAA_2026-10-01_Q2FY27.txt", "We saw inventory buildup in the trade channel this year.")
    _write_txt(tmp_path, "BBB_2026-10-02_Q2FY27.txt", TRANSCRIPT)

    def llm(system, user):
        if "this year" in user:
            raise ValueError("router returned prose")
        return _good_reply()

    out = _triage(tmp_path, llm, max_attempts=2).run()
    assert out["status"] == "ok" and out["rows_added"] == 1
    assert [r["ticker"] for r in _rows(tmp_path)] == ["BBB"]
    codes = [json.loads(ln)["code"] for ln in (tmp_path / "alt_data.jsonl").read_text().splitlines()]
    assert "TRANSCRIPT-SKIPPED" in codes and codes.count("LLM-ERROR") == 2


def test_rerun_is_idempotent_and_skips_the_llm(tmp_path):
    _write_txt(tmp_path)
    calls = []

    def llm(system, user):
        calls.append(1)
        return _good_reply()

    _triage(tmp_path, llm).run()
    out = _triage(tmp_path, llm).run()
    assert out["reason"] == "nothing_new" and len(calls) == 1
    assert len(_rows(tmp_path)) == 1
    _triage(tmp_path, llm, force=True).run()
    assert len(calls) == 2 and len(_rows(tmp_path)) == 1


def test_csv_transcripts_join_rows_per_key_and_skip_bad_rows(tmp_path):
    d = tmp_path / "transcripts"
    d.mkdir()
    with (d / "batch.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "date", "quarter", "text"])
        w.writeheader()
        w.writerow({"ticker": "infy", "date": "2026-10-16", "quarter": "q2fy27",
                    "text": "We saw inventory buildup in the trade channel during the quarter."})
        w.writerow({"ticker": "INFY", "date": "2026-10-16", "quarter": "Q2FY27",
                    "text": "Container freight delays added two weeks to our export cycle."})
        w.writerow({"ticker": "BAD", "date": "16/10/2026", "quarter": "Q2FY27", "text": "x"})
    docs = tt.load_transcripts(d)
    assert len(docs) == 1 and docs[0]["ticker"] == "INFY" and "freight delays" in docs[0]["text"]
    _triage(tmp_path, lambda s, u: _good_reply()).run()
    (row,) = _rows(tmp_path)
    assert row["inventory_buildup"] == "1" and row["freight_delays"] == "1" and row["price_hikes"] == "0"


def test_long_transcript_is_chunked_and_counts_summed(tmp_path):
    para = "Container freight delays added two weeks to our export cycle."
    text = "\n\n".join([para] + ["filler " * 300] * 3 + [para.replace("two", "three")])
    _write_txt(tmp_path, text=text)
    seen = []

    def llm(system, user):
        seen.append(user)
        quotes = [q for q in (para, para.replace("two", "three")) if q in user]
        return _empty(freight_delays=quotes)

    chunks = tt.chunk_text(text, limit=2_500)
    assert len(chunks) > 1 and all(len(c) <= 2_500 for c in chunks)
    _triage(tmp_path, llm, chunk_chars=2_500).run()
    (row,) = _rows(tmp_path)
    assert len(seen) > 1 and row["freight_delays"] == "2" and row["chunks"] == str(len(seen))


def test_validate_reply_rejects_off_schema():
    good = _good_reply()
    assert tt.validate_reply(good) is good
    bad_bool = _good_reply()
    bad_bool["counts"]["price_hikes"] = True
    missing = _good_reply()
    del missing["evidence"]["freight_delays"]
    assert tt.validate_reply(bad_bool) is None
    assert tt.validate_reply(missing) is None
    assert tt.validate_reply("not json") is None


def test_missing_directory_degrades_cleanly(tmp_path):
    out = _triage(tmp_path, lambda s, u: _good_reply()).run()
    assert out["ok"] is False and out["status"] == "degraded"
    assert not (tmp_path / "lake" / "semantic_friction.csv").exists()
