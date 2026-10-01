"""
Consistent snapshots for the dashboard mirror (audit Chunk 1 D10, decision #123).

`scripts/publish_dashboard_mirror.sh` (cron #33) used to rsync the LIVE
`data/brain_map.db` as a plain file while the engine's three processes commit
to it (rollback-journal mode), so the box could publish a torn copy. It now
calls this first:

    python -m src.dashboard.mirror_snapshot <out_dir>

which writes into `<out_dir>`:
  brain_map.db   — sqlite's online backup API (a transactionally consistent
                   copy; it waits on the 30 s busy timeout like any reader)
  journal.jsonl  — copied under the journal lock (decision #122), so never
                   a half-appended line

and prints the paths. The engine's files are only ever READ. Exit 0 with
whatever it could snapshot; a source that is absent is skipped by name.
"""
import shutil
import sqlite3
import sys
from pathlib import Path


def snapshot_db(src: Path, dest: Path) -> bool:
    if not Path(src).exists():
        return False
    from src.brain_map import BUSY_TIMEOUT_SECONDS
    tmp = dest.with_name(dest.name + ".part")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    target = sqlite3.connect(str(tmp))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    tmp.replace(dest)
    return True


def snapshot_journal(dest: Path) -> bool:
    from src import journal
    if not journal.JOURNAL_PATH.exists():
        return False
    tmp = dest.with_name(dest.name + ".part")
    with journal.locked():
        shutil.copyfile(journal.JOURNAL_PATH, tmp)
    tmp.replace(dest)
    return True


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m src.dashboard.mirror_snapshot <out_dir>")
        return 2
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    from src import brain_map
    for name, ok in (("brain_map.db", _safe(snapshot_db, Path(brain_map.DEFAULT_DB_PATH), out / "brain_map.db")),
                     ("journal.jsonl", _safe(snapshot_journal, out / "journal.jsonl"))):
        print(f"{out / name}" if ok is True else f"[snapshot] skipped {name}: {ok or 'absent'}")
    return 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:          # fail-open: the push sends what it has
        return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    sys.exit(main())
