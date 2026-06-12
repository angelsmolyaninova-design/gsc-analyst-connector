# GSC Analyst Connector

MCP connector that gives Claude access to your Google Search Console data.
Ask plain-English questions about your site traffic directly in Claude chat.

## Setup

### 1. Google Cloud Console

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable **Google Search Console API**
3. Create OAuth 2.0 credentials: **Web application**
4. Add redirect URI: `https://YOUR-RAILWAY-DOMAIN.railway.app/oauth/callback`
5. Copy Client ID and Client Secret

### 2. Supabase (Postgres)

1. Create a project at [supabase.com](https://supabase.com)
2. Go to **Settings → Database → Connection pooling**
3. Copy the **Transaction mode** connection string (port 6543)
4. Run `migrations/001_init.sql` in the SQL editor

### 3. Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | Supabase pooler connection string |
| `GOOGLE_CLIENT_ID` | OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `OAUTH_REDIRECT_URI` | `https://YOUR-DOMAIN/oauth/callback` |
| `TOKEN_ENCRYPTION_KEY` | Fernet key (generate below) |
| `BASE_URL` | `https://YOUR-RAILWAY-DOMAIN.railway.app` |
| `SESSION_SECRET_KEY` | Random 32+ char string |

**Generate Fernet key:**
```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

### 4. Deploy to Railway

```bash
# Link to Railway project (create via Railway dashboard first)
railway link

# Deploy
railway up
```

Or push to `main` branch for auto-deploy after linking the GitHub repo in Railway.

## Local Development

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env values

python -m app.main
```

Server runs at `http://localhost:8000`.

## Connecting to Claude

After OAuth, you'll receive a personal connector URL:
```
https://YOUR-DOMAIN/u/YOUR-TOKEN/sse
```

1. Go to Claude.ai → Settings → Connectors → Add custom connector
2. Paste your URL
3. Ask: *"What's happening with my site traffic?"*

## MCP Tools

| Tool | Purpose |
|---|---|
| `ping` | Health check |
| `site_overview` | Traffic summary vs prior period |
| `analyze_changes` | Page-level traffic change breakdown |
| `ai_visibility_snapshot` | AI search appearance data |
| `low_hanging_fruit` | Position 8-15 optimization opportunities |
