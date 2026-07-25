# research_archive/ — parked code, kept for reuse

Modules moved out of the live production path by the Phase-1 hygiene audit
(2026-07-25). Nothing here is imported by any cron job, systemd service,
LaunchAgent, or MCP server — verified by an AST dependency trace from every
real entrypoint (see `scripts/setup_cron.sh`, `CRON_SETUP.md`, `.mcp.json`).

**Rules:**
- Code here is FROZEN REFERENCE — logic kept so we never rewrite it from
  scratch, but it does not run, is not tested in CI, and is not maintained.
- `tests/` here are the archived modules' original tests, extracted verbatim;
  their imports still point at the old `src.` paths on purpose. `pytest.ini`
  (`testpaths = tests`) keeps them out of collection.
- To resurrect a module: move it back under `src/`, restore its tests into
  `tests/`, re-run the suite, and update `MODULES.md` in the same commit.

| File | Was | Why archived |
|---|---|---|
| `trade.py` | `src/trade.py` | Phase-3 manual paper-trading CLI, superseded by `master_scheduler` + `options_proposer`. |
| `view_positions.py` | `src/view_positions.py` | Standalone position viewer; no callers. |
| `model_benchmarker.py` | `scripts/model_benchmarker.py` | Benchmarked local LLMs for the annual-report pipeline; moved with that pipeline. |
| `analysis/annual_report_analyzer.py` | `src/analysis/…` | Annual-report forensic pipeline (parked per owner). |
| `analysis/business_metrics.py` | `src/analysis/…` | The 'darling' AR reader — same parked pipeline. |
| `analysis/cohort_comparator.py` | `src/analysis/…` | Same parked pipeline. |
| `analysis/conviction.py` | `src/analysis/…` | Research-stage conviction engine; MODULES.md already labelled it orphan. |
| `analysis/institutional_alpha.py` | `src/analysis/…` | Research signal, no production caller. |
| `analysis/liquidity_rank.py` | `src/analysis/…` | Only caller was `ticker_dossier` (also archived). |
| `analysis/ticker_dossier.py` | `src/analysis/…` | Cross-store lookup CLI; no callers, no tests. |
| `ingestion/flows_backfill.py` | `src/ingestion/…` | One-shot FII/DII historical file-ingestor; forward days come from `flows_tracker`. |
| `ingestion/fundamental_parser.py` | `src/ingestion/…` | No references anywhere. |
| `tests/test_annual_report_analyzer.py` | `tests/…` | Analyzer + cohort_comparator tests. |
| `tests/test_annual_report_analyzer_pipeline.py` | half of `tests/test_report_pipeline.py` | Analyzer half; the live `report_downloader` half stayed. |
| `tests/test_conviction_institutional_alpha.py` | part of `tests/test_analysis_signals.py` | conviction + institutional_alpha sections. |

**Deliberately NOT archived** despite appearing orphaned to a naive scan:
`src/brain_mcp.py` (live MCP server via `.mcp.json`) and `src/decay_engine.py`
(knowledge-graph edge decay — currently UNWIRED, a flagged latent bug, not a
duplicate; see the Phase-1 session notes).
