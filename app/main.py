"""
Main Starlette application.

MCP transport: low-level mcp.server.Server + SseServerTransport mounted
at explicit routes /u/{user_token}/sse (GET) and /u/{user_token}/messages/ (POST).
This pattern is required for claude.ai custom connectors.
"""
import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route, Mount

from mcp.server.sse import SseServerTransport

from app.mcp_server import make_mcp_server, VERSION
from app.oauth import handle_connect, handle_callback, _home_page
from app import db
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    await db.get_pool()
    start_scheduler()
    log.info("app_started version=%s", VERSION)
    yield
    stop_scheduler()
    await db.close_pool()
    log.info("app_stopped")


# ─── MCP per-user handler factory ───────────────────────────────────────────

def make_user_mcp_routes(user_token: str) -> tuple:
    """Return (sse_handler, messages_handler) bound to a user_token path."""
    # We create one shared SseServerTransport per route pair.
    # The transport manages the SSE connection and message routing.
    sse_transport = SseServerTransport(f"/u/{user_token}/messages/")
    mcp_server = make_mcp_server()

    async def sse_endpoint(request: Request):
        # Resolve user from token
        user = await db.get_user_by_token(user_token)
        if user is None:
            return HTMLResponse("Connector not found. Check your URL.", status_code=404)

        user_id = str(user["id"])
        log.info("mcp_connect user_id=%s", user_id)

        # Patch call_tool to inject user_id via context
        # We wrap the server's request handlers to carry user context.
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            read_stream, write_stream = streams
            # Run the MCP server with a modified call_tool that has user context
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    async def messages_endpoint(request: Request):
        await sse_transport.handle_post_message(
            request.scope, request.receive, request._send
        )

    return sse_endpoint, messages_endpoint


# ─── User-context-aware MCP server ──────────────────────────────────────────
# Since SseServerTransport routes are static, we use a different approach:
# a single shared MCP server with per-connection user resolution.

_sse_transport_registry: dict[str, SseServerTransport] = {}


def _get_transport(user_token: str) -> SseServerTransport:
    if user_token not in _sse_transport_registry:
        _sse_transport_registry[user_token] = SseServerTransport(
            f"/u/{user_token}/messages/"
        )
    return _sse_transport_registry[user_token]


async def sse_endpoint(request: Request):
    user_token = request.path_params["user_token"]
    user = await db.get_user_by_token(user_token)
    if user is None:
        return HTMLResponse("Connector not found. Check your connector URL.", status_code=404)

    user_id = str(user["id"])
    log.info("mcp_sse_connect user_id=%s", user_id)

    transport = _get_transport(user_token)
    mcp_server = _build_user_server(user_id)

    async with transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        read_stream, write_stream = streams
        await mcp_server.run(
            read_stream,
            write_stream,
            mcp_server.create_initialization_options(),
        )


async def messages_endpoint(request: Request):
    user_token = request.path_params["user_token"]
    transport = _get_transport(user_token)
    await transport.handle_post_message(
        request.scope, request.receive, request._send
    )


def _build_user_server(user_id: str):
    """Build an MCP server instance with user_id baked into call_tool dispatch."""
    from mcp.server import Server
    from mcp.types import Tool, TextContent
    from app.mcp_server import make_mcp_server as _make, _dispatch, VERSION

    server = Server("gsc-analyst")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        # Re-use the tool list from the shared server definition
        base = _make()
        # Call list_tools on a temporary instance to get the list
        # Simpler: just return the same list inline
        from mcp.types import Tool
        return [
            Tool(
                name="ping",
                description="Health check. Returns server version. Call to verify the connector is working.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="site_overview",
                description=(
                    "Returns traffic summary for a site over a recent period compared to "
                    "the previous equal period. Includes totals (clicks, impressions, CTR, "
                    "position), top 5 pages and queries with delta, and auto-flagged declines. "
                    "Call this first for a general 'what is happening' question."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {
                            "type": "string",
                            "description": "GSC property (e.g. 'sc-domain:example.com'). Defaults to user's first property.",
                        },
                        "period": {
                            "type": "string",
                            "enum": ["7d", "14d", "28d", "90d"],
                            "description": "Period length. Default: 28d.",
                        },
                    },
                },
            ),
            Tool(
                name="analyze_changes",
                description=(
                    "Decomposes traffic change into page-level drivers with root cause "
                    "hypotheses (position drop, CTR drop, impression loss, possible AI Overview "
                    "impact). Use for 'why did traffic change' or 'what dropped'."
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
                    "Shows how the site appears in Google AI features (AI Overviews etc.). "
                    "Returns impressions/clicks by AI appearance type vs prior period. "
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
                    "Finds queries ranked 8-15 with enough impressions to be worth optimizing. "
                    "Returns up to 10 rows with estimated upside and optimization hints. "
                    "Use for 'what should I fix first' or 'quick wins' questions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "site": {"type": "string"},
                        "min_impressions": {
                            "type": "integer",
                            "description": "Min impressions over 28 days. Default: 200.",
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


# ─── Web routes ─────────────────────────────────────────────────────────────

async def health(request: Request):
    return JSONResponse({"status": "ok", "version": VERSION})


async def home(request: Request):
    return HTMLResponse(_home_page())


async def connect(request: Request):
    return await handle_connect(request)


async def oauth_callback(request: Request):
    return await handle_callback(request)


# ─── App assembly ────────────────────────────────────────────────────────────

routes = [
    Route("/health", health),
    Route("/", home),
    Route("/connect", connect),
    Route("/oauth/callback", oauth_callback),
    Route("/u/{user_token}/sse", sse_endpoint),
    Route("/u/{user_token}/messages/", messages_endpoint, methods=["POST"]),
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
