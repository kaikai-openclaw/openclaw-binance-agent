#!/usr/bin/env python3
"""
1. 预览 / 执行：删除 2026-05-03 CST 09:00（UTC 01:00）之前的 trade_records
2. 清理孤立 sync_key
3. 对比今日 CST 09:00 后的币安成交 vs 本地记录
用法:
    python3 cleanup_and_check.py          # 仅预览，不修改
    python3 cleanup_and_check.py --apply  # 执行删除
"""
import os, sys, time, hmac, hashlib, datetime
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

import requests
import sqlite3

APPLY = '--apply' in sys.argv

api_key = os.environ.get('BINANCE_API_KEY', '')
api_secret = os.environ.get('BINANCE_API_SECRET', '')

# CST 09:00 = UTC 01:00
CUTOFF_UTC = '2026-05-03T01:00:00'
CUTOFF_MS  = 1777741200000  # 2026-05-03 01:00:00 UTC in ms

db_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'trading_state.db',
)


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


def fmt_ts(ms):
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime('%m-%d %H:%M:%S')


def main():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── 1. 预览要删除的记录 ──────────────────────────────────────────────
    to_delete = conn.execute("""
        SELECT tr.id, tr.symbol, tr.strategy_tag, tr.pnl_amount, tr.closed_at
        FROM trade_records tr
        WHERE tr.closed_at < ?
        ORDER BY tr.closed_at
    """, (CUTOFF_UTC,)).fetchall()

    to_keep = conn.execute("""
        SELECT tr.id, tr.symbol, tr.strategy_tag, tr.pnl_amount, tr.closed_at, tsk.sync_key
        FROM trade_records tr
        LEFT JOIN trade_sync_keys tsk ON tsk.trade_record_id = tr.id
        WHERE tr.closed_at >= ?
        ORDER BY tr.closed_at
    """, (CUTOFF_UTC,)).fetchall()

    orphan_keys = conn.execute("""
        SELECT tsk.sync_key, tsk.trade_record_id
        FROM trade_sync_keys tsk
        LEFT JOIN trade_records tr ON tr.id = tsk.trade_record_id
        WHERE tr.id IS NULL
    """).fetchall()

    print("=" * 90)
    print(f"【删除范围：closed_at < {CUTOFF_UTC} UTC（CST 09:00 之前）】")
    print("=" * 90)
    for r in to_delete:
        print(f"  id={r['id']:>3}  {r['symbol']:<14} {r['strategy_tag']:<28} pnl={r['pnl_amount']:>8.4f}  {r['closed_at'][:19]}")
    print(f"\n  共 {len(to_delete)} 条 trade_records 将被删除")
    print(f"  孤立 sync_key 将被清理: {len(orphan_keys)} 条")

    print("\n" + "=" * 90)
    print(f"【保留范围：closed_at >= {CUTOFF_UTC} UTC（CST 09:00 之后）】")
    print("=" * 90)
    for r in to_keep:
        print(f"  id={r['id']:>3}  {r['symbol']:<14} {r['strategy_tag']:<28} pnl={r['pnl_amount']:>8.4f}  {r['closed_at'][:19]}")
    print(f"\n  共 {len(to_keep)} 条保留，总 pnl = {sum(r['pnl_amount'] for r in to_keep):.4f} USDT")

    # ── 2. 执行删除 ──────────────────────────────────────────────────────
    if APPLY:
        delete_ids = [r['id'] for r in to_delete]
        if delete_ids:
            placeholders = ','.join('?' * len(delete_ids))
            # 先删 sync_keys（外键关联）
            conn.execute(f"DELETE FROM trade_sync_keys WHERE trade_record_id IN ({placeholders})", delete_ids)
            # 再删 trade_records
            conn.execute(f"DELETE FROM trade_records WHERE id IN ({placeholders})", delete_ids)
        # 清理孤立 sync_key
        conn.execute("""
            DELETE FROM trade_sync_keys
            WHERE trade_record_id NOT IN (SELECT id FROM trade_records)
        """)
        conn.commit()
        print(f"\n✅ 已删除 {len(delete_ids)} 条 trade_records 及关联 sync_keys")
        print(f"✅ 已清理孤立 sync_keys")
    else:
        print(f"\n⚠️  预览模式，未修改数据库。加 --apply 参数执行删除。")
        conn.close()
        return

    # ── 3. 删除后：对比币安今日 CST 09:00 后的成交 ──────────────────────
    print("\n" + "=" * 90)
    print("【币安今日 CST 09:00（UTC 01:00）后的成交（realizedPnl != 0）】")
    print("=" * 90)

    # 重新读取保留的记录
    kept = conn.execute("""
        SELECT tr.*, tsk.sync_key
        FROM trade_records tr
        LEFT JOIN trade_sync_keys tsk ON tsk.trade_record_id = tr.id
        ORDER BY tr.closed_at
    """).fetchall()

    local_order_ids = set()
    for r in kept:
        if r['sync_key']:
            parts = r['sync_key'].split(':')
            if len(parts) == 3:
                local_order_ids.add(parts[2])

    # 查询币安
    symbols = sorted(set(r['symbol'] for r in kept) | {'KAVAUSDT', 'ATUSDT', 'WLDUSDT', 'SWARMSUSDT', 'TRXUSDT', 'ALGOUSDT'})
    all_trades = []
    for sym in symbols:
        data = signed_get('/fapi/v1/userTrades', {'symbol': sym, 'startTime': CUTOFF_MS, 'limit': 1000})
        if isinstance(data, list):
            all_trades.extend(data)
        elif isinstance(data, dict) and 'code' in data:
            print(f"  {sym}: ERROR {data}")

    nonzero = [t for t in all_trades if float(t['realizedPnl']) != 0]

    # 按 orderId 聚合
    order_agg = {}
    for t in nonzero:
        oid = str(t['orderId'])
        if oid not in order_agg:
            order_agg[oid] = {'symbol': t['symbol'], 'orderId': oid, 'side': t['side'],
                               'qty': 0.0, 'pnl': 0.0, 'time': int(t['time'])}
        order_agg[oid]['qty'] += float(t['qty'])
        order_agg[oid]['pnl'] += float(t['realizedPnl'])
        order_agg[oid]['time'] = max(order_agg[oid]['time'], int(t['time']))

    print(f"{'symbol':<16} {'orderId':<18} {'side':<6} {'pnl':>12}  {'time(UTC)'}  {'本地状态'}")
    print("-" * 80)
    missing = []
    for oid, agg in sorted(order_agg.items(), key=lambda x: x[1]['time']):
        in_local = oid in local_order_ids
        status = "✅ 已落库" if in_local else "❌ 未落库"
        if not in_local:
            missing.append(agg)
        print(f"{agg['symbol']:<16} {oid:<18} {agg['side']:<6} {agg['pnl']:>12.4f}  {fmt_ts(agg['time'])}  {status}")

    binance_pnl = sum(a['pnl'] for a in order_agg.values())
    local_pnl = sum(r['pnl_amount'] for r in kept)

    print(f"\n币安 CST 09:00 后有 pnl 的 order: {len(order_agg)} 个，总 pnl = {binance_pnl:.4f} USDT")
    print(f"本地保留记录: {len(kept)} 条，总 pnl = {local_pnl:.4f} USDT")

    if missing:
        print(f"\n⚠️  未落库 {len(missing)} 笔，合计 pnl = {sum(a['pnl'] for a in missing):.4f} USDT")
        for a in missing:
            print(f"  {a['symbol']:<14} orderId={a['orderId']}  side={a['side']}  pnl={a['pnl']:.4f}  {fmt_ts(a['time'])} UTC")
    else:
        print("\n✅ 币安成交与本地记录完全一致，无漏记")

    # ── 4. 按策略汇总 ────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("【按策略汇总（清理后）】")
    print("=" * 90)
    rows = conn.execute("""
        SELECT strategy_tag,
               COUNT(*) as cnt,
               SUM(pnl_amount) as total_pnl,
               SUM(CASE WHEN pnl_amount > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_amount < 0 THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN pnl_amount = 0 THEN 1 ELSE 0 END) as zeros
        FROM trade_records
        GROUP BY strategy_tag
        ORDER BY total_pnl DESC
    """).fetchall()
    print(f"{'strategy_tag':<30} {'笔数':>5} {'总盈亏':>10} {'胜':>4} {'负':>4} {'零':>4}")
    print("-" * 60)
    for r in rows:
        print(f"{r['strategy_tag']:<30} {r['cnt']:>5} {r['total_pnl']:>10.4f} {r['wins']:>4} {r['losses']:>4} {r['zeros']:>4}")

    conn.close()


if __name__ == '__main__':
    main()
