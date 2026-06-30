"""
Main Starlette application.

MCP transport: low-level mcp.server.Server + SseServerTransport.
  GET  /sse?token={user_token}  — SSE connection (claude.ai connects here)
  POST /messages/               — client messages (handled by transport)
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

from app.mcp_server import VERSION, dispatch_tool
from app.oauth import handle_connect, handle_callback, _home_page
from app import db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

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


# ─── MCP server factory ─────────────────────────────────────────────────────

DETAIL_PARAM = {
    "type": "string",
    "enum": ["summary", "full"],
    "description": "Level of detail. summary (default): top 5 items. full: up to 10 items.",
}

SITE_PARAM = {
    "type": "string",
    "description": (
        "GSC property URI (e.g. 'sc-domain:example.com'). "
        "Omit to use the default property."
    ),
}

PERIOD_PARAM = {
    "type": "string",
    "enum": ["7d", "14d", "28d", "90d"],
    "description": "Look-back period. Default: 28d.",
}


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
                    "Health check. Returns server version and confirms "
                    "the connector is reachable. No data is returned."
                ),
                inputSchema={"type": "object", "properties": {}},
                annotations={
                    "title": "Ping",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            ),
            Tool(
                name="site_overview",
                description=(
                    "Returns a traffic summary from daily-batch Google Search Console data "
                    "(not real-time) for a site over a recent period, compared to the prior "
                    "equal period. Includes totals (clicks, impressions, CTR, position), "
                    "top pages and queries with change deltas, and auto-flagged declines. "
                    "Use as the first call for general traffic questions. "
                    "Returns an explicit empty-state message when no data exists."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": SITE_PARAM,
                        "period": PERIOD_PARAM,
                        "detail": DETAIL_PARAM,
                    },
                },
                annotations={
                    "title": "Site Overview",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            ),
            Tool(
                name="analyze_changes",
                description=(
                    "Decomposes a traffic change into page-level drivers using daily-batch "
                    "GSC data (not real-time). Returns pages with the largest click delta, "
                    "a diagnosis of likely cause (position drop, CTR drop, impression loss), "
                    "and flags for sudden day-over-day shifts. May note estimated possible "
                    "AI Overview impact where patterns suggest it, but this is a hypothesis "
                    "based on correlations, NOT a confirmed attribution. "
                    "Use when the user asks why traffic changed or what dropped."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": SITE_PARAM,
                        "period": PERIOD_PARAM,
                        "detail": DETAIL_PARAM,
                    },
                },
                annotations={
                    "title": "Analyze Changes",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            ),
            Tool(
                name="ai_visibility_snapshot",
                description=(
                    "Shows how the site appears in Google AI search features based on the "
                    "searchAppearance dimension in daily-batch GSC data. Returns impressions "
                    "and clicks by appearance type vs prior period. AI-specific appearance "
                    "types may be limited depending on what Google exposes via the API. "
                    "Returns an honest empty-state when no data is available — never "
                    "fabricates numbers. Use for AI search visibility questions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": SITE_PARAM,
                        "period": PERIOD_PARAM,
                    },
                },
                annotations={
                    "title": "AI Visibility",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            ),
            Tool(
                name="page_quick_audit",
                description=(
                    "Fetches a single page and extracts lightweight content signals: "
                    "title, H1, H2s (up to 10), meta description, approximate word count, "
                    "presence of JSON-LD structured data, and whether the page has a "
                    "noindex directive. Cross-references the URL with 28 days of GSC data "
                    "(clicks, impressions, position) if available. "
                    "Fetch may fail with 403 if the server blocks automated requests — "
                    "that is reported cleanly. No crawling, no LLM calls, no storage. "
                    "Use when the user asks about a specific page's content or structure."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Full URL of the page to audit (must start with http:// or https://).",
                        },
                        "site": SITE_PARAM,
                    },
                    "required": ["url"],
                },
                annotations={
                    "title": "Quick page audit",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            ),
            Tool(
                name="low_hanging_fruit",
                description=(
                    "Finds queries ranked in positions 8-15 with enough impressions to be "
                    "worth optimizing, using 28 days of daily-batch GSC data. Returns up to "
                    "10 rows with estimated extra clicks if the page reaches top-5 (rough "
                    "model assuming ~15%% avg top-5 CTR — actual results will vary). Includes "
                    "brief optimization hints. Use for quick-win or priority-fix questions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": SITE_PARAM,
                        "min_impressions": {
                            "type": "integer",
                            "description": "Minimum impressions over 28 days. Default: 200.",
                        },
                    },
                },
                annotations={
                    "title": "Low-Hanging Fruit",
                    "readOnlyHint": True,
                    "destructiveHint": False,
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        return await dispatch_tool(name, arguments, user_id=user_id)

    return server


# ─── MCP endpoints ───────────────────────────────────────────────────────────

async def sse_endpoint(request: Request):
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
            streams[0], streams[1],
            mcp_server.create_initialization_options(),
        )
    return Response()


# ─── Web routes ──────────────────────────────────────────────────────────────

async def health(request: Request):
    return JSONResponse({"status": "ok", "version": VERSION})


async def ping(request: Request):
    return JSONResponse({
        "status": "ok",
        "version": VERSION,
        "message": "GSC Analyst connector is running.",
        "routes": [
            "GET  /health", "GET  /ping", "GET  /",
            "GET  /connect", "GET  /oauth/callback",
            "GET  /sse?token=...", "POST /messages/",
        ],
    })


async def home(request: Request):
    return HTMLResponse(_home_page())


async def connect(request: Request):
    return await handle_connect(request)


async def oauth_callback(request: Request):
    return await handle_callback(request)


# ─── App assembly ─────────────────────────────────────────────────────────────

routes = [
    Route("/health", health),
    Route("/ping", ping),
    Route("/", home),
    Route("/connect", connect),
    Route("/oauth/callback", oauth_callback),
    Route("/sse", sse_endpoint, methods=["GET"]),
    Mount("/messages/", app=sse_transport.handle_post_message),
]

middleware = [
    Middleware(
        SessionMiddleware,
        secret_key=os.environ["SESSION_SECRET_KEY"],
        https_only=os.environ.get("BASE_URL", "").startswith("https"),
    )
]

app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        log_level="info",
    )
