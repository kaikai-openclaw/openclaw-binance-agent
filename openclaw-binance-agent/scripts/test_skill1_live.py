"""
Skill-1 真实流程测试

直接调用 Binance 合约公开 API，执行完整的三步量化筛选流程。
不需要 API Key（仅使用公开端点）。
"""

import json
from datetime import datetime, timezone

from src.infra.binance_public import BinancePublicClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.skills.skill1_collect import Skill1Collect


def main():
    print("=" * 60)
    print("Skill-1 真实流程测试")
    print("=" * 60)

    # 初始化
    print("\n[1/4] 初始化基础设施...")
    store = StateStore(db_path="data/test_state.db")
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
    print("[2/4] 构建触发输入...")
    trigger_data = {
        "trigger_time": datetime.now(timezone.utc).isoformat(),
        # 使用默认参数：3000万成交额、5%振幅、2-20%涨幅、1.5倍量比、4h K线、top 10
    }
    trigger_id = store.save("test_trigger", trigger_data)

    # 执行
    print("[3/4] 执行 Skill-1（调用 Binance API）...")
    print("       - Step 0: 获取可交易交易对")
    print("       - Step 1: 大盘过滤（ticker/24hr）")
    print("       - Step 2: 活跃度异动（K线量比）")
    print("       - Step 3: 技术指标评分（RSI + EMA + MACD）")
    print()

    out_id = skill1.execute(trigger_id)

    # 输出结果
    result = store.load(out_id)
    print("[4/4] 结果输出")
    print("=" * 60)

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

    store.close()
    print("✅ 测试完成")


if __name__ == "__main__":
    main()
