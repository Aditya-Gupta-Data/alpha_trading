# MANUAL OFFLINE TOOL — compiles the repo's docs, config and code into NotebookLM source packs and syncs them to Google Drive.
"""Each pack is a Google Doc (NotebookLM re-syncs Drive Docs by itself). Run by scripts/notebooklm_autosync.sh. Never imported from src/."""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_NAME = "Architecture_Codex.md"
CRAWL_DIRS = ("src", "config", "docs")
GUIDE_EXTRA = ("docs/README.md", "docs/ACCOUNTS.md", "docs/BRANCHES.md")
HISTORY_FILES = ("HANDOVER.md", "docs/handover_archive.md", "docs/observation_week_ledger.md")
WORD_CAP = 350_000  # NotebookLM refuses or truncates a source past 500k words; keep headroom
CHAR_CAP = 800_000  # a Google Doc holds about 1.02M characters
DOC_MIME = "application/vnd.google-apps.document"
HTTP_TIMEOUT_S = 600  # converting ~800k characters into a Google Doc can take minutes
CODE_PACKS = ("alpha_04_config", "alpha_05_code")  # Google cannot reliably convert hundreds of thousands of code lines into Docs
STALE_NOTE = "This part is no longer used: the repository now fits in fewer parts. It is safe to remove from the notebook.\n"
INCLUDE_EXT = {".py", ".md", ".json", ".yaml"}
EXCLUDE_DIRS = {".git", "__pycache__", "venv", ".venv", "logs", "node_modules"}
EXCLUDE_EXT = {".db", ".csv"}
MAX_FILE_BYTES = 2_000_000
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CONFIG_DIR = Path(os.environ.get("DRIVE_SYNC_DIR", Path.home() / ".config" / "alpha_trading"))
CREDENTIALS_FILE = Path(os.environ.get("DRIVE_CREDENTIALS_FILE", CONFIG_DIR / "drive_credentials.json"))
TOKEN_FILE = Path(os.environ.get("DRIVE_TOKEN_FILE", CONFIG_DIR / "drive_token.json"))
FOLDER_ID_FILE = CONFIG_DIR / "drive_folder_id"


def default_folder_id():
    if os.environ.get("DRIVE_FOLDER_ID"):
        return os.environ["DRIVE_FOLDER_ID"]
    return FOLDER_ID_FILE.read_text().strip() if FOLDER_ID_FILE.exists() else None


def _walk(base):
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            ext = path.suffix.lower()
            if ext in EXCLUDE_EXT or ext not in INCLUDE_EXT:
                continue
            found.append(path)
    return found


def collect_files(root=ROOT):
    root = Path(root)
    found = [p for p in sorted(root.glob("*.md")) if p.is_file()]
    for top in CRAWL_DIRS:
        if (root / top).is_dir():
            found += _walk(root / top)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def pack_of(rel):
    """Which NotebookLM source pack a repo-relative path belongs to."""
    if rel in HISTORY_FILES:
        return "alpha_02_history"
    if "/" not in rel or rel in GUIDE_EXTRA:
        return "alpha_01_guide"
    if rel.startswith("docs/"):
        return "alpha_03_specs"
    if rel.startswith("config/"):
        return "alpha_04_config"
    return "alpha_05_code"


PACK_TITLES = {
    "alpha_01_guide": "the guide: README, architecture, module index, decisions, cron, accounts, branches",
    "alpha_02_history": "the history: HANDOVER (newest first), the handover archive, the incident ledger",
    "alpha_03_specs": "design specs, plans, audits and research notes under docs/",
    "alpha_04_config": "configuration under config/",
    "alpha_05_code": "the Python source under src/",
}
GUIDE_ORDER = ("README.md", "docs/README.md", "CLAUDE.md", "ARCHITECTURE.md", "docs/ACCOUNTS.md",
               "docs/BRANCHES.md", "MODULES.md", "CRON_SETUP.md", "DECISIONS.md")


def _section(root, path):
    rel = path.relative_to(root).as_posix()
    raw = path.read_bytes()
    truncated = len(raw) > MAX_FILE_BYTES
    text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    note = f"\n[... truncated at {MAX_FILE_BYTES} of {len(raw)} bytes ...]" if truncated else ""
    if path.suffix == ".md":
        return f"\n---\n\n### File: {rel}\n\n{text.rstrip()}{note}\n"
    lang = path.suffix.lstrip(".")
    return f"\n---\n\n### File: {rel}\n\n~~~{lang}\n{text.rstrip()}{note}\n~~~\n"


def _chunks(sec, char_cap):
    """Split one oversized file section on line boundaries."""
    if len(sec) <= char_cap:
        return [sec]
    out, cur = [], ""
    for line in sec.splitlines(keepends=True):
        if cur and len(cur) + len(line) > char_cap:
            out.append(cur)
            cur = "\n[... file continues in the next part ...]\n"
        cur += line
    return out + [cur]


