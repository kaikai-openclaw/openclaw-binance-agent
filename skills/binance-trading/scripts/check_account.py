#!/usr/bin/env python3
"""
查询 Binance 合约账户状态（OpenClaw skill 调用入口）

输出统一格式的账户信息，与定时任务报告中的持仓/保护单/账户段落完全一致。
策略来源从 StateStore skill4_execute 执行结果的 strategy_tag 动态读取。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.daily_pnl import calculate_daily_realized_pnl
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore

from report_utils import (
    safe_float,
    build_symbol_source_map,
    tag_symbol_or_default,
    build_position_snapshots,
    build_protection_report,
    build_account_summary,
    render_positions_markdown,
    render_protection_markdown,
    render_account_markdown,
    render_warnings_markdown,
    protection_warnings,
    STRATEGY_TAG_MAP,
)


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

    db_dir = os.path.join(PROJECT_ROOT, "data")
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    source_map = build_symbol_source_map(store)

    try:
        account_info = fapi.get_account_info()
        total_balance = float(account_info.total_balance)

        # 获取持仓
        raw_positions = fapi.get_positions()
        position_symbols = {
            p.symbol for p in raw_positions if abs(p.position_amt) > 0
        }

        # 计算日已实现盈亏
        daily_realized_pnl = calculate_daily_realized_pnl(fapi, position_symbols)
        account_info.daily_realized_pnl = daily_realized_pnl

        # 构建持仓快照（使用 strategy_tag 来源映射）
        tag_source_map = {}
        for sym, (emoji, label) in source_map.items():
            tag_source_map[sym] = f"{emoji}{label}"
        positions = build_position_snapshots(
            total_balance, raw_positions, tag_source_map
        )

        # 构建保护单报告
        algo_orders = fapi.get_open_algo_orders()
        protection = build_protection_report(positions, algo_orders)
        warnings = protection_warnings(protection)

        # 构建账户摘要
        paper_mode = False
        account_summary = build_account_summary(account_info, positions, paper_mode)

        risk = {
            "single_trade_margin_limit_pct": 35,
            "single_symbol_position_limit_pct": 40,
            "daily_loss_stop_pct": 5,
            "risk_status": "normal",
        }

        # 输出统一格式
        lines = []
        lines.extend(render_positions_markdown(positions, source_map))

        # 按策略分组统计
        strategy_stats: dict[str, dict] = {}
        for pos in positions:
            sym = pos["symbol"]
            if sym in source_map:
                _, label = source_map[sym]
            else:
                label = "未知"
            if label not in strategy_stats:
                strategy_stats[label] = {
                    "margin": 0, "count": 0, "symbols": [], "unrealized": 0,
                }
            strategy_stats[label]["margin"] += pos["initial_margin"]
            strategy_stats[label]["count"] += 1
            strategy_stats[label]["symbols"].append(sym)
            strategy_stats[label]["unrealized"] += pos["unrealized_pnl"]

        if strategy_stats:
            lines.append("")
            lines.append("策略资金分布:")
            label_order = {"超跌": 0, "反转": 1, "做空": 2, "通用": 3, "未知": 9}
            for label in sorted(strategy_stats, key=lambda x: label_order.get(x, 8)):
                s = strategy_stats[label]
                pct = s["margin"] / total_balance * 100 if total_balance > 0 else 0
                emoji = "📌"
                for _, (e, l) in STRATEGY_TAG_MAP.items():
                    if l == label:
                        emoji = e
                        break
                lines.append(
                    f"- {emoji}{label}: {s['count']} 笔, "
                    f"保证金 {s['margin']:.2f} ({pct:.1f}%), "
                    f"浮盈亏 {s['unrealized']:+.2f}"
                )
                lines.append(f"  币种: {', '.join(s['symbols'])}")

        lines.append("")
        lines.extend(render_protection_markdown(protection))
        lines.append("")
        lines.extend(render_account_markdown(account_summary, risk))
        warn_lines = render_warnings_markdown(warnings, [])
        if warn_lines:
            lines.append("")
            lines.extend(warn_lines)

        print("\n".join(lines))

    except Exception as e:
        print(f"❌ 查询失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
