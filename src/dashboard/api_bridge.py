# MANUAL OFFLINE TOOL — the read-only JSON bridge for the desk UI (decision #113).
#   ALPHA_DATA_DIR=data/vm_mirror DASHBOARD_KEY=… uvicorn src.dashboard.api_bridge:app --port 8600
"""
src/dashboard/api_bridge.py — SEVEN GET ROUTES, nothing else.

The React desk (`frontend/`) reads exactly the shapes `src/dashboard/data.py`
already produces for the Streamlit page; this file serves them over HTTP:

    /api/treasury   /api/open-trades   /api/recent-outcomes
    /api/recon/latest   /api/recon/history   /api/audit   /api/freshness

Access: every route requires the shared desk key in `X-Access-Key` when
`DASHBOARD_KEY` is set (constant-time compare); no key configured = open
(local dev only). CORS is enabled for the UI origin(s) in `DASHBOARD_UI_ORIGIN`
(comma-separated; default `*` for the same-origin proxy set-up). No POST, no
writes, no broker call: the module imports only the read layer.
"""
from __future__ import annotations

import hmac
import os
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from src.dashboard import data as d

# The VM writes NAIVE local timestamps and its clock is IST ("2026-09-24T09:21:59").
# A browser parses a naive ISO string as ITS OWN local time, so a viewer outside
# India would see every time shifted (parity audit, 2026-09-24). The bridge
# therefore stamps +05:30 onto naive datetimes; dates and tz-aware strings pass
# through untouched. Numbers are never touched here.
_NAIVE_DT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")


def _ist(ts):
    if isinstance(ts, str) and _NAIVE_DT.match(ts):
        return ts.replace(" ", "T") + "+05:30"
    return ts

app = FastAPI(title="Alpha Desk read-only bridge", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("DASHBOARD_UI_ORIGIN", "*").split(",") if o.strip()],
    allow_methods=["GET"],
    allow_headers=["X-Access-Key"],
)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    """Every payload is read fresh from the mirror on each request; tell the
    browser (and any proxy) never to cache it."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _check_key(request: Request) -> None:
    expected = os.environ.get("DASHBOARD_KEY")
    if not expected:
        return
    given = request.headers.get("x-access-key") or ""
    if not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="access key rejected")


def _recon_row(r: dict) -> dict:
    """The UI's ReconRow: upper-case verdict, mismatches as strings."""
    return {"ts": _ist(r.get("ts")), "verdict": str(r.get("verdict") or "unknown").upper(),
            "broker_positions": int(r.get("broker_positions") or 0),
            "book_rows": int(r.get("book_rows") or 0),
            "mismatches": [str(m.get("detail") or m.get("kind") or m) if isinstance(m, dict) else str(m)
                           for m in (r.get("mismatches") or [])]}


@app.get("/api/treasury")
def treasury(request: Request):
    _check_key(request)
    t = d.treasury()
    if t.get("error"):
        raise HTTPException(status_code=503, detail=t["error"])
    for acct in ("PAPER_10L", "PAPER_2L", "PAPER_2L_ROT"):
        if acct in t and t[acct].get("rejections") is None:
            t[acct]["rejections"] = 0
        if acct in t:
            t[acct]["base_ts"] = _ist(t[acct].get("base_ts"))
    for p in t.get("equity_curve") or []:
        p["ts"] = _ist(p.get("ts"))
    for e in t.get("capital_events") or []:
        e["ts"] = _ist(e.get("ts"))
    return t


@app.get("/api/open-trades")
def open_trades(request: Request):
    _check_key(request)
    return d.open_trades()


@app.get("/api/recent-outcomes")
def recent_outcomes(request: Request):
    _check_key(request)
    return [dict(o, settled=_ist(o.get("settled"))) for o in d.recent_outcomes()]


@app.get("/api/recon/latest")
def recon_latest(request: Request):
    _check_key(request)
    r = d.latest_recon()
    return _recon_row(r) if r else None


@app.get("/api/recon/history")
def recon_history(request: Request):
    _check_key(request)
    rows = []
    for r in d._jsonl(d.RECON_PATH)[-30:]:
        rows.append(_recon_row(r))
    return rows


@app.get("/api/audit")
def audit(request: Request):
    _check_key(request)
    return [dict(e, ts=_ist(e.get("ts"))) for e in d.audit_events()]


@app.get("/api/freshness")
def freshness(request: Request):
    _check_key(request)
    return d.freshness()


@app.get("/api/health")
def health():
    return {"ok": True, "paper_only": True, "writes": False}
