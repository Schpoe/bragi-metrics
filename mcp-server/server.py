#!/usr/bin/env python3
"""bragi-metrics MCP server — exposes Jira metrics to Claude over HTTP SSE."""

import asyncio
import decimal
import json
import logging
import os
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


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return val


API_KEY = _require_env("MCP_API_KEY")
server = Server("bragi-metrics")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError(f"Not JSON serializable: {type(obj)}")


def to_json(data: Any) -> str:
    return json.dumps(data, default=_json_default, indent=2)


@contextmanager
def db():
    conn = queries.get_conn()
    try:
        yield conn
    finally:
        conn.close()


TOOLS = [
    types.Tool(
        name="get_quarterly_metrics",
        description=(
            "Full metrics snapshot for all PO teams (STORE, AAONE, AATWO, CONNECT) for a "
            "quarter. Returns efficiency (velocity, delivery %), quality (bugs created/resolved), "
            "lead time, and transparency (scope change, readiness %, issues resolved) — all with "
            "prev-quarter comparison."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "e.g. 2026", "minimum": 2020, "maximum": 2035},
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
                "year": {"type": "integer", "minimum": 2020, "maximum": 2035},
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
                    "enum": [
                        "velocity",
                        "delivery_pct",
                        "lead_time",
                        "bugs_created",
                        "bugs_resolved",
                        "issues_resolved",
                    ],
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
            "overdue status. Optionally filter by team or limit to released versions only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_key": {
                    "type": "string",
                    "enum": ["STORE", "AAONE", "AATWO", "CONNECT"],
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
        released_only = bool(arguments.get("released_only", False))
        limit = int(arguments.get("limit", 20))
        with db() as conn:
            rows = queries.release_quality(conn, project_key=project_key, released_only=released_only, limit=limit)
        return [types.TextContent(type="text", text=to_json({"releases": [dict(r) for r in rows]}))]

    if name == "list_available_metrics":
        return [types.TextContent(type="text", text=to_json({
            "teams": ["STORE", "AAONE", "AATWO", "CONNECT"],
            "tools": {t.name: t.inputSchema for t in TOOLS},
        }))]

    raise ValueError(f"Unknown tool: {name!r}")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
            log.warning("auth rejected path=%s", request.url.path)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


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
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=transport.handle_post_message),
        ]
    )
    app.add_middleware(BearerAuthMiddleware)
    return app


if __name__ == "__main__":
    log.info("starting bragi-metrics MCP server")
    uvicorn.run(build_app(), host="0.0.0.0", port=8000)
