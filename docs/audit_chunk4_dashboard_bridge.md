# Adversarial audit — Chunk 4: Dashboard & Bridge (2026-10-09)

Method: three finder lenses (data truth on the dashboard, the API server + Discord bot, reporting honesty of the Discord cards) read the files whole; the lead confirmed or refuted each against code and the VM mirror snapshot (10-09 00:52, read-only), defaulting to "refuted" when uncertain.

## Scope
`src/dashboard/{app,data,benchmarks,api_bridge,mirror_snapshot}.py`, `src/equity_history.py`, `scripts/publish_dashboard_mirror.sh`, `src/portfolio_report.py`, `src/market_snapshot.py`, `src/firm_mtm.py`, `src/positions.py`, `src/api.py`, `src/api_server.py`, `src/discord_bot.py`, `src/discord_client.py`, `src/notifier.py` (budget/spool/card builders), `src/human_pulse.py`, `src/ceo_brief.py`, `src/ceo_language.py`, `src/morning_brief.py`, `src/eod_summary.py`, `src/ops_monitor.py`, `src/validation/digest.py`. The React desk (`frontend/`) was being edited by a parallel session and is out of scope.

## Findings
