#!/usr/bin/env python3
"""
补录未落库的历史成交（OPGUSDT、CHZUSDT、EDGEUSDT）。
"""
import os, sys, time, hmac, hashlib, datetime
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

import requests
from src.infra.memory_store import MemoryStore
from src.infra.trade_sync import BinanceTradeSyncer
from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter

api_key = os.environ.get('BINANCE_API_KEY', '')
api_secret = os.environ.get('BINANCE_API_SECRET', '')

CUTOFF_MS = 1777741200000  # 2026-05-03 01:00 UTC = CST 09:00
STRATEGY_TAG = 'crypto_oversold_4h'

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'trading_state.db')

rate_limiter = RateLimiter()
fapi_client = BinanceFapiClient(api_key=api_key, api_secret=api_secret, rate_limiter=rate_limiter)
memory_store = MemoryStore(db_path=db_path)

syncer = BinanceTradeSyncer(fapi_client, memory_store)

symbols = ['OPGUSDT', 'CHZUSDT', 'EDGEUSDT']
metadata = {sym: {'rating_score': 6, 'position_size_pct': 2.0,
                   'hold_duration_hours': 0.0, 'strategy_tag': STRATEGY_TAG}
            for sym in symbols}

print(f"补录 {symbols}，strategy_tag={STRATEGY_TAG}")
synced = syncer.sync_closed_trades(symbols=symbols, metadata_by_symbol=metadata)
print(f"✅ 新落库 {synced} 笔")

# 验证结果
import sqlite3
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT tr.symbol, tr.strategy_tag, tr.pnl_amount, tr.closed_at, tsk.sync_key
    FROM trade_records tr
    LEFT JOIN trade_sync_keys tsk ON tsk.trade_record_id = tr.id
    WHERE tr.symbol IN ('OPGUSDT','CHZUSDT','EDGEUSDT')
    ORDER BY tr.closed_at
""").fetchall()
print(f"\n当前数据库中这三个币的记录（{len(rows)} 条）:")
for r in rows:
    print(f"  {r['symbol']:<14} {r['strategy_tag']:<22} pnl={r['pnl_amount']:>8.4f}  {r['closed_at'][:19]}")

print("\n按策略汇总:")
rows2 = conn.execute("""
    SELECT strategy_tag, COUNT(*) cnt, SUM(pnl_amount) total_pnl,
           SUM(CASE WHEN pnl_amount>0 THEN 1 ELSE 0 END) wins,
           SUM(CASE WHEN pnl_amount<0 THEN 1 ELSE 0 END) losses
    FROM trade_records GROUP BY strategy_tag ORDER BY total_pnl DESC
""").fetchall()
for r in rows2:
    print(f"  {r['strategy_tag']:<25} 笔={r['cnt']}  总pnl={r['total_pnl']:>8.4f}  胜={r['wins']} 负={r['losses']}")
conn.close()
memory_store.close()
