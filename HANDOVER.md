# Handover

The living status of the project. At the end of a chat, Claude refreshes this;
at the start of the next chat, paste it back in (or point Claude at the repo).

---

## Where we are
**Phase 1: Alerting — done, running automatically from the cloud. Phase 2: Suggestions — done, running automatically from the cloud. Phase 3: Paper trading — done, user ran their first real session 2026-07-03; still runs locally on the user's Mac (interactive, by design). Phase 4: replanned 2026-07-04, ALL STEPS DONE as of 2026-07-05 (4A journal/levers, 4B trade plans, 4C plan tracking, 4D news sentiment, 4E forecast layer, 4F learning-loop tuner) — Phase 4 is feature-complete, see below.**

Done:
- GitHub repo created (empty), scaffold ready to upload: https://github.com/Aditya-Gupta-Data/alpha_trading
  (user wants to defer this — building first, repo upload later).
- Full working pipeline: watchlist -> fetch prices -> check rules -> send alert.
- Rule engine tested with fake data (all tests pass, no internet needed).
- Ran locally by user (2026-07-02): all 5 unit tests passed, live run fetched
  real prices and correctly fired an alert (INFY.NS +5.64%).
- Alerts currently print to screen.
- Configurable watchlist in `config/watchlist.yaml` (4 condition types:
  price_above, price_below, percent_up, percent_down).

Also done:
- Email alert delivery built (`src/notifier.py` sends via Gmail SMTP using an
  App Password stored in a git-ignored `.env` file). Telegram is banned in
  India for the user, so email is the channel — see decision #10 in DECISIONS.md.
  Confirmed working end-to-end (2026-07-02): live INFY.NS alert arrived in
  the user's Gmail inbox.
- Watchlist filled with real picks: top 2 stocks from 5 industries (Banking:
  HDFCBANK/ICICIBANK, IT: TCS/INFY, Energy: RELIANCE/ONGC, FMCG: HINDUNILVR/ITC,
  Auto: MARUTI/TMPV) — 10 stocks, each with percent_up and percent_down
  rules at 3% (20 rules total). Lives in `config/watchlist.yaml`.
  Note: Tata Motors demerged Oct/Nov 2025 into TMCV.NS (commercial vehicles)
  and TMPV.NS (passenger vehicles/JLR) — old TATAMOTORS.NS ticker is dead.
  TMPV chosen since it's the passenger-vehicle comparable to Maruti.
- Daily scheduler built via macOS `launchd` (not cron — more reliable on Mac).
  Config at `~/Library/LaunchAgents/com.alphatrading.dailyalert.plist`, runs
  weekdays at 3:35 PM IST (just after NSE close), logs to `logs/run.log`
  (git-ignored). Only emails when a rule actually triggers, so quiet days = no
  email = no spam. Confirmed working (2026-07-02) via manual `launchctl start`
  test — real email arrived.
  Caveat: laptop must be awake at market/scheduled times to fire; solved
  properly once hosted off the laptop.

**Phase 2: Suggestions** — built 2026-07-02:
- `src/indicators.py`: SMA and Wilder's RSI (14-day), pure Python, no new deps.
- `src/suggestions.py`: pulls 1y daily history per ticker via yfinance, combines
  50/200-day SMA trend (uptrend/downtrend) with RSI zone (overbought >70,
  oversold <30, else neutral) into one plain-English line per stock. Flags a
  fresh Golden/Death Cross if the trend flipped since yesterday.
- `src/suggest.py`: entry point (`python -m src.suggest`), de-dupes watchlist
  tickers, emails a digest via `notifier.send_digest` (new function, reuses
  the same Gmail SMTP setup as alerts — no separate credentials needed).
  Advisory only — never places trades.
- Confirmed working end-to-end (2026-07-02): ran live, all 10 tickers resolved,
  real digest emailed.
- Second launchd job added: `~/Library/LaunchAgents/com.alphatrading.dailysuggestions.plist`,
  runs weekdays 8:00 AM IST (before market open), logs to `logs/suggest.log`.
  Loaded by the user 2026-07-02 — active.

