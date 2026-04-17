#!/usr/bin/env python3
"""
查询 Binance 合约账户状态（OpenClaw skill 调用入口）

输出：账户余额、可用保证金、未实现盈亏、当前持仓明细，
持仓按来源动态分类标记（从 StateStore 最近一次筛选结果自动匹配）：
  🔑 = 趋势候选（Skill-1 趋势动量策略）
  🌀 = 超跌候选（超跌反弹策略）
  🔄 = 反转候选（底部放量反转策略）
  � = 做空候选（超买做空策略）
  📌 = 手动/未知来源

持仓显示：数量、名义价值、保证金、资金占比、杠杆、盈亏
汇总显示：按策略分组的资金占比和资金量
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from binance.client import Client
from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore, StateNotFoundError

# ── 策略来源定义 ──────────────────────────────────────────
# skill_name → (emoji, 中文标签)
STRATEGY_SOURCES = {
    "skill1_collect":          ("🔑", "趋势"),
    "crypto_oversold_short":   ("🌀", "超跌短"),
    "crypto_oversold_long":    ("🌀", "超跌长"),
    "crypto_reversal_short":   ("🔄", "反转短"),
    "crypto_reversal_long":    ("🔄", "反转长"),
    "crypto_overbought_short": ("📉", "做空短"),
    "crypto_overbought_long":  ("📉", "做空长"),
}


def build_symbol_source_map(store: StateStore) -> dict:
    """从 StateStore 最近一次各策略筛选结果中，构建 symbol → (emoji, label) 映射。

    优先级：最近一次筛选结果覆盖旧结果。
    同一个 symbol 可能出现在多个策略中，取最近的那个。
    """
    source_map = {}  # symbol → (emoji, label, skill_name)

    for skill_name, (emoji, label) in STRATEGY_SOURCES.items():
        try:
            _, data = store.get_latest(skill_name)
            candidates = data.get("candidates", [])
            for c in candidates:
                sym = c.get("symbol", "")
                if sym:
                    # 不覆盖已有的（先到先得，按 STRATEGY_SOURCES 顺序）
                    if sym not in source_map:
                        source_map[sym] = (emoji, label)
        except StateNotFoundError:
            pass
        except Exception:
            pass

    return source_map


def tag_symbol(symbol: str, source_map: dict) -> str:
    """返回 symbol 的策略标签。"""
    if symbol in source_map:
        emoji, label = source_map[symbol]
        return f"{emoji}{label}"
    return ""


def order_tag_symbol(symbol: str, source_map: dict) -> str:
    """返回订单的策略标签（无来源时标记为手动）。"""
    if symbol in source_map:
        emoji, label = source_map[symbol]
        return f"{emoji}{label}"
    return "📌手动"


def main():
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("❌ 缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET 环境变量")
        sys.exit(1)

    fapi = BinanceFapiClient(
        api_key=api_key,
        api_secret=api_secret,
        rate_limiter=RateLimiter(),
    )
    raw_client = Client(api_key, api_secret)

    # 从 StateStore 动态构建策略来源映射
    db_dir = os.path.join(PROJECT_ROOT, "data")
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    source_map = build_symbol_source_map(store)

    try:
        info = fapi.get_account_info()
        total_balance = float(info.total_balance)

        print(f"📊 账户状态")
        print(f"   总资金:       {info.total_balance:.2f} USDT")
        print(f"   可用保证金:   {info.available_balance:.2f} USDT")
        print(f"   未实现盈亏:   {info.total_unrealized_pnl:.2f} USDT")

        # 用 raw_client 拿详细持仓（含保证金和杠杆）
        all_raw = raw_client.futures_position_information(symbol=None)
        active = []
        for p in all_raw:
            if float(p.get("positionAmt", 0)) != 0:
                active.append(p)

        if active:
            print(f"\n📈 持仓明细 ({len(active)} 笔)")
            print(
                f"  {'币种':<14} {'来源':<8} {'数量':>10} {'名义价值':>12} "
                f"{'保证金':>8} {'占资金%':>7} {'杠杆':>5} {'浮盈亏':>10}"
            )
            print("  " + "-" * 82)

            # 按策略分组统计
            strategy_stats = {}  # label → {"margin": 0, "notional": 0, "count": 0, "symbols": []}
            total_margin = 0.0
            total_notional = 0.0
            manual_positions = []

            for p in sorted(active, key=lambda x: abs(float(x.get("notional", 0))), reverse=True):
                sym = p["symbol"]
                amt = float(p["positionAmt"])
                entry = float(p["entryPrice"])
                unrealized = float(p["unRealizedProfit"])
                notional = abs(float(p.get("notional", 0)))
                margin = float(p["initialMargin"])
                lev = notional / margin if margin > 0 else 0
                margin_pct = margin / total_balance * 100 if total_balance > 0 else 0

                total_margin += margin
                total_notional += notional

                t = tag_symbol(sym, source_map)
                label = f" {t}" if t else ""

                # 归类到策略分组
                if sym in source_map:
                    _, strategy_label = source_map[sym]
                else:
                    strategy_label = "手动"

                if strategy_label not in strategy_stats:
                    strategy_stats[strategy_label] = {
                        "margin": 0, "notional": 0, "count": 0,
                        "symbols": [], "unrealized": 0,
                    }
                strategy_stats[strategy_label]["margin"] += margin
                strategy_stats[strategy_label]["notional"] += notional
                strategy_stats[strategy_label]["count"] += 1
                strategy_stats[strategy_label]["symbols"].append(sym)
                strategy_stats[strategy_label]["unrealized"] += unrealized

                print(
                    f"  {sym:<14} {t or '📌手动':<8} {abs(amt):>10.4f} "
                    f"{notional:>12,.2f} {margin:>8.2f} "
                    f"{margin_pct:>6.1f}% {lev:>5.1f}x {unrealized:>+10.2f}"
                )

            print("  " + "-" * 82)
            total_margin_pct = total_margin / total_balance * 100 if total_balance > 0 else 0
            print(
                f"  {'合计':<14} {'':8} {'':>10} {total_notional:>12,.2f} "
                f"{total_margin:>8.2f} {total_margin_pct:>6.1f}%"
            )

            # ── 按策略分组汇总 ──
            print(f"\n📊 策略资金分布")
            print(f"  {'策略':<10} {'持仓数':>6} {'保证金':>10} {'占资金%':>8} {'名义价值':>14} {'浮盈亏':>10}")
            print("  " + "-" * 64)

            # 策略标签排序：趋势 > 超跌 > 反转 > 做空 > 手动
            label_order = {"趋势": 0, "超跌短": 1, "超跌长": 2, "反转短": 3, "反转长": 4,
                           "做空短": 5, "做空长": 6, "手动": 9}
            for label in sorted(strategy_stats.keys(), key=lambda x: label_order.get(x, 8)):
                s = strategy_stats[label]
                pct = s["margin"] / total_balance * 100 if total_balance > 0 else 0
                # 找对应的 emoji
                emoji = "📌"
                for _, (e, l) in STRATEGY_SOURCES.items():
                    if l == label:
                        emoji = e
                        break
                print(
                    f"  {emoji}{label:<8} {s['count']:>6} "
                    f"{s['margin']:>10.2f} {pct:>7.1f}% "
                    f"{s['notional']:>14,.2f} {s['unrealized']:>+10.2f}"
                )

            print("  " + "-" * 64)

            # 列出各策略的币种
            for label in sorted(strategy_stats.keys(), key=lambda x: label_order.get(x, 8)):
                s = strategy_stats[label]
                emoji = "📌"
                for _, (e, l) in STRATEGY_SOURCES.items():
                    if l == label:
                        emoji = e
                        break
                print(f"   {emoji} {label}: {', '.join(s['symbols'])}")

        else:
            print("\n📈 当前无持仓")

        # 未完成订单
        open_orders = fapi.get_open_orders()
        if open_orders:
            print(f"\n📋 未完成订单 ({len(open_orders)} 笔)")
            for o in open_orders:
                sym = o.get("symbol", "")
                t = order_tag_symbol(sym, source_map)
                print(
                    f"   {sym} {t} {o.get('side','')} {o.get('type','')} "
                    f"价格:{o.get('price','')} 数量:{o.get('origQty','')}"
                )
        else:
            print("\n📋 无未完成订单")

        # 止盈止损条件单
        algo_orders = fapi.get_open_algo_orders()
        if algo_orders:
            by_symbol: dict = {}
            for o in algo_orders:
                sym = o.get("symbol", "?")
                by_symbol.setdefault(sym, []).append(o)

            print(f"\n🛡️ 止盈止损条件单 ({len(algo_orders)} 笔)")
            for sym, orders in sorted(by_symbol.items()):
                t = tag_symbol(sym, source_map)
                label = f" {t}" if t else ""
                entry = None
                for p in active:
                    if p["symbol"] == sym:
                        entry = float(p["entryPrice"])
                        break
                entry_str = f" (入场:{entry:.4f})" if entry else ""
                print(f"   {sym}{label}{entry_str}")
                for o in sorted(
                    orders, key=lambda x: float(x.get("triggerPrice", 0)), reverse=True
                ):
                    trigger = float(o.get("triggerPrice", 0))
                    qty = o.get("quantity", "")
                    side = o.get("side", "")
                    if entry:
                        if side == "SELL":
                            lbl = "止盈" if trigger > entry else "止损"
                        else:
                            lbl = "止盈" if trigger < entry else "止损"
                        pct = (trigger - entry) / entry * 100 if entry > 0 else 0
                        print(f"     {lbl} 触发:{trigger} ({pct:+.1f}%) 数量:{qty}")
                    else:
                        print(f"     {side} 触发:{trigger} 数量:{qty}")
        else:
            print("\n🛡️ 无止盈止损条件单")

    except Exception as e:
        print(f"❌ 查询失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
