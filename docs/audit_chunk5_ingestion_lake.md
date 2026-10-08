# Adversarial audit — Chunk 5: Ingestion & Data Lake (2026-10-09)

Method: two finder lenses (data quality & staleness of values; failure modes, tokens, rate limits, cron hygiene) read the files whole; the lead confirmed or refuted each against the code, defaulting to "refuted" when uncertain. Fixes are queued for the next usage window (the 85% guardrail).

## Scope
`src/dhan_client.py`, `src/dhan_guard.py`, `src/token_provider.py`, `src/renew_token.py`, `src/data_fetcher.py`, `src/lake.py`, the candle sink in `src/live_bridge.py`, `src/ingestion/*.py`, `src/news_processor.py`, `src/nse_calendar.py`, `src/staleness_guard.py`, `src/ops_monitor.py`, the quote parsing in `src/execution/live_pricer.py`, `scripts/setup_cron.sh` and the scripts it calls.

## Findings
