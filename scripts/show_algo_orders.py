#!/usr/bin/env python3
"""查看条件单详情"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter

client = BinanceFapiClient(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET"),
    rate_limiter=RateLimiter()
)

algo_orders = client.get_open_algo_orders()
print(f"共 {len(algo_orders)} 个条件单:\n")
for o in algo_orders:
    print(f"  {o.get('symbol','?')} | {o.get('side','?')} | {o.get('type','?')} | {o.get('algoType','?')}")
    print(f"    触发价: {o.get('triggerPrice', o.get('stopLossPrice','N/A'))}")
    print(f"    数量: {o.get('origQty', o.get('quantity','N/A'))}")
    print(f"    状态: {o.get('algoStatus', o.get('status','?'))}")
    print()
