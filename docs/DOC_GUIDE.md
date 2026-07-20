# DOC_GUIDE.md — How the docs are organized, and how to record fast without drift

Read this before adding to any doc. It exists to answer one question quickly —
**"I just did/decided X. Where does it go, and where does it NOT go?"** — so the
record stays fast to write and stays accurate over time.

The rule the whole repo runs on: **one fact has exactly one home. Everywhere
else points to that home — it does not copy it.** A copied fact is a fact that
will silently go stale in every copy but one (this has already happened — see
the drift notes below). When in doubt, write the fact once and link.

---

## 1. Ownership registry — who owns what (the single source of truth)

Each row is the ONLY place its subject may be stated in full. Every other doc
references it.

| Doc | Owns (write the full version here) | Do NOT put here |
|---|---|---|
| `OVERVIEW.md` | The elevator pitch; the project **non-negotiables** (paper-only, human-in-loop, cloud execution, one-file-at-a-time) as *policy*; the read-order. | Implementation detail; enforcement mechanics. |
| `ARCHITECTURE.md` | The 8-department **design narrative**; **manager-of-record** per department; structural invariants as *enforced* (which seam/test enforces each non-negotiable); the VM/Mac split + state model. | Per-module specifics (config keys, thresholds, verified IDs) — those live in `MODULES.md`. |
| `MODULES.md` | The **component index**: per-module operational detail — what it does, its config keys, thresholds, CLI, seam, current verification state. | Design rationale (→ `ARCHITECTURE.md`) or *why* a call was made (→ `DECISIONS.md`). |
| `DECISIONS.md` | Every locked decision: the **WHY + the mechanics + the result numbers**. One numbered row per decision. | Live build status ("done/deployed") — that's `HANDOVER.md`. |
| `HANDOVER.md` | A **live status index**: what is built / tested / deployed, current live numbers, and cold-pickup facts (credentials location, boot commands). Each entry is 2–3 lines that **point into** DECISIONS/MODULES/CRON_SETUP. | Re-narrated decision rationale or mechanics. If you're explaining *why*, you're in the wrong doc — put it in `DECISIONS.md` and link. |
| `CRON_SETUP.md` | The human-readable **schedule / install** guide. Ground truth is `scripts/setup_cron.sh`. | Job schedules restated elsewhere — always point here. |
| `DATA_CONTRACT.md` | The frontend **JSON contracts** / endpoint shapes. | Backend internals. |
| `PLAN.md` | Phase 4 **as-built** plan + status (supersedes the PRD's step order). | — |
| `VISION_PLAN.md` | The long-term **vision narrative** (Phases 5–13 blueprint + the "why"). | Live phase status — that's `HANDOVER.md`'s checklist (this split is already documented in `VISION_PLAN.md` and is the model to copy). |
| `docs/observation_week_ledger.md` | The **incident / bug log** — what was observed, when, how resolved. One issue per entry. | Decision rationale (link to `DECISIONS.md`) or module detail. |
| `docs/token_renewal_cadence.md` | The token-renewal **rule + removal procedure** (and any interim state). | Restated token facts — point here. |
| `docs/planning_index.md` | An **index/aggregator** of the `docs/` planning specs + their open questions. | New spec content (put it in the spec, index it here). |
| `docs/HOLY_GRAIL_PLAN.md` | The unified **roadmap synthesis** (Phases 0–5, its own numbering). | — |
| `docs/*_spec.md` | A single feature **spec**. | Status (→ `HANDOVER.md`). |
| `aditrader-phase4-master-handoff-prd.md` | **SUPERSEDED** — historical Phase-4 spec only. Do not build against it; where it differs from `PLAN.md`, `PLAN.md` wins. | Anything new. |

---

## 2. "Where does this go?" — routing rules

- **I locked a choice / trade-off** → one new row in `DECISIONS.md` (why + mechanics + numbers). Add a 2–3 line status line in `HANDOVER.md` that links to it.
- **I built / shipped / deployed a module** → operational detail in `MODULES.md`; status + live numbers in `HANDOVER.md` (linking the decision).
- **I changed a schedule** → `scripts/setup_cron.sh` (ground truth) + the table in `CRON_SETUP.md`. Nowhere else.
- **I hit a bug / observed something in the book** → `docs/observation_week_ledger.md` as a new Issue. Link it from the fixing decision.
- **I changed a JSON shape the frontend reads** → `DATA_CONTRACT.md`.
- **I touched token renewal** → `docs/token_renewal_cadence.md` (+ decision row for *why*).
- **I have a new design/feature idea, not built** → a `docs/*_spec.md`, indexed in `docs/planning_index.md`.

## 3. The cross-link rule (how to avoid re-creating duplication)

When the fact already has a home (section 1), write **one line** at the new
location instead of restating it:

> Rationale + mechanics: `DECISIONS.md` #NN.
> VM cron jobs: `CRON_SETUP.md` (authoritative: `scripts/setup_cron.sh`).
> Incident detail: `docs/observation_week_ledger.md` Issue N.

A `HANDOVER.md` entry that runs longer than ~3 lines is almost always
re-narrating something that belongs in `DECISIONS.md` — trim it to a status
line + a pointer.

---

## 4. Copy-paste templates

**A `DECISIONS.md` row** (one line in the master table; keep the newest block's
descending order):
```
| NN | SHORT TITLE (`src/file.py`, owner ruling YYYY-MM-DD): what was decided, the mechanics, and the result numbers. Kill switch / default state if any. N tests. | Why this over the alternative — the trade-off, stated once. |
```

**A `HANDOVER.md` status entry** (status only — link the why):
```
### <Feature> — ✅ DONE YYYY-MM-DD
Built (`src/file.py`), N tests, deployed <where>. Live: <one live number>.
Rationale + mechanics: `DECISIONS.md` #NN.
```

**A `docs/observation_week_ledger.md` issue** (keep ONE heading level — `## Issue N`):
```
## Issue N — <short title> (<date>)
**Observed:** <what happened, with the tell-tale evidence>.
**Cause:** <root cause>.
**Fix:** <what changed> — rationale in `DECISIONS.md` #NN.
**Status:** OPEN | RESOLVED (<date>).
```

**A `MODULES.md` row** (operational detail, escape any `\|` inside code spans):
```
| `src/dept/module.py` | Dept N, YYYY-MM-DD. What it does, config keys, thresholds, seam, current verification state. CLI `python3 -m src.dept.module`. |
```

---

## 5. Update cadence

Update docs **at milestones, not on every commit** (keeps the record signal-dense
— the existing rule in `OVERVIEW.md`). When you do update, update the **one
canonical home** plus any status line that points to it — never a third copy.

## 6. Known drift this guide is meant to end (audited 2026-07-20)

These are the live examples of one-fact-many-copies that motivated this guide;
fix by collapsing to the canonical home per section 1:
- **VM cron job count** stated as 4 / 6 / 7 across `HANDOVER.md` and `CRON_SETUP.md` while `setup_cron.sh` has ~20. Canonical: `CRON_SETUP.md` → `setup_cron.sh`.
- **Decision #48 (one Dhan token; Mac renew/push crons removed)** restated in `HANDOVER.md` with a stale copy still describing the removed Mac crons as live. Canonical: `DECISIONS.md` #48.
- **Guardrails / non-negotiables** copied (with divergence) across the PRD, `PLAN.md`, `VISION_PLAN.md`, `HOLY_GRAIL_PLAN.md`. Canonical: `OVERVIEW.md`.
- **Department topology** shown as "7 departments" in `docs/SYSTEM_MAP_AND_AUDIT.md` after Dept 8 was added. Canonical: `ARCHITECTURE.md`.
