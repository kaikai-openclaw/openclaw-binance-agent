#!/usr/bin/env python3
"""制定 ALGOUSDT 交易策略 (Skill-3)"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)

# 设置 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.infra.state_store import StateStore
from src.infra.binance_fapi import BinanceFapiClient
from src.infra.binance_public import BinancePublicClient
from src.infra.exchange_rules import LazyBinanceTradingRuleProvider
from src.infra.rate_limiter import RateLimiter
from src.infra.risk_controller import RiskController
from src.models.types import AccountState
from src.skills.skill3_strategy import Skill3Strategy

print("=" * 60)
print("制定 ALGOUSDT 交易策略 (Skill-3)")
print("=" * 60)

# 1. 初始化
store = StateStore(db_path="data/state_store.db")
with open("config/schemas/skill3_input.json") as f:
    in_schema = json.load(f)
with open("config/schemas/skill3_output.json") as f:
    out_schema = json.load(f)

# 加载 .env
if os.path.exists('.env'):
    with open('.env') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

# 初始化 Binance 客户端
fapi_client = BinanceFapiClient(
    api_key=os.getenv('BINANCE_API_KEY'),
    api_secret=os.getenv('BINANCE_API_SECRET'),
    rate_limiter=RateLimiter()
)

public_client = BinancePublicClient(rate_limiter=RateLimiter())
trading_rule_provider = LazyBinanceTradingRuleProvider(public_client)

# 获取账户状态
account_info = fapi_client.get_account_info()

def get_account_state():
    """账户状态提供者"""
    return AccountState(
        total_balance=account_info.total_balance,
        available_margin=account_info.available_balance,
        daily_realized_pnl=0.0,
    )

def get_market_price(symbol: str) -> Optional[float]:
    """市场价格提供者 - 从24小时行情获取"""
    try:
        tickers = public_client.get_tickers_24hr()
        for t in tickers:
            if t.get('symbol') == symbol:
                return float(t.get('lastPrice', 0))
    except Exception as e:
        print(f"获取 {symbol} 价格失败: {e}")
    return None

# 初始化风控
risk_controller = RiskController()

# 初始化 Skill3
skill3 = Skill3Strategy(
    state_store=store,
    input_schema=in_schema,
    output_schema=out_schema,
    risk_controller=risk_controller,
    account_state_provider=get_account_state,
    market_price_provider=get_market_price,
    trading_rule_provider=trading_rule_provider,
    risk_ratio=0.02,  # 2% 风险
    leverage=10,       # 10x 杠杆
)

# 2. 伪造 Skill-2 输出（ALGOUSDT 通过评级）
now = datetime.now(timezone.utc).isoformat()
s2_out_id = store.save(
    "skill2_analyze",
    {
        "pipeline_run_id": "algo-strategy",
        "ratings": [
            {
                "symbol": "ALGOUSDT",
                "rating_score": 7,
                "signal": "long",
                "confidence": 70.0,
                "comment": "做多"
            }
        ]
    },
)

s3_in_id = store.save("trigger_skill3", {"input_state_id": s2_out_id})

print(f"\n[data] Skill-2 输出 state_id: {s2_out_id}")
print(f"[data] Skill-3 输入 state_id: {s3_in_id}")
print("\n[run] 正在制定交易策略...")
print("-" * 60)

try:
    out_id = skill3.execute(s3_in_id)
    result = store.load(out_id)

    print("-" * 60)
    print(f"\n✅ 策略制定完成!")

    # 输出策略详情
    strategies = result.get("strategies", [])
    
    print(f"\n📋 交易策略:")
    print("=" * 60)
    
    if strategies:
        for strat in strategies:
            symbol = strat.get("symbol")
            signal = strat.get("signal")
            print(f"\n🎯 {symbol} - {signal.upper()}")
            print("-" * 40)
            print(f"入场价格: {strat.get('entry_price', 'N/A')} USDT")
            print(f"止损价格: {strat.get('stop_loss_price', 'N/A')} USDT")
            print(f"止盈价格: {strat.get('take_profit_price', 'N/A')} USDT")
            print(f"头寸规模: {strat.get('position_size', 'N/A')} USDT")
            print(f"风险比例: {strat.get('risk_ratio', 'N/A')}%")
            print(f"盈亏比: {strat.get('risk_reward_ratio', 'N/A')}")
            print(f"止损比例: {strat.get('stop_loss_pct', 'N/A')}%")
            print(f"止盈比例: {strat.get('take_profit_pct', 'N/A')}%")
            if strat.get('comment'):
                print(f"备注: {strat['comment']}")
    else:
        print("\n⚠️ 未生成策略")

except Exception as e:
    print(f"\n❌ 策略制定失败: {e}")
    import traceback
    traceback.print_exc()
finally:
    store.close()