def build_packs(root=ROOT, now=None, word_cap=WORD_CAP, char_cap=CHAR_CAP, with_code=True):
    """[(doc name, content)]: one pack per topic, split into parts over word_cap or char_cap; with_code=False keeps the docs only."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    groups = {}
    for path in collect_files(root):
        if not with_code and pack_of(path.relative_to(root).as_posix()) in CODE_PACKS:
            continue
        groups.setdefault(pack_of(path.relative_to(root).as_posix()), []).append(path)
    order = {r: i for i, r in enumerate(GUIDE_ORDER)}
    if "alpha_01_guide" in groups:
        groups["alpha_01_guide"].sort(key=lambda p: (order.get(p.relative_to(root).as_posix(), 99), p.name))
    out = []
    for pack in sorted(groups):
        parts, cur, words, chars = [], [], 0, 0
        for path in groups[pack]:
            for sec in _chunks(_section(root, path), char_cap):
                n, c = len(sec.split()), len(sec)
                if cur and (words + n > word_cap or chars + c > char_cap):
                    parts.append(cur)
                    cur, words, chars = [], 0, 0
                cur.append(sec)
                words, chars = words + n, chars + c
        parts.append(cur)
        for i, secs in enumerate(parts, 1):
            name = pack if len(parts) == 1 else f"{pack}_part{i}"
            head = (f"# alpha_trading — {PACK_TITLES[pack]}"
                    + (f" (part {i} of {len(parts)})" if len(parts) > 1 else "")
                    + f"\n\nGenerated {now.strftime('%Y-%m-%d %H:%M UTC')} from the repository "
                    f"({len(secs)} files). Each file starts with a `### File: <path>` header. "
                    "Docs describe `main`; work not merged yet is in docs/BRANCHES.md.\n")
            out.append((name, head + "".join(secs)))
    return out


def build_codex(root=ROOT, now=None):
    """The old single-file codex: every pack concatenated (kept for --output)."""
    return "".join(content for _, content in build_packs(root, now, word_cap=10**12, char_cap=10**12))


def get_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise SystemExit(f"OAuth client file missing: {CREDENTIALS_FILE}")
            if not sys.stdin.isatty():
                raise SystemExit("no valid Drive token and not interactive; run once by hand: python3 scripts/sync_to_drive.py")
            creds = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES).run_local_server(port=0)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
    import google_auth_httplib2
    import httplib2

    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_S))
    return build("drive", "v3", http=http, cache_discovery=False)


def find_existing_id(service, folder_id, name=CODEX_NAME, mime=None):
    query = f"name = '{name}' and '{folder_id}' in parents and trashed = false"
    if mime:
        query += f" and mimeType = '{mime}'"
    resp = service.files().list(q=query, fields="files(id, name)", pageSize=10, orderBy="modifiedTime desc").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def upload(service, folder_id, content, name=CODEX_NAME, as_doc=False):
    """Create or update one file by name; as_doc converts the markdown into a Google Doc (same id on update)."""
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown", resumable=False)
    file_id = find_existing_id(service, folder_id, name, DOC_MIME if as_doc else None)
    if file_id:
        service.files().update(fileId=file_id, media_body=media, fields="id").execute(num_retries=3)
        return "updated", file_id
    meta = {"name": name, "parents": [folder_id], "mimeType": DOC_MIME if as_doc else "text/markdown"}
    created = service.files().create(body=meta, media_body=media, fields="id").execute(num_retries=3)
    return "created", created["id"]


def stale_parts(service, folder_id, keep):
    """Pack Docs in the folder from an earlier, longer run that this run no longer writes."""
    query = f"'{folder_id}' in parents and trashed = false and mimeType = '{DOC_MIME}' and name contains 'alpha_0'"
    resp = service.files().list(q=query, fields="files(id, name)", pageSize=100).execute()
    return sorted(f["name"] for f in resp.get("files", []) if f["name"] not in keep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder-id", default=default_folder_id(), help="target Drive folder id (or DRIVE_FOLDER_ID, or the drive_folder_id file)")
    ap.add_argument("--dry-run", action="store_true", help="build the packs, print stats, no upload")
    ap.add_argument("--output-dir", help="also write every pack into this local folder (drag them into NotebookLM)")
    ap.add_argument("--output", help="also write the old single-file codex to this local path")
    ap.add_argument("--with-code", action="store_true", help="also build the config and src/ packs (the NotebookLM Docs are docs-only by default)")
    ap.add_argument("--root", default=str(ROOT), help="repository tree to read (the autosync passes a clean export of origin/main)")
    args = ap.parse_args(argv)

    packs = build_packs(Path(args.root), with_code=args.with_code)
    if args.output:
        Path(args.output).write_text(build_codex(Path(args.root)), encoding="utf-8")
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    for name, content in packs:
        if args.output_dir:
            (Path(args.output_dir) / f"{name}.md").write_text(content, encoding="utf-8")
        print(f"{name}: {content.count(chr(10) + '### File: ')} files, {len(content.split()):,} words")
    if args.dry_run:
        return 0
    if not args.folder_id:
        raise SystemExit("no target folder: pass --folder-id or set DRIVE_FOLDER_ID")
    service = get_service()
    for name, content in packs:
        action, file_id = upload(service, args.folder_id, content, name, as_doc=True)
        print(f"{name} {action} (Google Doc https://docs.google.com/document/d/{file_id})")
    for name in stale_parts(service, args.folder_id, {n for n, _ in packs}):
        upload(service, args.folder_id, STALE_NOTE, name, as_doc=True)
        print(f"{name} emptied (no longer used)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
