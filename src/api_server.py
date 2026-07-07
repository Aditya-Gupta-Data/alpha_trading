"""
Alpha Trading — Phase 9: the secure public gateway (Cloudflare Tunnel)
======================================================================

The ONE process the GCP VM exposes to the outside world. No firewall port
is ever opened — a Cloudflare Tunnel (`cloudflared`) dials OUT from the VM
and forwards public HTTPS traffic to this app on `localhost:8000`.

It wraps the existing engine API (src/api.py) rather than duplicating it:
the full engine app is mounted underneath, so the dashboard routes and the
new two-way Discord bridge ride behind one gate, one port, one process.

Security model — STRICT, fail-closed (unlike src.api's optional dev gate):
  * every request must carry an `x-api-key` header (or `Authorization:
    Bearer`) matching `.env`'s API_KEY, else 401 Unauthorized;
  * if API_KEY is not configured, EVERYTHING is refused with 503 — this
    app never runs open, there is no localhost-dev mode here (that's what
    running src.api:app directly is for);
  * only GET /api/health (liveness, no secrets) and CORS preflight OPTIONS
    are exempt.

Two-way Discord bridge:
  POST /api/discord/action   {"action": "approve"|"reject",
                              "trade_id": "<journal short_id>",
                              "why": "optional one-liner"}
  finds the journal's matching `pending_approval` entry and decides it with
  exactly the `--review-pending` CLI semantics (options_proposer.
  decide_pending, decision #31): approve -> "approved" ON PAPER (the plan
  tracker takes over; no broker anywhere, decision #11), reject ->
  "rejected" (the canonical skip). Entries the tracker already resolved
  hypothetically come back 409 — no approving with hindsight.

Run on the VM:  uvicorn src.api_server:app --host 127.0.0.1 --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Reuse the engine app and its battle-tested key helpers — one source of
# truth for how a key is read from .env and compared (constant-time).
from src.api import app as engine_app
from src.api import _extract_api_key, _keys_match, _read_api_key
from src import options_proposer


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run the engine app's lifespan (auto-sync + watchlist poll loops) —
    Starlette does not start a mounted app's lifespan on its own."""
    async with engine_app.router.lifespan_context(engine_app):
        yield


app = FastAPI(title="Alpha Trading Public Gateway", docs_url=None,
              redoc_url=None, lifespan=_lifespan)


class StrictApiKeyMiddleware(BaseHTTPMiddleware):
    """Fail-closed gate on EVERY route (own + mounted). 503 when API_KEY
    is unset, 401 when the header is missing or wrong. Only GET
    /api/health and OPTIONS preflights (which browsers send without custom
    headers) pass through."""

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.method == "GET" and request.url.path == "/api/health":
            return await call_next(request)
        expected = _read_api_key()
        if expected is None:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "Gateway locked: API_KEY is "
                         "not configured in .env — refusing to serve "
                         "unauthenticated. Set API_KEY and restart."},
            )
        provided = _extract_api_key(request)
        if not provided or not _keys_match(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"ok": False,
                         "error": "Unauthorized — valid x-api-key required."},
            )
        return await call_next(request)


app.add_middleware(StrictApiKeyMiddleware)


@app.get("/api/health")
def health():
    """Public liveness probe (the tunnel's target check). No secrets."""
    return {"status": "ok", "mode": "paper-only", "auth": "required"}


class DiscordActionRequest(BaseModel):
    action: str            # "approve" | "reject"
    trade_id: str          # the journal entry's short_id
    why: str = ""          # optional one-line reason, journaled verbatim


@app.post("/api/discord/action")
def discord_action(req: DiscordActionRequest):
    """Decide a pending_approval journal entry from the outside world
    (Discord bot, phone). Same semantics as the --review-pending CLI."""
    action = req.action.strip().lower()
    if action not in ("approve", "reject"):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"Unknown action {req.action!r} "
                     "— use 'approve' or 'reject'."},
        )
    result = options_proposer.decide_pending(
        req.trade_id.strip(), approve=(action == "approve"),
        why=req.why.strip() or "(decided via Discord bridge)")
    if result["status"] == "not_found":
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"No pending_approval entry with "
                     f"trade_id {req.trade_id!r} in the journal."},
        )
    if result["status"] == "already_resolved":
        verdict = (result["entry"].get("outcome") or {}).get("verdict")
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": "Entry already resolved "
                     f"hypothetically (verdict: {verdict}) — left as-is; no "
                     "approving with hindsight.", "trade_id": req.trade_id},
        )
    return {"ok": True, "decision": result["status"],
            "trade_id": req.trade_id, "entry": result["entry"]}


# Everything else — watchlist, chat, decision, scorecard, static dashboard —
# is the engine app, unchanged, now behind the strict gate. Mounted LAST so
# the gateway's own routes above win the match first.
app.mount("/", engine_app)
