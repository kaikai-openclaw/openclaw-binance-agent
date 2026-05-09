#!/usr/bin/env python3
"""运行 A 股底部反转扫描"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from src.infra.akshare_client import AkshareClient
from src.infra.state_store import StateStore
from src.skills.astock_reversal import AStockReversalSkill

print("📡 正在初始化 A 股数据源...")

store = StateStore(db_path="data/state_store.db")
client = AkshareClient(cache_db_path="data/kline_cache.db")

skill = AStockReversalSkill(
    state_store=store,
    input_schema={},
    output_schema={},
    client=client,
)

print("🔍 正在扫描 A 股底部反转信号...")
print("=" * 70)

try:
    result = skill.run({
        "skip_market_regime": False,
        "min_score": 40,
        "max_candidates": 20,
    })

    summary = result.get("filter_summary", {})
    skipped = summary.get("skipped_reason")

    if skipped == "market_regime_bear":
        print(f"\n⚠️  大盘环境不佳，底部反转策略暂停")
        print(f"   大盘趋势: {summary.get('market_trend', '?')}")
        print(f"   原因: {summary.get('market_reason', '?')}")
        sys.exit(0)

    print(f"\n📊 扫描漏斗:")
    print(f"   全部股票:       {summary.get('total_tickers', '?')}")
    print(f"   基础过滤后:     {summary.get('after_base_filter', '?')}")
    print(f"   反转过滤后:     {summary.get('after_reversal_filter', '?')}")
    print(f"   最终输出:       {summary.get('output_count', '?')}")

    candidates = result.get("candidates", [])
    if not candidates:
        print("\n⚠️  当前无符合条件的底部反转候选（可尝试降低 min_score）")
    else:
        print(f"\n🎯 底部反转候选 ({len(candidates)} 只):\n")
        for i, c in enumerate(candidates, 1):
            rsi_str = f"{c['rsi']:.1f}" if c.get("rsi") is not None else "N/A"
            atr_str = f"{c['atr_pct']:.2f}%" if c.get("atr_pct") is not None else "N/A"
            dist_str = f"{c['dist_bottom_pct']:+.1f}%" if c.get("dist_bottom_pct") is not None else "N/A"
            drop_str = f"{c['prior_drop_pct']:.1f}%" if c.get("prior_drop_pct") is not None else "N/A"
            print(f"   {i:>2}. {c['symbol']} {c.get('name', '')}")
            print(f"       反转评分: {c['reversal_score']:>3}/100  |  "
                  f"收盘: {c['close']:.2f}  |  "
                  f"涨跌: {c.get('change_pct', 0):+.2f}%")
            print(f"       放量比: {c.get('volume_surge_ratio', 0):.1f}x  |  "
                  f"距底: {dist_str}  |  "
                  f"前期跌幅: {drop_str}  |  "
                  f"RSI: {rsi_str}  |  ATR: {atr_str}")
            print(f"       信号: {c.get('signal_details', '—')}")
            # 分项得分
            print(f"       分项: 放量{c.get('volume_surge_score',0)} "
                  f"企稳{c.get('price_stable_score',0)} "
                  f"均线{c.get('ma_turn_score',0)} "
                  f"MACD{c.get('macd_reversal_score',0)} "
                  f"距底{c.get('dist_bottom_score',0)} "
                  f"跌幅{c.get('prior_drop_score',0)} "
                  f"换手{c.get('turnover_score',0)} "
                  f"KDJ{c.get('kdj_score',0)} "
                  f"影线{c.get('shadow_score',0)}")
            print()

        print("-" * 70)
        print(f"⏰ 扫描时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"⚠️  风险提示: 底部反转可能是假突破，跌破近期低点即止损")

except Exception as e:
    print(f"\n❌ 扫描失败: {e}")
    import traceback
    traceback.print_exc()
finally:
    store.close()
