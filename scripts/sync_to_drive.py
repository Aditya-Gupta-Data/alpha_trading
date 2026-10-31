# MANUAL OFFLINE TOOL — compiles src/config/docs into one Architecture_Codex.md and syncs it to Google Drive (NotebookLM source).
"""Not on any cron: runs from the optional git post-commit hook. Never imported from src/."""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_NAME = "Architecture_Codex.md"
CRAWL_DIRS = ("src", "config", "docs")
INCLUDE_EXT = {".py", ".md", ".json", ".yaml"}
EXCLUDE_DIRS = {".git", "__pycache__", "venv", ".venv", "logs", "node_modules"}
EXCLUDE_EXT = {".db", ".csv"}
MAX_FILE_BYTES = 400_000
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CONFIG_DIR = Path(os.environ.get("DRIVE_SYNC_DIR", Path.home() / ".config" / "alpha_trading"))
CREDENTIALS_FILE = Path(os.environ.get("DRIVE_CREDENTIALS_FILE", CONFIG_DIR / "drive_credentials.json"))
TOKEN_FILE = Path(os.environ.get("DRIVE_TOKEN_FILE", CONFIG_DIR / "drive_token.json"))
FOLDER_ID_FILE = CONFIG_DIR / "drive_folder_id"


def default_folder_id():
    if os.environ.get("DRIVE_FOLDER_ID"):
        return os.environ["DRIVE_FOLDER_ID"]
    return FOLDER_ID_FILE.read_text().strip() if FOLDER_ID_FILE.exists() else None


def collect_files(root=ROOT):
    found = []
    for top in CRAWL_DIRS:
        base = Path(root) / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                ext = path.suffix.lower()
                if ext in EXCLUDE_EXT or ext not in INCLUDE_EXT:
                    continue
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def build_codex(root=ROOT, now=None):
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    files = collect_files(root)
    parts = [
        f"# Architecture Codex — alpha_trading\n\nGenerated {now.strftime('%Y-%m-%d %H:%M UTC')} "
        f"from {', '.join(d + '/' for d in CRAWL_DIRS)} ({len(files)} files).\n"
    ]
    for path in files:
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        truncated = len(raw) > MAX_FILE_BYTES
        text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        note = f"\n[... truncated at {MAX_FILE_BYTES} of {len(raw)} bytes ...]" if truncated else ""
        lang = path.suffix.lstrip(".")
        parts.append(f"\n---\n\n### File: {rel}\n\n~~~{lang}\n{text.rstrip()}{note}\n~~~\n")
    return "".join(parts)


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
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_existing_id(service, folder_id):
    query = f"name = '{CODEX_NAME}' and '{folder_id}' in parents and trashed = false"
    resp = service.files().list(q=query, fields="files(id, name)", pageSize=10, orderBy="modifiedTime desc").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def upload(service, folder_id, content):
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown", resumable=False)
    file_id = find_existing_id(service, folder_id)
    if file_id:
        service.files().update(fileId=file_id, media_body=media, fields="id").execute()
        return "updated", file_id
    meta = {"name": CODEX_NAME, "parents": [folder_id], "mimeType": "text/markdown"}
    created = service.files().create(body=meta, media_body=media, fields="id").execute()
    return "created", created["id"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder-id", default=default_folder_id(), help="target Drive folder id (or DRIVE_FOLDER_ID, or the drive_folder_id file)")
    ap.add_argument("--dry-run", action="store_true", help="build the codex, print stats, no upload")
    ap.add_argument("--output", help="also write the codex to this local path")
    args = ap.parse_args(argv)

    content = build_codex()
    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
    n_files = content.count("\n### File: ")
    print(f"codex: {n_files} files, {len(content.encode('utf-8')):,} bytes")
    if args.dry_run:
        return 0
    if not args.folder_id:
        raise SystemExit("no target folder: pass --folder-id or set DRIVE_FOLDER_ID")
    action, file_id = upload(get_service(), args.folder_id, content)
    print(f"{CODEX_NAME} {action} (file id {file_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
