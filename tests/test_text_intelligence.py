"""
Tests for src/text_intelligence.py — the text→JSON backend manager.

Fully offline: the Anthropic HTTP calls are injected (post_fn/get_fn), no
network, no API key needed. Ledgers are redirected to tmp paths.

    python -m pytest tests/test_text_intelligence.py -q
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import text_intelligence as ti


def msg(text):
    """A minimal Anthropic Messages API success body."""
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


# ------------------------------------------------------- JSON extraction

def test_extract_json_strips_fences_and_prose():
    assert ti._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ti._extract_json('Sure! {"b": 2} hope that helps') == {"b": 2}
    assert ti._extract_json("no json here") is None
    assert ti._extract_json("[1,2,3]") is None          # array, not object


# ------------------------------------------------------- ClaudeExtractor

def test_chat_json_posts_and_parses():
    seen = {}

    def post_fn(url, body):
        seen["url"], seen["body"] = url, body
        return msg('{"target_entity": "CRUDE", "directional_bias": -1}')

    ex = ti.ClaudeExtractor(api_key="sk-test", model="claude-opus-4-8",
                            post_fn=post_fn)
    out = ex.chat_json("You extract signals.", "Crude spikes on OPEC cut")
    assert out == {"target_entity": "CRUDE", "directional_bias": -1}
    assert seen["url"] == ti.ANTHROPIC_URL
    assert seen["body"]["model"] == "claude-opus-4-8"
    # no forbidden sampling/thinking params (400 on Opus 4.8)
    assert "temperature" not in seen["body"] and "thinking" not in seen["body"]


def test_chat_json_fail_open_paths():
    # no api key -> None, and no call attempted
    assert ti.ClaudeExtractor(api_key=None).chat_json("s", "u") is None
    # empty user -> None
    assert ti.ClaudeExtractor(api_key="k", post_fn=lambda u, b: msg("{}")
                              ).chat_json("s", "") is None
    # a refusal -> None (no content to read)
    ref = ti.ClaudeExtractor(api_key="k",
                             post_fn=lambda u, b: {"stop_reason": "refusal",
                                                   "content": []})
    assert ref.chat_json("s", "u") is None
    # network returns None -> None
    assert ti.ClaudeExtractor(api_key="k", post_fn=lambda u, b: None
                              ).chat_json("s", "u") is None


def test_is_reachable_is_key_presence():
    assert ti.ClaudeExtractor(api_key="sk").is_reachable() is True
    assert ti.ClaudeExtractor(api_key=None).is_reachable() is False


# ----------------------------------------------------------- batch path

def test_submit_and_fetch_batch_keyed_by_custom_id():
    posted = {}

    def post_fn(url, body):
        posted["url"], posted["body"] = url, body
        return {"id": "batch_123", "processing_status": "in_progress"}

    ex = ti.ClaudeExtractor(api_key="k", post_fn=post_fn)
    bid = ex.submit_batch([("h1", "sys", "headline one"),
                           ("h2", "sys", "headline two")])
    assert bid == "batch_123"
    assert posted["url"] == ti.ANTHROPIC_BATCH_URL
    assert [r["custom_id"] for r in posted["body"]["requests"]] == ["h1", "h2"]

    # poll -> ended with a results url
    ex2 = ti.ClaudeExtractor(api_key="k", get_fn=lambda u: {
        "processing_status": "ended", "results_url": "https://x/results",
        "request_counts": {"succeeded": 2}})
    status = ex2.poll_batch("batch_123")
    assert status["status"] == "ended" and status["results_url"]

    # results JSONL (unordered) -> keyed dict; errored row -> None
    results = [
        {"custom_id": "h2", "result": {"type": "succeeded",
         "message": msg('{"target_entity": "NIFTY 50"}')}},
        {"custom_id": "h1", "result": {"type": "errored"}},
    ]
    ex3 = ti.ClaudeExtractor(api_key="k", get_fn=lambda u: results)
    out = ex3.fetch_batch_results("https://x/results")
    assert out["h1"] is None
    assert out["h2"] == {"target_entity": "NIFTY 50"}


def test_fetch_batch_results_parses_raw_jsonl_text():
    line = json.dumps({"custom_id": "h1", "result": {"type": "succeeded",
                       "message": msg('{"ok": true}')}})
    ex = ti.ClaudeExtractor(api_key="k", get_fn=lambda u: line + "\n")
    assert ti_get(ex, "u")["h1"] == {"ok": True}


def ti_get(ex, url):
    return ex.fetch_batch_results(url)


# --------------------------------------------------- backend selection

def test_get_extractor_routes_by_config():
    claude = ti.get_extractor(config={"text_intelligence_backend": "claude"})
    assert isinstance(claude, ti.ClaudeExtractor)
    # default + unknown both fall back to the local Ollama extractor
    from src.local_parser import LocalExtractor
    assert isinstance(ti.get_extractor(config={}), LocalExtractor)
    assert isinstance(
        ti.get_extractor(config={"text_intelligence_backend": "bogus"}),
        LocalExtractor)


# ------------------------------------------------- budget + incremental

def test_daily_budget_cap(tmp_path):
    ledger = tmp_path / "calls.jsonl"
    day = "2026-07-15"
    assert ti.within_daily_budget(cap=2, day=day, ledger=ledger) is True
    ti.record_call(day=day, ledger=ledger)
    ti.record_call(day=day, ledger=ledger)
    assert ti.calls_today(day, ledger) == 2
    assert ti.within_daily_budget(cap=2, day=day, ledger=ledger) is False
    # a different day is a clean slate
    assert ti.within_daily_budget(cap=2, day="2026-07-16", ledger=ledger) is True


def test_incremental_ledger_skips_seen_text(tmp_path):
    ledger = tmp_path / "seen.jsonl"
    assert ti.already_processed("Crude spikes", ledger=ledger) is False
    ti.mark_processed("Crude spikes", ledger=ledger)
    assert ti.already_processed("Crude spikes", ledger=ledger) is True
    assert ti.already_processed("Rupee falls", ledger=ledger) is False


# ------------------------------------------- news_parser routes through it

def test_news_parser_uses_injected_backend_unchanged():
    # a ClaudeExtractor with a fake post_fn drives parse_headline end to end
    from src.ingestion import news_parser
    ex = ti.ClaudeExtractor(api_key="k", post_fn=lambda u, b: msg(
        '{"target_entity": "CRUDE", "event_classification": "geopolitical_shock",'
        ' "directional_bias": -0.8, "horizon_impact": "SHORT",'
        ' "confidence_score": 0.7}'))
    frame = news_parser.parse_headline("Crude spikes on OPEC cut", extractor=ex)
    assert frame is not None and frame["target_entity"] == "CRUDE"
    assert frame["directional_bias"] == -0.8


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python", "-m", "pytest", __file__, "-q"]))
