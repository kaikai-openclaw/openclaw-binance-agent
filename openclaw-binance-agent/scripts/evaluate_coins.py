#!/usr/bin/env python3
"""
加密货币活跃度评测脚本

从 Binance 获取成交额前 N 的活跃币种，
使用 TradingAgents (多智能体分析框架) 进行深度评级。

用法：
    python scripts/evaluate_coins.py [--top N]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.integrations.trading_agents_adapter import create_trading_agents_analyzer


# ── Binance 市场数据 ────────────────────────────────────

def get_top_coins(limit: int = 20) -> list[dict]:
    """获取 Binance 成交额前 N 的币种。"""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr",
            timeout=10
        )
        data = r.json()
        usdt_pairs = [d for d in data if d["symbol"].endswith("USDT")]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: float(x.get("quoteVolume", 0)),
            reverse=True
        )[:limit]

        result = []
        for d in sorted_pairs:
            price = float(d["lastPrice"])
            high = float(d["highPrice"])
            low = float(d["lowPrice"])
            change = float(d["priceChangePercent"])
            volume = float(d["quoteVolume"])
            volatility = (high - low) / price * 100 if price > 0 else 0

            result.append({
                "symbol": d["symbol"],
                "price": price,
                "change_24h": change,
                "volume_24h": volume,
                "high_24h": high,
                "low_24h": low,
                "volatility": volatility,
            })
        return result
    except Exception as e:
        print(f"获取 Binance 数据失败: {e}")
        return []


# ── TradingAgents 分析（通过 adapter，读取 .env 配置）─────────────


# ── 主程序 ──────────────────────────────────────────────

def evaluate_coins(top: int = 5):
    """评测前 N 个活跃币种。"""

    from src.integrations.trading_agents_adapter import (
        DEFAULT_LLM_PROVIDER, DEFAULT_DEEP_THINK_LLM,
    )

    print("=" * 70)
    print("  加密货币活跃度评测报告 (TradingAgents AI)")
    print(f"  模型: {DEFAULT_LLM_PROVIDER} / {DEFAULT_DEEP_THINK_LLM}")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # 获取市场数据
    print("\n正在获取 Binance 市场数据...")
    coins = get_top_coins(limit=top)
    if not coins:
        print("获取市场数据失败")
        return

    print(f"获取到 {len(coins)} 个活跃币种\n")

    # 初始化 TradingAgents（通过 adapter，自动读取 .env 配置）
    print("正在初始化 TradingAgents (多智能体分析框架)...")
    print("首次初始化约需 60 秒，请耐心等待...\n")
    analyzer = None
    try:
        analyzer = create_trading_agents_analyzer()
        print("TradingAgents 初始化成功\n")
    except Exception as e:
        print(f"TradingAgents 初始化失败: {e}")
        print("将使用规则引擎作为备选\n")

    results = []

    for i, coin in enumerate(coins, 1):
        symbol = coin["symbol"]
        print(f"[{i:2}/{len(coins)}] TradingAgents 分析 {symbol}...", end=" ", flush=True)

        if analyzer:
            print("(分析中，约需 30-60 秒...)", end=" ", flush=True)
            try:
                analysis = analyzer(symbol, coin)
                # adapter 返回 rating_score，统一映射为 rating
                analysis = {
                    "rating": analysis.get("rating_score", 5),
                    "signal": analysis.get("signal", "hold"),
                    "confidence": analysis.get("confidence", 50),
                    "reason": analysis.get("comment", "")[:100] or analysis.get("signal", "hold"),
                }
            except Exception as e:
                print(f"分析失败: {e}", end=" ")
                analysis = None
        else:
            analysis = None

        if analysis:
            result = {
                **coin,
                "rating": analysis["rating"],
                "signal": analysis["signal"],
                "confidence": analysis["confidence"],
                "reason": analysis["reason"],
            }
            signal_icon = {"long": "📈", "short": "📉", "hold": "⏸️"}.get(analysis["signal"], "?")
            print(f"\n    评分 {analysis['rating']}/10 {signal_icon} ({analysis['confidence']}%)")
            print(f"    理由: {analysis['reason']}")
        else:
            result = {
                **coin,
                "rating": 5,
                "signal": "hold",
                "confidence": 50,
                "reason": "分析超时",
            }
            print("分析超时，使用默认评分 5/10 ⏸️")

        results.append(result)
        print()

    # ── 生成报告 ────────────────────────────────────────

    results.sort(key=lambda x: x["rating"], reverse=True)

    print("\n" + "=" * 70)
    print("  📊 评测结果（按评分排序）")
    print("=" * 70)
    print(f"{'排名':<4} {'币种':<12} {'价格':>14} {'24h涨跌':>10} {'评分':>6} {'信号':<6} {'置信度':>8}")
    print("-" * 70)

    for i, r in enumerate(results, 1):
        signal_icon = {"long": "📈", "short": "📉", "hold": "⏸️"}.get(r["signal"], "?")
        change_str = f"{r['change_24h']:+.2f}%"
        print(f"{i:<4} {r['symbol']:<12} ${r['price']:>12.4f} {change_str:>10} "
              f"{r['rating']:>5}/10 {signal_icon} {r['confidence']:>6}%")

    # ── 推荐交易 ────────────────────────────────────────

    print("\n" + "=" * 70)
    print("  💡 交易建议 (TradingAgents AI)")
    print("=" * 70)

    long_recs = [r for r in results if r["rating"] >= 7 and r["signal"] == "long"]
    short_recs = [r for r in results if r["rating"] >= 7 and r["signal"] == "short"]
    hold_recs = [r for r in results if r["rating"] < 7 or r["signal"] == "hold"]

    if long_recs:
        print("\n📈 建议做多（评分 ≥7）：")
        for r in long_recs[:5]:
            print(f"\n   • {r['symbol']}: {r['reason']}")
            print(f"     评分 {r['rating']}/10 | 置信度 {r['confidence']}%")

    if short_recs:
        print("\n📉 建议做空（评分 ≥7 且信号为 short）：")
        for r in short_recs[:5]:
            print(f"\n   • {r['symbol']}: {r['reason']}")
            print(f"     评分 {r['rating']}/10 | 置信度 {r['confidence']}%")

    if hold_recs:
        print("\n⏸️  观望（评分 <7）：")
        for r in hold_recs[:5]:
            print(f"   • {r['symbol']}: {r['reason']} (评分 {r['rating']}/10)")

    # ── 风险提示 ────────────────────────────────────────

    print("\n" + "=" * 70)
    print("  ⚠️  风险提示")
    print("=" * 70)

    volatile = [r for r in results if r["volatility"] > 30]
    if volatile:
        print("\n🔥 高波动币种（波动率 > 30%）：")
        for r in volatile:
            print(f"   • {r['symbol']}: 波动率 {r['volatility']:.1f}%")

    extreme = [r for r in results if abs(r["change_24h"]) > 50]
    if extreme:
        print("\n🚨 极端涨跌（24h > 50%）：")
        for r in extreme:
            emoji = "📈" if r["change_24h"] > 0 else "📉"
            print(f"   • {r['symbol']}: {r['change_24h']:+.2f}% {emoji}")

    print("\n  • TradingAgents 多智能体分析，仅供参考")
    print("  • 虚拟货币投资风险极高，请谨慎决策")
    print("  • 建议先用 Paper Mode 小资金验证策略")
    print("=" * 70)

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="加密货币活跃度评测")
    parser.add_argument("--top", type=int, default=5, help="评测前 N 个活跃币种 (默认 5)")
    args = parser.parse_args()
    evaluate_coins(top=args.top)
