#!/usr/bin/env python3
# MANUAL OFFLINE TOOL — called by scripts/node_token_mirror.sh (cron 07:15 under --with-token).
"""
scripts/node_token_mirror.py — validate the VM's Dhan access token line and swap it into the node's .env.

Reads ONE line (`DHAN_ACCESS_TOKEN=<jwt>`) on stdin. The token never appears in argv, in the
environment or in output. It is accepted only if it is a well-formed three-part JWT whose `exp`
is more than ten minutes away; otherwise the node keeps its current token and the exit code is 1.
Only the DHAN_ACCESS_TOKEN line of the node's .env changes (backup `.env.bak` first, atomic
replace, mode 600). It never renews anything — decision #48.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path


def token_from_line(line: str) -> str:
    line = (line or "").strip()
    if not line.startswith("DHAN_ACCESS_TOKEN="):
        return ""
    return line.split("=", 1)[1].strip().strip('"').strip("'")


def token_exp(tok: str):
    parts = tok.split(".")
    if len(tok) < 100 or len(parts) != 3:
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad)).get("exp")
    except Exception:
        return None


def mirror(line: str, env_path, now: float | None = None) -> tuple[bool, str]:
    """(ok, message). The message never contains the token."""
    now = time.time() if now is None else now
    tok = token_from_line(line)
    exp = token_exp(tok) if tok else None
    if not tok or exp is None:
        return False, "the VM line is not a well-formed token — node keeps its current token"
    if exp <= now + 600:
        return False, "the VM token is expired or about to be — node keeps its current token"
    env = Path(env_path)
    text = env.read_text() if env.exists() else ""
    out, done = [], False
    for ln in text.splitlines():
        if ln.split("=", 1)[0].strip() == "DHAN_ACCESS_TOKEN":
            out.append("DHAN_ACCESS_TOKEN=" + tok)
            done = True
        else:
            out.append(ln)
    if not done:
        out.append("DHAN_ACCESS_TOKEN=" + tok)
    if env.exists():
        bak = env.with_name(env.name + ".bak")
        bak.write_text(text)
        os.chmod(bak, 0o600)
    tmp = env.with_name(env.name + ".part")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env)
    return True, "token mirrored from the VM, expires " + time.strftime("%F %T", time.localtime(exp))


def main() -> int:
    ok, msg = mirror(sys.stdin.readline(), Path(".env"))
    print(f"[{time.strftime('%F %T')}] node_token_mirror: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
