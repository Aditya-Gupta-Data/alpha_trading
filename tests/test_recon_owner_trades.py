"""#188: the owner's real-money trades in the Dhan account are filed apart — never a mismatch, halt or card."""
from src.execution import recon_engine as re_


def _broker(**kw):
    b = {"reachable": True, "positions": [], "holdings": [], "orders": [], "funds": {}, "errors": []}
    b.update(kw)
    return b


POS = {"symbol": "RELIANCE", "security_id": "2885", "segment": "NSE_EQ", "net_qty": 10}
HOLD = {"symbol": "TCS", "security_id": "11536", "segment": "NSE_EQ", "qty": 5}
ORD = {"order_id": "42", "symbol": "NIFTY", "segment": "NSE_FNO", "side": "BUY", "status": "TRADED",
       "qty": 75, "filled_qty": 75}
BOOK = [{"ref": "abc12345", "account": "PAPER_10L", "underlying": "RELIANCE.NS", "margin_rs": 1.0,
         "since": None, "source": "margin_lock"}]


def test_owner_mode_is_the_production_default():
    assert re_.OWNER_TRADES_AT_BROKER is True


def test_every_broker_row_is_an_owner_trade_and_the_verdict_is_parity():
    r = re_.compare(_broker(positions=[POS], holdings=[HOLD], orders=[ORD]), BOOK)
    assert r["verdict"] == "parity" and r["mismatches"] == []
    assert [o["kind"] for o in r["owner_trades"]] == ["owner_position", "owner_holding", "owner_order"]
    assert all(o["detail"].startswith(re_.OWNER_TAG) for o in r["owner_trades"])
    assert len(r["paper_only"]) == 1                     # the paper book is still listed on its own


def test_a_same_name_position_is_still_the_owners_not_the_books():
    r = re_.compare(_broker(positions=[POS]), BOOK)      # paper also holds RELIANCE
    assert r["mismatches"] == [] and r["owner_trades"][0]["symbol"] == "RELIANCE"


def test_owner_trades_never_latch_or_card(monkeypatch, tmp_path):
    cards, trips = [], []
    monkeypatch.setattr(re_, "fetch_broker_book", lambda read_fn=None: _broker(positions=[POS], orders=[ORD]))
    monkeypatch.setattr(re_, "read_paper_book", lambda conn=None, db_path=None: BOOK)
    monkeypatch.setattr(re_, "_announce", lambda result: cards.append(result) if result.get("mismatches") else None)
    real = re_.halt_on_divergence
    monkeypatch.setattr(re_, "halt_on_divergence", lambda result, conn=None: trips.append(1) or real(result, conn))
    r = re_.run(log_path=tmp_path / "recon.jsonl")
    assert r["verdict"] == "parity" and r["halt"] == {"tripped": False, "targets": {}}
    assert cards == [] and len(r["owner_trades"]) == 2
    assert "owner trades 2" in re_.render(r) and re_.OWNER_TAG in re_.render(r)


def test_an_unread_broker_is_still_unknown_not_parity():
    r = re_.compare({"reachable": False, "errors": ["DH-902"]}, BOOK)
    assert r["verdict"] == "unknown" and r["owner_trades"] == []
