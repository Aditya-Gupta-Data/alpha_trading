# ACCOUNTS.md — the paper accounts, and what each one is for

*Written 2026-11-06 from `src/portfolio_manager.py`, `config.json` and
`DECISIONS.md`. All money here is paper. There is no broker order path in the
repository.*

The desk runs **seven paper accounts** in one database (`data/brain_map.db` on
the GCP VM). They share one signal engine but are separate books: each has its
own cash, margin locks, equity curve, halts and Discord verdicts. Most exist to
answer one question about the strategy, so read them as experiments run side
by side rather than as seven portfolios.

## The seven accounts

| Account | Start capital | Gets its trades from | Prices and exits how | Question it answers | Decision |
|---|---|---|---|---|---|
| `PAPER_10L` | ₹10,00,000 contributed (#119) | The options proposer, plus the equity desk's darling stocks | End-of-day model prices; exits through the OMS tickets the paper venue fills | The primary book: does the strategy make money at full size? | #79, #83, #102, #119 |
| `PAPER_2L` | ₹2,00,000 | A scaled copy of every signal PAPER_10L approves (at most the primary's lots) | Settles off the primary's exit, scaled by lots. The only book GEX can veto (#160) | Does the same strategy survive on a small account, where one lot is a big share of equity? | #102, #160 |
| `PAPER_2L_ROT` | ₹2,00,000 | Same signals as PAPER_2L | Same as PAPER_2L, except that when it lacks margin it may evict its weakest open trade (lowest remaining reward:risk) | Does recycling capital from weak trades into new ones beat first-come-first-served? | #115 |
| `PAPER_2L_LIVE` | ₹2,00,000 | Same signals, but only if every leg had a real bid/ask quote | Prices, marks and exits itself on live intraday quotes with the bid/ask crossed (buy at ask, sell at bid) | How much of the paper P&L survives real-market friction? | #120, #126, #130–#135 |
| `PAPER_2L_LIVE_GEX` | ₹2,00,000 | Like PAPER_2L_LIVE, but steered by the dealer-gamma (GEX) zone | Live crossed quotes, like PAPER_2L_LIVE | Does choosing the structure by gamma zone improve results? | #161 |
| `PAPER_SHADOW_LEARNER` | ₹2,00,000 | **Every raw setup**, including ones the advisory, the one-per-underlying rule (#68) and the cooldown would block. From 11-06 also long calls/puts (₹15,000 premium cap) | Live crossed quotes | Sample size for the Proving Court: what would happen if we took everything? Exempt from the ruin halt | #140, #182 |
| `PAPER_EVENTS` | ₹10,00,000 | Event cards from the event engine (policy news → LLM-estimated exposure → our materiality arithmetic → "priced in?" check), built on the Mini PC | Long OTM calls/puts only, bought at the ask, ≤ ₹5,000 risk per trade; settles itself | Can scheduled policy events be traded systematically? Isolated: never a copy of the primary | #156 |

`LIVE_ACCOUNTS` in the code (books that price and settle themselves) =
PAPER_2L_LIVE, PAPER_SHADOW_LEARNER, PAPER_2L_LIVE_GEX. `ISOLATED_ACCOUNTS` (books
with their own engine) = PAPER_EVENTS. `ROTATION_ACCOUNTS` = PAPER_2L_ROT.

## The equity desk lives inside PAPER_10L

The equity desk buys "darling" stocks (quality companies in their valuation
entry zone) with part of PAPER_10L's money, under the firm treasury's budget
(`equity_desk_capital_rs` 3,00,000 as the base; #83 routes capital between the
desks). Its exits are an ATR trailing stop, sent as OMS EXIT tickets (#107). It
has its own ruin-halt latch (#125). It is not a separate account.

## Rules that apply to every account

- **Sizing:** fixed-fractional risk per trade (`risk_per_trade_pct` 2.0 of the
  account's own equity) replaced the old ₹10k cap (#106). A single lot whose
  max loss is over 2.5% of equity is refused rather than floored to one lot
  (`max_lot_risk_pct`, #145); the learner is exempt.
- **Defined-risk spreads have no mid-trade stop** (#105). Directional spreads exit
  on the profit ratchet (#110, moved only on crossed quotes #135), at the
  forced pre-expiry exit (#129), or at expiry. Condors keep a static 65% take.
- **Ruin halt:** an account latches when its drawdown passes 10%. Since #181
  (deployed 2026-11-06) the halt reads the larger of realized and **true-equity**
  drawdown (open positions marked, from the last 2 fully priced readings ≤ 45
  minutes old). A latched account takes no new trades until the owner clears
  it. The learner is exempt.
- **Wings first:** a spread's protective BUY leg must fill before its SELL leg
  (#179); every venue sweep enforces it (#181).
- **Broker reconciliation** runs every 5 minutes in session; a divergence
  latches the desk-wide halt (#172, #179).

## Planned, switched off: the multi-bucket ledger

`multi_bucket_ledger` (M1/M4, #141, #142, #164, #170, #178) splits accounts into
capital buckets (DARLINGS, IDX_SPREADS, …) with ceilings and a halted-capital
firewall. The schema and seams are built; the config switch is **off**, so the
engine still runs on the flat per-account ledger.

## Where the numbers are

- Live state: the React desk on the dashboard box (Portfolios tab, #183), fed
  by the VM's mirror push every 15 minutes.
- Most recent figures in the docs: HANDOVER.md, 2026-11-06 blocks. On the
  15:46 readings that day, true-equity drawdowns were PAPER_10L 12.07%,
  PAPER_2L_ROT 12.14%, PAPER_2L_LIVE 12.53%, PAPER_2L_LIVE_GEX 5.25%; PAPER_2L
  was already latched (realized 10.04%). HANDOVER expected 10L, ROT and LIVE
  to latch on Monday 11-09.
