#!/usr/bin/env python3
"""运行 Skill-1：收集候选币种"""
import json
import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from src.infra.binance_public import BinancePublicClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.skills.skill1_collect import Skill1Collect

print("📡 正在连接 Binance 公开 API...")

# 初始化
store = StateStore(db_path="data/state_store.db")
limiter = RateLimiter()
client = BinancePublicClient(rate_limiter=limiter)

with open("config/schemas/skill1_input.json") as f:
    in_schema = json.load(f)
with open("config/schemas/skill1_output.json") as f:
    out_schema = json.load(f)

skill1 = Skill1Collect(
    state_store=store,
    input_schema=in_schema,
    output_schema=out_schema,
    client=client,
)

# 构建输入
print("🔍 正在筛选候选币种...")
print("=" * 60)

try:
    # 构建触发输入
    trigger_data = {
        "trigger_time": datetime.now(timezone.utc).isoformat(),
    }
    trigger_id = store.save("skill1_trigger", trigger_data)

    # 执行收集
    out_id = skill1.execute(trigger_id)
    result = store.load(out_id)

    # 输出结果
    summary = result.get("filter_summary", {})
    print(f"\n📊 筛选漏斗:")
    print(f"   全部交易对:     {summary.get('total_tickers', '?')}")
    print(f"   大盘过滤后:     {summary.get('after_base_filter', '?')}")
    print(f"   信号过滤后:     {summary.get('after_signal_filter', '?')}")
    print(f"   最终输出:       {summary.get('output_count', '?')}")

    candidates = result.get("candidates", [])
    if not candidates:
        print("\n⚠️  当前市场无符合条件的候选币种（可尝试放宽参数）")
    else:
        print(f"\n🎯 候选币种 ({len(candidates)} 个):\n")
        for i, c in enumerate(candidates, 1):
            rsi_str = f"{c['rsi']:.1f}" if c.get("rsi") is not None else "N/A"
            ema_str = "✅" if c.get("ema_bullish") else "❌"
            macd_str = "✅" if c.get("macd_bullish") else "❌"
            print(f"   {i}. {c['symbol']}")
            print(f"      评分: {c['signal_score']}/100  |  "
                  f"成交额: {c['quote_volume_24h']:,.0f} USDT  |  "
                  f"涨幅: {c['price_change_pct']:+.2f}%")
            print(f"      振幅: {c['amplitude_pct']:.2f}%  |  "
                  f"量比: {c['volume_surge_ratio']:.2f}x  |  "
                  f"RSI: {rsi_str}")
            print(f"      EMA多头: {ema_str}  |  MACD看多: {macd_str}")
            print()

        print("-" * 60)
        print(f"\n💡 下一步：请运行 Skill-2 对这些候选进行深度分析")
        print(f"   state_id: {out_id}")

except Exception as e:
    print(f"\n❌ 筛选失败: {e}")
    import traceback
    traceback.print_exc()
finally:
    store.close()
