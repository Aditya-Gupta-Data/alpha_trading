"""Decision #118 — the desk's public link: remembered on the VM, announced
once per change through the one Discord door; the notifier classes it as a
budgeted card, never a spool-only unknown."""
from datetime import datetime

from src import dashboard_link as dl, notifier


def test_first_sighting_and_a_change_announce_once_each_same_url_is_silent(tmp_path):
    state = tmp_path / "dashboard_url.txt"
    cards = []
    now = datetime(2026, 9, 28, 22, 30)
    r = dl.announce_if_changed("https://one.trycloudflare.com", state, cards.append, now)
    assert r == {"url": "https://one.trycloudflare.com", "previous": None, "changed": True,
                 "announced": True, "error": None}
    assert state.read_text() == "https://one.trycloudflare.com\n"
    r = dl.announce_if_changed("https://one.trycloudflare.com\n", state, cards.append, now)
    assert not r["changed"] and not r["announced"] and len(cards) == 1
    r = dl.announce_if_changed("https://two.trycloudflare.com", state, cards.append, now)
    assert r["changed"] and r["previous"] == "https://one.trycloudflare.com" and len(cards) == 2
    c = cards[1]
    assert c["event"] == "dashboard_link" and "https://two.trycloudflare.com" in c["description"]
    assert c["fields"][0]["value"] == "https://one.trycloudflare.com" and c["date"] == "2026-09-28"
    assert dl.remembered(state) == "https://two.trycloudflare.com"


def test_garbage_and_a_failing_card_never_raise(tmp_path):
    state = tmp_path / "u.txt"
    r = dl.announce_if_changed("", state, lambda c: None)
    assert r["error"] == "no https url given" and not state.exists()

    def boom(_):
        raise RuntimeError("discord down")
    r = dl.announce_if_changed("https://x.trycloudflare.com", state, boom)
    assert r["changed"] and not r["announced"] and "discord down" in r["error"]
    assert dl.remembered(state) == "https://x.trycloudflare.com"      # remembered even so


def test_the_link_card_is_budgeted_not_a_spool_only_unknown(tmp_path):
    assert "dashboard_link" in notifier.BUDGET_SCHEDULED
    verdict = notifier.budget_gate({"event": "dashboard_link"}, state_path=tmp_path / "b.json",
                                   queue_path=tmp_path / "q.jsonl", enabled=True)
    assert verdict == "send"
