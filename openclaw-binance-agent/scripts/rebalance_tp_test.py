#!/usr/bin/env python3
"""
方案三时间衰减止盈重挂脚本（dry-run 模式）

根据当前持仓时长，计算需要下调的止盈价，
先 dry-run 打印计划，确认后再执行真实操作。
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k] = v

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter
from src.skills.skill4_execute import Skill4Execute
from src.models.types import TradeDirection
from src.infra.exchange_rules import parse_symbol_trading_rule, round_price_to_tick
from decimal import Decimal

DRY_RUN = False         # True=只打印计划，False=真实执行
MAX_HOLD_HOURS = 24.0   # 与系统默认一致

client = BinanceFapiClient(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET"),
    rate_limiter=RateLimiter()
)

# 预加载交易规则（用于价格精度规整）
_exchange_raw = client._request_with_retry('GET', '/fapi/v1/exchangeInfo', {})
_tick_map: dict[str, Decimal] = {}
for _s in _exchange_raw.get('symbols', []):
    _rule = parse_symbol_trading_rule(_s)
    if _rule and _rule.tick_size > 0:
        _tick_map[_s['symbol']] = _rule.tick_size

def normalize_price(symbol: str, price: float) -> float:
    """按 tickSize 规整价格，避免 Binance -1111 精度错误。"""
    tick = _tick_map.get(symbol)
    if tick:
        return float(round_price_to_tick(price, tick))
    return price

now = datetime.now(timezone.utc)
now_ms = now.timestamp() * 1000
max_hold_seconds = MAX_HOLD_HOURS * 3600

print(f"{'='*60}")
print(f"方案三时间衰减止盈重挂  {'[DRY-RUN]' if DRY_RUN else '[真实执行]'}")
print(f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"{'='*60}\n")

# 获取持仓
positions = client.get_positions()
# 获取所有条件单
algo_orders = client.get_open_algo_orders()

actions = []  # 收集需要执行的操作

for pos in positions:
    sym = pos.symbol
    raw = getattr(pos, 'raw', {}) or {}
    update_ms = float(raw.get('updateTime') or 0)
    mark = float(raw.get('markPrice') or 0)
    entry = pos.entry_price
    qty = abs(pos.position_amt)

    if not update_ms or entry <= 0 or qty <= 0:
        continue

    elapsed_s = (now_ms - update_ms) / 1000
    elapsed_h = elapsed_s / 3600
    ratio = elapsed_s / max_hold_seconds

    # 找该持仓的止盈单（触发价高于入场价的 SELL 单）
    tp_orders = [
        o for o in algo_orders
        if o.get('symbol') == sym
        and str(o.get('side', '')).upper() == 'SELL'
        and Skill4Execute._is_take_profit_order(o, 'SELL', entry, TradeDirection.LONG)
    ]

    if not tp_orders:
        print(f"⚠️  {sym}: 未找到止盈单，跳过")
        continue

    # 取触发价最高的那张作为当前止盈单
    current_tp_order = max(tp_orders, key=lambda o: float(o.get('triggerPrice') or 0))
    current_tp_price = float(current_tp_order.get('triggerPrice') or 0)
    original_tp_price = current_tp_price  # 当前止盈即为原始止盈（未衰减过）

    # 计算新止盈价
    new_tp, new_step = Skill4Execute._calc_time_decay_tp(
        direction=TradeDirection.LONG,
        entry_price=entry,
        original_tp_price=original_tp_price,
        current_tp_price=current_tp_price,
        elapsed=elapsed_s,
        max_hold_seconds=max_hold_seconds,
        tp_decay_step=0,
        current_price=mark,
    )

    tp_dist_pct = (current_tp_price - entry) / entry * 100
    pnl_pct = (mark - entry) / entry * 100 if entry > 0 else 0

    print(f"{'─'*50}")
    print(f"  {sym}")
    print(f"  持仓时长: {elapsed_h:.1f}h / {MAX_HOLD_HOURS}h ({ratio*100:.0f}%)")
    print(f"  入场价:   {entry:.8g}")
    print(f"  当前价:   {mark:.8g}  浮盈: {pnl_pct:+.2f}%")
    print(f"  当前止盈: {current_tp_price:.8g}  (距入场 +{tp_dist_pct:.2f}%)")

    if new_tp is None:
        if ratio < 0.5:
            print(f"  方案三:   ⏳ 未触发（持仓时长不足50%）")
        else:
            print(f"  方案三:   ⚠️  安全校验拒绝（新止盈价 ≤ 当前价，不重挂）")
    else:
        new_tp_dist_pct = (new_tp - entry) / entry * 100
        decay_label = "step2 -40%" if new_step == 2 else "step1 -20%"
        print(f"  方案三:   ✅ {decay_label} → 新止盈 {new_tp:.8g}  (距入场 +{new_tp_dist_pct:.2f}%)")
        actions.append({
            'symbol': sym,
            'qty': qty,
            'old_tp': current_tp_price,
            'new_tp': normalize_price(sym, new_tp),
            'algo_id': current_tp_order.get('algoId'),
            'step': new_step,
        })
    print()

print(f"{'='*60}")
print(f"汇总: 共 {len(actions)} 个止盈单需要重挂")
print()

if not actions:
    print("无需操作。")
    sys.exit(0)

for a in actions:
    print(f"  {a['symbol']}: 撤 algoId={a['algo_id']}  旧止盈={a['old_tp']:.8g} → 新止盈={a['new_tp']:.8g}")

print()

if DRY_RUN:
    print(">>> DRY-RUN 模式，未执行任何真实操作。")
    print(">>> 确认无误后将 DRY_RUN = False 再执行。")
else:
    print(">>> 开始执行真实重挂...")
    success_count = 0
    for a in actions:
        sym = a['symbol']
        try:
            # 1. 撤旧止盈单
            if a['algo_id']:
                client.cancel_algo_order(symbol=sym, algo_id=int(a['algo_id']))
                print(f"  ✅ {sym}: 已撤旧止盈单 algoId={a['algo_id']}")

            # 2. 挂新止盈单
            result = client.place_take_profit_market_order(
                symbol=sym,
                side='SELL',
                quantity=a['qty'],
                stop_price=a['new_tp'],
                close_position=True,
            )
            print(f"  ✅ {sym}: 已挂新止盈单 triggerPrice={a['new_tp']:.8g}  algoId={result.order_id}")
            success_count += 1
        except Exception as exc:
            print(f"  ❌ {sym}: 操作失败 - {exc}")
            # 尝试恢复原止盈单
            try:
                result = client.place_take_profit_market_order(
                    symbol=sym,
                    side='SELL',
                    quantity=a['qty'],
                    stop_price=a['old_tp'],
                    close_position=True,
                )
                print(f"  ↩️  {sym}: 已恢复原止盈单 triggerPrice={a['old_tp']:.8g}")
            except Exception as exc2:
                print(f"  🚨 {sym}: 恢复原止盈单也失败！{exc2}  请手动检查！")

    print(f"\n完成: {success_count}/{len(actions)} 成功")
