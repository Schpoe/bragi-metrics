#!/usr/bin/env python3
"""bragi-metrics MCP server — exposes Jira metrics to Claude over HTTP SSE.

Auth model
----------
MCP_API_KEY  admin key (full access + user management)
bragi_<...>  per-user token (stored hashed in mcp_tokens table)

Endpoints
---------
GET  /health                         public
GET  /admin/users                    admin: list PO members + token counts
POST /admin/users                    admin: create user, returns plaintext token (once)
DELETE /admin/users/{email}          admin: deactivate user, revoke all tokens
POST /admin/users/{email}/tokens     admin: issue extra token for a user
GET  /my/tokens                      user: list own tokens (no plaintext)
POST /my/tokens                      user: create new token, returned once
DELETE /my/tokens/{token_id}         user: revoke own token
GET  /sse                            MCP SSE (admin key or user token)
POST /messages/                      MCP post-message (admin key or user token)
"""

import asyncio
import datetime
import decimal
import hashlib
import json
import logging
import os
import secrets
from contextlib import contextmanager
from typing import Any

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import queries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bragi-metrics")

TEAMS = ["STORE", "AAONE", "AATWO", "CONNECT", "BEST", "GROW", "TCSA"]


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return val


ADMIN_KEY = _require_env("MCP_API_KEY")
server = Server("bragi-metrics")


# -- JSON serialisation -------------------------------------------------------

def _json_default(obj: Any) -> Any:
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime.date):  # covers date and datetime
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def to_json(data: Any) -> str:
    return json.dumps(data, default=_json_default, indent=2)


# -- DB helpers ---------------------------------------------------------------

@contextmanager
def db():
    conn = queries.get_conn()
    try:
        yield conn
    finally:
        conn.close()


# -- Token / user management --------------------------------------------------

_MIGRATE_SQL = """
CREATE TABLE IF NOT EXISTS mcp_users (
    email      TEXT PRIMARY KEY,
    name       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active     BOOLEAN     NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id           SERIAL      PRIMARY KEY,
    user_email   TEXT        NOT NULL REFERENCES mcp_users(email) ON DELETE CASCADE,
    token_hash   TEXT        NOT NULL UNIQUE,
    label        TEXT        NOT NULL DEFAULT 'default',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ
);
"""


def _db_migrate() -> None:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(_MIGRATE_SQL)
        conn.commit()
    log.info("token tables ready")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _make_token() -> tuple[str, str]:
    """Return (plaintext, hash). bragi_ prefix makes tokens easy to identify."""
    raw = secrets.token_urlsafe(32)
    token = f"bragi_{raw}"
    return token, _hash_token(token)


def _lookup_token(conn, token: str) -> dict | None:
    """Return {email, name} if token is valid and not revoked; else None."""
    h = _hash_token(token)
    row = queries.fetchone(
        conn,
        """
        SELECT u.email, u.name
          FROM mcp_tokens t
          JOIN mcp_users u ON u.email = t.user_email
         WHERE t.token_hash = %s
           AND t.revoked_at IS NULL
           AND u.active = TRUE
        """,
        (h,),
    )
    if row:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mcp_tokens SET last_used_at = NOW() WHERE token_hash = %s",
                    (h,),
                )
            conn.commit()
        except Exception:
            pass  # non-fatal; don't block the request
    return dict(row) if row else None


# -- MCP tool definitions -----------------------------------------------------

