#!/usr/bin/env python3
"""
持仓管理脚本 — 方案二（止损上移）+ 方案三（时间衰减止盈）

每次执行：
1. 扫描所有活跃持仓（做多 + 做空）
2. 方案二：浮盈达到止损距离阈值时，上移止损到保本/锁利
3. 方案三：持仓时间超过 75% max_hold 时，下调止盈目标（幂等，不重复衰减；已取消50%门槛）
4. 输出 Markdown 格式报告（供 Telegram 推送）

注意：方案四（分批止盈）已拆分到独立脚本 partial_tp.py，建议 5 分钟 cron 执行。
两个脚本共享状态文件 manage_positions_state.json，通过 partial_tp_done 字段协调。

幂等保护：
- 用本地状态文件 manage_positions_state.json 记录每个持仓的原始止盈价和已衰减 step
- 每次执行前读取状态，避免重复衰减
- 持仓平仓后自动清理对应状态
"""

import fcntl
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

log = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                os.environ[k] = v

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter
from src.infra.exchange_rules import parse_symbol_trading_rule
from src.infra.state_store import StateStore
from src.skills.skill4_execute import Skill4Execute
from src.models.types import TradeDirection

MAX_HOLD_HOURS = 24.0
_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manage_positions_state.json"
)
LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manage_positions.lock"
)
# 状态文件共享锁：与 partial_tp.py 互斥，防止并发 read-modify-write 竞态
SHARED_STATE_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manage_positions_state.lock"
)


# ── 持仓方向辅助 ──────────────────────────────────────────
def _direction_of(position_amt: float) -> TradeDirection:
    return TradeDirection.LONG if position_amt > 0 else TradeDirection.SHORT


def _close_side_of(direction: TradeDirection) -> str:
    return "SELL" if direction == TradeDirection.LONG else "BUY"


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
    """原子写入：先写临时文件再 rename，防止进程中断导致状态文件损坏。"""
    state_dir = os.path.dirname(STATE_FILE)
    os.makedirs(state_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=state_dir, suffix=".tmp", delete=False
    ) as tmp:
        json.dump(state, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, STATE_FILE)


def _load_strategy_tags_from_state_store() -> dict[str, str]:
    """
    从 state_store.db 的 skill4_execute 快照中提取 symbol → strategy_tag 映射。

    遍历所有历史快照（旧到新），后写覆盖前写，确保每个 symbol 保留最近一次开仓的 strategy_tag。
    """
    tag_map: dict[str, str] = {}
    db_path = os.path.join(_DB_DIR, "state_store.db")
    if not os.path.exists(db_path):
        return tag_map
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT data FROM state_snapshots "
            "WHERE skill_name = 'skill4_execute' "
            "ORDER BY created_at ASC",
        ).fetchall()
        conn.close()
        for (raw,) in rows:
            data = json.loads(raw)
            for e in data.get("execution_results", []):
                sym = e.get("symbol", "")
                tag = e.get("strategy_tag", "") or ""
                if sym and tag and tag not in ("unknown", "crypto_generic"):
                    tag_map[sym] = tag
    except Exception:
        pass
    return tag_map


# ── 价格规整辅助 ──────────────────────────────────────────
_tick_map: dict = {}  # symbol → 价格精度 Decimal（tickSize）


def norm_price_floor(symbol: str, price: float) -> float:
    """向下取整到 tick（做多止损：规整后更低，离当前价更远，不会提前触发）。"""
    tick = _tick_map.get(symbol)
    if not tick:
        return price
    d = Decimal(str(price))
    return float((d / tick).to_integral_value(rounding=ROUND_DOWN) * tick)


def norm_price_ceil(symbol: str, price: float) -> float:
    """向上取整到 tick（做空止损：规整后更高，离当前价更远，不会提前触发）。"""
    tick = _tick_map.get(symbol)
    if not tick:
        return price
    d = Decimal(str(price))
    return float((d / tick).to_integral_value(rounding=ROUND_UP) * tick)


