# Polymarket MCP Server

Polymarket data for AI agents. Plug this into Claude Desktop, Cursor, Cline, or any other [MCP](https://modelcontextprotocol.io)-compatible client and ask your agent things like:

- *"Who are the top 10 Polymarket wallets by 30-day profit?"*
- *"Show me recent BUYs from smart money in the last 30 minutes"*
- *"Pull the latest trades for wallet 0xa5ea13..."*
- *"What's the market info for `mlb-nyy-tor-2026-06-13`?"*

The server is fully **stateless** — it pulls everything from public sources (our hosted signed Oracle + Polymarket's public APIs). No local database, no setup, no API keys.

---

## Tools

| Tool | What it does |
|---|---|
| `get_top_wallets` | Top wallets ranked by realized PnL over the last 30 days |
| `get_top_markets` | Top active markets by volume |
| `get_wallet_history` | Recent trades of a specific wallet (live from Polymarket Data API) |
| `get_market_info` | Market details by slug (active or closed) |
| `get_smart_money_flow` | Live BUYs from top wallets in the last N minutes |
| `get_signed_snapshot` | Current ECDSA-signed Polymarket snapshot |

## Resources

- `polymarket://wallets/top` — top 25 wallets (JSON)
- `polymarket://markets/top` — top 25 markets (JSON)

---

## Install

### Claude Desktop

Edit `claude_desktop_config.json` (location varies by OS, see [Anthropic docs](https://modelcontextprotocol.io/quickstart/user)):

```json
{
  "mcpServers": {
    "polymarket": {
      "command": "python",
      "args": ["-m", "mcp_server.server"]
    }
  }
}
```

Restart Claude Desktop. The tools will appear in the tools list.

### Cursor

In your project's `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "polymarket": {
      "command": "python",
      "args": ["-m", "mcp_server.server"]
    }
  }
}
```

### Smithery (one-click)

Find this server in the [Smithery catalogue](https://smithery.ai/server/polymarket-mcp) and click Install.

---

## Run as HTTP server

For web agents or Smithery-hosted setups:

```bash
python -m mcp_server.server --http --port 8000
```

Endpoint: `http://localhost:8000/mcp` (streamable HTTP transport).

---

## Data sources

| Source | What we read | Auth |
|---|---|---|
| **Our Oracle endpoint** | Signed snapshot: top markets + top 100 wallets, refreshed every 60 seconds | None |
| **Polymarket Data API** (`/activity`) | Live wallet trade history | None |
| **Polymarket Gamma API** (`/markets`) | Market details by slug | None |

The Oracle snapshot is ECDSA-signed (secp256k1). The signer address is published in each response — verify it yourself if you need provenance guarantees.

---

## Example agent prompt (Claude / Cursor)

> "Find the top 5 Polymarket wallets by 30-day PnL, then for each pull their last 5 trades and tell me which markets they are accumulating in right now."

The agent will call `get_top_wallets`, then loop `get_wallet_history`, then summarise — entirely through the MCP tools, no extra integration needed.

---

## License

MIT. The data is public (Polymarket exposes it through their own APIs); we just package it for AI agents.

---

## Pricing tiers (planned, post-launch)

- **Free** — everything in this README, no rate limits beyond what Polymarket/Cloudflare enforce.
- **Pro** ($9/mo, planned) — own watchlists, webhook delivery, custom signal alerts.
- **Enterprise** ($49/mo, planned) — dedicated endpoint, historical bulk queries, SLA.

Currently everything is free while we onboard early users — open an issue if you want notification when paid tiers launch.