TOOLS = [
    types.Tool(
        name="get_quarterly_metrics",
        description=(
            "Full metrics snapshot for all PO teams (STORE, AAONE, AATWO, CONNECT, BEST, GROW, TCSA) for a "
            "quarter. Returns efficiency (velocity, delivery %), quality (bugs created/resolved), "
            "lead time, and transparency (scope change, readiness %, issues resolved) — all with "
            "prev-quarter comparison."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "e.g. 2026"},
                "quarter": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "required": ["year", "quarter"],
        },
    ),
    types.Tool(
        name="get_monthly_metrics",
        description=(
            "Monthly metrics for all PO teams: bugs created/resolved, issues resolved, "
            "average lead time. Includes prev-month comparison."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "month": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["year", "month"],
        },
    ),
    types.Tool(
        name="get_metric_trend",
        description=(
            "Historical quarterly trend for a single metric across all PO teams. "
            "Returns one value per quarter per team — useful for trend narratives and charts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["velocity", "delivery_pct", "lead_time", "bugs_created", "bugs_resolved", "issues_resolved"],
                },
                "n_quarters": {
                    "type": "integer",
                    "default": 6,
                    "minimum": 1,
                    "maximum": 16,
                    "description": "How many quarters of history to return (default 6)",
                },
            },
            "required": ["metric"],
        },
    ),
    types.Tool(
        name="get_release_quality",
        description=(
            "Release quality metrics per Jira fix version: total issues, bug count and rate, "
            "open vs resolved issues, bugs fixed after the release date (escape rate), and "
            "overdue status. Filter by release name/fixVersion and/or team. When no team is "
            "given, an aggregated 'overall' rollup across the matched releases is also returned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "release_name": {
                    "type": "string",
                    "description": (
                        "Filter by fix version name (case-insensitive substring). "
                        "e.g. 'Bose QCE 3.0.3', 'QCE 3.0.3', or '3.0.3'."
                    ),
                },
                "project_key": {
                    "type": "string",
                    "enum": ["STORE", "AAONE", "AATWO", "CONNECT", "BEST", "GROW", "TCSA"],
                    "description": "Filter to a specific team. Omit for all teams.",
                },
                "released_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, only return releases marked as released in Jira.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum releases to return (newest release_date first).",
                },
            },
        },
    ),
    types.Tool(
        name="list_available_metrics",
        description=(
            "Lists team keys, queryable metric names, and available tool signatures. "
            "Call this first if unsure what to query."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    log.info("tool=%s args=%s", name, arguments)
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _dispatch, name, arguments)
    except Exception:
        log.exception("tool=%s failed", name)
        raise


def _release_overall(releases: list[dict], release_name: Any) -> dict:
    """Aggregate matched releases into a single overall quality rollup.

    Counts are summed; bug_pct is recomputed from totals (not averaged), so a
    fix version shared across teams reads as one combined release.
    """
    def total(field: str) -> int:
        return sum(int(r.get(field) or 0) for r in releases)

    bug_count = total("bug_count")
    story_count = total("story_count")
    non_meta = bug_count + story_count  # bug_pct denominator (excludes Epic/Sub-task)
    return {
        "release_name_filter": release_name,
        "release_count": len(releases),
        "teams": sorted({r["team"] for r in releases}),
        "total_issues": total("total_issues"),
        "bug_count": bug_count,
        "story_count": story_count,
        "bug_pct": round(100.0 * bug_count / non_meta, 1) if non_meta else None,
        "resolved_issues": total("resolved_issues"),
        "open_issues": total("open_issues"),
        "bugs_after_release": total("bugs_after_release"),
    }


def _dispatch(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "get_quarterly_metrics":
        yr, q = int(arguments["year"]), int(arguments["quarter"])
        with db() as conn:
            efficiency = {r["team"]: dict(r) for r in queries.quarterly_efficiency(conn, yr, q)}
            lead_time = {r["team"]: dict(r) for r in queries.quarterly_lead_time(conn, yr, q)}
            quality = {r["team"]: dict(r) for r in queries.quarterly_quality(conn, yr, q)}
            transparency_rows, readiness_valid = queries.quarterly_transparency(conn, yr, q)
            transparency = {r["team"]: dict(r) for r in transparency_rows}

        teams = sorted(set(efficiency) | set(lead_time) | set(quality) | set(transparency))
        result = []
        for team in teams:
            merged: dict[str, Any] = {"team": team}
            for src in (efficiency, lead_time, quality, transparency):
                merged.update(src.get(team, {}))
            merged["readiness_data_valid"] = readiness_valid
            result.append(merged)

        return [types.TextContent(type="text", text=to_json({"year": yr, "quarter": q, "teams": result}))]

    if name == "get_monthly_metrics":
        yr, m = int(arguments["year"]), int(arguments["month"])
        with db() as conn:
            rows = queries.monthly_data(conn, yr, m)
        return [types.TextContent(type="text", text=to_json({"year": yr, "month": m, "teams": [dict(r) for r in rows]}))]

    if name == "get_metric_trend":
        metric = arguments["metric"]
        n = int(arguments.get("n_quarters", 6))
        with db() as conn:
            data = queries.metric_trend(conn, metric, n)
        return [types.TextContent(type="text", text=to_json({"metric": metric, "quarters": data}))]

    if name == "get_release_quality":
        project_key = arguments.get("project_key")
        release_name = arguments.get("release_name")
        released_only = bool(arguments.get("released_only", False))
        limit = int(arguments.get("limit", 20))
        with db() as conn:
            rows = queries.release_quality(
                conn, project_key=project_key, released_only=released_only,
                limit=limit, release_name=release_name,
            )
        releases = [dict(r) for r in rows]
        payload = {"releases": releases}
        # No team requested -> add an aggregated rollup across matched releases.
        if not project_key:
            payload["overall"] = _release_overall(releases, release_name)
        return [types.TextContent(type="text", text=to_json(payload))]

    if name == "list_available_metrics":
        return [types.TextContent(type="text", text=to_json({
            "teams": TEAMS,
            "tools": {t.name: t.inputSchema for t in TOOLS},
        }))]

    raise ValueError(f"Unknown tool: {name!r}")


# -- Auth middleware ----------------------------------------------------------

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path == "/health":
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        token = auth[7:]

        # /admin/* -- admin key only
        if path.startswith("/admin"):
            if token != ADMIN_KEY:
                log.warning("admin auth rejected path=%s", path)
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

        # /my/* -- valid user token required; admin key not accepted here
        if path.startswith("/my"):
            if token == ADMIN_KEY:
                return JSONResponse(
                    {"error": "Use a personal user token for /my/* — not the admin key"},
                    status_code=403,
                )
            with db() as conn:
                user = _lookup_token(conn, token)
            if not user:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            request.state.user = user
            return await call_next(request)

        # MCP endpoints (/sse, /messages/*) -- admin key OR valid user token
        if token == ADMIN_KEY:
            request.state.user = {"email": "admin", "is_admin": True}
        else:
            with db() as conn:
                user = _lookup_token(conn, token)
            if not user:
                log.warning("mcp auth rejected path=%s", path)
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            request.state.user = user

        return await call_next(request)


# -- Admin endpoints ----------------------------------------------------------

async def handle_admin_users(request: Request) -> JSONResponse:
    if request.method == "GET":
        with db() as conn:
            rows = queries.fetchall(
                conn,
                """
                SELECT u.email, u.name, u.created_at, u.active,
                       COUNT(t.id) FILTER (WHERE t.revoked_at IS NULL) AS active_tokens,
                       COUNT(t.id) AS total_tokens,
                       MAX(t.last_used_at) AS last_used_at
                  FROM mcp_users u
             LEFT JOIN mcp_tokens t ON t.user_email = u.email
              GROUP BY u.email, u.name, u.created_at, u.active
              ORDER BY u.created_at
                """,
            )
        return JSONResponse([dict(r) for r in rows])

    # POST: create/reactivate user + provision first token
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    name = (body.get("name") or "").strip()
    if not email or not name:
        return JSONResponse({"error": "'email' and 'name' are required"}, status_code=400)

    plaintext, token_hash = _make_token()
    with db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mcp_users (email, name) VALUES (%s, %s) "
                    "ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, active = TRUE",
                    (email, name),
                )
                cur.execute(
                    "INSERT INTO mcp_tokens (user_email, token_hash, label) VALUES (%s, %s, 'initial')",
                    (email, token_hash),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error("create user %s failed: %s", email, e)
            return JSONResponse({"error": "Failed to create user"}, status_code=500)

    log.info("created user %s", email)
    return JSONResponse({"email": email, "name": name, "token": plaintext}, status_code=201)


async def handle_admin_user(request: Request) -> JSONResponse:
    """DELETE /admin/users/{email} -- deactivate user and revoke all tokens."""
    email = request.path_params["email"]
    with db() as conn:
        row = queries.fetchone(conn, "SELECT email FROM mcp_users WHERE email = %s", (email,))
        if not row:
            return JSONResponse({"error": "User not found"}, status_code=404)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mcp_tokens SET revoked_at = NOW() "
                "WHERE user_email = %s AND revoked_at IS NULL",
                (email,),
            )
            cur.execute("UPDATE mcp_users SET active = FALSE WHERE email = %s", (email,))
        conn.commit()

    log.info("deactivated user %s", email)
    return JSONResponse({"deactivated": email})


async def handle_admin_user_tokens(request: Request) -> JSONResponse:
    """POST /admin/users/{email}/tokens -- issue new token for an existing user."""
    email = request.path_params["email"]

    with db() as conn:
        row = queries.fetchone(
            conn, "SELECT email FROM mcp_users WHERE email = %s AND active = TRUE", (email,)
        )
    if not row:
        return JSONResponse({"error": "User not found or inactive"}, status_code=404)

    try:
        body = await request.json()
        label = (body.get("label") or "admin-issued").strip()[:64]
    except Exception:
        label = "admin-issued"

    plaintext, token_hash = _make_token()
    with db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mcp_tokens (user_email, token_hash, label) VALUES (%s, %s, %s)",
                    (email, token_hash, label),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error("issue token for %s failed: %s", email, e)
            return JSONResponse({"error": "Failed to create token"}, status_code=500)

    log.info("issued token label=%s for %s", label, email)
    return JSONResponse({"email": email, "token": plaintext, "label": label}, status_code=201)


# -- User self-service endpoints ----------------------------------------------

async def handle_my_tokens(request: Request) -> JSONResponse:
    user = request.state.user

    if request.method == "GET":
        with db() as conn:
            rows = queries.fetchall(
                conn,
                """
                SELECT id, label, created_at, revoked_at, last_used_at
                  FROM mcp_tokens
                 WHERE user_email = %s
                 ORDER BY created_at DESC
                """,
                (user["email"],),
            )
        return JSONResponse([dict(r) for r in rows])

    # POST: create new token
    try:
        body = await request.json()
        label = (body.get("label") or "personal").strip()[:64]
    except Exception:
        label = "personal"

    plaintext, token_hash = _make_token()
    with db() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mcp_tokens (user_email, token_hash, label) VALUES (%s, %s, %s)",
                    (user["email"], token_hash, label),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            log.error("create token for %s failed: %s", user["email"], e)
            return JSONResponse({"error": "Failed to create token"}, status_code=500)

    return JSONResponse({"token": plaintext, "label": label}, status_code=201)


async def handle_my_token(request: Request) -> JSONResponse:
    """DELETE /my/tokens/{token_id} -- revoke one of your own tokens."""
    user = request.state.user
    try:
        token_id = int(request.path_params["token_id"])
    except (ValueError, KeyError):
        return JSONResponse({"error": "Invalid token ID"}, status_code=400)

    with db() as conn:
        row = queries.fetchone(
            conn,
            "SELECT id FROM mcp_tokens WHERE id = %s AND user_email = %s AND revoked_at IS NULL",
            (token_id, user["email"]),
        )
        if not row:
            return JSONResponse({"error": "Token not found"}, status_code=404)
        with conn.cursor() as cur:
            cur.execute("UPDATE mcp_tokens SET revoked_at = NOW() WHERE id = %s", (token_id,))
        conn.commit()

    log.info("user %s revoked token %d", user["email"], token_id)
    return JSONResponse({"revoked": token_id})


# -- Health ------------------------------------------------------------------

def _ping_db() -> None:
    with db() as conn:
        queries.fetchone(conn, "SELECT 1")


async def handle_health(request: Request) -> JSONResponse:
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _ping_db)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        log.error("health check failed: %s", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


# -- App factory -------------------------------------------------------------

def build_app() -> Starlette:
    transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    app = Starlette(
        routes=[
            Route("/health", endpoint=handle_health),
            # Admin routes (MCP_API_KEY required)
            Route("/admin/users", endpoint=handle_admin_users, methods=["GET", "POST"]),
            Route("/admin/users/{email:path}/tokens", endpoint=handle_admin_user_tokens, methods=["POST"]),
            Route("/admin/users/{email:path}", endpoint=handle_admin_user, methods=["DELETE"]),
            # User self-service (personal bragi_* token required)
            Route("/my/tokens/{token_id}", endpoint=handle_my_token, methods=["DELETE"]),
            Route("/my/tokens", endpoint=handle_my_tokens, methods=["GET", "POST"]),
            # MCP SSE
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=transport.handle_post_message),
        ]
    )
    app.add_middleware(AuthMiddleware)
    return app


if __name__ == "__main__":
    _db_migrate()
    log.info("starting bragi-metrics MCP server")
    uvicorn.run(build_app(), host="0.0.0.0", port=8000)
