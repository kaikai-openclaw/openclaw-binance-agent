#!/usr/bin/env python3
"""
执行交易计划：
1. 清仓 STABLEUSDT（高风险）
2. 在 TREEUSDT 限价做多
3. 设置止损止盈条件单
"""
import os, sys
os.chdir("/Users/zengkai/MyProjects/MyTradingAgents/openclaw-binance-agent")

env_path = os.path.join(os.getcwd(), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k] = v

sys.path.insert(0, '.')
from src.infra.binance_fapi import BinanceFapiClient
import requests

client = BinanceFapiClient(
    api_key=os.environ.get("BINANCE_API_KEY"),
    api_secret=os.environ.get("BINANCE_API_SECRET")
)

total_balance = 160.38
available = 151.97

print("=" * 60)
print("📋 交易计划执行")
print("=" * 60)

# ── Step 1: 清仓 STABLEUSDT ────────────────────────────────
print("\n🛑 Step 1: 清仓 STABLEUSDT（止损）")
print(f"  当前: 1002枚 | 均价 0.02856 | 浮亏 -3.31 USDT")
print(f"  理由: 24h成交量仅 ~$200K，流动性极差；已亏损 -11.75%")

try:
    # 市价全平
    result = client.place_market_order("STABLEUSDT", "SELL", 1002.0)
    print(f"  ✅ 市价平仓完成 | 订单ID: {result.order_id} | 状态: {result.status}")
except Exception as e:
    print(f"  ❌ 平仓失败: {e}")
    print("  尝试用 closePosition 模式...")
    try:
        result = client.place_stop_market_order(
            "STABLEUSDT", "SELL", 0, float('inf'), close_position=True
        )
        print(f"  ✅ 强平完成: {result}")
    except Exception as e2:
        print(f"  ❌ 强平也失败: {e2}")

# 重新获取余额
import time; time.sleep(1)
account = client.get_account_info()
available_after = account.available_balance
print(f"\n  释放后可用资金: {available_after:.2f} USDT")

# ── Step 2: TREEUSDT 限价做多 ──────────────────────────────
print("\n📈 Step 2: TREEUSDT 限价做多")

# 当前价格
r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price?symbol=TREEUSDT", timeout=5)
current_price = float(r.json()['price'])
print(f"  当前价: ${current_price:.6f}")

# 入场区间: $0.061 ~ $0.0635
entry_price = 0.0620  # 略低于现价，更容易成交
risk_amount = min(available_after * 0.20, 32.0)  # 不超过20%仓位
leverage = 10

# 计算数量
qty = (risk_amount * leverage) / entry_price
# 调整精度
qty_rounded = round(qty, 0)  # 取整
print(f"  限价: ${entry_price:.4f}")
print(f"  仓位: {qty_rounded} TREEUSDT (~{qty_rounded * entry_price:.2f} USDT保证金)")
print(f"  杠杆: {leverage}x")

# 设置止损止盈价格
stop_loss = 0.0560   # -9.7%
take_profit = 0.0810  # +30.6%
print(f"  止损: ${stop_loss} (跌破后自动市价平仓)")
print(f"  止盈: ${take_profit} (触及后自动市价平仓)")

# 限价开多
try:
    limit_result = client.place_limit_order("TREEUSDT", "BUY", entry_price, qty_rounded)
    print(f"\n  ✅ 限价买单已挂 | ID: {limit_result.order_id} | 状态: {limit_result.status}")
except Exception as e:
    print(f"\n  ❌ 限价单失败: {e}")
    sys.exit(1)

time.sleep(0.5)

# 设置止损
try:
    sl_result = client.place_stop_market_order(
        "TREEUSDT", "SELL", qty_rounded, stop_loss
    )
    print(f"  ✅ 止损单已挂 | ID: {sl_result.order_id} | 触发价: ${stop_loss}")
except Exception as e:
    print(f"  ⚠️ 止损单失败: {e}")

# 设置止盈
try:
    tp_result = client.place_take_profit_market_order(
        "TREEUSDT", "SELL", qty_rounded, take_profit
    )
    print(f"  ✅ 止盈单已挂 | ID: {tp_result.order_id} | 触发价: ${take_profit}")
except Exception as e:
    print(f"  ⚠️ 止盈单失败: {e}")

print("\n" + "=" * 60)
print("📊 执行后账户状态")
account2 = client.get_account_info()
print(f"  总资金: {account2.total_balance:.2f} USDT")
print(f"  可用: {account2.available_balance:.2f} USDT")
positions2 = client.get_positions()
print(f"  持仓数: {len(positions2)}")
for p in positions2:
    print(f"    {p.symbol} | {p.position_amt} | 均价 {p.entry_price:.6f} | 浮亏 {p.unrealized_pnl:.2f}")
print("=" * 60)
print("✅ 交易计划执行完毕")
