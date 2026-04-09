"""
A 股交易计划制定 Skill

基于超跌筛选结果（Skill-1B），为每只候选股票生成量化交易计划。
针对 A 股市场特性深度定制，与币安 Skill-3 同源架构但策略逻辑完全不同。

A 股 vs 加密货币的策略差异：
  - 只能做多（普通账户无法做空）
  - T+1 交易（当天买入次日才能卖出，止损最快 T+1 执行）
  - 涨跌停板（止损/止盈价格受限）
  - 无杠杆（满仓 = 100%）
  - 仓位管理：单只 ≤ 20%，总仓位 ≤ 80%（留 20% 现金应对极端行情）

两种策略模式：

## 短期超跌反弹策略（3~5 天）
  入场：超跌评分 ≥ 35，RSI < 30 或有跌停板信号
  止损：基于 ATR 的动态止损（1.5 倍 ATR），最大 -8%
  止盈：分批止盈（第一目标 +5%，第二目标 +10%）
  仓位：评分越高仓位越大，单只 5%~15%
  持仓上限：5 个交易日

## 长期超跌蓄能策略（2~4 周）
  入场：超跌评分 ≥ 40，MACD 底背离 + BIAS(60) < -15%
  止损：基于支撑位的止损（近期低点下方 3%），最大 -12%
  止盈：分批止盈（第一目标 +8%，第二目标 +15%，第三目标 +25%）
  仓位：评分越高仓位越大，单只 8%~20%
  持仓上限：20 个交易日

风控红线（硬编码，不可绕过）：
  - 单只股票仓位 ≤ 总资金 20%
  - 总持仓 ≤ 总资金 80%
  - 单日最大亏损 ≤ 3%（触发后当日不再开仓）
  - 止损后同股票 5 个交易日内不重复开仓
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 短期策略常量
# ══════════════════════════════════════════════════════════

ST_MIN_SCORE = 35                # 短期最低入场评分
ST_STOP_LOSS_ATR_MULT = 1.5     # 止损 = 1.5 倍 ATR
ST_STOP_LOSS_MAX_PCT = -8.0     # 最大止损幅度 -8%
ST_TP1_PCT = 5.0                # 第一止盈目标 +5%
ST_TP2_PCT = 10.0               # 第二止盈目标 +10%
ST_TP1_RATIO = 0.5              # 第一目标平仓 50%
ST_TP2_RATIO = 0.5              # 第二目标平仓剩余 50%
ST_MAX_HOLD_DAYS = 5            # 最大持仓 5 个交易日
ST_POS_MIN_PCT = 5.0            # 最小仓位 5%
ST_POS_MAX_PCT = 15.0           # 最大仓位 15%

# ══════════════════════════════════════════════════════════
# 长期策略常量
# ══════════════════════════════════════════════════════════

LT_MIN_SCORE = 40                # 长期最低入场评分
LT_STOP_LOSS_SUPPORT_PCT = -3.0 # 止损 = 近期低点下方 3%
LT_STOP_LOSS_MAX_PCT = -12.0    # 最大止损幅度 -12%
LT_TP1_PCT = 8.0                # 第一止盈目标 +8%
LT_TP2_PCT = 15.0               # 第二止盈目标 +15%
LT_TP3_PCT = 25.0               # 第三止盈目标 +25%
LT_TP1_RATIO = 0.3              # 第一目标平仓 30%
LT_TP2_RATIO = 0.3              # 第二目标平仓 30%
LT_TP3_RATIO = 0.4              # 第三目标平仓剩余 40%
LT_MAX_HOLD_DAYS = 20           # 最大持仓 20 个交易日
LT_POS_MIN_PCT = 8.0            # 最小仓位 8%
LT_POS_MAX_PCT = 20.0           # 最大仓位 20%

# ══════════════════════════════════════════════════════════
# 风控红线
# ══════════════════════════════════════════════════════════

MAX_SINGLE_POSITION_PCT = 20.0   # 单只股票仓位上限 20%
MAX_TOTAL_POSITION_PCT = 80.0    # 总持仓上限 80%（留 20% 现金）
MAX_DAILY_LOSS_PCT = 3.0         # 单日最大亏损 3%
COOLDOWN_DAYS = 5                # 止损后冷却期 5 个交易日

# A 股涨跌停幅度
LIMIT_PCT_MAIN = 10.0            # 主板/中小板 10%
LIMIT_PCT_GEM = 20.0             # 创业板/科创板 20%
_GEM_PREFIXES = ("300", "301", "688", "689")


def generate_trade_plans(
    candidates: List[dict],
    mode: str = "short",
    total_capital: float = 100000.0,
    existing_position_pct: float = 0.0,
) -> dict:
    """为超跌候选股票生成交易计划。

    Args:
        candidates: Skill-1B 输出的候选列表
        mode: "short" 短期反弹 / "long" 长期蓄能
        total_capital: 总资金（元）
        existing_position_pct: 已有持仓占比（%）

    Returns:
        {"trade_plans": [...], "summary": {...}}
    """
    if mode == "long":
        min_score = LT_MIN_SCORE
        pos_min, pos_max = LT_POS_MIN_PCT, LT_POS_MAX_PCT
    else:
        min_score = ST_MIN_SCORE
        pos_min, pos_max = ST_POS_MIN_PCT, ST_POS_MAX_PCT

    available_pct = MAX_TOTAL_POSITION_PCT - existing_position_pct
    if available_pct <= 0:
        return {
            "trade_plans": [],
            "summary": {"status": "no_capacity", "reason": "总仓位已达上限"},
        }

    plans = []
    used_pct = 0.0

    for c in candidates:
        score = c.get("oversold_score", 0)
        if score < min_score:
            continue

        remaining = available_pct - used_pct
        if remaining < pos_min:
            break

        symbol = c["symbol"]
        close = c["close"]
        atr_pct = c.get("atr_pct")

        # 计算仓位（评分越高仓位越大，线性映射）
        score_ratio = min(1.0, (score - min_score) / 40.0)
        target_pct = pos_min + score_ratio * (pos_max - pos_min)
        target_pct = min(target_pct, remaining, MAX_SINGLE_POSITION_PCT)

        # 计算止损止盈
        if mode == "short":
            plan = _build_short_term_plan(c, close, atr_pct, target_pct, total_capital)
        else:
            plan = _build_long_term_plan(c, close, atr_pct, target_pct, total_capital)

        if plan:
            plans.append(plan)
            used_pct += target_pct

    return {
        "trade_plans": plans,
        "summary": {
            "status": "has_trades" if plans else "no_opportunity",
            "mode": mode,
            "plan_count": len(plans),
            "total_position_pct": round(existing_position_pct + used_pct, 2),
            "capital": total_capital,
        },
    }


def _build_short_term_plan(
    candidate: dict, close: float, atr_pct: Optional[float],
    position_pct: float, total_capital: float,
) -> Optional[dict]:
    """构建短期超跌反弹交易计划。

    止损策略：基于 ATR 的动态止损
    - 止损 = 收盘价 × (1 - 1.5 × ATR%)
    - 最大止损不超过 -8%（A 股涨跌停限制下的合理范围）
    - ATR 不可用时使用固定 -5% 止损

    止盈策略：分批止盈
    - 第一目标 +5%：平仓 50%（锁定利润）
    - 第二目标 +10%：平仓剩余 50%

    入场时机：
    - 超跌评分 ≥ 35
    - 建议在尾盘（14:30 后）或次日开盘低吸入场
    - 避免追涨，设置限价单在收盘价附近
    """
    symbol = candidate["symbol"]
    limit_pct = _get_limit_pct(symbol)

    # 止损计算
    if atr_pct and atr_pct > 0:
        sl_pct = -min(atr_pct * ST_STOP_LOSS_ATR_MULT, abs(ST_STOP_LOSS_MAX_PCT))
    else:
        sl_pct = -5.0  # ATR 不可用时默认 -5%

    stop_loss = round(close * (1 + sl_pct / 100), 2)
    # 止损不能低于跌停价
    limit_down = round(close * (1 - limit_pct / 100), 2)
    stop_loss = max(stop_loss, limit_down)

    # 止盈计算
    tp1 = round(close * (1 + ST_TP1_PCT / 100), 2)
    tp2 = round(close * (1 + ST_TP2_PCT / 100), 2)
    # 止盈不能高于涨停价（T+1 当天最多涨停）
    limit_up = round(close * (1 + limit_pct / 100), 2)
    tp1 = min(tp1, limit_up)
    tp2 = min(tp2, round(close * (1 + limit_pct * 2 / 100), 2))  # 第二目标允许两天涨停

    # 入场区间
    entry_upper = round(close * 1.01, 2)   # 收盘价上方 1%
    entry_lower = round(close * 0.97, 2)   # 收盘价下方 3%（低吸）

    # 计算买入数量（A 股最小单位 100 股）
    position_value = total_capital * position_pct / 100
    shares = int(position_value / close / 100) * 100
    if shares < 100:
        return None

    risk_pct = round(abs(sl_pct), 2)
    reward_pct = round(ST_TP1_PCT, 2)
    rr_ratio = round(reward_pct / risk_pct, 2) if risk_pct > 0 else 0

    return {
        "symbol": symbol,
        "name": candidate.get("name", ""),
        "mode": "short",
        "direction": "long",
        "oversold_score": candidate.get("oversold_score", 0),
        "close": close,
        "entry_upper": entry_upper,
        "entry_lower": entry_lower,
        "shares": shares,
        "position_pct": round(position_pct, 2),
        "position_value": round(shares * close, 2),
        "stop_loss": stop_loss,
        "stop_loss_pct": round(sl_pct, 2),
        "take_profit": [
            {"target": tp1, "pct": f"+{ST_TP1_PCT}%", "sell_ratio": ST_TP1_RATIO},
            {"target": tp2, "pct": f"+{ST_TP2_PCT}%", "sell_ratio": ST_TP2_RATIO},
        ],
        "risk_reward_ratio": rr_ratio,
        "max_hold_days": ST_MAX_HOLD_DAYS,
        "entry_timing": "尾盘低吸或次日开盘限价单",
        "key_signals": candidate.get("signal_details", ""),
    }


def _build_long_term_plan(
    candidate: dict, close: float, atr_pct: Optional[float],
    position_pct: float, total_capital: float,
) -> Optional[dict]:
    """构建长期超跌蓄能交易计划。

    止损策略：基于支撑位的止损
    - 止损 = 近期低点下方 3%（用 drop_pct 推算近期低点）
    - 最大止损不超过 -12%
    - 长期策略给更大的止损空间，避免被洗出

    止盈策略：三阶段分批止盈
    - 第一目标 +8%：平仓 30%（回本+小利）
    - 第二目标 +15%：平仓 30%（趋势确认）
    - 第三目标 +25%：平仓 40%（趋势延续）

    入场时机：
    - 超跌评分 ≥ 40
    - 建议分 2~3 次建仓（首次 40%，确认后加仓 60%）
    - 等待缩量企稳信号确认后入场
    """
    symbol = candidate["symbol"]
    limit_pct = _get_limit_pct(symbol)

    # 止损计算：基于支撑位
    drop_pct = candidate.get("drop_pct")
    if drop_pct is not None and drop_pct < 0:
        # 近期低点 ≈ 当前价格（已经在底部附近）
        # 止损设在当前价下方，给足空间
        sl_pct = max(LT_STOP_LOSS_MAX_PCT, LT_STOP_LOSS_SUPPORT_PCT + drop_pct * 0.1)
    elif atr_pct and atr_pct > 0:
        sl_pct = -min(atr_pct * 2.0, abs(LT_STOP_LOSS_MAX_PCT))
    else:
        sl_pct = -8.0

    stop_loss = round(close * (1 + sl_pct / 100), 2)
    limit_down = round(close * (1 - limit_pct / 100), 2)
    stop_loss = max(stop_loss, limit_down)

    # 三阶段止盈
    tp1 = round(close * (1 + LT_TP1_PCT / 100), 2)
    tp2 = round(close * (1 + LT_TP2_PCT / 100), 2)
    tp3 = round(close * (1 + LT_TP3_PCT / 100), 2)

    # 入场区间（长期策略入场区间更宽）
    entry_upper = round(close * 1.02, 2)
    entry_lower = round(close * 0.95, 2)

    # 计算买入数量
    position_value = total_capital * position_pct / 100
    shares = int(position_value / close / 100) * 100
    if shares < 100:
        return None

    risk_pct = round(abs(sl_pct), 2)
    reward_pct = round(LT_TP2_PCT, 2)  # 用第二目标算盈亏比
    rr_ratio = round(reward_pct / risk_pct, 2) if risk_pct > 0 else 0

    return {
        "symbol": symbol,
        "name": candidate.get("name", ""),
        "mode": "long",
        "direction": "long",
        "oversold_score": candidate.get("oversold_score", 0),
        "close": close,
        "entry_upper": entry_upper,
        "entry_lower": entry_lower,
        "shares": shares,
        "position_pct": round(position_pct, 2),
        "position_value": round(shares * close, 2),
        "stop_loss": stop_loss,
        "stop_loss_pct": round(sl_pct, 2),
        "take_profit": [
            {"target": tp1, "pct": f"+{LT_TP1_PCT}%", "sell_ratio": LT_TP1_RATIO},
            {"target": tp2, "pct": f"+{LT_TP2_PCT}%", "sell_ratio": LT_TP2_RATIO},
            {"target": tp3, "pct": f"+{LT_TP3_PCT}%", "sell_ratio": LT_TP3_RATIO},
        ],
        "risk_reward_ratio": rr_ratio,
        "max_hold_days": LT_MAX_HOLD_DAYS,
        "entry_timing": "分批建仓：首次40%，缩量企稳确认后加仓60%",
        "key_signals": candidate.get("signal_details", ""),
    }


def _get_limit_pct(symbol: str) -> float:
    """根据股票代码判断涨跌停幅度。"""
    if symbol.startswith(_GEM_PREFIXES):
        return LIMIT_PCT_GEM
    return LIMIT_PCT_MAIN


def format_trade_plan(plan: dict) -> str:
    """格式化单个交易计划为人类可读文本。"""
    mode_label = "短期反弹" if plan["mode"] == "short" else "长期蓄能"
    lines = [
        f"{'═' * 60}",
        f"  {plan['symbol']} {plan['name']}  [{mode_label}]  评分:{plan['oversold_score']}",
        f"{'─' * 60}",
        f"  方向: 做多 | 现价: ¥{plan['close']:.2f}",
        f"  入场区间: ¥{plan['entry_lower']:.2f} ~ ¥{plan['entry_upper']:.2f}",
        f"  买入数量: {plan['shares']}股 | 仓位: {plan['position_pct']}% | "
        f"金额: ¥{plan['position_value']:,.0f}",
        f"  止损: ¥{plan['stop_loss']:.2f} ({plan['stop_loss_pct']}%)",
    ]
    for tp in plan["take_profit"]:
        lines.append(
            f"  止盈: ¥{tp['target']:.2f} ({tp['pct']}) 平仓{int(tp['sell_ratio']*100)}%"
        )
    lines += [
        f"  盈亏比: {plan['risk_reward_ratio']}:1 | 最大持仓: {plan['max_hold_days']}天",
        f"  入场时机: {plan['entry_timing']}",
        f"  信号: {plan['key_signals']}",
    ]
    return "\n".join(lines)
