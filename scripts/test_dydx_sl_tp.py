#!/usr/bin/env python3
"""DYDXUSDT 止损止盈 - 通过 BinanceFapiClient 验证修复"""
import os, sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

env_path = os.path.join(PROJECT_ROOT, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from src.infra.binance_fapi import BinanceFapiClient

SYMBOL = "DYDXUSDT"
SL_PRICE = 0.09991   # 原始精度，应自动截断到 pricePrecision=3 → 0.099
TP_PRICE = 0.10918   # → 0.109
QTY = 274

def main():
    client = BinanceFapiClient(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
    )
    print(f"=== DYDXUSDT SL/TP 测试 ===")

    # 取消现有 algo 挂单
    print("\n[1] 取消现有 Algo 挂单...")
    cancelled = client.cancel_all_algo_orders(SYMBOL)
    print(f"  已取消 {cancelled} 笔")

    # 止损
    print(f"\n[2] 止损单 SELL {QTY} @ {SL_PRICE}...")
    sl = client.place_stop_market_order(SYMBOL, "SELL", QTY, SL_PRICE)
    print(f"  ✅ algoId={sl.order_id} status={sl.status}")

    # 止盈
    print(f"\n[3] 止盈单 SELL {QTY} @ {TP_PRICE}...")
    tp = client.place_take_profit_market_order(SYMBOL, "SELL", QTY, TP_PRICE)
    print(f"  ✅ algoId={tp.order_id} status={tp.status}")

    # 确认
    print(f"\n[4] 确认挂单...")
    orders = client.get_open_algo_orders(SYMBOL)
    for o in orders:
        print(f"  algoId={o.get('algoId')} {o.get('orderType')} "
              f"trigger={o.get('triggerPrice')} {o.get('algoStatus')}")
    print(f"\n完成。共 {len(orders)} 笔 Algo 挂单。")

if __name__ == "__main__":
    main()
