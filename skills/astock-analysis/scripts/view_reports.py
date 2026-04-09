#!/usr/bin/env python3
"""
查看 TradingAgents 历史分析报告

用法:
    python3 view_reports.py                          # 列出所有报告
    python3 view_reports.py --market astock           # 只看 A 股
    python3 view_reports.py --market crypto            # 只看加密货币
    python3 view_reports.py --symbol 600519            # 查看指定股票的最新报告
    python3 view_reports.py --symbol BTCUSDT           # 查看指定币种的最新报告
    python3 view_reports.py --symbol 600519 --date 2026-04-09  # 指定日期
"""
import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from src.infra.report_store import list_reports, read_report, DEFAULT_REPORTS_DIR


def main():
    parser = argparse.ArgumentParser(description="查看 TradingAgents 历史分析报告")
    parser.add_argument("--market", type=str, default="",
                        help="筛选市场（astock/crypto）")
    parser.add_argument("--symbol", type=str, default="",
                        help="查看指定标的的报告内容")
    parser.add_argument("--date", type=str, default="",
                        help="指定日期（YYYY-MM-DD），默认最新")
    parser.add_argument("--list", action="store_true", default=False,
                        help="仅列出报告列表，不显示内容")
    args = parser.parse_args()

    reports_dir = os.path.join(PROJECT_ROOT, DEFAULT_REPORTS_DIR)

    if args.symbol and not args.list:
        # 查看指定标的的报告内容
        content = read_report(args.symbol, args.date, args.market, reports_dir)
        if content:
            print(content)
        else:
            print(f"⚠️  未找到 {args.symbol} 的分析报告")
            # 列出该标的已有的报告
            reports = list_reports(args.market, args.symbol, reports_dir)
            if reports:
                print(f"\n已有报告:")
                for r in reports:
                    print(f"  {r['date']} | {r['market']} | {r['symbol']}")
        return

    # 列出报告
    reports = list_reports(args.market, args.symbol, reports_dir)
    if not reports:
        print("📭 暂无历史分析报告")
        print(f"   报告目录: {reports_dir}")
        print(f"   运行深度分析后会自动保存报告:")
        print(f"   .venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py 600519")
        return

    print(f"📋 历史分析报告（共 {len(reports)} 份）:\n")
    for r in reports:
        market_label = "A股" if r["market"] == "astock" else "加密货币"
        print(f"  {r['date']} | {market_label:4s} | {r['symbol']:14s} | {r['path']}")

    print(f"\n💡 查看报告内容:")
    print(f"   python3 {__file__} --symbol <代码>")
    print(f"   python3 {__file__} --symbol <代码> --date <日期>")


if __name__ == "__main__":
    main()
