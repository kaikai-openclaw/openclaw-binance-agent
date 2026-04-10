#!/usr/bin/env python3
"""
查询 Binance 合约账户状态（OpenClaw skill 调用入口）

输出：账户余额、可用保证金、未实现盈亏、当前持仓明细。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter


def main():
    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("❌ 缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET 环境变量")
        sys.exit(1)

    client = BinanceFapiClient(
        api_key=api_key,
        api_secret=api_secret,
        rate_limiter=RateLimiter(),
    )

    try:
        info = client.get_account_info()
        positions = client.get_positions()

        print(f"📊 账户状态")
        print(f"   总资金:       {info.total_balance:.2f} USDT")
        print(f"   可用保证金:   {info.available_balance:.2f} USDT")
        print(f"   未实现盈亏:   {info.total_unrealized_pnl:.2f} USDT")

        if positions:
            print(f"\n📈 持仓明细 ({len(positions)} 笔)")
            for p in positions:
                direction = "做多" if p.position_amt > 0 else "做空"
                print(f"   {p.symbol} {direction} | 数量:{abs(p.position_amt)} | "
                      f"入场:{p.entry_price:.4f} | 盈亏:{p.unrealized_pnl:.2f} USDT")
        else:
            print("\n📈 当前无持仓")

        # 查询未完成订单
        open_orders = client.get_open_orders()
        if open_orders:
            print(f"\n📋 未完成订单 ({len(open_orders)} 笔)")
            for o in open_orders:
                print(f"   {o.get('symbol','')} {o.get('side','')} {o.get('type','')} "
                      f"价格:{o.get('price','')} 数量:{o.get('origQty','')}")
        else:
            print("\n📋 无未完成订单")

        # 查询止盈止损条件单（Algo Orders）
        algo_orders = client.get_open_algo_orders()
        if algo_orders:
            # 按 symbol 分组展示
            by_symbol: dict = {}
            for o in algo_orders:
                sym = o.get("symbol", "?")
                by_symbol.setdefault(sym, []).append(o)

            print(f"\n🛡️ 止盈止损条件单 ({len(algo_orders)} 笔)")
            for sym, orders in sorted(by_symbol.items()):
                # 找到对应持仓的入场价
                entry = None
                for p in positions:
                    if p.symbol == sym:
                        entry = p.entry_price
                        break
                entry_str = f" (入场:{entry:.4f})" if entry else ""
                print(f"   {sym}{entry_str}")
                for o in sorted(orders, key=lambda x: float(x.get("triggerPrice", 0)), reverse=True):
                    trigger = float(o.get("triggerPrice", 0))
                    qty = o.get("quantity", "")
                    side = o.get("side", "")
                    # 判断是止盈还是止损
                    if entry:
                        if side == "SELL":
                            label = "止盈" if trigger > entry else "止损"
                        else:
                            label = "止盈" if trigger < entry else "止损"
                        pct = (trigger - entry) / entry * 100 if entry > 0 else 0
                        print(f"     {label} 触发:{trigger} ({pct:+.1f}%) 数量:{qty}")
                    else:
                        print(f"     {side} 触发:{trigger} 数量:{qty}")
        else:
            print("\n🛡️ 无止盈止损条件单")

    except Exception as e:
        print(f"❌ 查询失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
