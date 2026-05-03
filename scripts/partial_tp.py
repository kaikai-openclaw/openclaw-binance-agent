#!/usr/bin/env python3
"""
分批止盈脚本 — 方案四（独立运行，建议 5 分钟 cron）

每次执行：
1. 扫描所有活跃持仓
2. 浮盈 ≥ 止损距离 × 1.0 时，市价平掉 50% 仓位锁利，剩余继续持有
3. 输出 Markdown 格式报告（供 Telegram 推送）

幂等保护：
- 与 manage_positions.py 共享状态文件 manage_positions_state.json
- partial_tp_done=True 标记防止重复执行
- 持仓平仓后自动清理对应状态

进程锁：
- 独立锁文件 partial_tp.lock，与 manage_positions.lock 互不干扰
- 两个脚本可以安全并发运行（操作不同的订单类型）
"""
import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                v = v.strip().strip('"').strip("'")
                os.environ[k] = v

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter
from src.infra.exchange_rules import parse_symbol_trading_rule, normalize_order_quantity
from src.skills.skill4_execute import Skill4Execute
from src.models.types import TradeDirection

# 与 manage_positions.py 共享同一状态文件，partial_tp_done 标记互通
STATE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manage_positions_state.json')
LOCK_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'partial_tp.lock')
# 写状态时需同时持有此锁，防止与 manage_positions.py 并发写入状态文件产生竞态
SHARED_STATE_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manage_positions_state.lock')

_rule_map: dict = {}


def _direction_of(position_amt: float) -> TradeDirection:
    return TradeDirection.LONG if position_amt > 0 else TradeDirection.SHORT


def _close_side_of(direction: TradeDirection) -> str:
    return 'SELL' if direction == TradeDirection.LONG else 'BUY'


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    """原子写入，防止进程中断导致状态文件损坏。"""
    state_dir = os.path.dirname(STATE_FILE)
    os.makedirs(state_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode='w', dir=state_dir, suffix='.tmp', delete=False
    ) as tmp:
        json.dump(state, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, STATE_FILE)


# ── 初始化 ────────────────────────────────────────────────
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
if not api_key or not api_secret:
    print("🚨 BINANCE_API_KEY / BINANCE_API_SECRET 未配置，退出")
    sys.exit(1)

_lock_fh = open(LOCK_FILE, 'w')
try:
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("⚠️ 上一次执行尚未完成，本次跳过（进程锁已被占用）")
    _lock_fh.close()
    sys.exit(0)

client = BinanceFapiClient(
    api_key=api_key,
    api_secret=api_secret,
    rate_limiter=RateLimiter()
)

