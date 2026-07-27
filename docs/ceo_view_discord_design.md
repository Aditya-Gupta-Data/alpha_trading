# The CEO-View Discord — Directive 2 design (2026-07-27)

**Owner directive:** upgrade Discord payloads from raw data to plain English;
add a distinct Morning Brief and keep the EOD/CEO-brief distinct. Constraint:
**NO NEW ENGINES** — reuse existing data sources and the one notifier door.

## One correction to the owner's own example (flagged, not silently changed)

> "Macro regime matches 2018; **executing** X"

`CLAUDE.md` Rule 5 is explicit and binding: **the Macro Regime Engine has zero
execution authority.** Nothing wires `macro_regime.declare()`'s archetype/
strategy read to sizing or entry — that is a named Department-5 decision
gated on a passed statistical test, never a code change made on initiative.

So the plain-English layer says **"the playbook currently favours X"**, never
**"executing X."** Saying "executing" would describe capital acting on a
signal that, today, cannot act on anything. That gap between wording and
reality is exactly the kind of false confidence Directive 2 exists to remove,
not add. If the owner wants execution wired later, that is Directive-1-and-
beyond territory (opportunity-cost tracking) or a fresh Dept-5 ruling — not a
wording choice made here.

## What already exists (verified 07-27, reused not rebuilt)

| Data | Source | Honesty gate already built in |
|---|---|---|
| Regime declaration + top analog + strategy playbook | `data/macro_regime.json` (`analysis.macro_regime.declare`) | `declared: bool`, gated on `SIM_FLOOR`/`MIN_ANALOGS`/`ANALOG_FLOOR` — Stage A |
| Forward validation per (archetype, phase, recipe) | `data/strategy_scoreboard.json` (`analysis.strategy_scoreboard`) | `ACCUMULATING` / `FORWARD_CONFIRMED` / `FORWARD_CONTRADICTED` / `INCONCLUSIVE` — Stage B, currently 0 cells tracked (the clock is young, per HANDOVER) |
| Firm MTM + return | `firm_mtm.render_line` | day-1 CAGR edge already handled |
| Halt status | `portfolio_manager.halt_banner_lines` | Directive 3, shipped `f15182e` |
| Open book | `equity_desk.render_book_lines`, journal open positions | existing |
| Results-date proximity | `ingestion.earnings_calendar.days_to_results` | absent = None, never guessed |

## The two-layer honesty guardrail (this is the actual mechanism, not a rule of thumb)

1. **Naming an analog at all** requires `regime_doc["horizons"][hz]["declared"] is True`.
   Not declared → the sentence names NO episode, ever; it states the reason
   (`no_comparable_match` / `best sim < floor` / `analogs < floor` /
   `cache_miss_*` / `empty_current_window`) in plain English instead.
2. **Naming a strategy as more than an in-sample preference** requires that
   cell's scoreboard status be looked up (`table[archetype][phase]`, keyed by
   `strategy_id`) and reads `FORWARD_CONFIRMED`. Anything else — including
   the common case today, `ACCUMULATING` (0 cells tracked as of 07-27) —
   renders as "in-sample preference only, forward evidence still
   accumulating" and explicitly states nothing acts on it automatically.

Both checks live in ONE function (`ceo_language.macro_regime_sentence`) so
there is exactly one place that decides whether a claim is earned — no second
call site can accidentally skip the gate.

## The build

1. **`src/ceo_language.py`** — new plain-English rendering helpers, pure
   functions over already-computed data (not a new engine: no I/O of its
   own beyond the two JSON reads it's given paths to, both optional/
   injectable for tests):
   - `macro_regime_sentence(regime_doc=None, scoreboard_doc=None, horizon="shock")`
   - `book_summary_sentence(active_total, daily_pnl, net_delta)` — turns the
     EOD card's existing numbers into one narrated line (no new data).
2. **EOD card (`eod_summary`) language upgrade** — one new leading sentence
   field built from `book_summary_sentence` over numbers the card already
   computes. No new data sources, no new field beyond the sentence itself;
   existing numeric fields stay (a CEO who wants the detail can still see it).
3. **CEO brief upgrade** — `ceo_language.macro_regime_sentence` becomes a
   new "🌍 Macro Read" field (fail-open, absent file = field omitted, byte-
   identical to today). Wording only; no new data pulled beyond the file
   already written nightly by `macro_nightly`.
4. **New `src/morning_brief.py`** — the distinct pre-open card the owner
   asked for. Deliberately does NOT re-fetch live quotes/VIX (that needs a
   live Dhan token against a market that isn't open yet — fragile, and nothing
   here needs it): it reads the same four already-computed artifacts —
   `macro_regime.json` (last night's read), `strategy_scoreboard.json`,
   `earnings_calendar.json` (results due today/soon for open + watchlist
   tickers), and the firm MTM/open-book summary. Fields: halt banner (if
   any) leads, then "🌍 Overnight Macro Read", "📅 Today's Watchlist Events",
   "💼 Book Going Into Today". One card via `notifier.fire_broadcast`
   (`event="morning_brief"`), fail-open per field like every other digest.
   **Cron: 08:05 IST Mon-Fri** — after `renew_token` (07:00) and `suggest`
   (08:00, which stays the per-ticker technical read; this is the account-
   level narrative), before `master_scheduler` (09:10).

## Explicitly NOT built

- No live VIX/NIFTY pre-market fetch (fragility, not needed for the ask).
- No change to what data macro_nightly computes — wording layer only.
- No wiring of macro/strategy signals to sizing or entry (Rule 5).
- No new database — both new-data reads are existing JSON artifacts.
