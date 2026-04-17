#!/usr/bin/env python3
"""查询 Binance 账户状态（简化版，完整版见 skills/binance-trading/scripts/check_account.py）"""
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
sys.path.insert(0, script_path)

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore, StateNotFoundError

# 策略来源定义
STRATEGY_SOURCES = {
    "skill1_collect":          ("🔑", "趋势"),
    "crypto_oversold_short":   ("🌀", "超跌短"),
    "crypto_oversold_long":    ("🌀", "超跌长"),
    "crypto_reversal_short":   ("🔄", "反转短"),
    "crypto_reversal_long":    ("🔄", "反转长"),
    "crypto_overbought_short": ("📉", "做空短"),
    "crypto_overbought_long":  ("📉", "做空长"),
}


def build_source_map(store):
    source_map = {}
    for skill_name, (emoji, label) in STRATEGY_SOURCES.items():
        try:
            _, data = store.get_latest(skill_name)
            for c in data.get("candidates", []):
                sym = c.get("symbol", "")
                if sym and sym not in source_map:
                    source_map[sym] = (emoji, label)
        except (StateNotFoundError, Exception):
            pass
    return source_map


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

store = StateStore(db_path=os.path.join(script_path, "data", "state_store.db"))
source_map = build_source_map(store)

# 查询账户信息
print("\n📊 账户信息")
print("=" * 60)
account = client.get_account_info()
total_balance = float(account.total_balance)
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
        margin = float(raw.get('isolatedMargin', 0))
        notional = abs(float(raw.get('notional', 0)))
        margin_pct = margin / total_balance * 100 if total_balance > 0 else 0

        # 策略来源标签
        if pos.symbol in source_map:
            emoji, label = source_map[pos.symbol]
            tag = f" {emoji}{label}"
        else:
            tag = ""

        print(
            f"{pos.symbol}{tag} | 方向:{side} | 数量:{pos.position_amt} | "
            f"名义:{notional:,.2f} | 保证金:{margin:.2f}({margin_pct:.1f}%) | "
            f"盈亏:{pos.unrealized_pnl:+.4f}"
        )

store.close()
print("\n✅ 查询完成")
