#!/usr/bin/env python3
"""
全面检查币安成交记录与本地 trade_records 的差异（过去 72 小时）。
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


def fmt_ts(ms):
    return datetime.datetime.utcfromtimestamp(int(ms) / 1000).strftime('%m-%d %H:%M:%S')


def main():
    # 过去 72 小时（与 trade_sync lookback 一致）
    lookback_ms = int((time.time() - 72 * 3600) * 1000)
    lookback_dt = datetime.datetime.utcfromtimestamp(lookback_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')
    print(f"查询范围: {lookback_dt} UTC 至今（72小时）\n")

    # 本地数据库
    db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'trading_state.db',
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 本地所有已同步的 orderId
    all_local_keys = conn.execute(
        "SELECT sync_key, trade_record_id FROM trade_sync_keys WHERE sync_key LIKE 'binance_user_order:%'"
    ).fetchall()
    local_order_id_to_record = {}
    for r in all_local_keys:
        parts = r['sync_key'].split(':')
        if len(parts) == 3:
            local_order_id_to_record[parts[2]] = r['trade_record_id']

    # 本地所有 trade_records（72小时内）
    local_records = conn.execute("""
        SELECT tr.*, tsk.sync_key
        FROM trade_records tr
        LEFT JOIN trade_sync_keys tsk ON tsk.trade_record_id = tr.id
        ORDER BY tr.closed_at DESC
    """).fetchall()

    # ── 1. 拉取币安 72 小时内所有相关币种成交 ──────────────────────────
    # 从本地记录 + 日报提到的币种
    symbols_from_local = set(r['symbol'] for r in local_records)
    symbols_extra = {'WLDUSDT', 'ALGOUSDT', 'SWARMSUSDT', 'TRXUSDT', 'ATUSDT', 'KAVAUSDT'}
    symbols = sorted(symbols_from_local | symbols_extra)

    print("=" * 110)
    print(f"【币安 72h 内成交（realizedPnl != 0）— 查询 {len(symbols)} 个币种】")
    print("=" * 110)
    print(f"{'symbol':<16} {'orderId':<18} {'side':<6} {'qty':>10} {'price':>12} {'realizedPnl':>12}  {'time(UTC)'}  {'本地状态'}")
    print("-" * 110)

    all_binance_trades = []
    for sym in symbols:
        data = signed_get('/fapi/v1/userTrades', {'symbol': sym, 'startTime': lookback_ms, 'limit': 1000})
        if isinstance(data, list):
            for t in data:
                all_binance_trades.append(t)
        elif isinstance(data, dict) and 'code' in data:
            print(f"  {sym}: ERROR {data}")

    nonzero = [t for t in all_binance_trades if float(t['realizedPnl']) != 0]

    # 按 orderId 聚合（一个 order 可能有多笔 fill）
    order_agg = {}
    for t in nonzero:
        oid = str(t['orderId'])
        if oid not in order_agg:
            order_agg[oid] = {
                'symbol': t['symbol'],
                'orderId': oid,
                'side': t['side'],
                'qty': 0.0,
                'pnl': 0.0,
                'time': int(t['time']),
            }
        order_agg[oid]['qty'] += float(t['qty'])
        order_agg[oid]['pnl'] += float(t['realizedPnl'])
        order_agg[oid]['time'] = max(order_agg[oid]['time'], int(t['time']))

    missing_orders = []
    for oid, agg in sorted(order_agg.items(), key=lambda x: x[1]['time']):
        in_local = oid in local_order_id_to_record
        status = "✅ 已落库" if in_local else "❌ 未落库"
        if not in_local:
            missing_orders.append(agg)
        print(f"{agg['symbol']:<16} {oid:<18} {agg['side']:<6} {agg['qty']:>10.4f} {'':>12} {agg['pnl']:>12.4f}  {fmt_ts(agg['time'])}  {status}")

    print(f"\n币安 72h 内有 pnl 的 order 共 {len(order_agg)} 个")
    print(f"其中未落库: {len(missing_orders)} 个")
    print(f"币安 72h 总 realizedPnl: {sum(a['pnl'] for a in order_agg.values()):.4f} USDT")

    # ── 2. 未落库详情 ────────────────────────────────────────────────────
    if missing_orders:
        print("\n" + "=" * 110)
        print("【❌ 未落库成交详情（币安有、本地没有）】")
        print("=" * 110)
        missing_pnl = 0.0
        for agg in sorted(missing_orders, key=lambda x: x['time']):
            missing_pnl += agg['pnl']
            print(f"  {agg['symbol']:<14} orderId={agg['orderId']:<18} side={agg['side']}  "
                  f"qty={agg['qty']:.4f}  pnl={agg['pnl']:.4f}  time={fmt_ts(agg['time'])} UTC")
        print(f"\n  未落库合计 pnl: {missing_pnl:.4f} USDT")

    # ── 3. strategy_tag 问题分析 ─────────────────────────────────────────
    print("\n" + "=" * 110)
    print("【strategy_tag 问题分析】")
    print("=" * 110)

    # unknown / crypto_generic 的记录
    bad_tag = [r for r in local_records if r['strategy_tag'] in ('unknown', 'crypto_generic')]
    print(f"\nunknown/crypto_generic 记录共 {len(bad_tag)} 条:")
    for r in bad_tag:
        oid = None
        if r['sync_key']:
            parts = r['sync_key'].split(':')
            if len(parts) == 3:
                oid = parts[2]
        print(f"  id={r['id']:>3}  {r['symbol']:<14} tag={r['strategy_tag']:<16} "
              f"pnl={r['pnl_amount']:>8.4f}  {r['closed_at'][:19]}  orderId={oid or 'N/A'}")

    # ── 4. 本地 vs 币安 pnl 对比（按 orderId 匹配） ──────────────────────
    print("\n" + "=" * 110)
    print("【本地 pnl vs 币安 pnl 对比（已落库的 order）】")
    print("=" * 110)
    print(f"{'symbol':<16} {'orderId':<18} {'本地pnl':>12} {'币安pnl':>12} {'差异':>10}  {'strategy_tag'}")
    print("-" * 90)

    pnl_mismatch = []
    for oid, agg in order_agg.items():
        if oid in local_order_id_to_record:
            rec_id = local_order_id_to_record[oid]
            rec = conn.execute("SELECT * FROM trade_records WHERE id=?", (rec_id,)).fetchone()
            if rec:
                diff = rec['pnl_amount'] - agg['pnl']
                flag = "⚠️ " if abs(diff) > 0.01 else "  "
                print(f"{flag}{agg['symbol']:<16} {oid:<18} {rec['pnl_amount']:>12.4f} {agg['pnl']:>12.4f} {diff:>10.4f}  {rec['strategy_tag']}")
                if abs(diff) > 0.01:
                    pnl_mismatch.append({'symbol': agg['symbol'], 'orderId': oid,
                                         'local': rec['pnl_amount'], 'binance': agg['pnl'], 'diff': diff})

    if pnl_mismatch:
        print(f"\n⚠️  pnl 不一致的记录: {len(pnl_mismatch)} 条")
    else:
        print("\n✅ 已落库记录的 pnl 与币安一致")

    # ── 5. 按策略汇总（含未落库影响） ────────────────────────────────────
    print("\n" + "=" * 110)
    print("【按策略汇总（本地数据库）】")
    print("=" * 110)
    strategy_rows = conn.execute("""
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
    print(f"{'strategy_tag':<30} {'笔数':>6} {'总盈亏':>10} {'胜':>4} {'负':>4} {'零':>4}")
    print("-" * 65)
    for r in strategy_rows:
        print(f"{r['strategy_tag']:<30} {r['cnt']:>6} {r['total_pnl']:>10.4f} {r['wins']:>4} {r['losses']:>4} {r['zeros']:>4}")

    conn.close()


if __name__ == '__main__':
    main()
