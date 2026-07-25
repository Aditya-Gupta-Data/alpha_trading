# CLAUDE.md — standing instructions for AI agents in this repository

This file is loaded automatically at the start of every session. It is the
enforcement point for the Continuous Context protocol; `HANDOVER.md` restates
these rules for human readers, but **this file is the one that binds.**

---

## RULE 1 — Session start: read before you write

**The very first action of a new session is to read `HANDOVER.md` and
`PROJECT_TIMELINE.md`, before writing any code.**

`HANDOVER.md` opens with a dated current-state block: what is live, what is
broken, what is deliberately deferred. `PROJECT_TIMELINE.md` gives the arc and
the daily log. Between them you will know the system's state without the owner
having to explain it.

Then, depending on the task:
- changing behaviour → `ARCHITECTURE.md` (the 8-department map, the data flow)
- finding a file → `MODULES.md`
- asking "why is it like this?" → `DECISIONS.md` (85 numbered decisions)
- what runs when → `CRON_SETUP.md`

Do not skip this because the request looks small. Most of the expensive
mistakes available in this repo — archiving a live module, rewriting an
append-only ledger, wiring a shadow signal to capital — look like small
requests.

## RULE 2 — Session wrap: leave the map correct

**When the user signals the end of a session, or completes a deploy, you MUST
update `HANDOVER.md` with the current exact state, open bugs, and next
immediate steps BEFORE closing the session.**

Concretely, before you finish:

1. **Update `HANDOVER.md`.** Prepend or revise the dated current-state block at
   the top. It must answer: what is live now, what is broken or unwired, what
   the next person should do first. `HANDOVER.md` is
   **reverse-chronological — prepend, never rewrite the history below.**
2. **Run `bash scripts/wrap_session.sh`** to append the day's commits to
   `PROJECT_TIMELINE.md` and commit the doc update. It runs the suite as a gate
   and touches only that one file.
3. **Update `MODULES.md` in the same commit as any module change.** An
   undocumented module is a review bug, not a follow-up.
4. **Log incidents in `docs/observation_week_ledger.md`** — verified facts
   only. If a fix is unverified, say so.

`wrap_session.sh` deliberately does NOT write `HANDOVER.md`. Deciding what is
actually broken and what matters next requires judgment a script cannot infer
from commit subjects. That part is yours.

## RULE 3 — Never fabricate the record

The timeline, the ledgers and the decision log are the only memory this project
has across sessions. When you do not know something, write that you do not know
it. A plausible-sounding invention in a permanent document is worse than a gap,
because the next agent cannot tell the difference.

Specifically:
- Build history from `git log`, never from what you think you remember.
- `logs/macro_regime_declarations.jsonl`, `logs/macro_strategy_scores.jsonl`
  and the outcomes ledger are **append-only and immutable**. Rewriting them
  converts a forward test into a backtest and destroys the only out-of-sample
  record the system has.
- `DECISIONS.md` is append-only history. Add decisions; do not "clean it up".

## RULE 4 — Before you overwrite, look

When asked to rewrite or delete something, check what is actually there first:

```bash
git log -1 --format=%ad --date=short -- <file>
```

If the file contradicts how it was described — it is newer than expected, or
contains work you did not know about — say so and ask, rather than proceeding.
On 2026-07-25 a directive to "rewrite the docs, they're all dead" turned out to
apply to exactly one of four files; the other three were current.

## RULE 5 — What "live" means

The live execution path is: the 24 VM cron jobs in `scripts/setup_cron.sh`, the
3 Mac cron jobs, the 2 Mac LaunchAgents, the systemd services, and the MCP
server in `.mcp.json`. **If a module is not reachable from one of those, it is
not running** — whatever its docstring claims.

- `research_archive/` is on NO execution path. Never import it from `src/`.
- Files starting with `# MANUAL OFFLINE TOOL` or `# TEST INFRA` are
  intentionally off-cron. Do not "clean them up" as dead code.
- The Macro Regime Engine has **zero execution authority**. Wiring any of its
  output to sizing or entry is a Department 5 decision gated on a passed
  statistical test — never a code change you make on your own initiative.

## RULE 6 — Testing

The suite is hermetic and fast: **1,589 tests, ~85 seconds.**

```bash
python3 -m pytest -q                      # full suite — the pre-deploy gate
python3 -m pytest tests/test_foo.py -q    # while iterating
```

- No network, no live token, no Ollama, no production data files in tests.
- **A slow test is a bug report.** If a test takes seconds, it is almost
  certainly reaching a real external system rather than computing something
  hard.
- Fail-open behaviour is behaviour: test that the exception is swallowed *and*
  that later stages still ran.
- Run scoped files while iterating; run the full suite once before pushing,
  with no repo edits in flight.

## RULE 7 — House conventions

- **Paper money only.** No broker/order path exists in `src/`. Do not add one.
- Abstention is correct: missing data yields `None` or a named skip reason,
  never a fabricated default.
- One door per concern: one Discord door (`notifier.fire_broadcast`), one
  market-data door (`dhan_guard`), one settlement path (`plan_tracker`).
- The owner is non-technical. Explain in plain English and give copy-paste
  commands. Push back when something is wrong — that is expected, not rude.
