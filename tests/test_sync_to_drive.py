"""Codex builder + idempotent Drive upload, with no network."""
import importlib.util
import sys
import types
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("sync_to_drive", Path(__file__).resolve().parent.parent / "scripts" / "sync_to_drive.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def _tree(tmp_path):
    for rel, body in {
        "src/a.py": "print(1)\n",
        "src/__pycache__/a.py": "junk",
        "src/data.db": "bin",
        "src/rows.csv": "x,y",
        "src/notes.txt": "skip",
        "config/c.json": "{}",
        "config/x.yaml": "k: v",
        "docs/d.md": "# d",
        "logs/l.md": "excluded top-level",
        "src/venv/v.py": "junk",
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def test_codex_includes_and_excludes(tmp_path):
    _tree(tmp_path)
    codex = sync.build_codex(tmp_path)
    for rel in ("src/a.py", "config/c.json", "config/x.yaml", "docs/d.md"):
        assert f"### File: {rel}" in codex
    for rel in ("__pycache__", "data.db", "rows.csv", "notes.txt", "logs/l.md", "venv"):
        assert rel not in codex


class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self, num_retries=0):
        return self.result


class _Files:
    def __init__(self, existing):
        self.existing, self.calls = existing, []

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Req({"files": [{"id": "F1"}] if self.existing else []})

    def update(self, **kw):
        self.calls.append(("update", kw))
        return _Req({"id": kw["fileId"]})

    def create(self, **kw):
        self.calls.append(("create", kw))
        return _Req({"id": "NEW"})


class _Svc:
    def __init__(self, existing):
        self.f = _Files(existing)

    def files(self):
        return self.f


def _fake_media(monkeypatch):
    http = types.ModuleType("googleapiclient.http")
    http.MediaInMemoryUpload = lambda data, **kw: ("media", len(data))
    monkeypatch.setitem(sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setitem(sys.modules, "googleapiclient.http", http)


def test_upload_updates_existing_by_id(monkeypatch):
    _fake_media(monkeypatch)
    svc = _Svc(existing=True)
    assert sync.upload(svc, "FOLDER", "body") == ("updated", "F1")
    assert [c[0] for c in svc.f.calls] == ["list", "update"]
    assert svc.f.calls[1][1]["fileId"] == "F1"


def test_upload_creates_when_absent(monkeypatch):
    _fake_media(monkeypatch)
    svc = _Svc(existing=False)
    assert sync.upload(svc, "FOLDER", "body") == ("created", "NEW")
    assert svc.f.calls[1][1]["body"]["parents"] == ["FOLDER"]


def test_packs_route_files_and_split_over_cap(tmp_path):
    _tree(tmp_path)
    for rel, n in (("README.md", 5), ("HANDOVER.md", 5), ("docs/BRANCHES.md", 5), ("src/b.py", 50), ("src/c.py", 50)):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("word " * n)
    packs = dict(sync.build_packs(tmp_path, word_cap=80))
    assert "### File: README.md" in packs["alpha_01_guide"]
    assert "### File: docs/BRANCHES.md" in packs["alpha_01_guide"]
    assert "### File: HANDOVER.md" in packs["alpha_02_history"]
    assert "### File: docs/d.md" in packs["alpha_03_specs"]
    assert "### File: config/c.json" in packs["alpha_04_config"]
    code = [n for n in packs if n.startswith("alpha_05_code")]
    assert len(code) > 1 and all("_part" in n for n in code)
    assert sum(v.count("### File: src/") for k, v in packs.items() if k in code) == 3


def test_upload_as_google_doc(monkeypatch):
    _fake_media(monkeypatch)
    svc = _Svc(existing=False)
    sync.upload(svc, "FOLDER", "body", "alpha_01_guide", as_doc=True)
    assert "alpha_01_guide" in svc.f.calls[0][1]["q"] and sync.DOC_MIME in svc.f.calls[0][1]["q"]
    body = svc.f.calls[1][1]["body"]
    assert body["name"] == "alpha_01_guide" and body["mimeType"] == sync.DOC_MIME


def test_char_cap_splits_packs_and_oversized_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "big.py").write_text("x = 1\n" * 400)
    (tmp_path / "src" / "small.py").write_text("y = 2\n")
    packs = sync.build_packs(tmp_path, char_cap=1000)
    code = [c for n, c in packs if n.startswith("alpha_05_code")]
    assert len(code) > 2
    assert all(len(c) < 1400 for c in code)
    assert "file continues in the next part" in "".join(code)
