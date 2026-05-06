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
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

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
from src.infra.exchange_rules import parse_symbol_trading_rule, normalize_order_quantity
from src.skills.skill4_execute import Skill4Execute
from src.models.types import TradeDirection

# 与 manage_positions.py 共享同一状态文件，partial_tp_done 标记互通
STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manage_positions_state.json"
)
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "partial_tp.lock")
# 写状态时需同时持有此锁，防止与 manage_positions.py 并发写入状态文件产生竞态
SHARED_STATE_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "manage_positions_state.lock"
)

# trading_state.db 路径
_TRADING_STATE_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "trading_state.db"
)

_rule_map: dict = {}

# state_store.db 路径（用于 strategy_tag 兜底查询）
_STATE_STORE_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "state_store.db"
)


def _get_open_ms_from_db(symbol: str, direction: str) -> float:
    """
    从 position_open_times 表读取持仓开启时间（毫秒）。
    如果 JSON 文件中的 open_ms 为 0 或缺失，则尝试从数据库读取。
    """
    try:
        conn = sqlite3.connect(_TRADING_STATE_DB)
        try:
            cursor = conn.execute(
                "SELECT open_ms FROM position_open_times WHERE symbol = ? AND direction = ?",
                (symbol, direction.lower()),
            )
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return 0.0


# strategy_tag 查询缓存（延迟加载）
_strategy_tag_cache: dict[str, str] | None = None


def _get_strategy_tag_map() -> dict[str, str]:
    """
    从 state_store.db 的 skill4_execute 快照中提取 symbol → strategy_tag 映射。
    结果缓存，同一次执行内只查一次。
    """
    global _strategy_tag_cache
    if _strategy_tag_cache is not None:
        return _strategy_tag_cache
    _strategy_tag_cache = {}
    if not os.path.exists(_STATE_STORE_DB):
        return _strategy_tag_cache
    try:
        conn = sqlite3.connect(_STATE_STORE_DB)
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
                    _strategy_tag_cache[sym] = tag
    except Exception:
        pass
    return _strategy_tag_cache


def _direction_of(position_amt: float) -> TradeDirection:
    return TradeDirection.LONG if position_amt > 0 else TradeDirection.SHORT


def _close_side_of(direction: TradeDirection) -> str:
    return "SELL" if direction == TradeDirection.LONG else "BUY"


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
        mode="w", dir=state_dir, suffix=".tmp", delete=False
    ) as tmp:
        json.dump(state, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, STATE_FILE)


def record_partial_tp(
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    partial_qty: float,
    strategy_tag: str,
    open_ms: float,
    closed_at: str,
    order_id: str = "",
) -> None:
    """将分批止盈写入 trade_records，使用幂等 sync_key 防止与 trade_sync 重复。"""
    try:
        pnl = (
            (exit_price - entry_price) * partial_qty
            if direction == "LONG"
            else (entry_price - exit_price) * partial_qty
        )
        hold_hours = (
            max(
                0.0, (datetime.now(timezone.utc).timestamp() * 1000 - open_ms) / 3600000
            )
            if open_ms
            else 0.0
        )

        conn = sqlite3.connect(_TRADING_STATE_DB)
        # 整个写入在同一事务内：sync_key + trade_record 要么都成功，要么都回滚
        with conn:
            # 使用 trade_sync_keys 做幂等保护，防止 _sync_server_closed_trades 重复记录
            if order_id:
                closed_at_ms = int(datetime.fromisoformat(closed_at).timestamp() * 1000)
                # 写入自身的幂等 key
                partial_key = f"partial_tp:{symbol}:{order_id}:{closed_at_ms}"
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO trade_sync_keys (sync_key, trade_record_id, created_at) VALUES (?, ?, ?)",
                    (partial_key, None, closed_at),
                )
                if cursor.rowcount == 0:
                    conn.close()
                    return  # 已记录过，跳过
                # 同时写入 trade_sync 格式的 key，阻止 BinanceTradeSyncer 重复同步
                binance_key = f"binance_user_order:{symbol}:{order_id}:{closed_at_ms}"
                conn.execute(
                    "INSERT OR IGNORE INTO trade_sync_keys (sync_key, trade_record_id, created_at) VALUES (?, ?, ?)",
                    (binance_key, None, closed_at),
                )

            conn.execute(
                "INSERT INTO trade_records "
                "(symbol, direction, entry_price, exit_price, pnl_amount, "
                "hold_duration_hours, rating_score, position_size_pct, closed_at, strategy_tag, close_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    direction.lower(),
                    entry_price,
                    exit_price,
                    round(pnl, 6),
                    round(hold_hours, 4),
                    0,
                    0.0,
                    closed_at,
                    strategy_tag or "unknown",
                    "partial_tp",
                ),
            )
        conn.close()
    except Exception as exc:
        print(f"⚠️ {symbol} 分批止盈写入数据库失败（不影响交易）: {exc}")


