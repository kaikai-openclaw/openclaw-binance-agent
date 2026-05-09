#!/usr/bin/env python3
import os, sys, time, hmac, hashlib, datetime
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
import requests, sqlite3

api_key = os.environ.get('BINANCE_API_KEY', '')
api_secret = os.environ.get('BINANCE_API_SECRET', '')

def signed_get(path, params):
    params['recvWindow'] = 5000
    params['timestamp'] = int(time.time() * 1000)
    qs = urlencode(params)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params['signature'] = sig
    r = requests.get('https://fapi.binance.com' + path, params=params,
                     headers={'X-MBX-APIKEY': api_key}, timeout=10)
    return r.json()

CUTOFF_MS = 1777741200000  # 2026-05-03 01:00 UTC = CST 09:00

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'trading_state.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
local_keys = set(
    r['sync_key'].split(':')[2]
    for r in conn.execute("SELECT sync_key FROM trade_sync_keys WHERE sync_key LIKE 'binance_user_order:%'")
    if len(r['sync_key'].split(':')) == 3
)

symbols = ['OPGUSDT', 'CHZUSDT', 'EDGEUSDT']
print(f"{'symbol':<14} {'orderId':<18} {'side':<6} {'qty':>12} {'realizedPnl':>12}  {'time(CST)'}  {'本地状态'}")
print('-' * 95)

all_orders = {}
for sym in symbols:
    data = signed_get('/fapi/v1/userTrades', {'symbol': sym, 'startTime': CUTOFF_MS, 'limit': 1000})
    if isinstance(data, list):
        for t in data:
            oid = str(t['orderId'])
            if oid not in all_orders:
                all_orders[oid] = {'symbol': t['symbol'], 'orderId': oid, 'side': t['side'],
                                   'qty': 0.0, 'pnl': 0.0, 'time': int(t['time'])}
            all_orders[oid]['qty'] += float(t['qty'])
            all_orders[oid]['pnl'] += float(t['realizedPnl'])
            all_orders[oid]['time'] = max(all_orders[oid]['time'], int(t['time']))
    elif isinstance(data, dict):
        print(f'{sym}: ERROR {data}')

if not all_orders:
    print('  （三个币种今日 CST 09:00 后均无成交记录）')
else:
    missing = []
    for oid, a in sorted(all_orders.items(), key=lambda x: x[1]['time']):
        cst = datetime.datetime.utcfromtimestamp(a['time']/1000 + 8*3600).strftime('%m-%d %H:%M:%S')
        status = '✅ 已落库' if oid in local_keys else '❌ 未落库'
        if oid not in local_keys:
            missing.append(a)
        print(f"{a['symbol']:<14} {oid:<18} {a['side']:<6} {a['qty']:>12.4f} {a['pnl']:>12.4f}  {cst}  {status}")

    total_pnl = sum(a['pnl'] for a in all_orders.values())
    print(f"\n共 {len(all_orders)} 笔，总 pnl = {total_pnl:.4f} USDT")
    if missing:
        print(f"⚠️  未落库 {len(missing)} 笔，合计 pnl = {sum(a['pnl'] for a in missing):.4f} USDT")
    else:
        print("✅ 全部已落库")

conn.close()