try:
    # 预加载数量精度规则（分批止盈需要规整 qty）
    # 注意：exchangeInfo 在获取共享状态锁之前加载，避免长时间持锁。
    try:
        _exchange_raw = client._request_with_retry('GET', '/fapi/v1/exchangeInfo', {})
    except Exception as _exc:
        print(f"🚨 获取 exchangeInfo 失败，退出: {_exc}")
        sys.exit(1)

    for _s in _exchange_raw.get('symbols', []):
        _rule = parse_symbol_trading_rule(_s)
        if _rule:
            _rule_map[_s['symbol']] = _rule

    state = load_state()
    positions = client.get_positions()
    algo_orders = client.get_open_algo_orders()

    # 清理已平仓 symbol 的状态
    active_symbols = {pos.symbol for pos in positions if abs(pos.position_amt) > 0}
    for sym in list(state.keys()):
        if sym not in active_symbols:
            del state[sym]

    partial_tp_actions = []
    skipped = []

    for pos in positions:
        sym = pos.symbol

        if abs(pos.position_amt) == 0:
            continue

        raw = getattr(pos, 'raw', {}) or {}
        mark = float(raw.get('markPrice') or 0)
        entry = pos.entry_price
        qty = abs(pos.position_amt)

        if entry <= 0 or qty <= 0 or mark <= 0:
            skipped.append(f"{sym}: 数据不完整，跳过")
            continue

        direction = _direction_of(pos.position_amt)
        close_side = _close_side_of(direction)
        dir_label = '多' if direction == TradeDirection.LONG else '空'

        sym_state = state.get(sym, {})

        # ── 方向反转检测 ──────────────────────────────────────
        # 与 manage_positions.py 保持一致：方向变化时重置 partial_tp_done
        saved_direction = sym_state.get('direction')
        current_direction_str = 'LONG' if direction == TradeDirection.LONG else 'SHORT'
        if saved_direction is not None and saved_direction != current_direction_str:
            sym_state = {}
            state[sym] = {}

        # 已执行过分批止盈，跳过
        if sym_state.get('partial_tp_done', False):
            continue

        sym_rule = _rule_map.get(sym)
        if sym_rule is None:
            skipped.append(f"{sym}({dir_label}): 未找到交易规则，跳过")
            continue

        # 找止损单，计算 sl_dist
        sl_orders = [
            o for o in algo_orders
            if o.get('symbol') == sym
            and Skill4Execute._is_stop_loss_order(o, close_side, entry, direction)
        ]
        if not sl_orders:
            skipped.append(f"{sym}({dir_label}): 无止损单，无法计算 sl_dist，跳过")
            continue

        if direction == TradeDirection.LONG:
            sl_order = min(sl_orders, key=lambda o: float(o.get('triggerPrice') or 0))
        else:
            sl_order = max(sl_orders, key=lambda o: float(o.get('triggerPrice') or 0))

        sl_price = float(sl_order.get('triggerPrice') or 0)
        if sl_price <= 0:
            skipped.append(f"{sym}({dir_label}): 止损触发价无效，跳过")
            continue

        sl_dist = abs(entry - sl_price)
        if sl_dist == 0:
            skipped.append(f"{sym}({dir_label}): sl_dist=0（止损已在保本位），跳过")
            continue

        profit = (mark - entry) if direction == TradeDirection.LONG else (entry - mark)
        pnl_pct = profit / entry * 100
        ratio = profit / sl_dist

        if ratio < 1.0:
            # 未触发，不记录 skipped（避免每5分钟刷屏）
            continue

        # 规整 50% 数量
        raw_partial = qty / 2.0
        normed_partial = normalize_order_quantity(
            symbol=sym, quantity=raw_partial, price=mark, rule=sym_rule,
        )
        if normed_partial is None or normed_partial <= 0:
            skipped.append(
                f"{sym}({dir_label}): 分批止盈跳过，"
                f"50%数量 {raw_partial:.8g} 规整后不满足交易所约束"
            )
            continue

        # 确保剩余仓位不会成为无法平仓的碎仓
        remaining = qty - normed_partial
        remaining_ok = (
            remaining <= 0
            or normalize_order_quantity(
                symbol=sym, quantity=remaining, price=mark, rule=sym_rule,
            ) is not None
        )
        if not remaining_ok:
            skipped.append(
                f"{sym}({dir_label}): 分批止盈跳过，"
                f"剩余仓位 {remaining:.8g} 低于最小交易量"
            )
            continue

        partial_tp_actions.append({
            'symbol': sym,
            'partial_qty': float(normed_partial),
            'close_side': close_side,
            'dir_label': dir_label,
            'pnl_pct': pnl_pct,
            'ratio': ratio,
            'direction_str': current_direction_str,
        })

    # ── 执行分批止盈 ──────────────────────────────────────────
    # 无触发时静默退出，不推送 Telegram（避免每5分钟刷屏）
    if not partial_tp_actions:
        if skipped:
            # 有跳过记录时才输出，方便排查
            lines = [f"## 分批止盈 {datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')} (无触发)"]
            lines.append(f"\n_跳过 {len(skipped)} 个持仓:_")
            lines.extend(f"  - {s}" for s in skipped)
            print("\n".join(lines))
        sys.exit(0)

    # 获取共享状态锁（非阻塞）：
    # manage_positions.py 正在写状态时持有此锁，本次跳过，等下次5分钟再跑。
    # 这防止两个脚本并发 read-modify-write 状态文件导致 partial_tp_done 被覆盖。
    _state_lock_fh = open(SHARED_STATE_LOCK_FILE, 'w')
    try:
        fcntl.flock(_state_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️ manage_positions.py 正在写入状态，本次分批止盈跳过（状态锁被占用），等待下次执行")
        _state_lock_fh.close()
        sys.exit(0)

    now = datetime.now(timezone.utc)
    results = []
    try:
        # 重新读取状态，获取锁后再次读取以拿到最新值，防止锁等待期间状态被更新
        state = load_state()
        # 清理已平仓 symbol（锁内再次清理，保持一致性）
        for sym in list(state.keys()):
            if sym not in active_symbols:
                del state[sym]

        for a in partial_tp_actions:
            sym = a['symbol']
            # 二次检查：获取锁后再次确认 partial_tp_done，
            # 防止锁等待期间 manage_positions.py 已写入 partial_tp_done=True
            if state.get(sym, {}).get('partial_tp_done', False):
                results.append(f"ℹ️ **{sym}**({a['dir_label']}) 分批止盈已由其他进程执行，跳过")
                continue
            try:
                client.place_market_order(
                    symbol=sym,
                    side=a['close_side'],
                    quantity=a['partial_qty'],
                )
                # 写入 partial_tp_done，与 manage_positions.py 共享状态
                state[sym] = {
                    **state.get(sym, {}),
                    'partial_tp_done': True,
                    'direction': a['direction_str'],
                }
                results.append(
                    f"✅ **{sym}**({a['dir_label']}) 分批止盈(50%): "
                    f"市价平仓 {a['partial_qty']} 手 "
                    f"(浮盈{a['pnl_pct']:+.2f}%, {a['ratio']:.2f}x sl_dist)"
                )
            except Exception as exc:
                results.append(f"⚠️ **{sym}**({a['dir_label']}) 分批止盈失败: {exc}")

        save_state(state)
    finally:
        fcntl.flock(_state_lock_fh, fcntl.LOCK_UN)
        _state_lock_fh.close()

    # ── 输出报告 ──────────────────────────────────────────────
    lines = [f"## 分批止盈 {now.strftime('%m-%d %H:%M UTC')}"]
    lines.extend(results)
    if skipped:
        lines.append(f"\n_跳过 {len(skipped)} 个持仓:_")
        lines.extend(f"  - {s}" for s in skipped)
    print("\n".join(lines))

finally:
    fcntl.flock(_lock_fh, fcntl.LOCK_UN)
    _lock_fh.close()
