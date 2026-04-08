#!/usr/bin/env python3
"""查询 Binance 账户状态"""
import os
import sys

# 手动读取 .env 文件
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

# 添加 src 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter

# 初始化客户端
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

if not api_key or not api_secret:
    print("❌ 错误: 未找到 BINANCE_API_KEY 或 BINANCE_API_SECRET")
    sys.exit(1)

print("📡 正在连接 Binance API...")

client = BinanceFapiClient(
    api_key=api_key,
    api_secret=api_secret,
    rate_limiter=RateLimiter()
)

try:
    # 查询账户信息
    print("\n📊 账户信息")
    print("=" * 60)
    account = client.get_account_info()
    print(f"总资金: {account.total_balance:,.2f} USDT")
    print(f"可用资金: {account.available_balance:,.2f} USDT")
    print(f"未实现盈亏: {account.total_unrealized_pnl:+,.2f} USDT")

    # 查询持仓
    print("\n📍 当前持仓")
    print("=" * 60)
    positions = client.get_positions()

    if not positions:
        print("无持仓")
    else:
        for pos in positions:
            side = "做多" if pos.position_amt > 0 else "做空"
            pnl_pct = (pos.unrealized_pnl / (abs(pos.position_amt) * pos.entry_price)) * 100 if pos.entry_price > 0 else 0
            print(f"{pos.symbol:15s} {side:4s} {abs(pos.position_amt):.4f} | "
                  f"入场价: {pos.entry_price:,.2f} | "
                  f"盈亏: {pos.unrealized_pnl:+,.2f} USDT ({pnl_pct:+.2f}%) | "
                  f"杠杆: {pos.leverage}x")

    # 查询挂单
    print("\n📋 挂单状态")
    print("=" * 60)
    open_orders = client.get_open_orders()
    algo_orders = client.get_open_algo_orders()

    if not open_orders and not algo_orders:
        print("无挂单")
    else:
        if open_orders:
            print(f"普通订单: {len(open_orders)} 笔")
            for order in open_orders:
                print(f"  {order['symbol']} {order['side']} {order['type']} "
                      f"{order.get('origQty', 0)} @ {order.get('price', 'MARKET')} "
                      f"[{order['status']}]")

        if algo_orders:
            print(f"\n条件单: {len(algo_orders)} 笔")
            for order in algo_orders:
                print(f"  {order['symbol']} {order['side']} {order['type']} "
                      f"触发价: {order.get('triggerPrice', 'N/A')} "
                      f"[{order['algoStatus']}]")

    # 汇总
    print("\n📈 账户汇总")
    print("=" * 60)
    total_value = account.total_balance + account.total_unrealized_pnl
    print(f"总权益: {total_value:,.2f} USDT")
    if account.total_balance > 0:
        daily_pnl = account.total_unrealized_pnl
        daily_pnl_pct = (daily_pnl / account.total_balance) * 100
        print(f"当日盈亏: {daily_pnl:+,.2f} USDT ({daily_pnl_pct:+.2f}%)")

    print("\n✅ 查询完成")

except Exception as e:
    print(f"\n❌ 查询失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
