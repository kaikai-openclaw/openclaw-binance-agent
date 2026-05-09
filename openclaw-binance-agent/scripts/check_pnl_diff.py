#!/usr/bin/env python3
"""
检查币安成交记录与本地 trade_records 的差异。
"""
import os, sys, time, hmac, hashlib, datetime
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

import requests
import sqlite3

api_key = os.environ.get('BINANCE_API_KEY', '')
api_secret = os.environ.get('BINANCE_API_SECRET', '')


def signed_get(path, params):
    params['recvWindow'] = 5000
    params['timestamp'] = int(time.time() * 1000)
    qs = urlencode(params)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params['signature'] = sig
    r = requests.get(
        f'https://fapi.binance.com{path}',
        params=params,
        headers={'X-MBX-APIKEY': api_key},
        timeout=10,
    )
    return r.json()


def main():
    # 今日 UTC 00:00
    today_ms = 1746230400000  # 2026-05-03 00:00:00 UTC
    # 过去 72 小时（与 trade_sync lookback 一致）
    lookback_ms = int((time.time() - 72 * 3600) * 1000)

    # 本地有记录的币种 + 日报里提到的币种
    symbols = [
        'KAVAUSDT', 'ATUSDT', 'WLDUSDT', 'SWARMSUSDT', 'TRXUSDT', 'ALGOUSDT',
        'TAGUSDT', 'ZBTUSDT', 'ORCAUSDT', 'MOVRUSDT',
    ]

    # ── 1. 拉取币安今日成交 ──────────────────────────────────────────────
    print("=" * 100)
    print("【币安今日成交（UTC 2026-05-03，realizedPnl != 0）】")
    print("=" * 100)
    print(f"{'symbol':<16} {'orderId':<18} {'side':<6} {'qty':>10} {'price':>12} {'realizedPnl':>12}  {'time(UTC)'}")
    print("-" * 100)

    all_trades = []
    for sym in symbols:
        data = signed_get('/fapi/v1/userTrades', {'symbol': sym, 'startTime': today_ms, 'limit': 1000})
        if isinstance(data, list):
            for t in data:
                all_trades.append(t)
                pnl = float(t['realizedPnl'])
                if pnl != 0:
                    ts = int(t.get('time', 0)) / 1000
                    dt = datetime.datetime.utcfromtimestamp(ts).strftime('%m-%d %H:%M:%S')
                    print(f"{t['symbol']:<16} {str(t['orderId']):<18} {t['side']:<6} "
                          f"{float(t['qty']):>10.4f} {float(t['price']):>12.6f} {pnl:>12.4f}  {dt}")
        elif isinstance(data, dict) and 'code' in data:
            print(f"  {sym}: ERROR {data}")

    nonzero = [t for t in all_trades if float(t['realizedPnl']) != 0]
    binance_today_pnl = sum(float(t['realizedPnl']) for t in nonzero)
    print(f"\n币安今日有 realizedPnl 的成交: {len(nonzero)} 笔")
    print(f"币安今日总 realizedPnl: {binance_today_pnl:.4f} USDT")

    # ── 2. 读取本地数据库 ────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【本地 trade_records（今日 UTC 2026-05-03）】")
    print("=" * 100)

    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'trading_state.db',
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    local_today = conn.execute("""
        SELECT tr.*, tsk.sync_key
        FROM trade_records tr
        LEFT JOIN trade_sync_keys tsk ON tsk.trade_record_id = tr.id
        WHERE tr.closed_at >= '2026-05-03T00:00:00'
        ORDER BY tr.closed_at
    """).fetchall()

    print(f"{'symbol':<16} {'strategy_tag':<28} {'pnl':>10}  {'closed_at':<32}  {'sync_key'}")
    print("-" * 100)
    for r in local_today:
        print(f"{r['symbol']:<16} {r['strategy_tag']:<28} {r['pnl_amount']:>10.4f}  "
              f"{r['closed_at']:<32}  {r['sync_key'] or 'N/A'}")

    local_today_pnl = sum(r['pnl_amount'] for r in local_today)
    print(f"\n本地今日记录: {len(local_today)} 条，总盈亏: {local_today_pnl:.4f} USDT")

    # ── 3. 全量本地 sync_key 中的 orderId ───────────────────────────────
    all_local_keys = conn.execute(
        "SELECT sync_key FROM trade_sync_keys WHERE sync_key LIKE 'binance_user_order:%'"
    ).fetchall()
    local_order_ids = set()
    for r in all_local_keys:
        parts = r['sync_key'].split(':')
        if len(parts) == 3:
            local_order_ids.add(parts[2])

    # ── 4. 差异分析 ──────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【差异分析】")
    print("=" * 100)

    binance_order_ids = set(str(t['orderId']) for t in nonzero)
    missing_in_local = binance_order_ids - local_order_ids

    print(f"币安今日有 pnl 的 orderId ({len(binance_order_ids)} 个): {binance_order_ids}")
    print(f"本地已同步的所有 orderId ({len(local_order_ids)} 个): {local_order_ids}")
    print(f"\n⚠️  币安有但本地未落库的 orderId ({len(missing_in_local)} 个): {missing_in_local}")

    if missing_in_local:
        missing_pnl = 0.0
        print("\n【未落库成交详情】")
        print(f"{'symbol':<16} {'orderId':<18} {'side':<6} {'qty':>10} {'realizedPnl':>12}  {'time(UTC)'}")
        print("-" * 80)
        for t in nonzero:
            if str(t['orderId']) in missing_in_local:
                ts = int(t.get('time', 0)) / 1000
                dt = datetime.datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                pnl = float(t['realizedPnl'])
                missing_pnl += pnl
                print(f"{t['symbol']:<16} {str(t['orderId']):<18} {t['side']:<6} "
                      f"{float(t['qty']):>10.4f} {pnl:>12.4f}  {dt}")
        print(f"\n未落库成交合计 pnl: {missing_pnl:.4f} USDT")

    # ── 5. strategy_tag 问题：unknown 的记录 ────────────────────────────
    print("\n" + "=" * 100)
    print("【strategy_tag = unknown 的记录（全量）】")
    print("=" * 100)
    unknowns = conn.execute("""
        SELECT tr.*, tsk.sync_key
        FROM trade_records tr
        LEFT JOIN trade_sync_keys tsk ON tsk.trade_record_id = tr.id
        WHERE tr.strategy_tag = 'unknown' OR tr.strategy_tag = 'crypto_generic'
        ORDER BY tr.closed_at DESC
    """).fetchall()
    print(f"{'id':>4} {'symbol':<14} {'strategy_tag':<20} {'pnl':>10}  {'closed_at':<32}  {'sync_key'}")
    print("-" * 100)
    for r in unknowns:
        print(f"{r['id']:>4} {r['symbol']:<14} {r['strategy_tag']:<20} {r['pnl_amount']:>10.4f}  "
              f"{r['closed_at']:<32}  {r['sync_key'] or 'N/A'}")

    # ── 6. 汇总对比 ──────────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("【汇总对比】")
    print("=" * 100)
    print(f"  币安今日 realizedPnl 合计:  {binance_today_pnl:>10.4f} USDT")
    print(f"  本地今日 pnl_amount 合计:   {local_today_pnl:>10.4f} USDT")
    print(f"  差异（币安 - 本地）:        {binance_today_pnl - local_today_pnl:>10.4f} USDT")

    conn.close()


if __name__ == '__main__':
    main()
