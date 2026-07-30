"""Regression tests for src/suggest.py's second-chance retry pass.

Background (2026-07-30, observation ledger): the 08:00 IST cron run loses
its FIRST few watchlist names to a transient Dhan-side DH-905 window that
has always closed by the time the rest of the run has gone through. The
fix retries the skipped names once at the END of the run. These tests pin
that behaviour with a flaky fake analyze() — no network, no email.
"""

from src import suggest


def _result_for(ticker):
    """A minimal analyze() result the describe/bucket helpers accept."""
    return {
        "ticker": ticker,
        "uptrend": True,
        "fresh_cross": False,
        "rsi": 55.0,
        "price": 100.0,
    }


class _FlakyAnalyze:
    """Fails the FIRST call for each ticker in `flaky`, succeeds after —
    the shape of the transient early-run DH-905 window."""

    def __init__(self, flaky=(), dead=()):
        self.flaky = set(flaky)
        self.dead = set(dead)
        self.calls = {}

    def __call__(self, ticker):
        n = self.calls.get(ticker, 0)
        self.calls[ticker] = n + 1
        if ticker in self.dead:
            return None
        if ticker in self.flaky and n == 0:
            return None
        return _result_for(ticker)


def _run(monkeypatch, tickers, analyze_fn):
    sent = []
    monkeypatch.setattr(suggest, "load_tickers", lambda: list(tickers))
    monkeypatch.setattr(suggest, "analyze", analyze_fn)
    monkeypatch.setattr(
        suggest, "send_digest", lambda subject, body: sent.append((subject, body))
    )
    suggest.run_once()
    return sent


def test_transient_first_pass_failure_is_recovered_on_retry(monkeypatch, capsys):
    fake = _FlakyAnalyze(flaky={"HDFCBANK.NS"})
    sent = _run(monkeypatch, ["HDFCBANK.NS", "ICICIBANK.NS", "TCS.NS"], fake)

    out = capsys.readouterr().out
    # First pass skipped it, second pass got it back.
    assert "skip  HDFCBANK.NS" in out
    assert "recovered" in out
    assert "(1 recovered on retry)" in out
    # Exactly one retry for the flaky name, one call for the healthy ones.
    assert fake.calls["HDFCBANK.NS"] == 2
    assert fake.calls["ICICIBANK.NS"] == 1
    # The recovered name reaches the digest like any other.
    assert len(sent) == 1
    body = "\n".join(sent[0][1])
    assert "HDFCBANK.NS" in body


def test_both_pass_failure_stays_skipped_and_says_so(monkeypatch, capsys):
    fake = _FlakyAnalyze(dead={"HDFCBANK.NS"})
    sent = _run(monkeypatch, ["HDFCBANK.NS", "ICICIBANK.NS"], fake)

    out = capsys.readouterr().out
    assert "still-skipped  HDFCBANK.NS" in out
    assert "recovered on retry" not in out  # zero recoveries -> plain "Done."
    assert fake.calls["HDFCBANK.NS"] == 2   # retried once, never a third time
    # The dead name is honestly absent from the digest; the healthy one is in.
    body = "\n".join(sent[0][1])
    assert "HDFCBANK.NS" not in body
    assert "ICICIBANK.NS" in body


def test_clean_run_makes_no_retry_calls(monkeypatch, capsys):
    fake = _FlakyAnalyze()
    _run(monkeypatch, ["HDFCBANK.NS", "ICICIBANK.NS"], fake)

    out = capsys.readouterr().out
    assert "skip" not in out
    assert all(n == 1 for n in fake.calls.values())
