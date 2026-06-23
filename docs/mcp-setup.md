# bragi-metrics MCP Server

Exposes Jira + sprint metrics to Claude (and other MCP clients) over HTTP SSE.

## Connection

```
URL:   https://<host>/sse
Auth:  Bearer <your-token>
```

## Available tools

| Tool | Description |
|---|---|
| `list_available_metrics` | List all teams, tools, and their schemas |
| `get_quarterly_metrics` | Full snapshot for all teams for a quarter |
| `get_monthly_metrics` | Bug/issue/lead-time data per month |
| `get_metric_trend` | Historical trend for one metric across all teams |
| `get_release_quality` | Per-release bug rate, escape rate, open/resolved |

**Teams:** STORE, AAONE, AATWO, CONNECT, BEST, GROW, TCSA

## Token management

Every PO team member gets their own personal token. Tokens start with `bragi_`.

### Admin: provisioning users (requires `MCP_API_KEY`)

```bash
# Create a user (returns the token — shown once, store it)
curl -X POST https://<host>/admin/users \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@bragi.com", "name": "Alice"}'
# → {"email": "alice@bragi.com", "name": "Alice", "token": "bragi_..."}

# List all users and their token counts
curl https://<host>/admin/users \
  -H "Authorization: Bearer $MCP_API_KEY"

# Issue a replacement token (e.g. user lost theirs)
curl -X POST https://<host>/admin/users/alice@bragi.com/tokens \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"label": "replacement"}'

# Deactivate a user (revokes all their tokens)
curl -X DELETE https://<host>/admin/users/alice@bragi.com \
  -H "Authorization: Bearer $MCP_API_KEY"
```

### Users: managing your own tokens (requires your personal token)

```bash
# List your tokens (id, label, created/revoked/last-used timestamps)
curl https://<host>/my/tokens \
  -H "Authorization: Bearer bragi_..."

# Create an additional token (e.g. for a second device)
curl -X POST https://<host>/my/tokens \
  -H "Authorization: Bearer bragi_..." \
  -H "Content-Type: application/json" \
  -d '{"label": "work laptop"}'
# → {"token": "bragi_...", "label": "work laptop"}

# Revoke a token by ID (get ID from GET /my/tokens)
curl -X DELETE https://<host>/my/tokens/3 \
  -H "Authorization: Bearer bragi_..."
```

> **Note:** The admin key (`MCP_API_KEY`) cannot be used for `/my/*` endpoints.
> Each user must authenticate with their own personal token there.

## Configuring in Claude Code

Add to your MCP server config (`~/.claude/settings.json` or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "bragi-metrics": {
      "type": "sse",
      "url": "https://<host>/sse",
      "headers": {
        "Authorization": "Bearer bragi_<your-token>"
      }
    }
  }
}
```

## Deploy

```bash
git pull && docker compose up -d --build
```

The MCP server runs on port 8000 (internal). Expose it through the shared reverse proxy on `proxy_net`.
