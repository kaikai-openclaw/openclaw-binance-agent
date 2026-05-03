#!/usr/bin/env python3
"""
持仓管理脚本 — 方案二（止损上移）+ 方案三（时间衰减止盈）

每次执行：
1. 扫描所有做多持仓
2. 方案二：浮盈达到止损距离阈值时，上移止损到保本/锁利
3. 方案三：持仓时间超过 50%/75% max_hold 时，下调止盈目标（幂等，不重复衰减）
4. 输出 Markdown 格式报告（供 Telegram 推送）

幂等保护：
- 用本地状态文件 manage_positions_state.json 记录每个持仓的原始止盈价和已衰减 step
- 每次执行前读取状态，避免重复衰减
- 持仓平仓后自动清理对应状态
"""
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

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
from src.infra.exchange_rules import parse_symbol_trading_rule, round_price_to_tick
from src.skills.skill4_execute import Skill4Execute
from src.models.types import TradeDirection

MAX_HOLD_HOURS = 24.0
STATE_FILE = os.path.join(os.path.dirname(__file__), 'manage_positions_state.json')

# ── 加载/保存本地状态 ─────────────────────────────────────
def load_state() -> dict:
    """读取持仓管理状态（原始止盈价、已衰减 step、止损 step）。"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict) -> None:
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# ── 初始化 ────────────────────────────────────────────────
client = BinanceFapiClient(
    api_key=os.getenv("BINANCE_API_KEY"),
    api_secret=os.getenv("BINANCE_API_SECRET"),
    rate_limiter=RateLimiter()
)

# 预加载价格精度规则
_exchange_raw = client._request_with_retry('GET', '/fapi/v1/exchangeInfo', {})
_tick_map: dict[str, Decimal] = {}
for _s in _exchange_raw.get('symbols', []):
    _rule = parse_symbol_trading_rule(_s)
    if _rule and _rule.tick_size > 0:
        _tick_map[_s['symbol']] = _rule.tick_size

def norm_price(symbol: str, price: float) -> float:
    tick = _tick_map.get(symbol)
    return float(round_price_to_tick(price, tick)) if tick else price

now = datetime.now(timezone.utc)
now_ms = now.timestamp() * 1000
max_hold_seconds = MAX_HOLD_HOURS * 3600

state = load_state()
positions = client.get_positions()
algo_orders = client.get_open_algo_orders()

# 当前持仓的 symbol 集合，用于清理已平仓的状态
active_symbols = {pos.symbol for pos in positions if abs(pos.position_amt) > 0}

# 清理已平仓持仓的状态
for sym in list(state.keys()):
    if sym not in active_symbols:
        del state[sym]

sl_actions = []
tp_actions = []
skipped = []

for pos in positions:
    sym = pos.symbol
    raw = getattr(pos, 'raw', {}) or {}
    update_ms = float(raw.get('updateTime') or 0)
    mark = float(raw.get('markPrice') or 0)
    entry = pos.entry_price
    qty = abs(pos.position_amt)

    if not update_ms or entry <= 0 or qty <= 0 or mark <= 0:
        skipped.append(f"{sym}: 数据不完整，跳过")
        continue

    # 只处理做多持仓
    if pos.position_amt < 0:
        skipped.append(f"{sym}: 做空持仓，暂不处理")
        continue

    elapsed_s = (now_ms - update_ms) / 1000
    elapsed_h = elapsed_s / 3600
    pnl_pct = (mark - entry) / entry * 100

    # 找止损单和止盈单
    sl_orders = [
        o for o in algo_orders
        if o.get('symbol') == sym
        and Skill4Execute._is_stop_loss_order(o, 'SELL', entry, TradeDirection.LONG)
    ]
    tp_orders = [
        o for o in algo_orders
        if o.get('symbol') == sym
        and Skill4Execute._is_take_profit_order(o, 'SELL', entry, TradeDirection.LONG)
    ]

    if not sl_orders or not tp_orders:
        skipped.append(f"{sym}: 缺少止损或止盈单（SL={len(sl_orders)}, TP={len(tp_orders)}），跳过")
        continue

    current_sl_order = min(sl_orders, key=lambda o: float(o.get('triggerPrice') or 0))
    current_tp_order = max(tp_orders, key=lambda o: float(o.get('triggerPrice') or 0))
    current_sl = float(current_sl_order.get('triggerPrice') or 0)
    current_tp = float(current_tp_order.get('triggerPrice') or 0)

    if current_sl <= 0 or current_tp <= 0:
        skipped.append(f"{sym}: 止损/止盈触发价无效，跳过")
        continue

    sl_dist = entry - current_sl

    # 读取或初始化本地状态
    sym_state = state.get(sym, {})
    # 原始止盈价：首次见到时记录当前止盈价作为基准
    original_tp = sym_state.get('original_tp', current_tp)
    tp_decay_step = sym_state.get('tp_decay_step', 0)
    sl_step = sym_state.get('sl_step', 0)

    # 如果当前止盈价高于记录的原始止盈价（说明止盈被外部调高了），更新基准
    if current_tp > original_tp:
        original_tp = current_tp
        tp_decay_step = 0

    # ── 方案二：止损上移 ──────────────────────────────────
    new_sl, new_sl_step = Skill4Execute._calc_breakeven_sl(
        direction=TradeDirection.LONG,
        entry_price=entry,
        current_price=mark,
        sl_dist=sl_dist,
        current_sl_price=current_sl,
        sl_step=sl_step,
    )
    if new_sl is not None:
        sl_actions.append({
            'symbol': sym, 'qty': qty,
            'old_sl': current_sl, 'new_sl': norm_price(sym, new_sl),
            'sl_algo_id': current_sl_order.get('algoId'),
            'sl_step': new_sl_step,
            'entry': entry, 'mark': mark, 'pnl_pct': pnl_pct,
            'elapsed_h': elapsed_h,
            'sym_state': sym_state, 'original_tp': original_tp,
            'tp_decay_step': tp_decay_step,
        })

    # ── 方案三：时间衰减止盈 ──────────────────────────────
    new_tp, new_tp_step = Skill4Execute._calc_time_decay_tp(
        direction=TradeDirection.LONG,
        entry_price=entry,
        original_tp_price=original_tp,
        current_tp_price=current_tp,
        elapsed=elapsed_s,
        max_hold_seconds=max_hold_seconds,
        tp_decay_step=tp_decay_step,
        current_price=mark,
    )
    if new_tp is not None:
        tp_actions.append({
            'symbol': sym, 'qty': qty,
            'old_tp': current_tp, 'new_tp': norm_price(sym, new_tp),
            'tp_algo_id': current_tp_order.get('algoId'),
            'tp_step': new_tp_step,
            'entry': entry, 'mark': mark, 'pnl_pct': pnl_pct,
            'elapsed_h': elapsed_h,
            'original_tp': original_tp,
            'sl_step': sl_step,
        })
    else:
        # 无需调整，但确保状态已记录
        state[sym] = {
            'original_tp': original_tp,
            'tp_decay_step': tp_decay_step,
            'sl_step': sl_step,
        }

# ── 执行方案二 ────────────────────────────────────────────
sl_results = []
for a in sl_actions:
    sym = a['symbol']
    step_label = {1: '保本', 2: '锁0.5x', 3: '锁1x'}.get(a['sl_step'], '?')
    try:
        if a['sl_algo_id']:
            client.cancel_algo_order(symbol=sym, algo_id=int(a['sl_algo_id']))
        client.place_stop_market_order(
            symbol=sym, side='SELL', quantity=a['qty'],
            stop_price=a['new_sl'], close_position=True,
        )
        # 更新状态
        state[sym] = {
            'original_tp': a['original_tp'],
            'tp_decay_step': a['tp_decay_step'],
            'sl_step': a['sl_step'],
        }
        sl_results.append(
            f"✅ **{sym}** 止损上移({step_label}): {a['old_sl']:.6g} → {a['new_sl']:.6g} "
            f"(浮盈{a['pnl_pct']:+.2f}%)"
        )
    except Exception as exc:
        try:
            client.place_stop_market_order(
                symbol=sym, side='SELL', quantity=a['qty'],
                stop_price=a['old_sl'], close_position=True,
            )
            sl_results.append(f"⚠️ **{sym}** 止损上移失败，已恢复原止损单: {exc}")
        except Exception as exc2:
            sl_results.append(f"🚨 **{sym}** 止损上移失败且恢复失败！请手动检查！{exc2}")

# ── 执行方案三 ────────────────────────────────────────────
tp_results = []
for a in tp_actions:
    sym = a['symbol']
    step_label = {1: '-20%', 2: '-40%'}.get(a['tp_step'], '?')
    try:
        if a['tp_algo_id']:
            client.cancel_algo_order(symbol=sym, algo_id=int(a['tp_algo_id']))
        client.place_take_profit_market_order(
            symbol=sym, side='SELL', quantity=a['qty'],
            stop_price=a['new_tp'], close_position=True,
        )
        # 更新状态
        state[sym] = {
            'original_tp': a['original_tp'],
            'tp_decay_step': a['tp_step'],
            'sl_step': a['sl_step'],
        }
        tp_results.append(
            f"✅ **{sym}** 止盈下调({step_label}): {a['old_tp']:.6g} → {a['new_tp']:.6g} "
            f"(持仓{a['elapsed_h']:.1f}h, 浮盈{a['pnl_pct']:+.2f}%)"
        )
    except Exception as exc:
        try:
            client.place_take_profit_market_order(
                symbol=sym, side='SELL', quantity=a['qty'],
                stop_price=a['old_tp'], close_position=True,
            )
            tp_results.append(f"⚠️ **{sym}** 止盈下调失败，已恢复原止盈单: {exc}")
        except Exception as exc2:
            tp_results.append(f"🚨 **{sym}** 止盈下调失败且恢复失败！请手动检查！{exc2}")

# ── 保存状态 ──────────────────────────────────────────────
save_state(state)

# ── 输出报告 ──────────────────────────────────────────────
lines = [f"## 持仓管理 {now.strftime('%m-%d %H:%M UTC')}"]

if sl_results:
    lines.append("\n**方案二 止损上移**")
    lines.extend(sl_results)

if tp_results:
    lines.append("\n**方案三 止盈下调**")
    lines.extend(tp_results)

if not sl_results and not tp_results:
    lines.append("\n无需调整（所有持仓均未触发阈值）")

if skipped:
    lines.append(f"\n_跳过 {len(skipped)} 个持仓_")

print("\n".join(lines))
