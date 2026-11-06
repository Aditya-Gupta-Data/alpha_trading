"""scripts/branch_report.py: splice keeps the hand-written head; build sees unmerged and merged branches."""
import importlib.util
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("branch_report", ROOT / "scripts" / "branch_report.py")
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=repo, check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "DECISIONS.md").write_text("| 11 | **ONE.** x | y |\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "feature/done")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "done work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/done")
    _git(repo, "checkout", "-q", "-b", "feature/open")
    (repo / "DECISIONS.md").write_text("| 11 | **ONE.** x | y |\n| 12 | **TWO NEW.** z | w |\n")
    _git(repo, "commit", "-q", "-am", "feat: open work\n\nWhy it matters.")
    _git(repo, "checkout", "-q", "main")
    return repo


def test_splice_keeps_head_and_replaces_auto():
    old = f"# Mine\nnotes\n\n{br.BEGIN}\nold auto\n{br.END}\n"
    new = br.splice(old, "new auto")
    assert new.startswith("# Mine\nnotes\n")
    assert "new auto" in new and "old auto" not in new
    assert br.splice("", "x").startswith("# BRANCHES.md")


def test_decision_changes_flags_new_and_clash():
    main = "| 15 | **FIVE MAIN.** a | b |\n"
    branch = "| 15 | **FIVE OTHER.** a | b |\n| 16 | **SIX.** c | d |\n"
    new, clash = br.decision_changes(branch, main)
    assert new == [(16, "SIX.")]
    assert clash[0][0] == 15


def test_build_lists_open_branch_and_merged_branch(tmp_path):
    repo = _repo(tmp_path)
    out = br.build(root=repo, main="main", today=date(2030, 1, 1))
    assert "### `feature/open` — STALE" in out
    assert "feat: open work" in out and "> Why it matters." in out
    assert "Adds decision #12" in out
    assert "`feature/done`" in out.split("nothing left to merge", 1)[1]
