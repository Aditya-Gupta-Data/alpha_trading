#!/bin/bash
# ==============================================================================
# wrap_session.sh — the Continuous Context protocol (2026-07-25)
# ==============================================================================
# Run this at the end of a working session, or before a deploy. It:
#   1. collects the day's commits,
#   2. writes them into PROJECT_TIMELINE.md's auto-generated daily log,
#   3. runs the full test suite as a gate,
#   4. commits ONLY the doc change.
#
# Why this exists: documentation drift was fixed retroactively on 2026-07-25
# (README had been dead since 07-06). Retroactive cleanups are expensive and
# risky; a small daily append is neither.
#
#   bash scripts/wrap_session.sh                  # today, run tests, commit
#   bash scripts/wrap_session.sh --dry-run        # show me what you'd write
#   bash scripts/wrap_session.sh --date 2026-07-24
#   bash scripts/wrap_session.sh --skip-tests     # docs-only day, no code touched
#   bash scripts/wrap_session.sh --push           # also push to origin
#
# DESIGN NOTES (please keep these properties if you edit this file):
#   * It stages ONLY PROJECT_TIMELINE.md. Never `git add -A` here — this script
#     runs at the end of a session when unrelated work may be in the tree, and
#     silently sweeping that into a "doc sync" commit would be a nasty surprise.
#   * It is IDEMPOTENT: re-running for the same date regenerates that date's
#     block instead of appending a duplicate.
#   * It does NOT update HANDOVER.md. That file needs judgment about what is
#     actually broken and what comes next — a machine cannot infer that from
#     commit subjects. Writing it is the agent's job (see the Session Wrap Rule
#     in CLAUDE.md), and this script reminds you if it looks stale.
#   * The test run is a GATE: no green suite, no commit.
# ==============================================================================

set -euo pipefail

DATE="$(date +%F)"
DRY_RUN=0
SKIP_TESTS=0
DO_PUSH=0

while [ $# -gt 0 ]; do
    case "$1" in
        --date)       DATE="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --push)       DO_PUSH=1; shift ;;
        -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "wrap_session: unknown option '$1' (try --help)" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TIMELINE="PROJECT_TIMELINE.md"
MARKER="<!-- WRAP_SESSION:INSERT_BELOW -->"

if [ ! -f "$TIMELINE" ]; then
    echo "wrap_session: FATAL — $TIMELINE not found in $REPO_ROOT" >&2
    exit 1
fi
if ! grep -qF "$MARKER" "$TIMELINE"; then
    echo "wrap_session: FATAL — insertion marker missing from $TIMELINE." >&2
    echo "wrap_session: restore this line where the daily log should go:" >&2
    echo "  $MARKER" >&2
    exit 1
fi

# --- 1. Collect the day's commits -------------------------------------------
# Author-date, this branch only. Merge commits are skipped (they restate their
# children) and previous auto-sync commits are excluded so the log never
# describes its own bookkeeping.
COMMITS="$(git log --no-merges --since="${DATE} 00:00" --until="${DATE} 23:59" \
                   --date=short --format='%h|%s' \
           | grep -v '|chore: EOD doc sync' || true)"

if [ -z "$COMMITS" ]; then
    echo "wrap_session: no commits authored on $DATE — nothing to log."
    echo "wrap_session: (documentation is already current for this date.)"
    exit 0
fi

COMMIT_COUNT="$(printf '%s\n' "$COMMITS" | wc -l | tr -d ' ')"
FILES_TOUCHED="$(git log --no-merges --since="${DATE} 00:00" --until="${DATE} 23:59" \
                         --name-only --format='' | sort -u | grep -c . || true)"

# --- 2. Build the block ------------------------------------------------------
BLOCK="### ${DATE} · ${COMMIT_COUNT} commits · ${FILES_TOUCHED} files touched"
BLOCK="${BLOCK}
"
while IFS='|' read -r sha subject; do
    [ -z "$sha" ] && continue
    BLOCK="${BLOCK}
