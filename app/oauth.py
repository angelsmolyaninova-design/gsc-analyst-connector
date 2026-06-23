"""Google OAuth 2.0 web flow — manual implementation, no PKCE.

We use a standard authorization code flow for a confidential server-side client
(client_secret is present). PKCE is for public clients; it is not required here
and google_auth_oauthlib.flow.Flow auto-generates a code_verifier that we cannot
round-trip across requests, causing invalid_grant errors. Solved by building the
auth URL and token exchange ourselves with httpx + urllib.parse.
"""
import os
import secrets
import logging
import asyncio
from urllib.parse import urlencode

import httpx
import google.oauth2.id_token
import google.auth.transport.requests

from starlette.requests import Request
from starlette.responses import RedirectResponse, HTMLResponse, Response

from app import db
from app.crypto import encrypt_token

log = logging.getLogger(__name__)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URI = "https://www.googleapis.com/oauth2/v3/userinfo"

# Exact scopes sent to Google — order matters for display, not for function
SCOPES = " ".join([
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/webmasters.readonly",
])


def _auth_url(state: str) -> str:
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": os.environ["OAUTH_REDIRECT_URI"],
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",         # force refresh_token on every connect
        "state": state,
        # No code_challenge — standard confidential-client flow
    }
    url = f"{GOOGLE_AUTH_URI}?{urlencode(params)}"
    log.info("oauth_redirect_url scope=%r url=%s", SCOPES, url)
    return url


async def _exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens. Returns the token response dict."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uri": os.environ["OAUTH_REDIRECT_URI"],
                "grant_type": "authorization_code",
                # No code_verifier — no PKCE
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
    resp.raise_for_status()
    return resp.json()


async def _get_email(access_token: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GOOGLE_USERINFO_URI,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    resp.raise_for_status()
    return resp.json()["email"]


async def handle_connect(request: Request):
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    url = _auth_url(state)
    return RedirectResponse(url)


async def handle_callback(request: Request):
    stored_state = request.session.get("oauth_state")
    received_state = request.query_params.get("state")
    if not stored_state or stored_state != received_state:
        return HTMLResponse("Invalid OAuth state. Please try again.", status_code=400)

    error = request.query_params.get("error")
    if error:
        return HTMLResponse(f"OAuth error: {error}", status_code=400)

    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("Missing authorization code.", status_code=400)

    try:
        tokens = await _exchange_code(code)
    except httpx.HTTPStatusError as e:
        body = e.response.text
        log.error("oauth_token_fetch_failed status=%d body=%s", e.response.status_code, body)
        return HTMLResponse(
            f"Failed to retrieve tokens from Google: {body}", status_code=500
        )
    except Exception as e:
        log.error("oauth_token_fetch_failed error=%s", e)
        return HTMLResponse("Failed to retrieve tokens. Please try again.", status_code=500)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return HTMLResponse(
            "Google did not return a refresh token. "
            "Please revoke access at myaccount.google.com/permissions and try again.",
            status_code=400,
        )

    access_token = tokens.get("access_token", "")
    try:
        email = await _get_email(access_token)
    except Exception as e:
        log.error("oauth_userinfo_failed error=%s", e)
        return HTMLResponse("Could not retrieve your email from Google.", status_code=500)

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
            "INSERT INTO users (email, user_token, google_refresh_token) "
            "VALUES ($1, $2, $3) RETURNING id",
            email, user_token, encrypted_rt,
        )
        user_id = str(row["id"])

    # Discover Search Console properties
    from app.gsc_client import build_service, list_sites
    try:
        service = build_service(encrypted_rt)
        properties = list_sites(service)
    except Exception as e:
        log.error("oauth_list_sites_failed user_id=%s error=%s", user_id, e)
        properties = []

    for prop in properties:
        await db.execute(
            "INSERT INTO sites (user_id, property) VALUES ($1, $2) "
            "ON CONFLICT (user_id, property) DO NOTHING",
            user_id, prop,
        )

    base_url = os.environ["BASE_URL"]
    mcp_url = f"{base_url}/sse?token={user_token}"

    if not properties:
        return HTMLResponse(_zero_properties_page(email))

    asyncio.create_task(_run_backfill(user_id))
    return HTMLResponse(_success_page(mcp_url, email, len(properties)))


async def _run_backfill(user_id: str):
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
    prop_word = "property" if site_count == 1 else "properties"
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
    ol {{ padding-left: 20px; }}
    li {{ margin-bottom: 10px; }}
    .note {{ color: #666; font-size: 0.9rem; margin-top: 16px; }}
  </style>
</head>
<body>
  <h1>&#10003; You're connected!</h1>
  <p>Signed in as <strong>{email}</strong>.
     Found <strong>{site_count}</strong> Search Console {prop_word}.</p>

  <div class="card">
    <strong>Your personal connector URL:</strong><br><br>
    <code>{mcp_url}</code>
  </div>

  <h2>How to connect in Claude</h2>
  <ol>
    <li>Open <strong>Claude.ai</strong> and go to
        <strong>Settings &rarr; Connectors</strong>.</li>
    <li>Click <strong>Add custom connector</strong>.</li>
    <li>Paste the URL above into the connector URL field.</li>
    <li>Save &mdash; Claude will confirm the connection.</li>
    <li>Start a new chat and ask:
        <em>&ldquo;What&rsquo;s happening with my site traffic?&rdquo;</em></li>
  </ol>

  <p class="note">
    &#9200; Data collection started in the background.
    Results will be ready within <strong>~10 minutes</strong>.
  </p>
</body>
</html>"""


def _zero_properties_page(email: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>No Properties Found — GSC Analyst</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 680px;
            margin: 60px auto; padding: 0 24px; color: #111; line-height: 1.6; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 8px; color: #b45309; }}
    .card {{ background: #fffbeb; border: 1px solid #f59e0b; border-radius: 8px;
             padding: 20px; margin: 24px 0; }}
    ol {{ padding-left: 20px; }}
    li {{ margin-bottom: 10px; }}
    a {{ color: #1a56db; }}
    a.btn {{ display: inline-block; margin-top: 24px; padding: 12px 24px;
             background: #1a56db; color: #fff; text-decoration: none;
             border-radius: 6px; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>No Search Console Properties Found</h1>
  <p>Signed in as <strong>{email}</strong>, but your Google account has no
     Search Console properties linked to it.</p>

  <div class="card">
    <strong>What to do:</strong>
    <ol>
      <li>Go to <a href="https://search.google.com/search-console" target="_blank">
          Google Search Console</a> and add your website as a property.</li>
      <li>Complete the verification process (DNS, HTML file, or other method).</li>
      <li>Come back here and click the button below to reconnect.</li>
    </ol>
  </div>

  <a class="btn" href="/connect">Reconnect after adding a property &rarr;</a>
</body>
</html>"""


def _home_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GSC Analyst</title>
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
    Ask Claude plain-English questions about your Google Search Console data &mdash;
    traffic trends, ranking drops, AI search visibility, and quick wins &mdash;
    directly in your Claude chat.
  </p>
  <a class="btn" href="/connect">Connect your site &rarr;</a>
</body>
</html>"""
