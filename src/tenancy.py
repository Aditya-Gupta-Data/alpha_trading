"""
src/tenancy.py — Phase 3+ Multi-Seat Topology (M6): Per-Tenant SQLite Isolation & Role Permissions
===================================================================================================

Implements Vector 6 of `docs/ARCHITECTURE_ROADMAP.md` (§4 / §5 line 266):
  1. Canonical tenant identity (`validate_tenant_id`, `PRIMARY_TENANT_ID = "owner"`):
     - Normalizes `None` / `""` / `"owner"` / `"primary"` to `"owner"` so every
       existing single-seat caller remains byte-identical on `data/brain_map.db`.
     - Validates secondary tenant IDs (`[a-z0-9][a-z0-9_-]{0,30}`) to prevent
       path traversal (`..`, `/`, `\\`) or SQL identifier injection.
  2. Per-tenant SQLite path resolution & isolated connection factory
     (`tenant_db_path`, `connect_tenant`):
     - Maps `"owner"` -> `<base_dir>/brain_map.db` and secondary seat `<t>` ->
       `<base_dir>/tenants/<t>/brain_map.db`.
     - Installs a SQLite connection authorizer (`_tenant_authorizer`) that denies
       `SQLITE_ATTACH` (`ATTACH DATABASE`) across all connections, preventing any
       seat from attaching or querying another tenant's database file.
     - Enforces `mode="ro"` (`file:...?mode=ro` + `PRAGMA query_only = ON`) for
       read-only seats (`viewer`, `analyst` on execution tables).
     - Enforces the `CLAUDE.md` Rule 6 pytest muzzle: refuses to open the repo's
       real `data/` directory under `PYTEST_CURRENT_TEST` unless an explicit
       isolated `base_dir` (e.g. `tmp_path`) is supplied.
  3. Role-based seat permissions composed with node-role topology
     (`SeatRole`, `ROLE_PERMISSIONS`, `check_seat_permission`):
     - Roles: `"owner"`, `"operator"`, `"analyst"`, `"viewer"`.
     - Composes with `src.node_role` (`execution_refusal`, `llm_jobs_refusal`) so
       the Mini PC analytics node never executes paper-book mutations (#146) and
       the GCP VM never runs heavy local LLM jobs (#149).

Pure Python + stdlib (`os`, `re`, `sqlite3`, `pathlib`); zero network or broker imports.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from src import node_role

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"

PRIMARY_TENANT_ID = "owner"
PRIMARY_ALIASES = frozenset({"", "owner", "primary", "default"})
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

BUSY_TIMEOUT_MS = 30_000

# Canonical seat roles (M6 multi-seat topology)
ROLE_OWNER = "owner"
ROLE_OPERATOR = "operator"
ROLE_ANALYST = "analyst"
ROLE_VIEWER = "viewer"
VALID_ROLES = frozenset({ROLE_OWNER, ROLE_OPERATOR, ROLE_ANALYST, ROLE_VIEWER})

# Canonical action vocabulary
ACTION_READ = "read"
ACTION_ANALYTICS = "analytics"
ACTION_LLM_EXTRACT = "llm_extract"
ACTION_PROPOSE_PAPER = "propose_paper"
ACTION_APPROVE_PAPER = "approve_paper"
ACTION_HALT_TRIP = "halt_trip"
ACTION_HALT_CLEAR = "halt_clear"
ACTION_CAPITAL_INJECT = "capital_inject"
ACTION_TENANT_ADMIN = "tenant_admin"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset({
        ACTION_READ,
        ACTION_ANALYTICS,
        ACTION_LLM_EXTRACT,
        ACTION_PROPOSE_PAPER,
        ACTION_APPROVE_PAPER,
        ACTION_HALT_TRIP,
        ACTION_HALT_CLEAR,
        ACTION_CAPITAL_INJECT,
        ACTION_TENANT_ADMIN,
    }),
    ROLE_OPERATOR: frozenset({
        ACTION_READ,
        ACTION_PROPOSE_PAPER,
        ACTION_APPROVE_PAPER,
        ACTION_HALT_TRIP,
    }),
    ROLE_ANALYST: frozenset({
        ACTION_READ,
        ACTION_ANALYTICS,
        ACTION_LLM_EXTRACT,
        ACTION_PROPOSE_PAPER,
    }),
    ROLE_VIEWER: frozenset({
        ACTION_READ,
    }),
}

# Actions that mutate the execution book and must run on the execution node (VM, #146)
EXECUTION_NODE_ACTIONS = frozenset({
    ACTION_APPROVE_PAPER,
    ACTION_CAPITAL_INJECT,
})

# Actions that run local LLM mining/evolution and belong on the analytics node (Mini PC, #149)
ANALYTICS_NODE_ACTIONS = frozenset({
    ACTION_LLM_EXTRACT,
})


def validate_tenant_id(tenant_id: str | None = None) -> str:
    """Return the canonical tenant ID or raise `ValueError` on invalid input.

    `None`, `""`, `"owner"`, `"primary"`, and `"default"` normalize to `"owner"`
    (the single-seat primary database). Secondary tenant IDs must match
    `^[a-z0-9][a-z0-9_-]{0,30}$` and cannot contain path separators or `..`."""
    raw = "" if tenant_id is None else str(tenant_id).strip().lower()
    if raw in PRIMARY_ALIASES:
        return PRIMARY_TENANT_ID
    if ".." in raw or "/" in raw or "\\" in raw or not TENANT_ID_RE.match(raw):
        raise ValueError(
            f"invalid tenant_id {tenant_id!r}: must match {TENANT_ID_RE.pattern}"
        )
    return raw


def tenant_db_path(
    tenant_id: str | None = None,
    base_dir: Path | str | None = None,
) -> Path:
    """Resolve the isolated SQLite `brain_map.db` path for `tenant_id`.

    - `"owner"` (primary) -> `<base_dir>/brain_map.db` (backward-compatible with
      single-seat callers).
    - Secondary tenant `<tid>` -> `<base_dir>/tenants/<tid>/brain_map.db`."""
    tid = validate_tenant_id(tenant_id)
    root_dir = Path(base_dir).resolve() if base_dir is not None else DEFAULT_DATA_DIR.resolve()
    if tid == PRIMARY_TENANT_ID:
        return root_dir / "brain_map.db"
    return root_dir / "tenants" / tid / "brain_map.db"


def _is_real_data_dir(path: Path) -> bool:
    try:
        resolved = path.resolve()
        real_data = DEFAULT_DATA_DIR.resolve()
        return resolved == real_data or real_data in resolved.parents
    except OSError:
        return True


def _tenant_authorizer(action_code: int, arg1, arg2, db_name, trigger_name) -> int:
    """SQLite authorizer firewall: denies `ATTACH DATABASE` (`SQLITE_ATTACH`) so a
    seat's connection can never attach or read/write another tenant's DB file."""
    if action_code == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def connect_tenant(
    tenant_id: str | None = None,
    mode: str = "rw",
    base_dir: Path | str | None = None,
    role: str | None = None,
) -> sqlite3.Connection:
    """Open an isolated SQLite connection for `tenant_id` (`mode="rw"` or `"ro"`).

    - Enforces `role="viewer"` -> `mode="ro"` (viewers can never open a writable handle).
    - Enforces the `CLAUDE.md` Rule 6 pytest muzzle against touching the real `data/` tree.
    - Installs `_tenant_authorizer` to block cross-tenant `ATTACH DATABASE` statements."""
    if mode not in ("rw", "ro"):
        raise ValueError(f"invalid connection mode {mode!r}: expected 'rw' or 'ro'")
    if role is not None:
        norm_role = str(role).strip().lower()
        if norm_role not in VALID_ROLES:
            raise ValueError(f"unknown seat role {role!r}; valid={sorted(VALID_ROLES)}")
        if norm_role == ROLE_VIEWER and mode != "ro":
            raise PermissionError("viewer seat role is restricted to mode='ro' connections")

    db_file = tenant_db_path(tenant_id, base_dir=base_dir)
    if os.environ.get("PYTEST_CURRENT_TEST") and _is_real_data_dir(db_file):
        raise RuntimeError(
            f"tenancy.connect_tenant muzzled under pytest for real data path {db_file}; "
            "pass an isolated base_dir=tmp_path"
        )

    if mode == "ro":
        if not db_file.exists():
            raise FileNotFoundError(f"tenant DB does not exist for read-only open: {db_file}")
        conn = sqlite3.connect(
            f"file:{db_file}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
        )
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only = ON")
    else:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file), timeout=BUSY_TIMEOUT_MS / 1000.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_tenant_authorizer)
    return conn


def check_seat_permission(
    role: str,
    action: str,
    tenant_id: str | None = None,
    target_tenant_id: str | None = None,
    enforce_topology: bool = True,
    env: dict | None = None,
    hostname: str | None = None,
) -> dict:
    """Verify whether a seat with `role` on `tenant_id` may perform `action`.

    Enforces:
      1. Role validation (`owner` | `operator` | `analyst` | `viewer`).
      2. Cross-tenant boundary: a seat on `tenant_id` cannot act on a different
         `target_tenant_id` unless `role == "owner"` AND `tenant_id == "owner"`.
      3. Action capability matrix (`ROLE_PERMISSIONS`).
      4. Node-role topology (`src.node_role`, when `enforce_topology=True`):
         - `EXECUTION_NODE_ACTIONS` (`approve_paper`, `capital_inject`) are refused
           on the Mini PC analytics node (`node_role.execution_refusal`, #146).
         - `ANALYTICS_NODE_ACTIONS` (`llm_extract`) are refused on the execution
           VM unless `ALPHA_ALLOW_LLM_JOBS=1` (`node_role.llm_jobs_refusal`, #149)."""
    norm_role = str(role or "").strip().lower()
    norm_action = str(action or "").strip().lower()
    try:
        seat_tid = validate_tenant_id(tenant_id)
        target_tid = validate_tenant_id(
            target_tenant_id if target_tenant_id is not None else seat_tid
        )
    except ValueError as exc:
        return {
            "allowed": False,
            "role": norm_role,
            "action": norm_action,
            "reason": f"invalid_tenant: {exc}",
        }

    if norm_role not in VALID_ROLES:
        return {
            "allowed": False,
            "role": norm_role,
            "action": norm_action,
            "tenant_id": seat_tid,
            "target_tenant_id": target_tid,
            "reason": f"unknown_role: {role!r}",
        }

    # Cross-tenant isolation check: only the primary owner seat can administer another tenant
    if seat_tid != target_tid and not (norm_role == ROLE_OWNER and seat_tid == PRIMARY_TENANT_ID):
        return {
            "allowed": False,
            "role": norm_role,
            "action": norm_action,
            "tenant_id": seat_tid,
            "target_tenant_id": target_tid,
            "reason": (
                f"cross_tenant_denied: seat {seat_tid!r} ({norm_role}) cannot access "
                f"tenant {target_tid!r}"
            ),
        }

    allowed_actions = ROLE_PERMISSIONS[norm_role]
    if norm_action not in allowed_actions:
        return {
            "allowed": False,
            "role": norm_role,
            "action": norm_action,
            "tenant_id": seat_tid,
            "target_tenant_id": target_tid,
            "reason": f"role_permission_denied: role {norm_role!r} lacks {norm_action!r}",
        }

    if enforce_topology:
        if norm_action in EXECUTION_NODE_ACTIONS:
            refusal = node_role.execution_refusal(env=env, hostname=hostname)
            if refusal:
                return {
                    "allowed": False,
                    "role": norm_role,
                    "action": norm_action,
                    "tenant_id": seat_tid,
                    "target_tenant_id": target_tid,
                    "reason": f"topology_execution_refused: {refusal}",
                }
        if norm_action in ANALYTICS_NODE_ACTIONS:
            refusal = node_role.llm_jobs_refusal(env=env, hostname=hostname)
            if refusal:
                return {
                    "allowed": False,
                    "role": norm_role,
                    "action": norm_action,
                    "tenant_id": seat_tid,
                    "target_tenant_id": target_tid,
                    "reason": f"topology_llm_refused: {refusal}",
                }

    return {
        "allowed": True,
        "role": norm_role,
        "action": norm_action,
        "tenant_id": seat_tid,
        "target_tenant_id": target_tid,
        "reason": "ok",
    }

