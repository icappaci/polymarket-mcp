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

# ─────────────── Endpoints ───────────────

# Our own hosted signed snapshot (built every minute by oracle/update_and_push.ps1)
ORACLE_SNAPSHOT_URL = "https://polymarket-oracle.istarley2000.workers.dev/snapshot.json"

# Polymarket public APIs (no auth required)
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

_UA = "polymarket-mcp/0.2"
_HTTP_TIMEOUT_S = 12.0

mcp = FastMCP("polymarket-mcp")


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
    """Top Polymarket wallets ranked by realized PnL (last 30 days).

    Uses the latest signed Oracle snapshot (refreshed every minute on our side).
    Useful for: smart-money tracking, copy-trade strategies, alpha-trader
    discovery, building wallet leaderboards.

    Args:
        limit: Max wallets to return (1-100, default 25). Snapshot already
               contains the top 100 — this just caps the result.
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
    """Top active Polymarket markets by volume (last 30 days).

    Reads from the same signed Oracle snapshot as get_top_wallets.

    Args:
        limit: Max markets to return (1-100, default 25).
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
    """Recent trade history for a specific Polymarket wallet (live).

    Pulls the latest TRADE events directly from Polymarket's public Data API.
    Use this for real-time tracking of a specific trader.

    Args:
        wallet: 0x-prefixed Polymarket wallet address (proxy address).
        limit:  Number of recent trades to return (1-200, default 50).
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
    """Detailed info about a Polymarket market by slug.

    Uses Polymarket's Gamma API. Works for both active and closed markets.

    Args:
        slug: Market slug (e.g. 'btc-updown-5m-1781000000', 'mlb-nyy-tor-2026-06-13').
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
    """Recent BUY trades from top-performing Polymarket wallets ('smart money flow').

    Combines the top-N wallets from our signed Oracle snapshot with live
    activity from each wallet via Polymarket Data API. Filters to BUYs in
    the last N minutes — surfaces markets where smart money is accumulating
    right now.

    Args:
        lookback_minutes: Look only at trades from the last N minutes (1-1440, default 60).
        top_wallets_n:    How many top wallets to query (1-50, default 20).
        max_results:      Cap on returned trades (1-200, default 50).
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
    """Current ECDSA-signed Polymarket data snapshot.

    Returns the full signed JSON (top markets + top wallets + signature block).
    Same source as our public Oracle endpoint — verify the signature against
    the published signer address.

    Useful for: on-chain references, immutable audit trails, decentralized
    verification of Polymarket data.
    """
    snap = _get_snapshot()
    if not snap:
        return {"error": "oracle_unavailable"}
    return snap


# ─────────────── Resources ───────────────

@mcp.resource("polymarket://wallets/top")
def resource_top_wallets() -> str:
    """Top 25 Polymarket wallets by 30-day PnL (JSON)."""
    return json.dumps(get_top_wallets(), indent=2)


@mcp.resource("polymarket://markets/top")
def resource_top_markets() -> str:
    """Top 25 active Polymarket markets by volume (JSON)."""
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
        logging.info("Starting HTTP/SSE transport on %s:%d", args.host, args.port)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        logging.info("Starting stdio transport")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