- \`${sha}\` ${subject}"
done <<< "$COMMITS"
BLOCK="${BLOCK}
"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "wrap_session: --dry-run, would insert into $TIMELINE:"
    echo "---------------------------------------------------------------"
    printf '%s\n' "$BLOCK"
    echo "---------------------------------------------------------------"
    exit 0
fi

# --- 3. Insert (idempotently) ------------------------------------------------
# Python does the edit: it is already a hard dependency of this repo, and it
# handles "replace the existing block for this date" without fragile sed ranges.
BLOCK="$BLOCK" DATE="$DATE" MARKER="$MARKER" TIMELINE="$TIMELINE" python3 <<'PYEOF'
import os, re, pathlib

path = pathlib.Path(os.environ["TIMELINE"])
text = path.read_text()
marker, date, block = os.environ["MARKER"], os.environ["DATE"], os.environ["BLOCK"]

# SPLIT AT THE MARKER FIRST. Everything above it is HAND-WRITTEN narrative and
# is never touched — the hand-written Acts use the same "### <date>" heading
# style as the generated blocks, so a whole-file regex would happily eat them.
# (It did, on 2026-07-25, during this script's own first live run.)
head, sep, tail = text.partition(marker)
if not sep:
    raise SystemExit("wrap_session: marker vanished between check and write")

# Drop an existing generated block for this date — in the TAIL only.
existing = re.compile(
    r"\n### " + re.escape(date) + r" · .*?(?=\n### |\n---\n|\Z)", re.S)
if existing.search(tail):
    tail = existing.sub("", tail)
    print(f"wrap_session: replacing existing {date} entry")

# Newest entries sit directly under the marker (reverse-chronological).
path.write_text(head + marker + "\n\n" + block.strip() + "\n" + tail)
print(f"wrap_session: wrote {date} block into {path}")
PYEOF

# --- 4. The test gate --------------------------------------------------------
if [ "$SKIP_TESTS" -eq 1 ]; then
    echo "wrap_session: --skip-tests given, NOT running the suite."
    echo "wrap_session: only do this on a docs-only day."
else
    echo "wrap_session: running the full suite (~90s) as the gate..."
    if ! python3 -m pytest -q; then
        echo "" >&2
        echo "wrap_session: SUITE RED — the timeline was updated but NOTHING was" >&2
        echo "wrap_session: committed. Fix the failures, then re-run this script." >&2
        echo "wrap_session: (your $TIMELINE edit is still on disk, and re-running" >&2
        echo "wrap_session:  will regenerate rather than duplicate it.)" >&2
        exit 1
    fi
fi

# --- 5. Commit ONLY the doc --------------------------------------------------
if git diff --quiet -- "$TIMELINE"; then
    echo "wrap_session: $TIMELINE unchanged — nothing to commit."
else
    # `git commit -- <path>` is PATHSPEC-LIMITED: it commits that file only,
    # ignoring whatever else is already staged. Plain `git add <path>` followed
    # by `git commit` would commit the WHOLE INDEX — so anything the user had
    # staged before running this would be silently swept into a "doc sync"
    # commit. That is the exact surprise this script promises not to spring.
    git commit -q -m "chore: EOD doc sync — ${DATE} (${COMMIT_COUNT} commits)" \
        -- "$TIMELINE"
    echo "wrap_session: committed the $DATE timeline entry."
fi

# --- 6. Nudges (never automatic) ---------------------------------------------
# HANDOVER.md needs judgment, so this only reminds — it never writes.
HANDOVER_DATE="$(git log -1 --format=%ad --date=short -- HANDOVER.md 2>/dev/null || echo 'never')"
if [ "$HANDOVER_DATE" != "$DATE" ]; then
    echo ""
    echo "wrap_session: ⚠️  HANDOVER.md was last updated $HANDOVER_DATE, not today."
    echo "wrap_session:    If today changed the system's STATE (a deploy, a new"
    echo "wrap_session:    limitation, a bug found), update it before you stop."
    echo "wrap_session:    See the Session Wrap Rule in CLAUDE.md."
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo ""
    echo "wrap_session: note — you still have uncommitted changes in the tree."
    echo "wrap_session: this script deliberately left them alone."
fi

if [ "$DO_PUSH" -eq 1 ]; then
    echo "wrap_session: pushing to origin..."
    git push origin HEAD
else
    echo ""
    echo "wrap_session: done (not pushed — use --push, or push when ready)."
fi