# ── 初始化 ────────────────────────────────────────────────
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
if not api_key or not api_secret:
    print("🚨 BINANCE_API_KEY / BINANCE_API_SECRET 未配置，退出")
    sys.exit(1)

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

try:
    # 预加载数量精度规则（分批止盈需要规整 qty）
    # 注意：exchangeInfo 在获取共享状态锁之前加载，避免长时间持锁。
    try:
        _exchange_raw = client._request_with_retry("GET", "/fapi/v1/exchangeInfo", {})
    except Exception as _exc:
        print(f"🚨 获取 exchangeInfo 失败，退出: {_exc}")
        sys.exit(1)

    for _s in _exchange_raw.get("symbols", []):
        _rule = parse_symbol_trading_rule(_s)
        if _rule:
            _rule_map[_s["symbol"]] = _rule

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

        raw = getattr(pos, "raw", {}) or {}
        mark = float(raw.get("markPrice") or 0)
        entry = pos.entry_price
        qty = abs(pos.position_amt)

        if entry <= 0 or qty <= 0 or mark <= 0:
            skipped.append(f"{sym}: 数据不完整，跳过")
            continue

        direction = _direction_of(pos.position_amt)
        close_side = _close_side_of(direction)
        dir_label = "多" if direction == TradeDirection.LONG else "空"

        sym_state = state.get(sym, {})

        # ── 方向反转检测 ──────────────────────────────────────
        # 与 manage_positions.py 保持一致：方向变化时重置 partial_tp_done
        saved_direction = sym_state.get("direction")
        current_direction_str = "LONG" if direction == TradeDirection.LONG else "SHORT"
        if saved_direction is not None and saved_direction != current_direction_str:
            sym_state = {}
            state[sym] = {}

        # 已执行过分批止盈，跳过
        if sym_state.get("partial_tp_done", False):
            continue

        sym_rule = _rule_map.get(sym)
        if sym_rule is None:
            skipped.append(f"{sym}({dir_label}): 未找到交易规则，跳过")
            continue

        # 找止损单，计算 sl_dist
        sl_orders = [
            o
            for o in algo_orders
            if o.get("symbol") == sym
            and Skill4Execute._is_stop_loss_order(o, close_side, entry, direction)
        ]
        if not sl_orders:
            skipped.append(f"{sym}({dir_label}): 无止损单，无法计算 sl_dist，跳过")
            continue

        if direction == TradeDirection.LONG:
            sl_order = min(sl_orders, key=lambda o: float(o.get("triggerPrice") or 0))
        else:
            sl_order = max(sl_orders, key=lambda o: float(o.get("triggerPrice") or 0))

        sl_price = float(sl_order.get("triggerPrice") or 0)
        if sl_price <= 0:
            skipped.append(f"{sym}({dir_label}): 止损触发价无效，跳过")
            continue

        sl_dist = abs(entry - sl_price)

        # 记录原始止损距离：首次见到 sl_dist > 0 时存入状态，
        # 后续止损上移到保本位（sl_dist==0）时复用原始值
        if sl_dist > 0:
            sym_state["original_sl_dist"] = sl_dist
            state[sym] = {**state.get(sym, {}), "original_sl_dist": sl_dist}
        elif sl_dist == 0:
            original_sl_dist = sym_state.get("original_sl_dist")
            if original_sl_dist and float(original_sl_dist) > 0:
                sl_dist = float(original_sl_dist)
            else:
                skipped.append(
                    f"{sym}({dir_label}): sl_dist=0（止损已在保本位）且无原始记录，跳过"
                )
                continue

        profit = (mark - entry) if direction == TradeDirection.LONG else (entry - mark)
        pnl_pct = profit / entry * 100
        ratio = profit / sl_dist

        # 触发比例：浮盈达到 1.0 倍止损距离即触发（所有策略统一）
        trigger_ratio = 1.0

        if ratio < trigger_ratio:
            # 未触发，不记录 skipped（避免每5分钟刷屏）
            continue

        # 规整 50% 数量
        raw_partial = qty / 2.0
        normed_partial = normalize_order_quantity(
            symbol=sym,
            quantity=raw_partial,
            price=mark,
            rule=sym_rule,
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
                symbol=sym,
                quantity=remaining,
                price=mark,
                rule=sym_rule,
            )
            is not None
        )
        if not remaining_ok:
            skipped.append(
                f"{sym}({dir_label}): 分批止盈跳过，"
                f"剩余仓位 {remaining:.8g} 低于最小交易量"
            )
            continue

        partial_tp_actions.append(
            {
                "symbol": sym,
                "partial_qty": float(normed_partial),
                "close_side": close_side,
                "dir_label": dir_label,
                "pnl_pct": pnl_pct,
                "ratio": ratio,
                "direction_str": current_direction_str,
                "entry": entry,
                "open_ms": sym_state.get("open_ms", 0.0)
                or _get_open_ms_from_db(sym, current_direction_str),
                "strategy_tag": sym_state.get("strategy_tag")
                or _get_strategy_tag_map().get(sym, "unknown"),
            }
        )

    # ── 执行分批止盈 ──────────────────────────────────────────
    # 无触发时也需要保存状态（original_sl_dist 等字段可能已更新）
    if not partial_tp_actions:
        save_state(state)
        if skipped:
            # 有跳过记录时才输出，方便排查
            lines = [
                f"## 分批止盈 {datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')} (无触发)"
            ]
            lines.append(f"\n_跳过 {len(skipped)} 个持仓:_")
            lines.extend(f"  - {s}" for s in skipped)
            print("\n".join(lines))
        sys.exit(0)

    # 获取共享状态锁（非阻塞）：
    # manage_positions.py 正在写状态时持有此锁，本次跳过，等下次5分钟再跑。
    # 这防止两个脚本并发 read-modify-write 状态文件导致 partial_tp_done 被覆盖。
    _state_lock_fh = open(SHARED_STATE_LOCK_FILE, "w")
    try:
        fcntl.flock(_state_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            "⚠️ manage_positions.py 正在写入状态，本次分批止盈跳过（状态锁被占用），等待下次执行"
        )
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
            sym = a["symbol"]
            # 二次检查：获取锁后再次确认 partial_tp_done，
            # 防止锁等待期间 manage_positions.py 已写入 partial_tp_done=True
            if state.get(sym, {}).get("partial_tp_done", False):
                results.append(
                    f"ℹ️ **{sym}**({a['dir_label']}) 分批止盈已由其他进程执行，跳过"
                )
                continue
            try:
                order_result = client.place_market_order(
                    symbol=sym,
                    side=a["close_side"],
                    quantity=a["partial_qty"],
                )
                closed_at = datetime.now(timezone.utc).isoformat()
                # 写入 partial_tp_done，与 manage_positions.py 共享状态
                state[sym] = {
                    **state.get(sym, {}),
                    "partial_tp_done": True,
                    "direction": a["direction_str"],
                }
                # 获取实际成交均价，回退到 mark price，最终回退到 entry
                exit_price = order_result.price if order_result.price > 0 else 0.0
                if exit_price <= 0:
                    pos_match = next((p for p in positions if p.symbol == sym), None)
                    if pos_match:
                        raw = getattr(pos_match, "raw", {}) or {}
                        exit_price = float(raw.get("markPrice", 0) or 0)
                if exit_price <= 0:
                    exit_price = a["entry"]
                # 写入 trade_records
                record_partial_tp(
                    symbol=sym,
                    direction=a["direction_str"],
                    entry_price=a["entry"],
                    exit_price=exit_price,
                    partial_qty=a["partial_qty"],
                    strategy_tag=a["strategy_tag"],
                    open_ms=a["open_ms"],
                    closed_at=closed_at,
                    order_id=order_result.order_id,
                )
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