def norm_sl(symbol: str, price: float, direction: TradeDirection) -> float:
    """
    规整止损价，确保规整后止损不会比计划更靠近当前价（不提前触发）：
    - 做多止损在下方：向下取整（规整后更低，离当前价更远）
    - 做空止损在上方：向上取整（规整后更高，离当前价更远）
    执行前仍需二次校验确认规整后未倒退。
    """
    if direction == TradeDirection.LONG:
        return norm_price_floor(symbol, price)
    else:
        return norm_price_ceil(symbol, price)


def norm_tp(symbol: str, price: float, direction: TradeDirection) -> float:
    """
    规整止盈价，确保规整后止盈不会穿越当前价（避免 Binance -2021 拒单）：
    - 做多止盈在上方：向上取整（规整后更高，不会低于当前价）
    - 做空止盈在下方：向下取整（规整后更低，不会高于当前价）
    执行前仍需二次校验确认规整后未穿越 mark price。
    """
    if direction == TradeDirection.LONG:
        return norm_price_ceil(symbol, price)
    else:
        return norm_price_floor(symbol, price)


# ── 初始化 ────────────────────────────────────────────────
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
if not api_key or not api_secret:
    print("🚨 BINANCE_API_KEY / BINANCE_API_SECRET 未配置，退出")
    sys.exit(1)

