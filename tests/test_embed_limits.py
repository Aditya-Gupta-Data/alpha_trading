"""Discord's hard embed limits, enforced at the one door (`notifier._build_embed`).

Discord answers HTTP 400 to the WHOLE card when any limit is breached, and a
blank field value renders as nothing. 2026-09-21: the dual paper treasury and
the OMS rows grew the evening cards past the limits.
"""
import json

from src import notifier as n


def _card(fields, event="ceo_brief", description="d"):
    return n._build_embed({"event": event, "ticker": "", "date": "2026-09-21",
                           "description": description, "fields": fields})


def _assert_valid(embed):
    assert len(embed["title"]) <= 256
    assert len(embed.get("description", "")) <= 4096
    assert 0 < len(embed["fields"]) <= 25
    for f in embed["fields"]:
        assert 1 <= len(f["name"]) <= 256
        assert 1 <= len(f["value"]) <= 1024 and f["value"].strip()
        assert isinstance(f["inline"], bool)
    assert n._embed_size(embed) <= 6000
    json.dumps({"embeds": [embed]})          # structurally serialisable


def test_a_short_card_passes_through_byte_identical():
    fields = [{"name": "A", "value": "one", "inline": True},
              {"name": "B", "value": "two\nlines", "inline": False}]
    assert _card(fields)["fields"] == fields


def test_blank_and_missing_values_never_render_empty():
    e = _card([{"name": "A", "value": ""}, {"name": "", "value": None},
               {"name": "C", "value": "   "}])
    _assert_valid(e)
    assert [f["value"] for f in e["fields"]] == ["—", "—", "—"]


def test_an_overlong_field_is_split_on_lines_not_cut():
    lines = [f"line {i:03d} " + "x" * 50 for i in range(60)]
    e = _card([{"name": "💰 Risk & Capital", "value": "\n".join(lines)},
               {"name": "After", "value": "still here"}])
    _assert_valid(e)
    names = [f["name"] for f in e["fields"]]
    assert names[0] == "💰 Risk & Capital" and "💰 Risk & Capital (cont.)" in names
    assert names[-1] == "After"
    rejoined = "\n".join(f["value"] for f in e["fields"][:-1])
    assert rejoined.split("\n") == lines      # every line survived, whole


def test_a_code_fence_is_closed_and_reopened_across_a_split():
    table = "head\n```\n" + "\n".join("ROW " + "y" * 40 for _ in range(60)) + "\n```\ntail"
    e = _card([{"name": "💼 Equity Desk", "value": table}])
    _assert_valid(e)
    assert len(e["fields"]) > 1
    for f in e["fields"]:
        assert f["value"].count("```") % 2 == 0


def test_one_giant_unbroken_line_is_hard_wrapped():
    e = _card([{"name": "A", "value": "z" * 3000}])
    _assert_valid(e)
    assert sum(f["value"].count("z") for f in e["fields"]) == 3000


def test_the_6000_total_is_held_and_the_trim_is_announced():
    e = _card([{"name": f"S{i}", "value": "q" * 1000} for i in range(12)])
    _assert_valid(e)
    assert e["fields"][0]["name"] == "S0"              # the head is what survives
    assert e["fields"][-1]["name"] == "✂️ Card trimmed"
    assert "trailing section(s)" in e["fields"][-1]["value"]


def test_more_than_25_fields_are_capped():
    e = _card([{"name": f"F{i}", "value": "v"} for i in range(40)])
    _assert_valid(e)
    assert e["fields"][-1]["name"] == "✂️ Card trimmed"


def test_overlong_title_and_description_are_clipped():
    e = n._build_embed({"event": "x" * 400, "ticker": "T", "date": "d",
                        "description": "w" * 9000, "fields": []})
    assert len(e["title"]) <= 256 and len(e["description"]) <= 4096


def test_dual_treasury_sized_risk_field_keeps_its_mtm_line():
    """The regression itself: the MTM line rides LAST in Risk & Capital and a
    blind [:1024] was eating it once the field grew."""
    from src import ceo_brief
    risk = {"available": True, "daily_pnl": 0.0, "net_delta": -405.0,
            "open_spreads": 6, "open_equities": 0, "resolved": 0,
            "wilson": "w" * 300, "drawdown": "d" * 300, "blocked": "b" * 300,
            "ages": ["• " + "a" * 200]}
    field = ceo_brief._risk_field(risk)
    assert len(field["value"]) > 1024 and "Firm MTM" in field["value"]
    e = _card([field])
    _assert_valid(e)
    assert any("Firm MTM" in f["value"] for f in e["fields"])
