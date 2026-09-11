# Dhan Orders API — read-only broker-book sync: requirements and plan

*Placed in `docs/` on 2026-09-11 as a live design spec (designed, not
scheduled, not built). Indexed from `ROADMAP.md`. The HANDOVER block of the
same date records the session; decision #94 records the scoping.*

**Status: v1.0 requirements agreed with the owner on 2026-09-11. PLAN ONLY.
No code exists. Building requires an explicit go from the owner, and the
first commit of that build must be the DECISIONS.md entry that permits a
read-only broker-book reader while keeping order placement forbidden.**

**Rule 7 is unchanged by this document.** No broker/order path exists in
`src/`. Nothing here is on any execution path.

## 1. Context

The owner asked to integrate Dhan's Orders API into the alpha_trading desk and to agree requirements first, confirming details against the official Dhan connector (the Dhan MCP server attached to this session).

Where the repo stands today (verified in the repo and by the explorer):

- `src/dhan_client.py` is **data-only**. No order method exists anywhere in `src/`.
- Every planning document forbids an order path: `CLAUDE.md` Rule 7, `ROADMAP.md` "Not on this roadmap", `HANDOVER.md` standing constraints, decisions #9, #11, #31, #41, #45, #53, #83, #91. `PROP_ROADMAP.md` M2 says "do not pre-build the execution layer".
- **V1 CODE FREEZE** (owner, 2026-08-05): no new execution features; observability and hygiene permitted.
- Critic's readiness verdict 2026-09-09: not ready for real capital (29 resolved trades, per-trade Sharpe 0.23).
- Token rules: one active Dhan token per client id (#48); the Mac holds no live token and only the VM renews (07:00 IST). Account-control secrets live in GCP Secret Manager (#47).

### Owner decisions taken 2026-09-11 (this session)

| Question | Decision |
|---|---|
| Scope of first release | **Read-only sync**: order book, trade book, super-order book, positions, funds into the desk. No placement, modify or cancel. |
| Lift the "no broker path" house rule? | **Not yet — plan only.** Nothing in the repo changes until this document is agreed. |
| Which desk first | **Both**: equity darling book (NSE_EQ) and options spreads (NSE_FNO) from day one. |
| Where it runs | **Mac only for now.** |

Consequence: read-only sync is an *observability* feature, which the code freeze permits, and it needs no static-IP whitelisting. It is also the first row of the `PROP_ROADMAP.md` M2 gap table ("reconciliation against the broker's book") built in the safest possible order.

## 2. What the official Dhan connector and docs confirmed (2026-09-11)

Live calls made through the Dhan MCP today, all read-only:

| Call | Result |
|---|---|
| Order book (list) | no orders today |
| Super order book | none |
| Trade book (today) | none |
| Positions | none open |
| Holdings | `DH-1111 No holdings available` (an empty state returned as an error code) |
| Funds | available ₹1,411.18, utilised ₹0 |
| Margin calc (1 × RELIANCE CNC @ ₹1,400) | total margin ₹1,400, leverage 1X, insufficient by ₹11.18 |

The MCP token worked while the VM's data token is believed live (09-10 sweep captured 88/88). **Unverified inference:** the MCP's consent-flow token may not collide with the VM's web/renew token under decision #48. Must be tested before relying on it.

Endpoints (base `https://api.dhan.co/v2/`, header `access-token`):

| Purpose | Method + path | Needed for read-only release |
|---|---|---|
| Order book | `GET /orders` | yes |
| Order by id | `GET /orders/{order-id}` | yes |
| Order by correlation id | `GET /orders/external/{correlation-id}` | later |
| Trade book (today) | `GET /trades` | yes |
| Trades by order | `GET /trades/{order-id}` | yes |
| Trade history (multi-day, paginated) | `GET /trades/{from}/{to}/{page}` dates `YYYY-MM-DD`, page from 0 | yes (backfill) |
| Super order book | `GET /super/orders` | yes |
| Positions / holdings / funds | portfolio endpoints (via SDK) | yes |
| Ledger | `GET /ledger?from-date&to-date` | optional |
| Place / modify / cancel / slice | `POST /orders`, `PUT`, `DELETE /orders/{id}`, `POST /orders/slicing` | **no — out of scope** |
| Super place / modify / cancel | `POST /super/orders`, `PUT`, `DELETE /super/orders/{id}/{leg}` | **no — out of scope** |
| Live order updates | `wss://api-order-update.dhan.co`, auth by first message `{LoginReq:{MsgCode:42, ClientId, Token}, UserType:"SELF"}` | later |
| Postback | HTTP POST to a public URL configured at token generation | later, VM only |

Enums to model (exact strings): order status `TRANSIT, PENDING, REJECTED, CANCELLED, PART_TRADED, TRADED, EXPIRED`, plus super-order only `CLOSED, TRIGGERED`; leg names `ENTRY_LEG, TARGET_LEG, STOP_LOSS_LEG`; segments `NSE_EQ, NSE_FNO, BSE_EQ, BSE_FNO, MCX_COMM, NSE_CURRENCY, BSE_CURRENCY, IDX_I`; product `CNC, INTRADAY, MARGIN, MTF, CO, BO`; order type `LIMIT, MARKET, STOP_LOSS, STOP_LOSS_MARKET`; validity `DAY, IOC`; transaction `BUY, SELL`.

Order-book record fields: `orderId, dhanClientId, correlationId, orderStatus, transactionType, exchangeSegment, productType, orderType, validity, tradingSymbol, securityId, quantity, disclosedQuantity, price, triggerPrice, afterMarketOrder, boProfitValue, boStopLossValue, legName, algoId, filledQty, remainingQuantity, averageTradedPrice, omsErrorCode, omsErrorDescription, createTime, updateTime, exchangeTime, drvExpiryDate, drvOptionType, drvStrikePrice`.

Trade record fields: `orderId, exchangeOrderId, exchangeTradeId, tradedQuantity, tradedPrice, ...` and in trade history additionally `brokerageCharges, stt, sebiTax, stampDuty, exchangeTransactionCharges, serviceTax, isin, customSymbol`.

Limits and rules that bind the design:

- Rate limits: Order APIs 10/s, 250/min, 1000/h, 7000/day. Non-trading APIs 20/s. Data APIs 5/s. Must route through the host-wide throttle in `src/dhan_client.py` (`_throttle`, `data/.dhan_throttle`).
- Static-IP whitelisting is required **only** for placement (orders, super, forever). Reads need none. Whitelisted IPs cannot change for 7 days.
- Web token validity 24 h; consent-flow API credentials 12 months. One active token per client id (#48).
- **No idempotency guarantee** is documented for placement; `correlationId` (≤30 chars) is the only client-side tag. Matters for phase 2, not for reads.
- Websocket heartbeat and reconnection behaviour are not documented.
- Error codes: DH-901 auth, DH-902 no Data API subscription, DH-903 account/segment inactive, DH-904 rate limit, DH-905 bad input, DH-906 bad order request or invalid token, DH-907 data unavailable, DH-908 server error, DH-909 network, DH-910 other, DH-1111 no holdings (empty state).
- Installed SDK `dhanhq` already exposes `get_order_list, get_order_by_id, get_order_by_correlationID, get_trade_book, get_trade_history, get_super_order_list` and also the placement methods, which the read-only module must never import.

## 3. Requirements for the read-only sync release

### Functional

- **R1 Pull.** On a schedule from the Mac, fetch order book, super-order book, today's trade book, positions and funds. Backfill trades with trade history for the last N sessions on first run and after any gap.
- **R2 Normalise.** Map every Dhan record to one internal order-state model covering all nine statuses and the three leg names, with times kept in IST and raw payload retained. Missing fields stay `None`, never defaulted.
- **R3 Persist, append-only.** Write a broker-book ledger the same way the repo's other ledgers work (see section 5 once the execution-path map is in). Never rewrite; a status change is a new row.
- **R4 Reconcile both desks.** Compare the broker book with the paper book for equity darlings (by `securityId`, segment `NSE_EQ`) and options spreads (by contract `securityId`, segment `NSE_FNO`). Output a daily diff: orders at the broker with no paper twin, paper positions with no broker twin, quantity and price mismatches. Today the expected result is "broker book empty"; the report must say that honestly.
- **R5 Instrument mapping gap.** No NSE_FNO contract security ids are stored anywhere in the repo; options are addressed by strike and type through the option chain. The reconciler needs a contract-id resolver built from the public scrip master (`src/ingestion/scrip_master.py` already fetches `api-scrip-master.csv` without a token). This is a prerequisite for the options half of R4.
- **R6 Errors.** Reuse `src/dhan_guard.py` classification. Treat DH-1111 as an empty state. Distinguish "token dead" from "book empty" in the report, the same lesson as observation-week Issues 5/6.
- **R7 Reporting.** One card in the CEO brief: rows pulled, last successful sync time, diff counts, and the fund balance. A red card on N consecutive failed syncs.

### Non-functional

- **N1 No placement path.** The module imports no `place_*`, `modify_*`, `cancel_*` SDK method and constructs no write request. A test enforces this by scanning the module source for the forbidden names, so the guarantee is mechanical, not conventional.
- **N2 Hermetic tests.** Fixtures are recorded MCP/SDK payloads including the double-nested `data` shape and the DH-1111 case. No network in tests.
- **N3 Token source without breaking #48.** The Mac must not mint a token. Two candidate sources, decision pending: (a) copy the VM's current token to the Mac (reverse of `scripts/push_token_to_vm.sh`), or (b) a consent-flow token as the MCP uses, after verifying it does not invalidate the VM token.
- **N4 Rate budget.** All calls go through the host-wide throttle. One sync cycle is roughly 5 calls, so a 15-minute cadence uses under 1% of any limit.
- **N5 Freeze-compatible.** Observability only; no change to `plan_tracker`, `margin_locks`, sizing or entry.

## 4. Deliberately out of scope for this release

Placement, modify, cancel, super orders, slicing, forever orders, live order-update websocket, postback, static-IP whitelisting, funding the real account, any change to CLAUDE.md Rule 7. These belong to phase 2 and each needs a numbered decision.

## 5. Design sketch (to be built only after sign-off)

The repo has no broker abstraction and no ABC/Protocol idiom; it uses module-level seams and injectable callables. The closest analogue to the module we need is `src/dhan_guard.py` `SafeDhanClient`: uniform never-raises contract, `last_error`, an append-only `audit`, `strict=` flag. Model the reader on it.

New modules (names provisional):

| Module | Role | Reuses |
|---|---|---|
| `src/broker/dhan_orders_reader.py` | The one **broker-book door** (read-only). Methods: `order_book()`, `super_order_book()`, `trade_book()`, `trade_history(from, to)`, `positions()`, `funds()`. Same empty-state contract as `SafeDhanClient`. | `dhan_client._get_client`, `_throttle`, `unwrap_payload`; `dhan_guard.classify_failure`, `DhanApiError`; `token_provider.get_token` |
| `src/broker/order_state.py` | Pure normalisation of Dhan payloads to internal dict rows (plain dicts + JSONL, matching the repo; no dataclasses). Status and leg enums as module constants. | none |
| `src/broker/contract_ids.py` | NSE_FNO contract `securityId` resolver from the public scrip master (underlying, expiry, strike, type). Closes the R5 gap. | `src/ingestion/scrip_master.py` `fetch_master`, `index_master` |
| `src/broker/reconcile.py` | Pure diff of broker rows vs paper positions for both desks. | `src/positions.py` `active_positions()`; `data/darling_ids.json` via `equity_desk.security_id_for` |
| `src/broker/sync.py` | Mac cron entry point (`python3 -m src.broker.sync`): pull, normalise, append, reconcile, report. Fail-open per stage, like the nightly. | `notifier.fire_broadcast` for the card |

Ledger: `logs/broker_book.jsonl`, append-only, one row per observed (orderId, orderStatus, updateTime) tuple plus one row per trade by `exchangeTradeId`. Duplicate observations are skipped by key, never rewritten. Reconciliation result goes to `logs/broker_reconciliation.jsonl`, one row per sync.

Config: one new `config.json` key `broker_sync_enabled`, default **false** in code (the repo's rule that a stale config can never enable spending). Read into a module constant in `src/config.py` like the other flags.

Reporting: one card in the CEO brief via the existing Discord door. Red card after 3 consecutive failed syncs, with the DH code shown, so a dead token is never mistaken for an empty book.

Schedule: Mac cron (the Mac already runs 3 cron jobs and 2 LaunchAgents). Cadence pending owner answer (section 7, question 2). Documented in `CRON_SETUP.md` when built.

Tests: `tests/test_broker_reader.py`, `test_order_state.py`, `test_contract_ids.py`, `test_reconcile.py`, `test_broker_sync.py`. Follow `tests/test_dhan_guard.py`: real recorded payloads as module constants, `mock.patch.object` on `_get_client` and `_throttle`, `tmp_path` ledgers. Plus the N1 guard test that scans `src/broker/` for `place_`, `modify_`, `cancel_`, `slice` and fails if any appear.

Doc changes when (and only when) this is approved: DECISIONS.md new entry recording "read-only broker-book sync permitted; placement still forbidden"; CLAUDE.md Rule 7 reworded to "no order *placement* path"; ROADMAP.md, README.md, ARCHITECTURE.md, MODULES.md, CRON_SETUP.md, HANDOVER.md updated in the same commit.

What phase 2 would need that this release deliberately does not build: additive keys in the `data/journal.jsonl` entry schema for broker order id, partial fills, rejects and average price (readers must tolerate absence); the insertion points are `options_proposer._leg_fill`, `plan_tracker.resolve_intraday_profit_take` / `_settle_spread_cash`, and `equity_desk.fund_entry` / `settle_exit`, all downstream of `portfolio_manager.request_entry` so every halt fires first. `next_gen_engine/execution_algo.py` (limit chase, leg sequencing) is the natural home for order-working logic.

## 6. Phase 2 preview: placement with human approval (not approved)

Prerequisites, in order: (1) owner lifts Rule 7 by numbered decision and ends or amends the code freeze; (2) VM gets a static external IP, whitelisted at Dhan (7-day lock); (3) `PROP_ROADMAP.md` M2 entry criteria met, including the phone-reachable kill switch; (4) an intent ledger with `correlationId` for idempotency across restarts, since Dhan documents none; (5) order-state machine fed by the websocket with polling fallback; (6) multi-leg spreads placed leg-by-leg with partial-fill and freeze-quantity handling, because Dhan has no atomic basket order; (7) Discord approve button already exists in paper (#11, #45) and becomes the gate.

## 7. Owner answers (2026-09-11, closes the open questions)

| Question | Answer | Design consequence |
|---|---|---|
| Token source for the Mac | **From the VM.** | New `scripts/pull_token_from_vm.sh`, the reverse of `push_token_to_vm.sh`: `gcloud compute ssh` reads only `DHAN_ACCESS_TOKEN` from the VM's `.env`, single-line replace into the Mac's `.env`, same chmod-600 temp-file discipline, never on a command line. Run at 07:20 IST after the VM's 07:00 renewal, and again on any DH-901/DH-906 from the reader. The Mac never runs `renew_token`. Mac cron lines are installed by the owner from their own Terminal (TCC blocks Claude), as `CRON_SETUP.md` already records. |
| Cadence | **Yes: every 15 min in market hours plus one EOD backfill.** | Mac cron `*/15 9-15 * * 1-5` guarded by the market-hours adapter, plus `19:05` EOD trade-history backfill for the last 5 sessions. About 5 calls per cycle, 27 cycles a day, well under limits. |
| Manual orders to mirror? | **No trading. Infra readiness only; the tool needs improvement before real capital.** | The reconciler runs in **infra-ready mode**: an empty broker book is the expected state, so paper positions without a broker twin are informational, never a red card. Red cards fire only on sync failures (auth, rate limit, network) or on any *unexpected* broker row, which would mean an order was placed outside the system. |
| Target account | **Same as above.** | The ₹1,411 account is the infra account. No funding is planned. Fund balance is still reported on the card so an unexpected deposit or debit is visible. |

**Status after these answers: requirements agreed as v1.0, plan only.** Building starts only on an explicit go from the owner, and the first commit of that build is the DECISIONS.md entry, not code.

## 8. Verification plan

- Unit tests on normalisation, ledger append, reconciliation, forbidden-method guard.
- End-to-end: run one sync on the Mac and compare row-for-row with the Dhan MCP `orderbook list`, `tradebook list`, `portfolio positions` output for the same minute.
- Fault drill: run with a dead token and confirm the report says "auth failure", not "book empty".
