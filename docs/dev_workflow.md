# Dev Workflow — the Speed & Scale Protocol (owner directive, 2026-07-22)

> Binding on every session from 2026-07-22. Written when the build sprint
> opened and the 12-minute full-suite wait became the bottleneck.

## 1. Fast-path testing (iteration) vs the full gate (deploy)

- **While iterating on a module:** run ONLY that module's tests —
  `python3 -m pytest tests/test_<module>.py -q` (or `-k <pattern>`).
  MODULES.md names the test file for every module; keep that current.
- **The full suite (`python3 -m pytest tests/ -q`) runs at exactly ONE
  moment: as the pre-commit/pre-deploy gate.** Never mid-iteration.
  It stays mandatory there — on 2026-07-22 the full gate caught a
  cross-module flake no scoped run would have found. Run it in the
  background and keep working; do not sit and watch it.
- **Never edit repo files while the full gate is running.** A mid-run
  edit produced a phantom test failure on 2026-07-22 and cost a full
  re-run. Data jobs writing `data/` (git-ignored) are fine.

## 2. Zero tech debt

"We'll clean this up later" is banned. Every pass is production-shaped:
module docstring stating the contract, injectable seams, NULL-honest
returns, fail-open loops, tests in the same commit, MODULES.md row in
the same commit (the standing index rule). If a shortcut is truly
unavoidable it is not silent debt — it becomes a ledger/DECISIONS entry
with an owner date, or it doesn't ship.

## 3. Parallel lanes (the multi-lane highway)

The department structure IS the concurrency model — lanes conflict only
when they touch the same manager seam:

- **Lane rule:** one workstream = one department (one module cluster +
  its tests + its MODULES.md row). Two workstreams in flight must not
  edit the same files; the composition roots (`market_loop`,
  `master_scheduler`, `patience_basket.eod_chain`) are the shared seams
  — a lane that must touch one does so in its own tiny, immediately-
  committed change.
- **Git:** `main` stays deployable, always (the VM only ever pulls
  `main`). Small single-purpose commits, committed as soon as the
  scoped tests pass and gated by one full-suite run before push.
  A feature too big to land green in a session gets a short-lived
  `feat/<name>` branch, merged fast-forward after the full gate —
  never a long-lived divergence. `lovable-ui` stays its own branch,
  never auto-merged (standing rule).
- **Long-running data jobs** (backfills, refreshes, miners) run in the
  background against `data/` and never block a code lane. One NSE-
  hitting job at a time (shared IP courtesy); queue the next behind it.
- **The VM is a lane of its own:** deploy = `git pull` on `main` only.
  Nothing is ever edited directly on the VM.

## 3b. Multi-agent handoff rules (added 2026-07-23, learned the hard way)

The M1 macro_lake build had THREE write collisions between a worker
session and the PM session editing one file. The rules that prevent it:

- **"Done" means HANDS OFF.** The moment a worker reports done, the
  owner closes/stops that worker chat BEFORE relaying the report to the
  PM. A worker that keeps polishing after its done-report is a defect,
  not diligence — the PM owns the file from the handoff onward.
- **One writer per file, ever.** If the PM needs a worker revision after
  handoff, the PM sends an amendment prompt and does not touch the file
  until the worker's NEXT done-report; or the PM takes the file back and
  the worker never touches it again. Never both at once.
- **The commit gate reads pytest's EXIT CODE, never a piped tail** —
  `pytest … | tail` reports tail's success, and that is how a broken
  intermediate got committed (and amended away) on 2026-07-23.

## 4. The standing session loop

1. Pick the task lane (task list / cycle_hunter_plan.md).
2. Build with scoped tests only.
3. Full suite in background → commit (tests + MODULES.md same commit).
4. Push; deploy to the VM only when the change affects it.
5. Ledger anything observed (observation ledger — verified facts only).
