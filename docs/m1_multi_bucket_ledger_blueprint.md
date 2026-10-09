# M1 — Multi-Bucket Ledger: architectural blueprint (2026-10-09, design only; no SQL shipped)

Goal: run Index Options, Equity Swing and Darlings (and any future book) as ring-fenced **portfolios**
inside one `brain_map.db`, each with its own capital, margin wall, halts and P&L — so a drawdown in
one bucket can never halt another — while keeping every existing ledger row and the Court's history.

## 1. Today's shape (what we migrate from)
- Primary: `account_state` (one row, id=1), `margin_locks` (keyed by journal_ref; equity-desk locks tagged `eqd:`), `equity_curve`, `account_events`, `treasury_state` (one row: equity-desk budget).
- Shadows: `paper_accounts` (PAPER_2L, PAPER_2L_ROT, PAPER_2L_LIVE, PAPER_SHADOW_LEARNER), `paper_margin_locks`, `paper_equity_curve`, `paper_account_events`, `paper_live_positions`.
- OMS: `trade_tickets` / `trade_legs` / `leg_events` carry `account_id`.
- Court: `outcomes` (journal_ref), `shadow_trades` (mode, host_ref), `net_equity_history` (account_id).
- Off-DB: `data/journal.jsonl` (options, primary verdict + `accounts{}` per shadow), `logs/equity_shadow_journal.jsonl` (darlings).

## 2. New tables
```sql
portfolios (
  portfolio_id   TEXT PRIMARY KEY,         -- 'IDX_SPREADS', 'EQ_SWING', 'DARLINGS', 'SHADOW_LEARNER', ...
  account_id     TEXT NOT NULL,            -- the owning paper account (PAPER_10L, PAPER_2L, ... ) → one account hosts many buckets
  strategy_family TEXT NOT NULL,           -- 'index_options' | 'equity_swing' | 'darlings' | 'learner'
  starting_capital REAL NOT NULL,
  realized_pnl   REAL NOT NULL DEFAULT 0,
  peak_equity    REAL NOT NULL,
  risk_per_trade_pct REAL NOT NULL,        -- #106 per bucket
  max_drawdown_pct   REAL NOT NULL,        -- ruin latch per bucket
  daily_loss_pct     REAL NOT NULL,        -- daily breaker per bucket
  halted_at      TEXT, halt_reason TEXT,   -- the latch lives HERE, not on the account
  created_at     TEXT NOT NULL, retired_at TEXT
);
capital_allocations (                      -- the treasury's moves, append-only (replaces one-row treasury_state)
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, portfolio_id TEXT NOT NULL REFERENCES portfolios,
  delta_rs REAL NOT NULL, from_portfolio_id TEXT, reason TEXT NOT NULL, actor TEXT NOT NULL
);
portfolio_margin_locks (                   -- ONE lock table for every bucket (primary + shadows)
  portfolio_id TEXT NOT NULL REFERENCES portfolios, journal_ref TEXT NOT NULL,
  margin_rs REAL NOT NULL, lots INTEGER NOT NULL DEFAULT 1, locked_at TEXT NOT NULL,
  released_at TEXT, pnl_net REAL, PRIMARY KEY (portfolio_id, journal_ref)
);
portfolio_equity_curve (portfolio_id, ts, equity, peak_equity, drawdown_pct);
portfolio_events (portfolio_id, ts, event_type, journal_ref, detail);
```
Column additions (nullable, additive — #25 discipline): `portfolio_id` on `trade_tickets`, `paper_live_positions`, `outcomes`, `shadow_trades`, `net_equity_history`, `margin_locks`, `paper_margin_locks`; `portfolio_id` on every journal row (`journal.jsonl`) and darling entry (`equity_shadow_journal.jsonl`).
Views keep today's readers alive: `account_state_v` (sum of a host account's buckets), `paper_accounts_v`.

## 3. Risk partitioning
- **Margin:** `request_entry(portfolio_id, …)` checks `starting_capital + realized_pnl − SUM(open locks)` of THAT bucket only. A bucket's locks never count against another bucket of the same account; the account-level view is a report, never a gate.
- **P&L:** settlement credits `portfolios.realized_pnl` of the bucket on the lock; `portfolio_equity_curve` is per bucket; the account view sums them.
- **Halts:** the ruin latch and the daily breaker compare a bucket's own equity to its own `max_drawdown_pct` / `daily_loss_pct`; `halted_at` sits on the bucket. Options spreads at −10% halt IDX_SPREADS; DARLINGS keeps trading. (The #125 desk latch becomes the DARLINGS bucket's latch; `treasury_state` becomes a `capital_allocations` history.)
- **One door stays one door:** `portfolio_manager.request_entry` / `release_entry` gain a `portfolio_id`; `plan_tracker`, `live_pricer._settle` and `equity_desk.settle_exit` pass the bucket on the row. The Shadow Learner is simply a bucket with `max_drawdown_pct = NULL` (no wall), expressed in data instead of an `if account ==` branch.

## 4. Migration (zero data loss, reversible)
1. Additive DDL only: create the new tables, add nullable `portfolio_id` columns. Nothing dropped, nothing rewritten in place.
2. Seed buckets: `IDX_SPREADS` ← `account_state` (its capital and realized P&L minus the darling share), `DARLINGS` ← `treasury_state` budget + the `eqd:` locks' P&L, one bucket per existing paper account (`PAPER_2L/IDX`, `ROT/IDX`, `LIVE/IDX`, `SHADOW_LEARNER/IDX`).
3. Backfill `portfolio_id` from what each row already says: `margin_locks.journal_ref LIKE 'eqd:%'` → DARLINGS else IDX_SPREADS; `paper_*` by account; `outcomes`/`shadow_trades` by their journal_ref's host; `trade_tickets` by `account_id` + ticket source; journal rows by `spread` present (IDX) vs darling ledger (DARLINGS).
4. Reconcile before cut-over: `SUM(bucket.equity) == account equity` and `SUM(open bucket locks) == open account locks`, per account, printed as a PARITY line (the recon-engine pattern). Mismatch = stop, nothing switched.
5. Cut-over behind a config switch `multi_bucket_ledger: true`: writers take the bucket path; readers fall back to the views. The old tables stay as the audit trail (append-only ledgers untouched: outcomes, macro ledgers, journal history).
6. Tooling: `scripts/migrate_m1_buckets.py` with `--dry-run` default and `--apply` (the K2/D5 pattern), one `account_events` row per step; Issue-31 rule: open-book dry-run on the VM first.

## 5. Routing
- `options_proposer.build_proposal` stamps `proposal["portfolio_id"]` from the strategy family (`strategy_router.family_of` → IDX_SPREADS; the learner path → SHADOW_LEARNER); `to_journal_entry` carries it; `evaluate_shadow_accounts` judges each shadow's bucket; approval issues tickets with the bucket; `live_bridge` / `live_pricer` read it off the row for marks, exits and settlement.
- `equity_shadow_proposer` / `equity_desk.fund_entry` stamp DARLINGS; a future EQ_SWING proposer stamps its own.
- Court: `shadow_trades.portfolio_id` lets the Proving Court read evidence per bucket; `strategy_registry` keys cells by (bucket, archetype).
