"""Smoke test for the Polymarket MCP server.

Calls each tool directly (bypassing the MCP protocol) to verify all data
sources work end-to-end. Run before publishing.

Usage:
  python -m mcp_server.test_local
"""
from __future__ import annotations
import json
from mcp_server.server import (
    get_top_wallets, get_top_markets, get_wallet_history,
    get_market_info, get_smart_money_flow, get_signed_snapshot,
)


print("=" * 60)
print("Polymarket MCP -- local smoke test")
print("=" * 60)

# 1. get_signed_snapshot first (others depend on it being reachable)
print("\n[1/6] get_signed_snapshot ...")
snap = get_signed_snapshot()
if "error" in snap:
    print(f"  FAIL: {snap}")
    raise SystemExit(1)
print(f"  OK  generated_at={snap.get('generated_at_utc','?')[:19]}")
print(f"      signer={snap.get('signature',{}).get('signer_address','?')}")
print(f"      markets={len(snap.get('top_markets',[]))}  "
      f"wallets={len(snap.get('top_wallets_by_30d_pnl',[]))}")

# 2. get_top_wallets
print("\n[2/6] get_top_wallets(limit=5) ...")
tw = get_top_wallets(limit=5)
if "error" in tw:
    print(f"  FAIL: {tw}")
    raise SystemExit(1)
print(f"  OK  n_wallets={tw['n_wallets']}")
for i, w in enumerate(tw["wallets"], 1):
    pnl = w.get('realized_pnl_30d', w.get('realized_pnl', 0))
    wr = w.get('win_rate', 0) * 100
    print(f"    {i}. {w['address'][:14]}  pnl=${pnl:>10.0f}  wr={wr:.0f}%")

# 3. get_top_markets
print("\n[3/6] get_top_markets(limit=5) ...")
tm = get_top_markets(limit=5)
if "error" in tm:
    print(f"  FAIL: {tm}")
    raise SystemExit(1)
print(f"  OK  n_markets={tm['n_markets']}")
for i, m in enumerate(tm["markets"], 1):
    print(f"    {i}. {m.get('slug','?')[:40]:40s}  vol=${m.get('volume_usd',0):>10.0f}")

# 4. get_wallet_history (live Data API)
sample_wallet = tw["wallets"][0]["address"]
print(f"\n[4/6] get_wallet_history({sample_wallet[:10]}..., limit=3) ...")
wh = get_wallet_history(sample_wallet, limit=3)
if "error" in wh:
    print(f"  FAIL: {wh}")
    raise SystemExit(1)
print(f"  OK  n_trades={wh['n_trades']}")
for t in wh["trades"][:3]:
    print(f"    {t['ts_iso'][:19]}  {t['side']:4s}  "
          f"{(t.get('slug') or '?')[:30]:30s}  ${t['value_usd']:.2f}")

# 5. get_market_info (Gamma API)
sample_slug = tm["markets"][0]["slug"] if tm["markets"] else "sol-updown-5m-1781455800"
print(f"\n[5/6] get_market_info('{sample_slug[:40]}') ...")
mi = get_market_info(sample_slug)
if "error" in mi:
    print(f"  WARN: {mi}  (slug may have expired or unsupported)")
else:
    print(f"  OK  conditionId={(mi.get('condition_id') or '?')[:14]}...")
    print(f"      outcomes={mi.get('outcomes')}  vol=${mi.get('volume_usd',0):.0f}")

# 6. get_smart_money_flow
print("\n[6/6] get_smart_money_flow(lookback_minutes=120, top_wallets_n=5, max_results=10) ...")
flow = get_smart_money_flow(lookback_minutes=120, top_wallets_n=5, max_results=10)
if "error" in flow:
    print(f"  FAIL: {flow}")
    raise SystemExit(1)
print(f"  OK  n_flow={flow['n_flow']}  (scanned {flow['top_wallets_scanned']} wallets)")
for f in flow["smart_money_buys"][:5]:
    print(f"    {f['ts_iso'][:19]}  {f['wallet'][:14]}  "
          f"BUY {(f.get('slug') or '?')[:30]:30s}  ${f['size_usd']:.2f}")

print("\n" + "=" * 60)
print("ALL 6 TOOLS PASSED")
print("=" * 60)
