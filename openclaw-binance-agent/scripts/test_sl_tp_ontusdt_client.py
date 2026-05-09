#!/usr/bin/env python3
"""
ONTUSDT 止损止盈 - 通过更新后的 BinanceFapiClient 测试

先取消之前的测试挂单，再重新下单验证 client 方法正确性。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

env_path = os.path.join(PROJECT_ROOT, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

from src.infra.binance_fapi import BinanceFapiClient

SYMBOL = "ONTUSDT"
STOP_LOSS_PRICE = 0.0760    # 原始 0.075951，ONTUSDT pricePrecision=4
TAKE_PROFIT_PRICE = 0.0830  # 原始 0.082998


def main():
    api_key = os.environ["BINANCE_API_KEY"]
    api_secret = os.environ["BINANCE_API_SECRET"]
    client = BinanceFapiClient(api_key=api_key, api_secret=api_secret)

    print("=" * 50)
    print("  BinanceFapiClient 止损止盈测试 (Algo Order API)")
    print("=" * 50)

    # 1. 查持仓
    print(f"\n[1/6] 查询 {SYMBOL} 持仓...")
    pos = client.get_position_risk(SYMBOL)
    print(f"  持仓: {pos.position_amt} @ {pos.entry_price}")
    print(f"  标记价: {pos.mark_price}  杠杆: {pos.leverage}x")

    # 2. 取消之前的 algo 挂单
    print(f"\n[2/6] 取消 {SYMBOL} 现有 Algo 挂单...")
    existing = client.get_open_algo_orders(SYMBOL)
    print(f"  现有 Algo 挂单: {len(existing)} 笔")
    for o in existing:
        algo_id = o.get("algoId")
        print(f"    取消 algoId={algo_id} ({o.get('orderType')})")
        try:
            client.cancel_algo_order(SYMBOL, algo_id)
            print(f"    ✅ 已取消")
        except Exception as e:
            print(f"    ❌ 取消失败: {e}")

    # 3. 下止损单
    print(f"\n[3/6] 下止损单 (place_stop_market_order)...")
    print(f"  止损价: {STOP_LOSS_PRICE}")
    sl = client.place_stop_market_order(
        symbol=SYMBOL,
        side="SELL",
        quantity=0,
        stop_price=STOP_LOSS_PRICE,
        close_position=True,
    )
    print(f"  ✅ order_id={sl.order_id} status={sl.status} price={sl.price}")

    # 4. 下止盈单
    print(f"\n[4/6] 下止盈单 (place_take_profit_market_order)...")
    print(f"  止盈价: {TAKE_PROFIT_PRICE}")
    tp = client.place_take_profit_market_order(
        symbol=SYMBOL,
        side="SELL",
        quantity=0,
        stop_price=TAKE_PROFIT_PRICE,
        close_position=True,
    )
    print(f"  ✅ order_id={tp.order_id} status={tp.status} price={tp.price}")

    # 5. 确认 Algo 挂单
    print(f"\n[5/6] 确认 Algo 挂单...")
    orders = client.get_open_algo_orders(SYMBOL)
    print(f"  Algo 挂单数: {len(orders)}")
    for o in orders:
        print(f"    algoId={o.get('algoId')} type={o.get('orderType')} "
              f"trigger={o.get('triggerPrice')} status={o.get('algoStatus')}")

    # 6. 也检查普通挂单（应该为 0）
    print(f"\n[6/6] 确认普通挂单...")
    normal = client.get_open_orders(SYMBOL)
    print(f"  普通挂单数: {len(normal)}")

    # 汇总
    print(f"\n{'='*50}")
    print("  结果汇总")
    print(f"{'='*50}")
    print(f"  止损单: ✅ algoId={sl.order_id} @ {sl.price}")
    print(f"  止盈单: ✅ algoId={tp.order_id} @ {tp.price}")
    print(f"  持仓: {pos.position_amt} ONT @ {pos.entry_price}")


if __name__ == "__main__":
    main()
