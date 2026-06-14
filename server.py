"""Polymarket MCP Server — Polymarket data for AI agents (Claude Desktop,
Cursor, Cline, Manus, and any MCP-compatible client).

This server is fully stateless: all data is fetched from public sources
(our hosted Oracle endpoint + Polymarket's public APIs). No local DuckDB
or filesystem state required — Smithery can host it directly and any
user can install it without setup.

Tools:
  - get_top_wallets       — top wallets ranked by realized PnL
  - get_top_markets       — top active markets by volume
  - get_wallet_history    — recent trades of a specific wallet
  - get_market_info       — market details by slug
  - get_smart_money_flow  — recent BUYs from top wallets
  - get_signed_snapshot   — current ECDSA-signed Polymarket snapshot

Run (stdio — Claude Desktop / Cursor):
  python -m mcp_server.server

Run (HTTP/SSE — Smithery hosted):
  python -m mcp_server.server --http --port 8000
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

# ─────────────── Endpoints ───────────────

# Our own hosted signed snapshot (built every minute by oracle/update_and_push.ps1)
ORACLE_SNAPSHOT_URL = "https://polymarket-oracle.istarley2000.workers.dev/snapshot.json"

# Polymarket public APIs (no auth required)
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

_UA = "polymarket-mcp/0.2"
_HTTP_TIMEOUT_S = 12.0

# Disable MCP's built-in DNS rebinding protection — when running behind a
# PaaS proxy (Render / Fly / Smithery gateway) the Host header is dynamic
# and would otherwise be rejected as "Invalid Host header" with HTTP 421.
# The PaaS edge already validates routing, so we don't need this layer.
_transport_sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("polymarket-mcp", transport_security=_transport_sec)


# ─────────────── HTTP helpers ───────────────

def _http_json(url: str, timeout: float = _HTTP_TIMEOUT_S) -> Any:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# In-memory cache for the signed snapshot (fetched at most every 30s)
_snapshot_cache: dict = {"data": None, "ts": 0.0}
SNAPSHOT_CACHE_TTL_S = 30


def _get_snapshot(force_refresh: bool = False) -> dict | None:
    """Return latest signed snapshot, cached 30s in-memory."""
    now = time.time()
    if not force_refresh and _snapshot_cache["data"] and \
            (now - _snapshot_cache["ts"]) < SNAPSHOT_CACHE_TTL_S:
        return _snapshot_cache["data"]
    try:
        data = _http_json(ORACLE_SNAPSHOT_URL)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logging.warning("[mcp] snapshot fetch failed: %s", e)
        return _snapshot_cache["data"]   # serve stale if available
    _snapshot_cache["data"] = data
    _snapshot_cache["ts"] = now
    return data


# ─────────────── Tools ───────────────

@mcp.tool()
def get_top_wallets(limit: int = 25) -> dict:
    """Get the top-performing Polymarket wallets ranked by realized profit
    over the last 30 days.

    USE THIS WHEN the user asks any of:
      - "Who are the top traders on Polymarket?"
      - "Show me the most profitable wallets"
      - "Which addresses have been winning lately?"
      - "Find me alpha traders / smart money"
      - "Build a leaderboard of Polymarket whales"

    Each wallet entry includes: 0x address, 30-day realized PnL in USD,
    number of closed trades, wins, win rate. Wallets are pre-filtered to
    only include those with >=5 closed trades AND positive PnL (filters
    out lucky one-shotters and pure losers).

    Data source: hosted signed Oracle snapshot, ECDSA-signed by us
    (secp256k1, same curve as Ethereum). The snapshot.signer_address is
    verifiable on-chain — you can prove the data wasn't tampered with.
    Snapshot refreshes every 60 seconds; this tool serves an in-memory
    cache (30s TTL) so back-to-back calls are free.

    COMMONLY COMBINED WITH:
      - get_wallet_history(address) — drill into what a top wallet just did
      - get_smart_money_flow() — see what top wallets are buying RIGHT NOW

    Args:
        limit: How many wallets to return, ranked best-to-worst.
               Range 1-100, default 25. The underlying snapshot already
               contains the top 100 — this just truncates.

    Returns: {
      "lookback_days": 30,
      "snapshot_generated_at": ISO timestamp of last data refresh,
      "signer_address": 0x... (signer of the underlying snapshot),
      "n_wallets": int (== min(limit, 100)),
      "wallets": [
        {
          "address": "0x...",
          "realized_pnl_30d": float (USD),
          "win_rate": float (0..1),
          "n_trades_30d": int,
          "wins_30d": int
        }, ...
      ]
    }
    """
    limit = max(1, min(int(limit), 100))
    snap = _get_snapshot()
    if not snap:
        return {"error": "oracle_unavailable",
                "hint": "The signed Polymarket Oracle endpoint is not reachable. "
                        "Retry in a few seconds."}
    wallets = snap.get("top_wallets_by_30d_pnl") or snap.get("wallets") or []
    return {
        "lookback_days": snap.get("lookback_days", 30),
        "snapshot_generated_at": snap.get("generated_at_utc"),
        "signer_address": snap.get("signature", {}).get("signer_address"),
        "n_wallets": min(len(wallets), limit),
        "wallets": wallets[:limit],
    }


@mcp.tool()
def get_top_markets(limit: int = 25) -> dict:
    """Get the highest-volume active Polymarket markets right now.

    USE THIS WHEN the user asks any of:
      - "What's hot on Polymarket?"
      - "Show me the biggest prediction markets"
      - "Which markets have the most trading volume?"
      - "What are people betting on?"
      - "Find markets I should pay attention to"

    Returns active (not yet resolved) markets sorted by total trading
    volume. Each entry includes slug (use it as input to get_market_info
    or get_wallet_history filters), title, USD volume, liquidity, end
    date, and current outcome prices.

    Markets span every category Polymarket lists: politics, crypto,
    sports, entertainment, geopolitics, weather, science. If the user
    asks for a specific category, you'll need to filter the response
    yourself by checking the slug prefix or title.

    Data source: same signed Oracle snapshot as get_top_wallets
    (refreshed every 60s, cached 30s in-memory). The first call may take
    1-2 seconds; subsequent calls return instantly from cache.

    COMMONLY COMBINED WITH:
      - get_market_info(slug) — drill into one market's full detail
      - get_smart_money_flow() — see if any top wallets are positioned
        in these markets

    Args:
        limit: How many markets to return, ranked by volume desc.
               Range 1-100, default 25. The snapshot already holds the
               top 50 — values >50 silently saturate at 50.

    Returns: {
      "snapshot_generated_at": ISO timestamp,
      "n_markets": int,
      "markets": [
        {
          "slug": "btc-updown-5m-...",
          "condition_id": "0x...",
          "title": "Will Brazil win on 2026-06-13?",
          "end_date_utc": ISO timestamp,
          "volume_usd": float,
          "liquidity_usd": float,
          "outcomes": [{"name": str, "price": float}, ...]
        }, ...
      ]
    }
    """
    limit = max(1, min(int(limit), 100))
    snap = _get_snapshot()
    if not snap:
        return {"error": "oracle_unavailable"}
    markets = snap.get("top_markets") or snap.get("markets") or []
    return {
        "snapshot_generated_at": snap.get("generated_at_utc"),
        "n_markets": min(len(markets), limit),
        "markets": markets[:limit],
    }


@mcp.tool()
def get_wallet_history(wallet: str, limit: int = 50) -> dict:
    """Get the recent trade history for a specific Polymarket wallet — LIVE,
    not cached. Reads directly from Polymarket's Data API.

    USE THIS WHEN the user:
      - Asks "what did wallet 0x... do?" or pastes a wallet address
      - Wants to track a specific trader's moves
      - Got a wallet from get_top_wallets() and wants to see their actual
        trades
      - Needs the latest activity (within seconds) for a wallet — this
        tool is live, not cached

    Each trade returns: timestamp (unix + ISO), market slug, market title,
    outcome the wallet picked, side (BUY/SELL), size in shares, price per
    share, total USD value, on-chain condition_id, and outcome_index.

    NOTE: Polymarket users have TWO addresses — their "EOA" (the wallet
    they signed in with) and their "proxy wallet" (the address that
    actually holds positions and trades). This tool needs the PROXY
    wallet (42-char 0x address). get_top_wallets returns proxy addresses
    directly, so you can pass those straight in.

    Returns the most recent trades, newest first. Most wallets have
    hundreds-to-thousands of trades total, so limit acts as a recency
    window (limit=50 gives you "last 50 trades" not "all trades").

    COMMONLY COMBINED WITH:
      - get_top_wallets() — first find a top wallet, then call this on
        their address
      - get_market_info(slug) — drill into the market a wallet just
        entered

    Args:
        wallet: 0x-prefixed 42-character Polymarket proxy wallet address.
                Must be exactly 42 chars including the 0x prefix.
        limit:  How many recent trades, newest first. Range 1-200,
                default 50.

    Returns: {
      "wallet": "0x...",
      "n_trades": int,
      "trades": [
        {
          "ts_unix": int, "ts_iso": ISO timestamp,
          "slug": "market-slug", "title": "Market question?",
          "outcome": "Yes" | "No" | "Up" | team name | etc.,
          "side": "BUY" | "SELL",
          "size_shares": float, "price": float (0..1), "value_usd": float,
          "condition_id": "0x...", "outcome_index": 0 | 1
        }, ...
      ]
    }
    Returns {"error": ..., "hint": ...} on invalid wallet format or API failure.
    """
    if not wallet or not wallet.startswith("0x") or len(wallet) != 42:
        return {"error": "invalid_wallet",
                "hint": "wallet must be a 0x-prefixed 42-char address"}
    limit = max(1, min(int(limit), 200))
    qs = urllib.parse.urlencode({
        "user": wallet, "limit": limit, "offset": 0,
        "type": "TRADE", "sortBy": "TIMESTAMP",
    })
    try:
        acts = _http_json(f"{DATA_API}/activity?{qs}")
    except Exception as e:
        return {"error": f"data_api_fetch_failed: {e}", "wallet": wallet}
    trades = []
    for a in (acts if isinstance(acts, list) else []):
        ts = int(a.get("timestamp", 0))
        size = float(a.get("size", 0))
        price = float(a.get("price", 0))
        trades.append({
            "ts_unix": ts,
            "ts_iso": dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(),
            "slug": a.get("slug"),
            "title": (a.get("title") or "")[:200],
            "outcome": a.get("outcome"),
            "side": a.get("side"),
            "size_shares": size,
            "price": price,
            "value_usd": round(size * price, 2),
            "condition_id": a.get("conditionId"),
            "outcome_index": a.get("outcomeIndex"),
        })
    return {"wallet": wallet, "n_trades": len(trades), "trades": trades}


@mcp.tool()
def get_market_info(slug: str) -> dict:
    """Get full details about a single Polymarket market by its slug —
    works for both active AND closed/resolved markets.

    USE THIS WHEN the user:
      - Mentions a specific market by slug (e.g. 'btc-updown-5m-...',
        'mlb-nyy-tor-2026-06-13', 'will-trump-win-2028')
      - Asks "what's the question on market X?"
      - Asks "did market X resolve? what was the outcome?"
      - Pastes a Polymarket URL — extract the slug from the URL path
      - Wants outcomes, prices, end date, resolution source for one market

    Slugs come from many sources:
      - get_top_markets() / get_wallet_history() responses
      - URL paths on polymarket.com (the part after /event/)
      - Polymarket activity feeds

    Returns full market metadata: human title, description, end/start
    dates, current closed/active status, total volume + liquidity in USD,
    list of outcomes with their CLOB token IDs (needed for orderbook
    queries), and the resolution source URL (e.g. ESPN, MLB.com, an X
    post — explains who/what decides the outcome).

    Data source: Polymarket's Gamma API. Live, not cached. ~200ms latency.

    COMMONLY COMBINED WITH:
      - get_wallet_history — see who traded this market
      - get_smart_money_flow — check if top wallets touched this market
        in recent activity (filter the response's slug field)

    Args:
        slug: The market's URL slug. Examples:
              'mlb-nyy-tor-2026-06-13', 'btc-updown-5m-1781000000',
              'will-trump-be-2028-republican-nominee'.
              No prefix, no URL — just the slug itself.

    Returns: {
      "slug": str, "condition_id": "0x...",
      "title": "Will X happen?", "description": "Long-form rules text",
      "end_date_utc": ISO, "start_date_utc": ISO,
      "closed": bool, "active": bool,
      "volume_usd": float, "liquidity_usd": float,
      "outcomes": ["Yes", "No"] | ["Brazil", "Morocco", "Draw"] | etc.,
      "clob_token_ids": ["123...", "456..."] (one per outcome, same order),
      "resolution_source": URL or text describing what decides the outcome
    }
    Returns {"error": "not_found", ...} if the slug doesn't exist.
    """
    if not slug:
        return {"error": "missing_slug"}
    # Try active first, then closed
    for closed_flag in ("false", "true"):
        qs = urllib.parse.urlencode({"slug": slug, "closed": closed_flag, "limit": 1})
        try:
            data = _http_json(f"{GAMMA_API}/markets?{qs}")
        except Exception as e:
            return {"error": f"gamma_fetch_failed: {e}", "slug": slug}
        if isinstance(data, list) and data:
            m = data[0]
            # Parse outcomes / token_ids (often returned as JSON-encoded strings)
            try:
                outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
            except json.JSONDecodeError:
                outcomes = []
            try:
                token_ids = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
            except json.JSONDecodeError:
                token_ids = []
            return {
                "slug": m.get("slug"),
                "condition_id": m.get("conditionId"),
                "title": m.get("question"),
                "description": (m.get("description") or "")[:500],
                "end_date_utc": m.get("endDate"),
                "start_date_utc": m.get("startDate"),
                "closed": bool(m.get("closed")),
                "active": bool(m.get("active")),
                "volume_usd": float(m.get("volume", 0) or 0),
                "liquidity_usd": float(m.get("liquidity", 0) or 0),
                "outcomes": outcomes,
                "clob_token_ids": token_ids,
                "resolution_source": m.get("resolutionSource"),
            }
    return {"error": "not_found", "slug": slug}


@mcp.tool()
def get_smart_money_flow(
    lookback_minutes: int = 60,
    top_wallets_n: int = 20,
    max_results: int = 50,
) -> dict:
    """Find what TOP PERFORMERS are buying RIGHT NOW on Polymarket.
    This is the headline composite tool of this server.

    USE THIS WHEN the user asks any of:
      - "What's smart money doing right now?"
      - "Are whales accumulating anywhere?"
      - "Where is alpha flowing on Polymarket today?"
      - "Show me where the best traders just entered positions"
      - "Any unusual concentration of top wallets in one market?"

    HOW IT WORKS (internally): pulls the top-N wallets ranked by 30-day
    realized PnL (same source as get_top_wallets), then queries each
    wallet's recent activity via Polymarket's Data API, filters to BUYs
    inside the lookback window, and returns the combined feed sorted
    newest-first. One call → one combined view across many wallets.

    This is the kind of signal that's hard to assemble from raw APIs
    (would normally require: top wallets query + N parallel activity
    queries + filter + sort + merge). The tool wraps all that into a
    single LLM-callable.

    Each entry includes WHO bought (wallet address), THEIR track record
    (30d PnL, win rate — for context on credibility), WHAT they bought
    (slug, title, outcome), at WHAT price, for HOW MUCH USD, and WHEN.

    Concentration analysis: if you see 3+ wallets buying the same slug
    within minutes, that's a high-confidence signal. The LLM should
    surface this pattern in its summary when present.

    INTERPRETATION CAVEATS:
      - Polymarket Data API typically lags 5-30 seconds behind on-chain
        truth. "Real-time" here means ~30s freshness, not millisecond.
      - A single top wallet entering doesn't mean others will follow.
        Multi-wallet concentration is the stronger signal.
      - Smart money can be wrong — track record is base rate, not certainty.

    Args:
        lookback_minutes: Time window for "recent" — only trades within
                          the last N minutes are returned. Range 1-1440
                          (1 min to 24 h), default 60. Use 5-15 for
                          "right now", 60-180 for "this morning", 1440
                          for "today".
        top_wallets_n:    How many top-PnL wallets to scan. Range 1-50,
                          default 20. Higher = broader signal but slower
                          (one HTTP call per wallet to Data API).
        max_results:      Cap the returned trade list. Range 1-200,
                          default 50. Wallets often have many trades in
                          a short window — without a cap one whale's
                          activity could dominate the response.

    Returns: {
      "lookback_minutes": int,
      "top_wallets_scanned": int,
      "n_flow": int (== len(smart_money_buys)),
      "smart_money_buys": [
        {
          "wallet": "0x...",
          "wallet_pnl_30d": float (USD — credibility signal),
          "wallet_win_rate": float (0..1),
          "ts_unix": int, "ts_iso": ISO,
          "slug": "market-slug", "title": "Question?",
          "outcome": "Yes" | team-name | etc.,
          "price": float (0..1 — entry price on Polymarket),
          "size_usd": float,
          "condition_id": "0x..."
        }, ...
      ]  (sorted newest-first)
    }
    """
    lookback_minutes = max(1, min(int(lookback_minutes), 1440))
    top_wallets_n = max(1, min(int(top_wallets_n), 50))
    max_results = max(1, min(int(max_results), 200))

    top = get_top_wallets(limit=top_wallets_n)
    if "error" in top:
        return top
    cutoff_ts = int((dt.datetime.now(dt.timezone.utc) -
                     dt.timedelta(minutes=lookback_minutes)).timestamp())
    flow = []
    for w in top["wallets"]:
        addr = w.get("address")
        if not addr:
            continue
        qs = urllib.parse.urlencode({
            "user": addr, "limit": 30, "offset": 0,
            "type": "TRADE", "sortBy": "TIMESTAMP",
        })
        try:
            acts = _http_json(f"{DATA_API}/activity?{qs}")
        except Exception:
            continue
        for a in (acts if isinstance(acts, list) else []):
            ts = int(a.get("timestamp", 0))
            if ts < cutoff_ts:
                continue
            if (a.get("side") or "").upper() != "BUY":
                continue
            size = float(a.get("size", 0))
            price = float(a.get("price", 0))
            flow.append({
                "wallet": addr,
                "wallet_pnl_30d": w.get("realized_pnl_30d") or w.get("realized_pnl"),
                "wallet_win_rate": w.get("win_rate"),
                "ts_unix": ts,
                "ts_iso": dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat(),
                "slug": a.get("slug"),
                "title": (a.get("title") or "")[:160],
                "outcome": a.get("outcome"),
                "price": price,
                "size_usd": round(size * price, 2),
                "condition_id": a.get("conditionId"),
            })
    flow.sort(key=lambda x: -x["ts_unix"])
    return {
        "lookback_minutes": lookback_minutes,
        "top_wallets_scanned": len(top["wallets"]),
        "n_flow": len(flow[:max_results]),
        "smart_money_buys": flow[:max_results],
    }


@mcp.tool()
def get_signed_snapshot() -> dict:
    """Get the current ECDSA-signed Polymarket data snapshot — the full
    JSON document with cryptographic signature attached.

    USE THIS WHEN the user:
      - Needs verifiable / tamper-proof Polymarket data (audit trails,
        compliance, on-chain integrations)
      - Asks for "raw snapshot" or "the full data dump"
      - Wants to verify provenance — "prove this came from Polymarket
        and wasn't modified"
      - Is building a smart contract / DeFi protocol that needs an
        oracle feed from prediction markets

    HOW SIGNING WORKS: each snapshot is signed with secp256k1 (the same
    elliptic curve Ethereum uses). The signature covers a SHA-256 hash
    of the canonical JSON (sorted keys, no whitespace). Consumers
    re-compute the hash, ecrecover the signer address from the signature,
    and check it matches the published signer (0x1C2Dd3AFA33cF338332C47024BdB747d3240551C).
    If both match → data integrity proven.

    For most LLM agent use cases (research, "show me X") you do NOT
    need this tool — use get_top_wallets / get_top_markets / etc.
    instead, which extract the relevant fields from this same snapshot
    in a cleaner format. This tool is for the EDGE CASE where you need
    the full signed envelope.

    Verification reference implementation:
    https://github.com/icappaci/polymarket-mcp/blob/main/verify_snapshot.py
    (Python, ~30 lines, uses eth-account library)

    Returns: {
      "schema_version": str,
      "generated_at_utc": ISO timestamp,
      "generated_at_unix": int,
      "lookback_days": 30,
      "top_markets": [ ... 50 entries ... ],
      "top_wallets_by_30d_pnl": [ ... 100 entries ... ],
      "signature": {
        "signer_address": "0x1C2D...551C",
        "algorithm": "ECDSA-secp256k1 over SHA256(canonical_json)",
        "digest_sha256": "hex string",
        "signature_hex": "hex string (65 bytes)",
        "recoverable": true
      }
    }
    Size: ~37 KB. Refresh interval: 60 seconds on origin, 30 seconds
    cached here in the MCP server.
    """
    snap = _get_snapshot()
    if not snap:
        return {"error": "oracle_unavailable"}
    return snap


# ─────────────── Resources ───────────────

@mcp.resource("polymarket://wallets/top")
def resource_top_wallets() -> str:
    """Static reference resource: the top 25 Polymarket wallets by 30-day
    realized PnL, as a JSON document.

    Use this resource when you want a stable read-only reference (e.g. for
    grounding context at the start of an agent session). For dynamic
    queries with arguments (custom limit, etc.) use the get_top_wallets
    tool instead.
    """
    return json.dumps(get_top_wallets(), indent=2)


@mcp.resource("polymarket://markets/top")
def resource_top_markets() -> str:
    """Static reference resource: the top 25 active Polymarket markets by
    trading volume, as a JSON document.

    Use this resource for stable context grounding. For dynamic queries
    (custom limit, filters) use the get_top_markets tool instead.
    """
    return json.dumps(get_top_markets(), indent=2)


# ─────────────── Entrypoint ───────────────

def main():
    import argparse
    import os
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true",
                   help="Run HTTP/SSE transport (for Smithery / Render / web). "
                        "Default: stdio (for Claude Desktop / Cursor).")
    # Render and most PaaS providers set $PORT. Fall back to 8000 for local.
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                   help="Bind host. 0.0.0.0 for hosted/Render, 127.0.0.1 for local-only.")
    args = p.parse_args()

    # Render auto-enables HTTP mode when PORT env is present
    is_hosted = "PORT" in os.environ
    use_http = args.http or is_hosted

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    if use_http:
        # MCP's DNS rebinding protection is disabled above (TransportSecuritySettings).
        # Streamable HTTP is the modern MCP transport (replaces SSE) — required
        # for proper Smithery hosted gateway integration. Endpoint: POST /mcp
        import uvicorn
        logging.info("Starting Streamable HTTP transport on %s:%d (POST /mcp)",
                     args.host, args.port)
        app = mcp.streamable_http_app()
        uvicorn.run(app, host=args.host, port=args.port,
                    log_level="info", access_log=True,
                    forwarded_allow_ips="*")
    else:
        logging.info("Starting stdio transport")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
