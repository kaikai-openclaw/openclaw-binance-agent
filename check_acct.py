#!/usr/bin/env python3
"""查询 Binance 账户状态"""
import os
import sys

# 手动读取 .env 文件
script_path = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(script_path, '.env')
if os.path.exists(env_path):
    with open(env_path) as ef:
        for line in ef:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

# 添加 src 到路径
sys.path.insert(0, os.path.join(script_path, 'src'))

from infra.binance_fapi import BinanceFapiClient
from infra.rate_limiter import RateLimiter

# 初始化客户端
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

if not api_key or not api_secret:
    print("❌ 错误: 未找到 BINANCE_API_KEY 或 BINANCE_API_SECRET")
    sys.exit(1)

client = BinanceFapiClient(
    api_key=api_key,
    api_secret=api_secret,
    rate_limiter=RateLimiter()
)

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
        raw = pos.raw
        side = raw.get('positionSide', 'UNKNOWN')
        print(f"{pos.symbol} | 方向:{side} | 数量:{pos.position_amt} | 盈亏:{pos.unrealized_pnl:+.4f} | 保证金:{raw.get('isolatedMargin', 0)}")

print("\n✅ 查询完成")