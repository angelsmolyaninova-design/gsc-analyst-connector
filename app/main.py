"""
Main Starlette application.

MCP transport: low-level mcp.server.Server + SseServerTransport.
  - GET /sse?token={user_token}   → SSE connection (claude.ai connects here)
  - POST /messages/               → client → server messages (handled by transport)

The SseServerTransport sends an "endpoint" event that directs the client to POST
to /messages/?session_id=<uuid>. We mount /messages/ with Starlette Mount so the
ASGI app receives the request directly without Starlette's request wrapping.

Connector URL shown to users: https://domain/sse?token={user_token}
"""
import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route, Mount

from mcp.server.sse import SseServerTransport

from app.mcp_server import VERSION, _dispatch
from app.oauth import handle_connect, handle_callback, _home_page
from app import db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

# Single shared transport — session_id ties each POST back to the right SSE connection
sse_transport = SseServerTransport("/messages/")


@asynccontextmanager
async def lifespan(app):
    await db.get_pool()
    start_scheduler()
    log.info("app_started version=%s", VERSION)
    yield
    stop_scheduler()
    await db.close_pool()
    log.info("app_stopped")


# ─── MCP server factory (per-connection, with user_id baked in) ──────────────

def _build_user_server(user_id: str):
    from mcp.server import Server
    from mcp.types import Tool, TextContent

    server = Server("gsc-analyst")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="ping",
                description=(
                    "Health check. Returns server version. "
                    "Call to verify the connector is working."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="site_overview",
                description=(
                    "Returns traffic summary for a site over a recent period compared to "
                    "the previous equal period. Includes totals (clicks, impressions, CTR, "
                    "position), top 5 pages and queries with delta, and auto-flagged declines. "
                    "Call this first for a general 'what is happening with my site' question."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {
                            "type": "string",
                            "description": (
                                "GSC property URI (e.g. 'sc-domain:example.com'). "
                                "Omit to use the user's first property."
                            ),
                        },
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Look-back period. Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="analyze_changes",
                description=(
                    "Decomposes traffic change into page-level drivers. Returns pages with "
                    "largest click delta, diagnosis of cause (position drop, CTR drop, "
                    "impression loss), flags for sudden day-over-day shifts, and estimated "
                    "AI Overview impact where applicable. "
                    "Use for 'why did traffic change' or 'what dropped'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="ai_visibility_snapshot",
                description=(
                    "Shows how the site appears in Google AI features (AI Overviews, etc.). "
                    "Returns impressions and clicks by AI appearance type vs prior period. "
                    "Returns honest 'not available' if data is missing. "
                    "Use for questions about AI search visibility."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="low_hanging_fruit",
                description=(
                    "Finds queries ranked in positions 8-15 with enough impressions to "
                    "be worth optimizing. Returns up to 10 rows with estimated extra clicks "
                    "if page reaches top-5, plus brief optimization hints. "
                    "Use for 'what should I fix first' or 'quick wins' questions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "min_impressions": {
                            "type": "integer",
                            "description": "Minimum impressions over 28 days. Default: 200.",
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            result = await _dispatch(name, arguments, user_id=user_id)
        except Exception as e:
            log.error("tool_error tool=%s user_id=%s error=%s", name, user_id, e)
            return [TextContent(type="text", text=f"Error: {e}")]
        return [TextContent(type="text", text=result)]

    return server


# ─── MCP endpoints ───────────────────────────────────────────────────────────

async def sse_endpoint(request: Request):
    """GET /sse?token={user_token} — establish SSE connection for claude.ai."""
    token = request.query_params.get("token")
    if not token:
        return Response("Missing token query parameter.", status_code=400)

    user = await db.get_user_by_token(token)
    if user is None:
        return Response("Connector not found. Check your connector URL.", status_code=404)

    user_id = str(user["id"])
    log.info("mcp_sse_connect user_id=%s", user_id)

    mcp_server = _build_user_server(user_id)

    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )

    # Must return a Response to avoid TypeError on client disconnect
    return Response()


# ─── Web routes ──────────────────────────────────────────────────────────────

async def health(request: Request):
    return JSONResponse({"status": "ok", "version": VERSION})


async def ping(request: Request):
    """No-auth smoke test — confirms app is up and routes are working."""
    return JSONResponse({
        "status": "ok",
        "version": VERSION,
        "message": "GSC Analyst connector is running. Complete OAuth to get your connector URL.",
        "routes": [
            "GET  /health",
            "GET  /ping           (this endpoint — no auth)",
            "GET  /               (home page)",
            "GET  /connect        (start OAuth)",
            "GET  /oauth/callback (OAuth return)",
            "GET  /sse?token=...  (MCP SSE — requires valid token from OAuth)",
            "POST /messages/      (MCP messages — used by claude.ai internally)",
        ],
    })


async def debug_routes(request: Request):
    """Shows live registered routes for debugging. Remove after smoke test."""
    from starlette.routing import Route as R, Mount as M
    entries = []
    for r in app.routes:
        if isinstance(r, R):
            entries.append({
                "type": "Route",
                "path": r.path,
                "methods": sorted(r.methods) if r.methods else ["*"],
                "name": r.name,
            })
        elif isinstance(r, M):
            entries.append({
                "type": "Mount",
                "path": r.path,
                "name": r.name,
            })
    return JSONResponse({"routes": entries})


async def home(request: Request):
    return HTMLResponse(_home_page())


async def connect(request: Request):
    return await handle_connect(request)


async def oauth_callback(request: Request):
    return await handle_callback(request)


# ─── App assembly ─────────────────────────────────────────────────────────────
# Mount /messages/ as a raw ASGI app (not a Route endpoint) — required by SDK.

routes = [
    Route("/health", health),
    Route("/ping", ping),
    Route("/debug/routes", debug_routes),
    Route("/", home),
    Route("/connect", connect),
    Route("/oauth/callback", oauth_callback),
    Route("/sse", sse_endpoint, methods=["GET"]),
    Mount("/messages/", app=sse_transport.handle_post_message),
]

middleware = [
    Middleware(
        SessionMiddleware,
        secret_key=os.environ.get("SESSION_SECRET_KEY", "dev-secret-change-me"),
        https_only=os.environ.get("BASE_URL", "").startswith("https"),
    )
]

app = Starlette(
    routes=routes,
    middleware=middleware,
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        log_level="info",
    )
