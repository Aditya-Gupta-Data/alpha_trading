"""
Dept 1 clerk `ingestion/report_downloader.py`, fully offline: queue reading,
NSE listing parsing, idempotency, outage codes, the never-crash loop —
against fake fetchers (no network, tmp dirs).

(The analyzer half moved to research_archive/tests/ with
annual_report_analyzer.py, Phase-1 cleanup 2026-07-25.)
"""
import json

from src.ingestion import report_downloader as RD


# ------------------------------------------------------------- downloader

LISTING = {"data": [
    {"fromYr": "2023", "toYr": "2024", "fileName": "https://x/AR_old.pdf"},
    {"fromYr": "2024", "toYr": "2025", "fileName": "https://x/AR_new.pdf"},
    {"fromYr": "2025", "toYr": "2026", "fileName": "https://x/notes.zip"},
]}


def test_latest_report_picks_newest_pdf_only():
    row = RD.latest_report(LISTING)
    assert row["toYr"] == "2025"            # the 2026 row isn't a PDF
    assert RD.latest_report({"data": []}) is None
    assert RD.latest_report(None) is None


def test_latest_report_fiscal_picks_exact_year_not_newest():
    row = RD.latest_report(LISTING, fiscal="2024")
    assert row["fromYr"] == "2023" and row["toYr"] == "2024"
    # a year with no usable row is an honest None, never a fallback
    assert RD.latest_report(LISTING, fiscal="2019") is None


def test_fetch_one_honours_fiscal_year(tmp_path):
    r = RD.fetch_one("RELIANCE.NS", fetch_json_fn=lambda u: LISTING,
                     fetch_bytes_fn=lambda u: b"%PDF-1.7 fake",
                     out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                     sleep_fn=lambda s: None, fiscal="2024")
    assert r["status"] == "downloaded"
    assert (tmp_path / "RELIANCE" / "AR_RELIANCE_2023_2024.pdf").exists()

    miss = RD.fetch_one("RELIANCE.NS", fetch_json_fn=lambda u: LISTING,
                        fetch_bytes_fn=lambda u: b"%PDF-1.7 fake",
                        out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                        sleep_fn=lambda s: None, fiscal="2019")
    assert miss["status"] == "outage" and miss["code"] == "RD-404"


def test_fetch_one_downloads_and_is_idempotent(tmp_path):
    calls = []

    def fake_json(url):
        calls.append(url)
        return LISTING

    r = RD.fetch_one("RELIANCE.NS", fetch_json_fn=fake_json,
                     fetch_bytes_fn=lambda u: b"%PDF-1.7 fake",
                     out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                     sleep_fn=lambda s: None)
    assert r["status"] == "downloaded"
    assert "symbol=RELIANCE" in calls[0]     # .NS stripped for NSE
    assert (tmp_path / "RELIANCE" / "AR_RELIANCE_2024_2025.pdf").exists()

    again = RD.fetch_one("RELIANCE.NS", fetch_json_fn=fake_json,
                         fetch_bytes_fn=lambda u: b"%PDF-1.7 fake",
                         out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                         sleep_fn=lambda s: None)
    assert again["status"] == "already_have"


def test_fetch_one_rejects_a_non_pdf_body(tmp_path):
    r = RD.fetch_one("TCS", fetch_json_fn=lambda u: LISTING,
                     fetch_bytes_fn=lambda u: b"<html>rate limited</html>",
                     out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                     sleep_fn=lambda s: None)
    assert r["status"] == "outage" and r["code"] == "RD-500"
    logged = (tmp_path / "out.jsonl").read_text()
    assert "RD-500" in logged


def test_fetch_one_survives_a_dead_api_with_one_retry(tmp_path):
    attempts, naps = [], []

    def dead(url):
        attempts.append(url)
        raise ConnectionError("HTTP Error 401: refused")

    r = RD.fetch_one("INFY", fetch_json_fn=dead,
                     fetch_bytes_fn=lambda u: b"",
                     out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                     sleep_fn=naps.append)
    assert r["status"] == "outage" and r["code"] == "RD-401"
    assert len(attempts) == 2 and naps == [RD.RETRY_PAUSE]


def test_run_loop_never_crashes_and_summarizes(tmp_path):
    def flaky(url):
        if "BAD" in url:
            raise ValueError("boom")
        return LISTING

    out = RD.run(tickers=["RELIANCE", "BAD", "TCS"],
                 fetch_json_fn=flaky, fetch_bytes_fn=lambda u: b"%PDF ok",
                 out_dir=tmp_path, log_path=tmp_path / "out.jsonl",
                 sleep_fn=lambda s: None)
    assert out["attempted"] == 3
    assert out["summary"]["downloaded"] == 2
    assert out["summary"]["outage"] == 1


def test_load_queue_reads_step1_output_and_fails_honest(tmp_path):
    q = tmp_path / "queue.json"
    q.write_text(json.dumps({"tickers": ["RELIANCE.NS", "VEDL"]}))
    assert RD.load_queue(q) == ["RELIANCE.NS", "VEDL"]
    assert RD.load_queue(tmp_path / "missing.json") == []