**Phase 3: Paper trading** — built 2026-07-03 (decisions #11, #12):
- Human-in-the-loop by the user's explicit choice: engine proposes, user
  approves/rejects in the terminal and logs a one-line "why"; review scores
  both the engine and the user later. NOT fully automatic.
- `src/portfolio.py`: fake Rs.1,00,000 portfolio in `data/portfolio.json`
  (git-ignored). Rails: whole shares, can't overspend, max 25%/stock.
- `src/strategy.py`: proposals — BUY on fresh Golden Cross or uptrend+RSI<=30;
  SELL holdings on Death Cross or downtrend.
- `src/trade.py`: interactive session (`python3 -m src.trade`) — proposes,
  asks y/n + why, executes on paper, emails a session digest.
- `src/journal.py` + `data/journal.jsonl`: every decision logged with the
  engine's signal, the user's decision, and their why.
- `src/review.py` (`python3 -m src.review`): after 7+ days, scores each entry
  (WIN/LOSS for approvals, GOOD SKIP/MISSED GAIN for rejections), emails a
  scorecard. This is the "improving" loop the user asked for.
- 10 new offline tests in `tests/test_portfolio.py` (15 total, all passing).
- Live-tested end-to-end 2026-07-03 by Claude (proposed + executed a paper BUY
  of ONGC, journal + portfolio + digest email all verified), then data/ was
  RESET so the user starts clean. No broker connected anywhere — cannot touch
  real money.
- Bug fixed along the way: Yahoo returns a NaN row for "today" before market
  open, which poisoned SMA/RSI. Both data paths now dropna() closes.
- User ran their FIRST REAL session 2026-07-03: approved BUY 106 x ONGC.NS
  @ Rs.234.99 (Rs.24,908.94), why logged as "testing default suggestions".
  Portfolio: Rs.75,091.06 cash + 106 ONGC.NS, total value Rs.100,000 (flat,
  as expected on day one). This is now live user data in data/journal.jsonl
  and data/portfolio.json (git-ignored) — do not reset these again.

**Hosting** — done 2026-07-03, using the user's Google Cloud $300 trial credit:
- GCP project `alpha-trading-app-2026`, billing account `01DA87-9A665E-85DBB4`
  linked, Compute Engine API enabled.
- VM `alpha-trading-vm` in `us-central1-a`, machine type `e2-micro` — this
  specific size/region combo is covered by GCP's "Always Free" tier, so it
  stays $0/month even after the $300 trial credit expires (~90 days).
  Debian 12, 30GB pd-standard disk, timezone set to Asia/Kolkata.
- Only `src/`, `config/`, `requirements.txt`, and `.env` were copied to the VM
  (via `gcloud compute scp`) — NOT `tests/`, `data/`, or `logs/`. Phase 3
  (paper trading) deliberately stays local on the user's Mac since it's
  interactive and holds the real portfolio state; moving it to the VM would
  mean SSHing in just to answer y/n prompts, which is worse, not better.
- Cron jobs on the VM (`crontab -l` to view) mirror the old launchd schedule:
  `35 15 * * 1-5` → `python3 -m src.main` (alerts), `0 8 * * 1-5` →
  `python3 -m src.suggest` (suggestions), both logging to `~/alpha_trading/logs/`
  on the VM (not the local Mac logs/ folder — separate log locations now).
- Confirmed working: both scripts ran live on the VM and reached Yahoo Finance
  + Gmail successfully.
- The local launchd jobs (`com.alphatrading.dailyalert` and
  `com.alphatrading.dailysuggestions`) were UNLOADED on the user's Mac
  (`launchctl unload ...`) to avoid duplicate emails — the plist files are
  still on disk in `~/Library/LaunchAgents/` if ever needed again, just inactive.
- To SSH back into the VM later: `gcloud compute ssh alpha-trading-vm --zone=us-central1-a`
  (run from the alpha_trading project folder, needs gcloud CLI + billing project
  already configured, which they are).
- If code changes are made to src/config in the future, they need to be
  re-copied to the VM with the same `gcloud compute scp --recurse` command
  used originally (see chat history) — there's no auto-sync/deploy pipeline.

**Email simplification** — done 2026-07-03, user asked for mail to be easier
to read (non-technical, phone-read emails):
- Alerts (`src/main.py`): used to send ONE email per triggered rule (could be
  multiple emails per run). Now batches all triggers into a single digest via
  `notifier.send_digest`. `notifier.send_alert` was unused after this and was
  deleted.
- Suggestions (`src/suggest.py` + `src/suggestions.py`): the email used to
  dump the same technical line as the terminal (RSI numbers, "downtrend",
  "neutral" etc). Added `suggestions.bucket()` and `suggestions.plain_english_line()`
  — the email now groups stocks into "WORTH A LOOK" / "KEEP AN EYE ON" /
  "NOTHING TO DO" with one plain sentence each, no jargon. Terminal output
  (`describe()`) is UNCHANGED and stays technical — that's for the user
  actively reading before a trade.py session, kept detailed on purpose.
- Trade sessions (`src/trade.py`) and scorecards (`src/review.py`): emails
  now read as plain sentences ("You bought 106 shares of ONGC.NS at
  Rs.234.99...") instead of terse `APPROVED: BUY 106 x ONGC.NS @ ...` shorthand.
- Updated code was re-copied to the cloud VM (`gcloud compute scp --recurse
  src alpha-trading-vm:~/alpha_trading/`) so the 3:35 PM / 8:00 AM cron jobs
  use the new format too. All 15 local tests still pass after these changes.

**Phase 4: planning done 2026-07-04, build not started:**
- User shared a "Plan and scope" doc describing the real end state: news +
  market data -> forecast -> trade plan -> rationale -> user dialogue ->
  paper trade -> learning loop. This superseded the narrower step order in
  `aditrader-phase4-master-handoff-prd.md` (that file's own 4A/4B/4C only
  covered structured journaling, news sentiment, and a win-rate auto-tuner —
  no forecasting, no full trade plans, no automatic plan-outcome tracking).
- Full replanned breakdown (6 steps, 4A-4F) lives in `PLAN.md` — always read
  that for the current build order, not this summary.
- Scope decisions locked in and recorded in `DECISIONS.md` (#13-#17):
  NSE/BSE equities + options (options -> Phase 5, data source TBD), swing
  holding period first (intraday -> later phase), news via free RSS +
  LLM-summarized sentiment JSON (isolated script, core scripts never touch
  raw articles), no dedicated in-tool dialogue feature for v1 (user already
  does this in Claude sessions).
- **Phase 4A DONE 2026-07-04** (structured journal + risk levers):
  `src/journal.py` `new_entry()` now writes `risk_levers` {sl_pct, size} and
  `pattern_tags` on every record (new params optional, fall back to
  config.json defaults, old journal lines still read fine). `src/trade.py`
  now prompts for chart-pattern tags (always) and stop-loss % + position
  size (only when a trade is approved, config default on Enter). Verified:
  compiles, simulated session logs a complete entry, live ONGC journal
  entry untouched, all 15 tests pass.
  IMPORTANT carry-over: the captured `size` and `sl_pct` are NOT yet acted
  on — trades still execute `prop["shares"]` from strategy.py and no stop is
  enforced. Wiring size into sizing is 4B; enforcing/ tracking the stop is 4C.
- **Phase 4B engine half DONE 2026-07-04**: `config.json`/`src/config.py`
  gained risk levers (risk_level=moderate mapping to 1% of portfolio risked
  per trade, take_profit_rr=2.0, max_concurrent_positions=4,
  alt_entry_pullback_pct=2.0). `src/strategy.py` rewritten: `propose_plans()`
  returns full plan dicts (entry_rule, stop_loss, target, risk_reward,
  max_loss_rs, invalidation, plain-English rationale; primary + optional
  pullback-limit alternative for buys, exit plan for sells). Position sizing
  is now risk-based (risk budget ÷ stop distance, capped by
  default_investment_size + Phase 3 rails) — positions come out much smaller
  than the old "max affordable" sizing, by design. Old `propose()` contract
  preserved (primary dict or None). Verified on fake data: sizing math, both
  variants, sell plans, max-positions rail, 15/15 tests.
- **Phase 4B second half DONE 2026-07-04**: `src/trade.py` switched to
  `propose_plans()` — sessions now print the full plan (entry rule, stop,
  target, R:R, max loss, invalidation) and show the alternative limit plan
  as context (display-only until 4C can track conditional entries). The 4A
  lever answers now DRIVE execution: position size sets the executed share
  count (clamped by cash/25% rails; Enter keeps the engine's risk-based
  sizing), and a custom stop-loss % moves both stop and target to keep the
  configured R:R. Levers are only asked on approved BUYs (sells are exits,
  nothing to size). `src/journal.py` gained a `plan` block on every entry —
  new_entry() copies explicit keys, so without this the 4B plan fields were
  being DROPPED at journaling time (an earlier note here claimed they flowed
  automatically — that was wrong and is now fixed); 4C's stop/target
  tracking depends on this block. Session digest email now includes a plain
  "get out at X, take profit at Y" line. Verified via scripted sessions:
  custom levers (stop+target move, resize), Enter-defaults (plan unchanged),
  sell path (no lever prompts); live journal untouched; 15/15 tests.
- **Phase 4C DONE 2026-07-04** (automatic plan tracking): new
  `src/plan_tracker.py` resolves every journaled 4B plan against daily
  OHLC — stop hit / target hit / time stop (`plan_max_days: 30` in config).
  Approved BUYs: paper position auto-closes at the plan's exit price
  (bracket-order semantics — approving the plan approved its exits);
  rejected plans resolve hypothetically (GOOD SKIP / MISSED GAIN). Outcomes
  carry r_multiple, days_in_trade, pnl_rs, resolution — 4F's tuner food.
  Runs LOCALLY only (needs data/portfolio.json): auto-sweep at the start of
  every trade session + manual `python3 -m src.plan_tracker`. Pessimistic
  tie-break (stop before target on the same day); entry day never scanned.
  review.py got a skip-guard (plan entries belong to the tracker; old-style
  entries like the live ONGC buy still blunt-scored as before — its
  2026-07-10 first scorecard is unaffected). Verified offline: 8 resolution
  paths + review deferral; live run no-ops correctly (ONGC entry has no
  plan block); 15/15 tests.
- ⚠️ DEPLOY NOTE: the VM still runs the OLD pre-config.py code and does NOT
  have root `config.json`. The documented scp command (`src config
  requirements.txt`) copies the `config/` DIR but not root `config.json` —
  if src/ is ever re-copied without also copying `config.json`, the VM cron
  jobs will crash at import (config.py fails loudly by design). Next deploy
  must be: `gcloud compute scp --recurse --zone=us-central1-a src config
  config.json requirements.txt alpha-trading-vm:~/alpha_trading/`.
- **Phase 4D DONE 2026-07-04** (isolated news sentiment): new
  `src/news_processor.py` fetches Google News RSS per watchlist ticker and
  batches them into ONE Gemini 2.0 Flash call (free tier, raw HTTPS via
  stdlib urllib — no SDK) that returns a sentiment score (-5..+5) and a
  ≤3-word driver per ticker, written to `data/news_sentiment.json`
  (git-ignored). Fully isolated: imports no core trading code, reads the
  watchlist YAML directly. Fallback (user's choice): missing key or any
  LLM/parse failure → every ticker neutral 0 / "no data" / stale=true,
  source="fallback", never crashes. Model output coerced into schema
  (clamp/round score, truncate focus). `.env.example` gained GEMINI_API_KEY;
  `requirements.txt` gained certifi. Fixed a macOS/VM SSL cert issue by
  pointing urllib at certifi's CA bundle (live RSS fetch now works — real
  ONGC/INFY headlines confirmed). Run: `python3 -m src.news_processor`.
  Verified: isolation (AST import scan), fallback path, schema coercion,
  live RSS fetch, 15/15 tests.
  ✅ RESOLVED 2026-07-05: GEMINI_API_KEY is set in `.env` and confirmed
  live end-to-end — `data/news_sentiment.json` now has `source: "gemini"`,
  10/10 real reads (e.g. TCS.NS -5 "sharp price crash", RELIANCE.NS +2
  "Jio IPO"). Two real issues fixed along the way, both worth knowing:
  (1) the first API key was created via AI Studio's "create in new project"
  option, which gets ZERO free-tier quota (HTTP 429, limit:0) — the fix was
  creating the key against the user's EXISTING billed GCP project
  (`alpha-trading-app-2026`, the one already used for VM hosting) instead;
  (2) the pinned model `gemini-2.0-flash` had been deprecated (HTTP 404
  "no longer available") — swapped to the `gemini-flash-lite-latest` ALIAS
  in `src/news_processor.py` so it auto-tracks Google's current model and
  won't silently 404 again after the next deprecation. Still not scheduled
  on the VM (genuinely not needed yet — see 4D deploy note above).
- **Phase 4E DONE 2026-07-05** (forecast layer): new `src/forecast.py`
  combines Phase 2 technicals with `data/news_sentiment.json` into a
  transparent, rule-based forecast per stock — directional bias
  (bullish/bearish/neutral), confidence %, top drivers, time horizon.
  Weighted checklist, max +/-10 points: trend 50/200 SMA (+/-4), fresh
  Golden/Death Cross (+/-2), RSI oversold/overbought mean-reversion
  (+/-2), news sentiment scaled to +/-2 (ignored if the news entry is
  `stale`, so it never blocks on news_processor not having run yet).
  `forecast(ticker)` mirrors `suggestions.analyze()`'s None-if-not-enough-
  history contract. Runnable standalone: `python3 -m src.forecast` (same
  pattern as `src.suggest`/`src.news_processor`, prints one line + drivers
  per watchlist ticker). Reads the news JSON directly, no import of
  news_processor — keeps 4D's isolation boundary intact. NOT wired into
  strategy.py/trade.py yet — it's a standalone read today; that
  integration (if any) is 4F's call. Verified live end-to-end: real
  technicals + real Gemini sentiment for all 10 watchlist tickers (e.g.
  TCS.NS BEARISH/60%, ONGC.NS BULLISH/32%). 8 new offline tests in
  `tests/test_forecast.py` (monkeypatched, no internet), 23/23 tests pass.
- **Phase 4F DONE 2026-07-05** (learning-loop tuner) — **Phase 4 is now
  feature-complete.** New `src/tuner.py`: reads resolved BUY-plan outcomes
  (from 4C's plan tracker) and learns a weight per BUY archetype —
  strategy.py only ever fires two ("fresh Golden Cross" or "uptrend with a
  dip/RSI oversold"), which map 1:1 onto forecast.py's bullish cross/RSI
  drivers. Writes `data/brain_weights.json`, which `src/forecast.py` now
  reads and applies (multiplying just those two bullish point
  contributions; the bearish mirrors and trend/news drivers stay fixed —
  no journaled archetype to learn those from). Weight formula: `1.0 +
  avg_r_multiple * tuner_weight_sensitivity` (config, 0.25), clamped to
  `tuner_weight_bounds` (config, [0.5, 1.5]); an archetype stays neutral
  (1.0) until it has `tuner_min_samples` (config, 5) resolved trades.
  Pattern tags (4A's free-text labels) get their own informational
  win/loss report in `brain_weights.json` but aren't fed into any weight —
  they're free text with no matching checklist driver.
  **Design deviation from the original PLAN.md text**: that text (written
  2026-07-04, before forecast.py existed) said strategy.py would read the
  weights and group by "confidence bucket" — changed to forecast.py (the
  actual checklist) and "plan archetype" (the actual journaled signal),
  since those are what really exists now. See PLAN.md's 4F section for
  the full reasoning.
  Verified: 7 new offline tests (`tests/test_tuner.py`, fake journal
  entries); live run against the real journal correctly found 0 resolved
  plans (the only real entry, ONGC, predates 4B plans) and wrote an
  all-neutral `brain_weights.json`, no crash; `python3 -m src.forecast`
  re-run clean with that file in place (identical output to pre-4F, as
  expected with neutral weights). 30/30 tests passing project-wide.
- Running deploy debt (unchanged): VM still on old pre-config.py code; next
  scp must include root `config.json` AND `news_processor.py`'s new files
  (`config.json`, updated `requirements.txt`) + a `pip install -r
  requirements.txt` on the VM for certifi. `src/forecast.py` and
  `src/tuner.py` also need to ship in that same next deploy (neither is
  scheduled/needed on the VM yet — both are local, on-demand tools so
  far — but they're new files under `src/` so they must ride along
  whenever the next scp happens).

**Phase 5+ vision doc received 2026-07-05, saved as `VISION_PLAN.md`, build
NOT started:**
- The user shared a "Phase 5+ Master Blueprint" (same pattern as the Phase 4
  "Plan and scope" doc): Phase 5 Discord bot interface (analyst on the
  phone, /analyze command + Gemini chat), Phase 6 SQLite "Brain Map"
  (data/brain_map.db, event-pattern memory beyond brain_weights.json),
  Phase 7 time-travel backtesting simulator (day-by-day historical replay
  that trains the Brain Map), Phase 8 advanced NSE data ingestion (block
  deals / corporate actions / options prep, via broker data API or
  scraping). Full text + the exact per-step prompts live in
  `VISION_PLAN.md` — read that, not this summary.
- Its guardrails match the existing design: strictly paper trading (no
  broker execution code anywhere), modular standalone files talking via
  local JSON/SQLite, no heavy databases (sqlite3 only).
- **Phase 5 step 1 (Discord bot) BUILT 2026-07-05, verified connecting,
  awaiting two user portal actions to go fully live:**
  - User created the app ("ADiTrader Analyst") and provided the token;
    it's in `.env` as `DISCORD_BOT_TOKEN`. `discord.py` installed and
    added to `requirements.txt`.
  - New `src/discord_bot.py` (`python3 -m src.discord_bot`, runs until
    Ctrl+C): `/analyze` slash command runs the 4E forecast (whole
    watchlist, or one ticker via the optional argument — `.NS` appended
    automatically), deferred + chunked under Discord's 3s/2000-char
    limits, yfinance work pushed off the event loop via
    `asyncio.to_thread`. Replying to the bot or @mentioning it chats via
    Gemini (same raw-urllib pattern + `gemini-flash-lite-latest` alias as
    news_processor; context block = watchlist + latest news sentiment,
    deliberately NOT portfolio/journal). Read-only by design per
    VISION_PLAN guardrails: imports only src.forecast, no
    portfolio/trade/strategy imports, cannot execute anything.
  - GOTCHA solved: `SSL_CERT_FILE` must be set to certifi's bundle BEFORE
    `import discord` (discord.py builds its SSL context at import time) —
    setting it after the import still fails CERTIFICATE_VERIFY_FAILED.
    The module handles this itself now; no shell env var needed.
  - VERIFIED with a throwaway non-privileged-intents script: token valid,
    TLS clean, logs in as "ADiTrader Analyst#0107". Still pending from
    the user, in the Discord portal/app: (1) enable Message Content
    Intent (Bot tab) — without it the real bot exits with a friendly
    error (PrivilegedIntentsRequired is caught and explained); (2) open
    their OAuth invite URL and add the bot to a server they own — as of
    the check it was in ZERO servers, so /analyze has nowhere to appear
    yet. After both: run `python3 -m src.discord_bot` and test /analyze
    from the phone.
  - Not wired into trade.py (by design, this step); no tests added yet
    for the bot file (it's all I/O; nothing offline-testable worth
    faking at this step).
- Claude's review notes for when the build starts (not yet discussed with
  the user, raise them at the right step): (a) Phase 5 needs a new pip
  dependency `discord.py`, and a long-running bot process — fine to run
  manually on the Mac for testing, but "pings your phone anytime"
  eventually means hosting it on the always-on VM, which would put
  `.env`'s Discord+Gemini keys there too; (b) Phase 6 says "upgrade"
  brain_weights.json to SQLite — treat it as ADDING brain_map.db alongside
  (4F's tuner/forecast weight loop keeps working) unless the user
  explicitly wants it replaced; (c) Phase 7's "override datetime.now()
  across the app" is fragile — the cleaner equivalent is passing an
  as-of date through the existing analyze/propose path (yfinance history
  is already date-sliceable), same outcome, no monkeypatching; also
  news_sentiment history doesn't exist for past dates, so simulated
  training is technicals-only unless we accept that gap; (d) Phase 8 NSE
  website scraping breaks often (NSE actively blocks bots) — the doc's own
  broker-API route (Dhan/Upstox free data keys) is the reliable path.

## Next steps (in order)
1. Phase 4 is feature-complete (4A-4F all done). Phase 5+ is planned in
   `VISION_PLAN.md` but NOT started — first mover is the USER: create the
   Discord bot + token (VISION_PLAN.md Phase 5 external steps), then say
   go. Do not start `src/discord_bot.py` before both.
2. User keeps running `python3 -m src.trade` in the evenings as new signals
   come up (it's interactive, so it stays a manual/by-hand step, done locally).
   It auto-sweeps the 4C plan tracker at the start of each session.
3. Around 2026-07-10 (7+ days after the ONGC buy), run `python3 -m src.review`
   for the first scorecard — will show whether the ONGC call was a win. (This
   is the pre-4B entry; later plan-carrying trades resolve via
   `python3 -m src.plan_tracker`/the trade.py auto-sweep instead.)
4. Optionally run `python3 -m src.news_processor` then `python3 -m src.forecast`
   for a news-informed technical read on the watchlist, and `python3 -m
   src.tuner` periodically once there are 5+ resolved plan-carrying trades, so
   `src/forecast.py` starts leaning on whichever BUY archetype has actually
   been paying off.
5. Upload scaffold to the GitHub repo (deferred until logic is solid).
6. If future code changes touch src/config/requirements, remember to re-scp
   them to the VM — nothing auto-deploys yet. `src/forecast.py` and
   `src/tuner.py` aren't on the VM at all yet (not needed there — see the
   Phase 4F deploy note above).

## Open questions for the user
- None open right now — watchlist size, alert channel, and suggestion logic are all decided.

## Working notes
- User is non-technical: Claude writes all code and gives copy-paste steps.
- This Claude session runs locally on the user's own Mac (not a remote sandbox),
  so Bash commands here execute directly on their machine and yfinance/Gmail
  calls are real — no more need to defer live-testing to the user.
