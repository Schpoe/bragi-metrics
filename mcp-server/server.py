#!/usr/bin/env python3
"""bragi-metrics MCP server — exposes Jira metrics to Claude over HTTP SSE."""

import decimal
import json
import os
from contextlib import contextmanager
from typing import Any

import mcp.types as types
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import queries

API_KEY = os.environ["MCP_API_KEY"]
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


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_quarterly_metrics",
            description=(
                "Full metrics snapshot for all PO teams (STORE, AAONE, AATWO, CONNECT) "
                "for a given quarter. Returns efficiency (velocity, delivery %), quality "
                "(bugs created/resolved), lead time, and transparency (scope change, "
                "readiness %, issues resolved) — all with prev-quarter comparison."
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
            name="list_available_metrics",
            description="Lists team keys, queryable metric names, and available tool signatures. Call this first if unsure what to query.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
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
            rows = [dict(r) for r in queries.monthly_data(conn, yr, m)]
        return [types.TextContent(type="text", text=to_json({"year": yr, "month": m, "teams": rows}))]

    if name == "get_metric_trend":
        metric = arguments["metric"]
        n = int(arguments.get("n_quarters", 6))
        with db() as conn:
            result = queries.trend_quarterly(conn, metric, n)
        return [types.TextContent(type="text", text=to_json({"metric": metric, "quarters": result}))]

    if name == "list_available_metrics":
        return [types.TextContent(type="text", text=to_json({
            "teams": list(queries.TEAMS),
            "metrics": ["velocity", "delivery_pct", "lead_time", "bugs_created", "bugs_resolved", "issues_resolved"],
            "tools": {
                "get_quarterly_metrics": "year (int), quarter (1–4)",
                "get_monthly_metrics": "year (int), month (1–12)",
                "get_metric_trend": "metric (str), n_quarters (int, default 6)",
                "list_available_metrics": "(no args)",
            },
        }))]

    raise ValueError(f"Unknown tool: {name}")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


def build_app() -> Starlette:
    transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(streams[0], streams[1], server.create_initialization_options())

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=transport.handle_post_message),
        ]
    )
    app.add_middleware(BearerAuthMiddleware)
    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=8000)
