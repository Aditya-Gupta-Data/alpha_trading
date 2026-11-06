# docs/ — index

*Last reviewed 2026-11-06. Every file in `docs/` is listed here with what it is
and whether it still describes the system. When you add a doc, add a row.*

**How to read a doc's status:** many specs were written before they were built,
and their own header still says "PLANNING ONLY" or "not built". Where the record
shows the work was built later, the **Status** column says so and names the
decision. When a spec's header and this index disagree, trust this index, then
`DECISIONS.md`, then the spec.

## Start here (current, describes the system as it is)

| File | What it is | Status |
|---|---|---|
| [ACCOUNTS.md](ACCOUNTS.md) | The seven paper accounts, what each one tests, the rules they share | Current (11-06) |
| [BRANCHES.md](BRANCHES.md) | Work on branches that is NOT in `main` yet, plus uncommitted edits | Regenerated at every session wrap |
| [ARCHITECTURE_ROADMAP.md](ARCHITECTURE_ROADMAP.md) | The owner-fixed long-term build order: GraphRAG → Cycle Hunter → OMS / multi-bucket / multi-seat | Current order. Its header says "nothing built yet" (10-28), but Phases 1–3 were built since (#162–#166) |
| [HOME_NODE_SETUP.md](HOME_NODE_SETUP.md) | The Mini PC analytics node: setup, roles, what it may never run | Current (#143–#149) |
| [observation_week_ledger.md](observation_week_ledger.md) | The incident ledger: every bug and hotfix, verified facts only (Issues 1–58+) | Current, append-only |
| [hypotheses.md](hypotheses.md) | Ideas waiting for the Proving Court; none is a live rule | Current |
| [dev_workflow.md](dev_workflow.md) | How to work in the repo: scoped tests, full suite as the gate, parallel lanes | Current |

## Feature specs for things that are built

| File | What it is | Status |
|---|---|---|
| [gex_greek_pnl_spec.md](gex_greek_pnl_spec.md) | Dealer gamma (GEX) profiles, zero-gamma, gamma zones, Greek P&L | Built, seams 1–3 (#152–#155); steers PAPER_2L and PAPER_2L_LIVE_GEX (#160, #161) |
| [earnings_awareness_spec.md](earnings_awareness_spec.md) | Earnings proximity: labels, the hard ≤5-day gate, open-book watch | Built S1–S5 (#150, #151, #167) |
| [EVENT_PLAYBOOK.md](EVENT_PLAYBOOK.md) | The method for trading policy events (facts first, materiality, priced-in) | Built as event engine 1.1 on PAPER_EVENTS (#156) |
| [EVENT_ENGINE_PLAN.md](EVENT_ENGINE_PLAN.md) | The original event-engine plan | Historical: says itself it was superseded by the build |
| [ALTDATA_PLAYBOOK.md](ALTDATA_PLAYBOOK.md) | Alt-data discovery framework (Vahan, GeM, Agmarknet, Grid-India, NPCI UPI) and target backlog | Built as the Mini PC fleet; capture-only, edge court #177 |
| [m1_multi_bucket_ledger_blueprint.md](m1_multi_bucket_ledger_blueprint.md) | Splitting accounts into capital buckets | Schema and seams built (#141, #142, #164, #170, #178); config switch still OFF |
| [macro_regime_engine_spec.md](macro_regime_engine_spec.md) | The Macro Regime Engine (DTW over the macro lake, archetypes) | Built (M1–M4); header still says planning only. Zero execution authority |
| [macro_clustering_report.md](macro_clustering_report.md) | The engine's first clustering output (07-23) | Historical result |
| [stage_b_forward_scoring_spec.md](stage_b_forward_scoring_spec.md) | The 60-session forward-scoring clock for macro declarations | Built 07-24; still accumulating sessions |
| [strategy_registry_spec.md](strategy_registry_spec.md) | Regime-conditioned playbooks (replaced the cancelled dashboard) | SR-1 to SR-3 built 07-23; SR-4 cancelled by the owner |
| [auto_discovery_spec.md](auto_discovery_spec.md) | Unsupervised pattern scanner + significance gate | AD-1 to AD-4 built 07-23; the AD-2 significance gate has admitted nothing (closed 08-01) |
| [self_evolving_brain_map.md](self_evolving_brain_map.md) | Lifecycle locator, decayed Bayesian confidence, Brier ship gate | Built in #163 (`brain_evolution.py`); header still says planning only |
| [cycle_hunter_plan.md](cycle_hunter_plan.md) | Teaching the brain map long-term cycles; the ₹1L proof-gate budget | Partly built (#163); G1–G5 gates tracked in PROP_ROADMAP |
| [ceo_view_discord_design.md](ceo_view_discord_design.md) | Plain-English Discord + Morning Brief | Built 07-27 |
| [opportunity_cost_design.md](opportunity_cost_design.md) | Blocked trades tracked in the shadow ledger | Built 07-27 |
| [walkaway_protocol_design.md](walkaway_protocol_design.md) | The "SYSTEM PAUSED" layer for the risk-of-ruin halt | Built 07-27 (since extended by #125, #181) |
| [token_renewal_cadence.md](token_renewal_cadence.md) | One Dhan token renewal at 07:00 IST | Built; still the standard (cron job 1) |
| [dhan_broker_book_sync_plan.md](dhan_broker_book_sync_plan.md) | Read-only Dhan order-book sync and reconciliation | Recon built (#94, #118, #172, #179) |
| [local_llm_benchmark_2026-10-29.md](local_llm_benchmark_2026-10-29.md) | HP laptop local-LLM benchmark vs the FreeLLMAPI router | Result: the router stays (#148, #173) |

## Audits (all closed 2026-10-09, rulings in #121–#139)

[audit_chunk1_accounting_ledger.md](audit_chunk1_accounting_ledger.md) ·
[audit_chunk2_paper_2l_live_sweep.md](audit_chunk2_paper_2l_live_sweep.md) ·
[audit_chunk3_analytics_sizing.md](audit_chunk3_analytics_sizing.md) ·
[audit_chunk4_dashboard_bridge.md](audit_chunk4_dashboard_bridge.md) ·
[audit_chunk5_ingestion_lake.md](audit_chunk5_ingestion_lake.md) ·
[audit_chunk6_equity_desk_knowledge_graph.md](audit_chunk6_equity_desk_knowledge_graph.md)

Status lines inside them are from the day they were written; the fixes are in #122–#139.

## Not built: designs and visions on hold

| File | What it is | Status |
|---|---|---|
| [h4_simulator_experiment_design.md](h4_simulator_experiment_design.md) | Pyramid vs one-and-done simulator experiment | Draft. A harness was committed 07-27; the experiment's verdict is not in the record |
| [spread_aware_tuner_design.md](spread_aware_tuner_design.md) | Tuning entries by bid/ask spread | Draft, not built |
| [ews_phase2_spec.md](ews_phase2_spec.md) | Early-warning system × pattern engine | Vision, "do not build" |
| [thematic_playbooks_spec.md](thematic_playbooks_spec.md) | Thematic playbooks | Blueprint (07-09); the record does not show it built as written |
| [PHASE_8_NEWS_INGESTION_SPEC.md](PHASE_8_NEWS_INGESTION_SPEC.md) | Semantic news ingestion | Spec from 07-08; news now flows through the RSS ingester and the GraphRAG news shadow (#174) instead |
| [discord_ingestion_pipeline.md](discord_ingestion_pipeline.md) | Ingesting Discord JSON into the graph | Blueprint, not built |
| [commercial_tip_verifier.md](commercial_tip_verifier.md) | Verifying paid tips | Blueprint, not built; the storefront is shelved |

## History and reference material

| File | What it is |
|---|---|
| [SYSTEM_BLUEPRINT.md](SYSTEM_BLUEPRINT.md) | One-page desk map and weak points **as of 2026-09-19**. Older than the Mini PC split, the learner, GEX and events; use ARCHITECTURE.md and ACCOUNTS.md for the current picture |
| [handover_archive.md](handover_archive.md) | HANDOVER blocks before 2026-08-11 |
| [deploy_2026-07-20.md](deploy_2026-07-20.md) | The 07-20 deploy runbook |
| [v2_sandbox_state.md](v2_sandbox_state.md) | State dump of the V2 sandbox (`next_gen_engine/`), 08-16 |
| [gemini_research_gap_analysis.md](gemini_research_gap_analysis.md) | 07-15 gap analysis of an external research report |
| `Advanced Systematic NSE Options Trading.txt` | External research text (07-15), reference only |
| [archive_v0/](archive_v0/README.md) | The earliest plans and overviews (OVERVIEW, PLAN, VISION_PLAN, HOLY_GRAIL_PLAN, …). Superseded |
| `LIVE_TRADE_BOOK.md` | Generated nightly by cron job 32; git-ignored, so it exists only on the machine that made it |

## For NotebookLM

`python3 scripts/sync_to_drive.py --dry-run --output-dir <folder>` writes the
whole repository as six topic packs, each under NotebookLM's per-source limit:

| Pack | Contents |
|---|---|
| `alpha_01_guide.md` | README, CLAUDE, ARCHITECTURE, this index, ACCOUNTS, BRANCHES, MODULES, CRON_SETUP, DECISIONS and the other root docs |
| `alpha_02_history.md` | HANDOVER, the handover archive, the incident ledger |
| `alpha_03_specs.md` | Everything else in `docs/` |
| `alpha_04_config.md` | `config/` |
| `alpha_05_code_part1.md`, `_part2.md` | `src/` |

Without `--dry-run` (and with a Drive folder id set up) it uploads the packs to
Drive, updating each by name.
