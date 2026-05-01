"""
账户报告公共工具模块

所有 cron 脚本和 check_account.py 共享的账户信息构建、持仓快照、
保护单报告、策略来源映射等函数统一在此维护，确保输出格式一致。
"""
import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── 策略来源定义 ──────────────────────────────────────────
# strategy_tag → (emoji, 中文标签)
STRATEGY_TAG_MAP: dict[str, tuple[str, str]] = {
    "crypto_oversold_long":    ("🌀", "超跌"),
    "crypto_oversold_short":   ("🌀", "超跌"),
    "crypto_reversal_long":    ("🔄", "反转"),
    "crypto_reversal_short":   ("🔄", "反转"),
    "crypto_overbought_long":  ("📉", "做空"),
    "crypto_overbought_short": ("📉", "做空"),
    "crypto_generic":          ("⚙️", "通用"),
}


# ── 安全类型转换 ──────────────────────────────────────────

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fmt_optional(value: Any, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


# ── 策略来源映射 ──────────────────────────────────────────

def build_symbol_source_map(store) -> dict[str, tuple[str, str]]:
    """从 StateStore skill4_execute 执行结果中，构建 symbol → (emoji, label) 映射。

    遍历所有 skill4_execute 记录（由新到旧），以最近一次执行携带的
    strategy_tag 为准。未找到的 symbol 不会加入映射，调用方应降级为 "📌未知"。
    """
    source_map: dict[str, tuple[str, str]] = {}
    try:
        conn = getattr(store, "_conn", None)
        if conn is None:
            return source_map
        rows = conn.execute(
            """
            SELECT data FROM state_snapshots
            WHERE skill_name = 'skill4_execute'
            ORDER BY created_at DESC
            """,
        ).fetchall()
        for (raw,) in rows:
            data = json.loads(raw)
            for e in data.get("execution_results", []):
                sym = e.get("symbol", "")
                if not sym or sym in source_map:
                    continue
                tag = e.get("strategy_tag", "") or ""
                if tag in STRATEGY_TAG_MAP:
                    source_map[sym] = STRATEGY_TAG_MAP[tag]
    except Exception:
        pass
    return source_map


def tag_symbol(symbol: str, source_map: dict) -> str:
    """返回 symbol 的策略标签（如 '🌀超跌'），无来源时返回空字符串。"""
    if symbol in source_map:
        emoji, label = source_map[symbol]
        return f"{emoji}{label}"
    return ""


def tag_symbol_or_default(symbol: str, source_map: dict) -> str:
    """返回 symbol 的策略标签，无来源时返回 '📌未知'。"""
    return tag_symbol(symbol, source_map) or "📌未知"


# ── 持仓快照构建 ──────────────────────────────────────────

def build_position_snapshots(
    total_balance: float,
    positions: list[Any],
    source_map: Optional[dict[str, str]] = None,
) -> list[dict]:
    """从 Binance 持仓列表构建标准化持仓快照，按保证金降序排列。"""
    source_map = source_map or {}
    snapshots = []
    for pos in positions:
        raw = getattr(pos, "raw", {}) or {}
        symbol = getattr(pos, "symbol", raw.get("symbol", ""))
        amount = safe_float(getattr(pos, "position_amt", raw.get("positionAmt", 0)))
        if amount == 0:
            continue
        entry = safe_float(getattr(pos, "entry_price", raw.get("entryPrice", 0)))
        mark = safe_float(raw.get("markPrice"), entry)
        unrealized = safe_float(
            getattr(pos, "unrealized_pnl", raw.get("unRealizedProfit", 0))
        )
        leverage = safe_float(getattr(pos, "leverage", raw.get("leverage", 0)))
        notional = abs(safe_float(raw.get("notional")))
        if notional <= 0 and mark > 0:
            notional = abs(amount) * mark
        margin = safe_float(raw.get("initialMargin"))
        if margin <= 0:
            margin = safe_float(raw.get("positionInitialMargin"))
        if margin <= 0 and leverage > 0:
            margin = notional / leverage
        if leverage <= 0 and margin > 0:
            leverage = notional / margin

        direction = "long" if amount > 0 else "short"
        if entry > 0 and mark > 0:
            if direction == "long":
                price_change_pct = (mark - entry) / entry * 100
            else:
                price_change_pct = (entry - mark) / entry * 100
        else:
            price_change_pct = 0.0

        snapshots.append({
            "symbol": symbol,
            "source": source_map.get(symbol, "手动/未知"),
            "direction": direction,
            "quantity": abs(amount),
            "entry_price": entry,
            "mark_price": mark,
            "price_change_pct": round(price_change_pct, 4),
            "unrealized_pnl": unrealized,
            "notional_value": notional,
            "initial_margin": margin,
            "margin_pct_of_equity": round(
                margin / total_balance * 100, 4
            ) if total_balance > 0 else 0.0,
            "leverage": round(leverage, 4),
            "roi_on_margin_pct": round(
                unrealized / margin * 100, 4
            ) if margin > 0 else 0.0,
            "liquidation_price": safe_float(raw.get("liquidationPrice")),
        })
    return sorted(snapshots, key=lambda p: p["initial_margin"], reverse=True)


# ── 保护单报告 ────────────────────────────────────────────

def classify_protection_label(side: str, trigger: float, entry: float) -> str:
    """根据方向和触发价判断保护单类型（止损/止盈/条件单）。"""
    if entry <= 0 or trigger <= 0:
        return "条件单"
    if side == "SELL":
        return "止盈" if trigger > entry else "止损"
    if side == "BUY":
        return "止盈" if trigger < entry else "止损"
    return "条件单"


def build_protection_report(
    positions: list[dict],
    algo_orders: list[dict],
) -> dict:
    """构建保护单健康报告。"""
    positions_by_symbol = {p["symbol"]: p for p in positions}
    orders = []
    health: dict[str, dict] = {}

    for symbol in set(positions_by_symbol) | {
        str(o.get("symbol", "")) for o in algo_orders if o.get("symbol")
    }:
        health[symbol] = {
            "has_position": symbol in positions_by_symbol,
            "has_stop_loss": False,
            "has_take_profit": False,
            "stop_loss_count": 0,
            "take_profit_count": 0,
            "duplicate_protection_orders": 0,
            "status": "ok",
        }

    for order in algo_orders:
        symbol = str(order.get("symbol", ""))
        if not symbol:
            continue
        pos = positions_by_symbol.get(symbol)
        entry = safe_float(pos.get("entry_price")) if pos else 0.0
        side = str(order.get("side", "")).upper()
        order_type = str(order.get("type", ""))
        trigger = safe_float(order.get("triggerPrice"))
        label = classify_protection_label(side, trigger, entry)

        if label == "止损":
            health[symbol]["has_stop_loss"] = True
            health[symbol]["stop_loss_count"] += 1
        elif label == "止盈":
            health[symbol]["has_take_profit"] = True
            health[symbol]["take_profit_count"] += 1

        orders.append({
            "symbol": symbol,
            "type": order_type,
            "label": label,
            "side": side,
            "trigger_price": trigger,
            "entry_price": entry,
            "distance_from_entry_pct": round(
                (trigger - entry) / entry * 100, 4
            ) if entry > 0 and trigger > 0 else 0.0,
            "quantity": order.get("quantity", ""),
            "close_position": str(order.get("closePosition", "")).lower() == "true"
            or order.get("closePosition") is True,
            "algo_id": str(order.get("algoId", order.get("orderId", ""))),
        })

    for item in health.values():
        duplicate_count = max(item["stop_loss_count"] - 1, 0) + max(
            item["take_profit_count"] - 1, 0
        )
        item["duplicate_protection_orders"] = duplicate_count
        if not item["has_position"] and (item["has_stop_loss"] or item["has_take_profit"]):
            item["status"] = "warning"
        elif not item["has_stop_loss"] or not item["has_take_profit"] or duplicate_count > 0:
            item["status"] = "warning"

    return {
        "orders": sorted(orders, key=lambda o: (o["symbol"], o["label"], o["trigger_price"])),
        "health": dict(sorted(health.items())),
    }


# ── 账户摘要 ──────────────────────────────────────────────

def build_account_summary(account: Any, positions: list[dict], paper_mode: bool) -> dict:
    """构建标准化账户摘要字典。"""
    total_balance = safe_float(getattr(account, "total_balance", 0))
    total_margin = sum(p["initial_margin"] for p in positions)
    total_notional = sum(p["notional_value"] for p in positions)
    total_unrealized = sum(p["unrealized_pnl"] for p in positions)
    daily_realized_pnl = safe_float(getattr(account, "daily_realized_pnl", 0))
    daily_loss_pct = (
        abs(min(daily_realized_pnl, 0.0)) / total_balance * 100
        if total_balance > 0
        else 0.0
    )
    return {
        "total_balance": total_balance,
        "available_margin": safe_float(getattr(account, "available_balance", 0)),
        "total_unrealized_pnl": safe_float(
            getattr(account, "total_unrealized_pnl", total_unrealized)
        ),
        "daily_realized_pnl": daily_realized_pnl,
        "daily_loss_pct": round(daily_loss_pct, 4),
        "position_count": len(positions),
        "total_position_margin": round(total_margin, 8),
        "total_position_margin_pct": round(
            total_margin / total_balance * 100, 4
        ) if total_balance > 0 else 0.0,
        "total_notional_value": round(total_notional, 8),
        "paper_mode": paper_mode,
    }


# ── 交易决策摘要 ──────────────────────────────────────────

def build_decision(
    ratings: list[dict],
    plans: list[dict],
    execution_results: list[dict],
    rating_threshold: int,
) -> dict:
    """构建交易决策摘要。"""
    executed_count = sum(
        1 for r in execution_results if r.get("status") in {"open", "filled", "paper_trade"}
    )
    rejected_count = sum(
        1 for r in execution_results if r.get("status") == "rejected_by_risk"
    )
    failed_count = sum(
        1 for r in execution_results if r.get("status") == "execution_failed"
    )
    if executed_count > 0:
        action = "trade"
        reason = "存在已开仓或已成交结果"
    elif not ratings:
        action = "no_trade"
        reason = f"无币种通过 {rating_threshold} 分评级门槛"
    elif not plans:
        action = "no_trade"
        reason = "无交易计划通过策略或风控"
    else:
        action = "no_trade"
        reason = "未产生可执行成交"
    return {
        "action": action,
        "reason": reason,
        "trade_plan_count": len(plans),
        "risk_blocked_count": rejected_count,
        "execution_failed_count": failed_count,
        "executed_count": executed_count,
    }


# ── 辅助函数 ──────────────────────────────────────────────

def metadata_by_symbol(execution_results: list[dict]) -> dict[str, dict[str, Any]]:
    """从 skill4 执行结果提取 symbol → metadata 映射，供 trade_syncer 使用。"""
    metadata: dict[str, dict[str, Any]] = {}
    for result in execution_results:
        symbol = result.get("symbol")
        if not symbol:
            continue
        metadata[symbol] = {
            "rating_score": result.get("rating_score", 6),
            "position_size_pct": result.get("position_size_pct", 0.0),
            "hold_duration_hours": result.get("hold_duration_hours", 0.0),
        }
    return metadata


def protection_warnings(protection: dict) -> list[str]:
    """从保护单报告中提取告警信息。"""
    warnings = []
    for symbol, health in protection.get("health", {}).items():
        if health.get("duplicate_protection_orders", 0) > 0:
            warnings.append(
                f"{symbol} 存在重复保护单 {health['duplicate_protection_orders']} 张"
            )
        if health.get("has_position") and not health.get("has_stop_loss"):
            warnings.append(f"{symbol} 有持仓但缺少止损保护单")
        if health.get("has_position") and not health.get("has_take_profit"):
            warnings.append(f"{symbol} 有持仓但缺少止盈保护单")
        if not health.get("has_position") and (
            health.get("has_stop_loss") or health.get("has_take_profit")
        ):
            warnings.append(f"{symbol} 无持仓但存在残留保护条件单")
    return warnings


# ── 统一 Markdown 渲染：持仓 + 保护单 + 账户 ─────────────

def render_positions_markdown(
    positions: list[dict],
    source_map: Optional[dict] = None,
    max_detail: int = 5,
) -> list[str]:
    """渲染持仓明细 Markdown 段落。

    超过 max_detail 笔时，只展示前 max_detail 笔详情 + 其余摘要，
    防止 Telegram 4096 字符限制导致发送失败。
    """
    source_map = source_map or {}
    lines = ["当前持仓:"]
    if not positions:
        lines.append("- 当前无持仓")
        return lines

    detail_positions = positions[:max_detail]
    rest_positions = positions[max_detail:]

    for pos in detail_positions:
        tag = tag_symbol_or_default(pos["symbol"], source_map)
        lines.extend([
            f"- {pos['symbol']} {pos['direction']} ({tag})",
            f"  数量: {pos['quantity']}",
            f"  入场价: {pos['entry_price']}",
            f"  当前价: {pos['mark_price']}",
            f"  价格涨跌: {pos['price_change_pct']:+.2f}%",
            f"  浮盈亏: {pos['unrealized_pnl']:+.2f} USDT",
            f"  名义价值: {pos['notional_value']:.2f} USDT",
            f"  保证金: {pos['initial_margin']:.2f} USDT",
            f"  资金占比: {pos['margin_pct_of_equity']:.2f}%",
            f"  杠杆: {pos['leverage']:.2f}x",
            f"  保证金收益率: {pos['roi_on_margin_pct']:+.2f}%",
        ])
        if pos.get("liquidation_price") and pos["liquidation_price"] > 0:
            lines.append(f"  强平价: {pos['liquidation_price']}")

    if rest_positions:
        rest_pnl = sum(p["unrealized_pnl"] for p in rest_positions)
        rest_margin = sum(p["initial_margin"] for p in rest_positions)
        symbols = ", ".join(p["symbol"] for p in rest_positions)
        lines.append(
            f"- 其余 {len(rest_positions)} 笔: "
            f"保证金 {rest_margin:.2f}, 浮盈亏 {rest_pnl:+.2f} "
            f"({symbols})"
        )

    return lines


def render_protection_markdown(protection: dict, max_detail: int = 8) -> list[str]:
    """渲染保护单状态 Markdown 段落。超过 max_detail 个币种时只显示异常项。"""
    lines = ["保护单状态:"]
    health = protection.get("health", {})
    if not health:
        lines.append("- 无保护单")
        return lines

    # 优先显示有问题的
    warning_items = {s: h for s, h in health.items() if h.get("status") != "ok"}
    ok_items = {s: h for s, h in health.items() if h.get("status") == "ok"}

    shown = 0
    for symbol, h in warning_items.items():
        if shown >= max_detail:
            break
        duplicate = h.get("duplicate_protection_orders", 0)
        detail = f"止损 {h['stop_loss_count']} 张, 止盈 {h['take_profit_count']} 张"
        if duplicate:
            detail += f", 重复 {duplicate} 张"
        lines.append(f"- {symbol}: {h['status']} ({detail})")
        shown += 1

    for symbol, h in ok_items.items():
        if shown >= max_detail:
            break
        lines.append(
            f"- {symbol}: ok (止损 {h['stop_loss_count']}, 止盈 {h['take_profit_count']})"
        )
        shown += 1

    remaining = len(health) - shown
    if remaining > 0:
        lines.append(f"- 其余 {remaining} 个币种保护单正常")

    return lines


def render_account_markdown(account: dict, risk: Optional[dict] = None) -> list[str]:
    """渲染账户状态 + 风险状态 Markdown 段落。"""
    lines = [
        "账户状态:",
        f"- 总资金: {account['total_balance']:.2f} USDT",
        f"- 可用保证金: {account['available_margin']:.2f} USDT",
        f"- 未实现盈亏: {account['total_unrealized_pnl']:+.2f} USDT",
        f"- 持仓数: {account.get('position_count', 0)}",
        f"- 持仓保证金: {account['total_position_margin']:.2f} USDT",
        f"- 持仓资金占比: {account['total_position_margin_pct']:.2f}%",
        f"- 持仓名义价值: {account.get('total_notional_value', 0):.2f} USDT",
        f"- 日已实现盈亏: {account.get('daily_realized_pnl', 0):+.2f} USDT",
        f"- 日亏损比例: {account['daily_loss_pct']:.2f}%",
        f"- Paper Mode: {str(account['paper_mode']).lower()}",
    ]
    if risk:
        lines.extend([
            "",
            "风险状态:",
            f"- 单笔保证金上限: {risk['single_trade_margin_limit_pct']}%",
            f"- 单币种持仓上限: {risk['single_symbol_position_limit_pct']}%",
            f"- 日亏损停止阈值: {risk['daily_loss_stop_pct']}%",
            f"- 当前状态: {risk['risk_status']}",
        ])
    return lines


def render_warnings_markdown(warnings: list[str], errors: list[str]) -> list[str]:
    """渲染异常与注意事项 Markdown 段落。"""
    if not warnings and not errors:
        return []
    lines = ["异常与注意事项:"]
    for w in warnings:
        lines.append(f"- WARNING: {w}")
    for e in errors:
        lines.append(f"- ERROR: {e}")
    return lines
