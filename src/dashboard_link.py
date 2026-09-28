"""
src/dashboard_link.py — remember the desk's public link, announce a change
=========================================================================
Decision #118 (2026-09-28). The always-on dashboard box publishes through a
free trycloudflare.com quick tunnel whose URL changes on every restart, and
the box's watchdog now restarts it whenever Cloudflare drops it. The trading
VM's mirror push (`scripts/publish_dashboard_mirror.sh`) reads the box's
current URL after each push and calls this module: the URL is kept in
`data/dashboard_url.txt` (plain text, one line — readable by any brief) and
a CHANGE fires one 🔗 card through the one Discord door. Same URL = silence.

    python3 -m src.dashboard_link --url https://…trycloudflare.com
    python3 -m src.dashboard_link            # print the remembered link

Fail-open: a bad state file or a failed card never breaks the push.
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "dashboard_url.txt"
IST = timezone(timedelta(hours=5, minutes=30))
EVENT = "dashboard_link"


def remembered(state_path=None) -> str | None:
    path = Path(state_path) if state_path is not None else STATE_PATH
    try:
        value = path.read_text().strip()
        return value or None
    except OSError:
        return None


def _card(url: str, previous: str | None, now: datetime) -> dict:
    return {"event": EVENT, "ticker": "desk", "date": now.date().isoformat(),
            "description": (f"The dashboard link changed (the tunnel was restarted). "
                            f"Open: {url}\nSame access key as before."),
            "fields": [{"name": "Previous", "value": previous or "(none remembered)",
                        "inline": False}]}


def announce_if_changed(url: str, state_path=None, fire_fn=None, now: datetime = None) -> dict:
    """Remember `url`; fire one card only when it differs from the remembered
    one (a first sighting counts as a change — the owner needs the link).
    Returns {url, previous, changed, announced, error}."""
    url = (url or "").strip()
    out = {"url": url or None, "previous": remembered(state_path), "changed": False,
           "announced": False, "error": None}
    if not url.startswith("https://"):
        out["error"] = "no https url given"
        return out
    if url == out["previous"]:
        return out
    out["changed"] = True
    path = Path(state_path) if state_path is not None else STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(url + "\n")
    except OSError as exc:
        out["error"] = f"state write failed: {exc}"
    try:
        if fire_fn is None:
            from src.notifier import fire_broadcast as fire_fn
        fire_fn(_card(url, out["previous"], now or datetime.now(IST)))
        out["announced"] = True
    except Exception as exc:                       # the push must not fail on a card
        out["error"] = f"announce failed: {exc}"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="remember / announce the desk's public link")
    ap.add_argument("--url", default=None, help="the box's current tunnel URL")
    args = ap.parse_args(argv)
    if args.url is None:
        print(remembered() or "(no link remembered)")
        return 0
    print(json.dumps(announce_if_changed(args.url), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
