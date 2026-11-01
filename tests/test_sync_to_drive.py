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

    def execute(self):
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
