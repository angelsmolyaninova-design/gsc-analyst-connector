"""Google OAuth 2.0 web flow handlers."""
import os
import secrets
import logging
import asyncio

from starlette.requests import Request
from starlette.responses import RedirectResponse, HTMLResponse

from google_auth_oauthlib.flow import Flow
import google.oauth2.id_token
import google.auth.transport.requests

from app import db
from app.crypto import encrypt_token
from app.gsc_client import list_sites

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _make_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [os.environ["OAUTH_REDIRECT_URI"]],
            }
        },
        scopes=SCOPES,
        redirect_uri=os.environ["OAUTH_REDIRECT_URI"],
    )


async def handle_connect(request: Request):
    flow = _make_flow()
    state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    response = RedirectResponse(auth_url)
    # Store state in signed cookie via itsdangerous (via Starlette sessions)
    request.session["oauth_state"] = state
    return response


async def handle_callback(request: Request):
    stored_state = request.session.get("oauth_state")
    received_state = request.query_params.get("state")
    if not stored_state or stored_state != received_state:
        return HTMLResponse("Invalid OAuth state. Please try again.", status_code=400)

    code = request.query_params.get("code")
    if not code:
        error = request.query_params.get("error", "unknown")
        return HTMLResponse(f"OAuth error: {error}", status_code=400)

    try:
        flow = _make_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Exception as e:
        log.error("oauth_token_fetch_failed error=%s", e)
        return HTMLResponse("Failed to retrieve tokens. Please try again.", status_code=500)

    # Get user email from id_token or userinfo
    try:
        id_info = google.oauth2.id_token.verify_oauth2_token(
            creds.id_token,
            google.auth.transport.requests.Request(),
            os.environ["GOOGLE_CLIENT_ID"],
        )
        email = id_info["email"]
    except Exception as e:
        log.error("oauth_id_token_verify_failed error=%s", e)
        return HTMLResponse("Could not verify your Google account. Please try again.", status_code=500)

    refresh_token = creds.refresh_token
    if not refresh_token:
        return HTMLResponse(
            "Google did not return a refresh token. "
            "Please revoke access at myaccount.google.com/permissions and try again.",
            status_code=400,
        )

    # Upsert user
    user_token = secrets.token_urlsafe(32)
    encrypted_rt = encrypt_token(refresh_token)

    existing = await db.fetchrow("SELECT id, user_token FROM users WHERE email = $1", email)
    if existing:
        await db.execute(
            "UPDATE users SET google_refresh_token=$1, is_active=true WHERE email=$2",
            encrypted_rt, email,
        )
        user_token = existing["user_token"]
        user_id = str(existing["id"])
    else:
        row = await db.fetchrow(
            """
            INSERT INTO users (email, user_token, google_refresh_token)
            VALUES ($1, $2, $3) RETURNING id
            """,
            email, user_token, encrypted_rt,
        )
        user_id = str(row["id"])

    # Discover and store their Search Console properties
    from app.gsc_client import build_service
    try:
        service = build_service(encrypted_rt)
        properties = list_sites(service)
    except Exception as e:
        log.error("oauth_list_sites_failed user_id=%s error=%s", user_id, e)
        properties = []

    for prop in properties:
        await db.execute(
            """
            INSERT INTO sites (user_id, property)
            VALUES ($1, $2) ON CONFLICT (user_id, property) DO NOTHING
            """,
            user_id, prop,
        )

    # Kick off backfill in background
    if properties:
        asyncio.create_task(_run_backfill(user_id, encrypted_rt, properties))

    base_url = os.environ["BASE_URL"]
    mcp_url = f"{base_url}/sse?token={user_token}"

    return HTMLResponse(_success_page(mcp_url, email, len(properties)))


async def _run_backfill(user_id: str, encrypted_rt: str, properties: list[str]):
    from app.collector import backfill

    user = await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    if not user:
        return
    sites = await db.get_sites_for_user(user_id)
    for site in sites:
        try:
            await backfill(site, user)
        except Exception as e:
            log.error("backfill_task_error site=%s error=%s", site["property"], e)


def _success_page(mcp_url: str, email: str, site_count: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Connected — GSC Analyst</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 680px;
            margin: 60px auto; padding: 0 24px; color: #111; line-height: 1.6; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 8px; }}
    .card {{ background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 8px;
             padding: 20px; margin: 24px 0; }}
    code {{ background: #eee; padding: 2px 6px; border-radius: 4px;
            font-size: 0.9em; word-break: break-all; }}
    .steps ol {{ padding-left: 20px; }}
    .steps li {{ margin-bottom: 10px; }}
    .note {{ color: #666; font-size: 0.9rem; margin-top: 16px; }}
    .tag {{ display: inline-block; background: #d4f4dd; color: #1a6b2f;
            border-radius: 4px; padding: 2px 8px; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <h1>&#10003; You're connected!</h1>
  <p>Signed in as <strong>{email}</strong>. Found <strong>{site_count}</strong> Search Console {"property" if site_count == 1 else "properties"}.</p>

  <div class="card">
    <strong>Your personal connector URL:</strong><br>
    <code>{mcp_url}</code>
  </div>

  <div class="steps">
    <h2>How to connect in Claude</h2>
    <ol>
      <li>Open <strong>Claude.ai</strong> and go to <strong>Settings → Connectors</strong>.</li>
      <li>Click <strong>Add custom connector</strong>.</li>
      <li>Paste your URL above into the connector URL field.</li>
      <li>Save — Claude will confirm the connection.</li>
      <li>Start a new chat and ask something like: <em>"What's happening with my site traffic?"</em></li>
    </ol>
  </div>

  <p class="note">
    &#9200; Data is being collected in the background and will be ready within <strong>~10 minutes</strong>.
    Your first question might show limited data if you ask immediately.
  </p>
</body>
</html>"""


def _home_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GSC Analyst — AI-powered Search Console insights</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; max-width: 580px;
           margin: 80px auto; padding: 0 24px; color: #111; line-height: 1.6; }
    h1 { font-size: 2rem; margin-bottom: 12px; }
    p { font-size: 1.05rem; color: #333; }
    a.btn { display: inline-block; margin-top: 32px; padding: 14px 28px;
            background: #1a56db; color: #fff; text-decoration: none;
            border-radius: 6px; font-weight: 600; font-size: 1rem; }
    a.btn:hover { background: #1244b8; }
  </style>
</head>
<body>
  <h1>GSC Analyst</h1>
  <p>
    Ask Claude plain-English questions about your Google Search Console data —
    traffic trends, ranking drops, AI search visibility, and quick wins —
    directly in your Claude chat.
  </p>
  <a class="btn" href="/connect">Connect your site &rarr;</a>
</body>
</html>"""