# 进程锁：防止 cron 重叠触发时两个实例并发修改同一持仓的条件单。
# flock 非阻塞，第二个实例直接退出，不干扰第一个实例的撤单/挂单序列。
_lock_fh = open(LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("⚠️ 上一次执行尚未完成，本次跳过（进程锁已被占用）")
    _lock_fh.close()
    sys.exit(0)

client = BinanceFapiClient(
    api_key=api_key, api_secret=api_secret, rate_limiter=RateLimiter()
)

_state_lock_fh = None  # 初始化为 None，防止 finally 中 NameError
try:  # 锁保护区：确保任何异常都能释放文件锁
    # 预加载价格精度规则
    try:
        _exchange_raw = client._request_with_retry("GET", "/fapi/v1/exchangeInfo", {})
    except Exception as _exc:
        print(f"🚨 获取 exchangeInfo 失败，退出: {_exc}")
        sys.exit(1)

    for _s in _exchange_raw.get("symbols", []):
        _rule = parse_symbol_trading_rule(_s)
        if _rule and _rule.tick_size > 0:
            _tick_map[_s["symbol"]] = Decimal(str(_rule.tick_size))

    now = datetime.now(timezone.utc)
    now_ms = now.timestamp() * 1000
    max_hold_seconds = MAX_HOLD_HOURS * 3600

    positions = client.get_positions()
    algo_orders = client.get_open_algo_orders()
    active_symbols = {pos.symbol for pos in positions if abs(pos.position_amt) > 0}

    # ── 共享状态锁：仅保护 load_state → 扫描计算 → save_state 的窗口 ──
    # partial_tp.py 用非阻塞锁，获取不到直接跳过，不会被此处长时间阻塞。
    _state_lock_fh = open(SHARED_STATE_LOCK_FILE, "w")
    try:
        fcntl.flock(_state_lock_fh, fcntl.LOCK_EX)  # 阻塞等待（通常极短）
    except Exception:
        _state_lock_fh.close()
        _state_lock_fh = None
        raise
    try:
        state = load_state()
        _strategy_tag_map = _load_strategy_tags_from_state_store()

        for sym in list(state.keys()):
            if sym not in active_symbols:
                del state[sym]

        sl_actions = []
        tp_actions = []
        skipped = []

        for pos in positions:
            sym = pos.symbol

            if abs(pos.position_amt) == 0:
                continue

            raw = getattr(pos, "raw", {}) or {}
            update_ms = float(raw.get("updateTime") or 0)
            mark = float(raw.get("markPrice") or 0)
            entry = pos.entry_price
            qty = abs(pos.position_amt)

            if update_ms <= 0 or entry <= 0 or qty <= 0 or mark <= 0:
                skipped.append(f"{sym}: 数据不完整，跳过")
                continue

            direction = _direction_of(pos.position_amt)
            close_side = _close_side_of(direction)
            dir_label = "多" if direction == TradeDirection.LONG else "空"

            sym_state_pre = state.get(sym, {})
            open_ms = sym_state_pre.get("open_ms")

            # ── 方向反转检测：重置与方向相关的状态字段 ──────────────
            saved_direction = sym_state_pre.get("direction")
            current_direction_str = (
                "LONG" if direction == TradeDirection.LONG else "SHORT"
            )
            if saved_direction is not None and saved_direction != current_direction_str:
                sym_state_pre = {}
                state[sym] = {}
                open_ms = None

            if open_ms is None:
                # 首次见到该持仓（或方向刚反转）：用当前时间作为开仓时间起点。
                open_ms = now_ms
            else:
                try:
                    open_ms = float(open_ms)
                    if open_ms <= 0:
                        open_ms = now_ms
                except (TypeError, ValueError):
                    open_ms = now_ms

            # 确保 strategy_tag 存在：从 state_store.db 的 skill4 历史记录补充
            if not state.get(sym, {}).get("strategy_tag"):
                looked_up_tag = _strategy_tag_map.get(sym, "")
                if looked_up_tag:
                    state.setdefault(sym, {})["strategy_tag"] = looked_up_tag

            elapsed_s = max(0.0, (now_ms - open_ms) / 1000)
            elapsed_h = elapsed_s / 3600
            pnl_pct = (
                (mark - entry)
                / entry
                * 100
                * (1 if direction == TradeDirection.LONG else -1)
            )

            sl_orders = [
                o
                for o in algo_orders
                if o.get("symbol") == sym
                and Skill4Execute._is_stop_loss_order(o, close_side, entry, direction)
            ]
            tp_orders = [
                o
                for o in algo_orders
                if o.get("symbol") == sym
                and Skill4Execute._is_take_profit_order(o, close_side, entry, direction)
            ]

            if not sl_orders or not tp_orders:
                skipped.append(
                    f"{sym}({dir_label}): 缺少止损或止盈单（SL={len(sl_orders)}, TP={len(tp_orders)}），跳过"
                )
                continue

            if direction == TradeDirection.LONG:
                current_sl_order = min(
                    sl_orders, key=lambda o: float(o.get("triggerPrice") or 0)
                )
                current_tp_order = max(
                    tp_orders, key=lambda o: float(o.get("triggerPrice") or 0)
                )
            else:
                current_sl_order = max(
                    sl_orders, key=lambda o: float(o.get("triggerPrice") or 0)
                )
                current_tp_order = min(
                    tp_orders, key=lambda o: float(o.get("triggerPrice") or 0)
                )

            current_sl = float(current_sl_order.get("triggerPrice") or 0)
            current_tp = float(current_tp_order.get("triggerPrice") or 0)

            if current_sl <= 0 or current_tp <= 0:
                skipped.append(f"{sym}({dir_label}): 止损/止盈触发价无效，跳过")
                continue

            sl_dist = abs(entry - current_sl)

            sym_state = state.get(sym, {})
            try:
                original_tp = float(sym_state.get("original_tp", current_tp))
                if not (original_tp > 0):
                    original_tp = current_tp
            except (TypeError, ValueError):
                original_tp = current_tp
            try:
                tp_decay_step = max(0, min(2, int(sym_state.get("tp_decay_step", 0))))
            except (TypeError, ValueError):
                tp_decay_step = 0
            try:
                sl_step = max(0, min(3, int(sym_state.get("sl_step", 0))))
            except (TypeError, ValueError):
                sl_step = 0

            tp_improved = (
                current_tp > original_tp
                if direction == TradeDirection.LONG
                else current_tp < original_tp
            )
            if tp_improved:
                original_tp = current_tp
                tp_decay_step = 0
                # 立即持久化重置，防止后续方案三安全校验失败时状态丢失
                state[sym] = {
                    **state.get(sym, {}),
                    "original_tp": original_tp,
                    "tp_decay_step": 0,
                    "sl_step": sl_step,
                    "open_ms": open_ms,
                    "direction": current_direction_str,
                }

            # ── 方案二：止损上移 ──────────────────────────────────
            # sl_dist==0 时止损已在保本位，无法继续上移，跳过。
            if sl_dist > 0:
                new_sl, new_sl_step = Skill4Execute._calc_breakeven_sl(
                    direction=direction,
                    entry_price=entry,
                    current_price=mark,
                    sl_dist=sl_dist,
                    current_sl_price=current_sl,
                    sl_step=sl_step,
                )
                if new_sl is not None:
                    normed_sl = norm_sl(sym, new_sl, direction)
                    sl_moved = (
                        normed_sl > current_sl
                        if direction == TradeDirection.LONG
                        else normed_sl < current_sl
                    )
                    if not sl_moved:
                        skipped.append(
                            f"{sym}({dir_label}): 止损上移规整后未前进 "
                            f"({new_sl:.8g} → {normed_sl:.8g} vs current={current_sl:.8g})，跳过"
                        )
                    else:
                        sl_actions.append(
                            {
                                "symbol": sym,
                                "qty": qty,
                                "old_sl": current_sl,
                                "new_sl": normed_sl,
                                "sl_algo_id": current_sl_order.get("algoId"),
                                "sl_step": new_sl_step,
                                "entry": entry,
                                "mark": mark,
                                "pnl_pct": pnl_pct,
                                "elapsed_h": elapsed_h,
                                "original_tp": original_tp,
                                "tp_decay_step": tp_decay_step,
                                "open_ms": open_ms,
                                "direction": direction,
                                "close_side": close_side,
                                "dir_label": dir_label,
                                "direction_str": current_direction_str,
                            }
                        )

            # ── 方案三：时间衰减止盈 ──────────────────────────────
            new_tp, new_tp_step = Skill4Execute._calc_time_decay_tp(
                direction=direction,
                entry_price=entry,
                original_tp_price=original_tp,
                current_tp_price=current_tp,
                elapsed=elapsed_s,
                max_hold_seconds=max_hold_seconds,
                tp_decay_step=tp_decay_step,
                current_price=mark,
            )
            if new_tp is not None:
                normed_tp = norm_tp(sym, new_tp, direction)
                tp_safe = (
                    normed_tp > mark
                    if direction == TradeDirection.LONG
                    else normed_tp < mark
                )
                if not tp_safe:
                    skipped.append(
                        f"{sym}({dir_label}): 止盈下调规整后穿越当前价 "
                        f"({new_tp:.8g} → {normed_tp:.8g} vs mark={mark:.8g})，跳过"
                    )
                else:
                    tp_actions.append(
                        {
                            "symbol": sym,
                            "qty": qty,
                            "old_tp": current_tp,
                            "new_tp": normed_tp,
                            "tp_algo_id": current_tp_order.get("algoId"),
                            "tp_step": new_tp_step,
                            "entry": entry,
                            "mark": mark,
                            "pnl_pct": pnl_pct,
                            "elapsed_h": elapsed_h,
                            "original_tp": original_tp,
                            "open_ms": open_ms,
                            "direction": direction,
                            "close_side": close_side,
                            "dir_label": dir_label,
                            "direction_str": current_direction_str,
                        }
                    )
            else:
                # new_tp is None：无需衰减，仅刷新状态。
                # 用 ** 展开保留已有字段（含 partial_tp_done），避免覆盖分批止盈标记。
                state[sym] = {
                    **state.get(sym, {}),
                    "original_tp": original_tp,
                    "tp_decay_step": tp_decay_step,
                    "sl_step": sl_step,
                    "open_ms": open_ms,
                    "direction": current_direction_str,
                }

        # ── 辅助：cancel 后 polling 确认旧单已清除 ──────────────────
        def _cancel_and_verify(symbol: str, algo_id: int, order_type: str) -> bool:
            """
            取消指定 algo 条件单，并 polling 确认 Binance 已真正清除。

            Binance cancel API 返回成功不代表订单立即从活动列表消失，
            立即 place 新单会触发 -4130（"order already existing"）。
            本函数最多等待 12 秒（3 次 × 4 秒间隔），确认旧单清除后再返回。
            若最终无法清除，尝试 cancel_all_algo_orders(symbol) 兜底。

            返回 True=已清除，False=无法清除（已记录告警）。
            """
            for attempt in range(3):
                try:
                    client.cancel_algo_order(symbol=symbol, algo_id=algo_id)
                except Exception as exc:
                    log.warning(
                        f"[{symbol}] {order_type} cancel algoId={algo_id} 失败: {exc}"
                    )
                    return False
                # polling：等待 Binance 同步
                for poll_round in range(6):
                    time.sleep(2)
                    alive = [
                        o
                        for o in client.get_open_algo_orders()
                        if o["symbol"] == symbol and int(o.get("algoId", 0)) == algo_id
                    ]
                    if not alive:
                        return True
                    log.warning(
                        f"[{symbol}] {order_type} cancel 第 {poll_round + 1} 次检查仍存在，重试 cancel"
                    )
                    try:
                        client.cancel_algo_order(symbol=symbol, algo_id=algo_id)
                    except Exception:
                        pass
            # 最后兜底：取消该币种所有 algo 单，再重新挂（重新挂由调用方负责）
            log.warning(
                f"[{symbol}] {order_type} cancel polling {algo_id} 最终无法清除，使用 cancel_all_algo_orders 兜底"
            )
            try:
                client.cancel_all_algo_orders(symbol=symbol)
                time.sleep(3)
                return True
            except Exception as exc:
                log.error(f"[{symbol}] cancel_all_algo_orders 兜底也失败: {exc}")
                return False

        # ── 执行方案二 ────────────────────────────────────────────
        sl_results = []
        for a in sl_actions:
            sym = a["symbol"]
            close_side = a["close_side"]
            dir_label = a["dir_label"]
            step_label = {1: "保本", 2: "锁0.5x", 3: "锁1x"}.get(a["sl_step"], "?")

            # Binance -4130：同方向不允许同时存在多张 closePosition=True 条件单
            # 必须先撤旧单再挂新单；挂新单失败立即用原价恢复
            if not a["sl_algo_id"]:
                sl_results.append(
                    f"⚠️ **{sym}**({dir_label}) 止损单缺少 algoId，跳过上移（止损保护不变）"
                )
                continue

            if not _cancel_and_verify(sym, int(a["sl_algo_id"]), "SL"):
                sl_results.append(
                    f"🚨 **{sym}**({dir_label}) SL cancel 无法清除，跳过上移，请手动处理"
                )
                continue

            time.sleep(0.5)
            try:
                client.place_stop_market_order(
                    symbol=sym,
                    side=close_side,
                    quantity=a["qty"],
                    stop_price=a["new_sl"],
                    close_position=True,
                )
                state[sym] = {
                    **state.get(sym, {}),
                    "original_tp": a["original_tp"],
                    "tp_decay_step": a["tp_decay_step"],
                    "sl_step": a["sl_step"],
                    "open_ms": a["open_ms"],
                    "direction": a["direction_str"],
                }
                sl_results.append(
                    f"✅ **{sym}**({dir_label}) 止损上移({step_label}): "
                    f"{a['old_sl']:.6g} → {a['new_sl']:.6g} (浮盈{a['pnl_pct']:+.2f}%)"
                )
            except Exception as place_exc:
                try:
                    time.sleep(2)
                    client.place_stop_market_order(
                        symbol=sym,
                        side=close_side,
                        quantity=a["qty"],
                        stop_price=a["old_sl"],
                        close_position=True,
                    )
                    sl_results.append(
                        f"⚠️ **{sym}**({dir_label}) 止损上移失败，已恢复原止损单({a['old_sl']:.6g}): {place_exc}"
                    )
                except Exception as restore_exc:
                    sl_results.append(
                        f"🚨 **{sym}**({dir_label}) 止损上移失败且恢复失败！持仓无止损保护！请立即手动处理！"
                        f" 上移错误={place_exc} | 恢复错误={restore_exc}"
                    )
                state[sym] = {
                    **state.get(sym, {}),
                    "original_tp": a["original_tp"],
                    "tp_decay_step": a["tp_decay_step"],
                    "sl_step": a["sl_step"],
                    "open_ms": a["open_ms"],
                    "direction": a["direction_str"],
                }

        # ── 执行方案三 ────────────────────────────────────────────
        tp_results = []
        for a in tp_actions:
            sym = a["symbol"]
            close_side = a["close_side"]
            dir_label = a["dir_label"]
            step_label = {1: "-20%", 2: "-40%"}.get(a["tp_step"], "?")

            if not a["tp_algo_id"]:
                tp_results.append(
                    f"⚠️ **{sym}**({dir_label}) 止盈单缺少 algoId，跳过下调（止盈保护不变）"
                )
                continue

            if not _cancel_and_verify(sym, int(a["tp_algo_id"]), "TP"):
                tp_results.append(
                    f"🚨 **{sym}**({dir_label}) TP cancel 无法清除，跳过下调，请手动处理"
                )
                continue

            time.sleep(0.5)
            try:
                client.place_take_profit_market_order(
                    symbol=sym,
                    side=close_side,
                    quantity=a["qty"],
                    stop_price=a["new_tp"],
                    close_position=True,
                )
                state[sym] = {
                    **state.get(sym, {}),
                    "original_tp": a["original_tp"],
                    "tp_decay_step": a["tp_step"],
                    "sl_step": state.get(sym, {}).get("sl_step", a["sl_step"]),
                    "open_ms": a["open_ms"],
                    "direction": a["direction_str"],
                }
                tp_results.append(
                    f"✅ **{sym}**({dir_label}) 止盈下调({step_label}): "
                    f"{a['old_tp']:.6g} → {a['new_tp']:.6g} "
                    f"(持仓{a['elapsed_h']:.1f}h, 浮盈{a['pnl_pct']:+.2f}%)"
                )
            except Exception as place_exc:
                try:
                    time.sleep(2)
                    client.place_take_profit_market_order(
                        symbol=sym,
                        side=close_side,
                        quantity=a["qty"],
                        stop_price=a["old_tp"],
                        close_position=True,
                    )
                    tp_results.append(
                        f"⚠️ **{sym}**({dir_label}) 止盈下调失败，已恢复原止盈单({a['old_tp']:.6g}): {place_exc}"
                    )
                except Exception as restore_exc:
                    tp_results.append(
                        f"🚨 **{sym}**({dir_label}) 止盈下调失败且恢复失败！止盈保护丢失！请手动处理！"
                        f" 下调错误={place_exc} | 恢复错误={restore_exc}"
                    )
                current_state = state.get(sym, {})
                state[sym] = {
                    **current_state,
                    "original_tp": a["original_tp"],
                    "tp_decay_step": a["tp_decay_step"],
                    "sl_step": current_state.get("sl_step", a["sl_step"]),
                    "open_ms": a["open_ms"],
                    "direction": a["direction_str"],
                }

        # ── 保存状态 ──────────────────────────────────────────────
        save_state(state)

    finally:
        # 共享状态锁：save_state 完成后立即释放
        if _state_lock_fh is not None:
            fcntl.flock(_state_lock_fh, fcntl.LOCK_UN)
            _state_lock_fh.close()

    # ── 输出报告 ──────────────────────────────────────────────
    # 只在有实际变动时输出，无变动则静默
    if not sl_results and not tp_results:
        pass  # 无变动，静默退出
    else:
        lines = [f"## 持仓管理 {now.strftime('%m-%d %H:%M UTC')}"]
        if sl_results:
            lines.append("\n**方案二 止损上移**")
            lines.extend(sl_results)
        if tp_results:
            lines.append("\n**方案三 止盈下调**")
            lines.extend(tp_results)
        if skipped:
            lines.append(f"\n_跳过 {len(skipped)} 个持仓:_")
            lines.extend(f"  - {s}" for s in skipped)
        print("\n".join(lines))

finally:
    # 无论正常退出还是异常，都确保释放进程锁
    fcntl.flock(_lock_fh, fcntl.LOCK_UN)
    _lock_fh.close()
