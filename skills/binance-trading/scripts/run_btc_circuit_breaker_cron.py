#!/usr/bin/env python3
"""
BTC 熔断器独立定时任务。

独立检测 BTC 市场暴跌并执行持仓保护，在 Skill4 之外提供全局保护。
- 检查频率：每 5 分钟（可配置）
- 首次触发预警时执行具体操作（收紧止损/减仓/全平）
- 同一级别重复触发不重复操作
- 级别升级时才执行新操作
- 输出 Markdown 或 JSON 格式报告

用途：
  1. 有持仓时：BTC 暴跌时直接执行减仓/平仓保护
  2. 无持仓时：设置 Paper Mode 阻止新开仓
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.binance_public import BinancePublicClient
from src.infra.circuit_breaker import CircuitBreaker, CircuitLevel
from src.infra.exchange_rules import (
    LazyBinanceTradingRuleProvider,
    normalize_order_quantity,
)
from src.infra.risk_controller import RiskController
from src.infra.rate_limiter import RateLimiter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("btc_circuit")


DB_DIR = os.path.join(PROJECT_ROOT, "data")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _calc_ema(closes: list, period: int) -> Optional[float]:
    """计算 EMA"""
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


LAST_LEVEL_FILE = os.path.join(DB_DIR, "circuit_breaker_last_level.json")
LAST_LEVEL_LOCK = os.path.join(DB_DIR, "circuit_breaker_last_level.lock")


def _load_last_triggered_level() -> int:
    """加载上次触发的级别（线程安全）"""
    lock_path = Path(LAST_LEVEL_LOCK)
    level_path = Path(LAST_LEVEL_FILE)
    acquired = False
    try:
        for _ in range(10):
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.1)
        if not acquired:
            log.warning("[btc_circuit] 获取级别文件锁超时，使用默认级别0")
            return 0
        if level_path.exists():
            try:
                data = json.loads(level_path.read_text())
                return data.get("level", 0)
            except Exception:
                return 0
        return 0
    finally:
        if acquired:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except Exception:
                pass


def _save_last_triggered_level(level: int) -> None:
    """保存触发的级别（原子写入）"""
    lock_path = Path(LAST_LEVEL_LOCK)
    level_path = Path(LAST_LEVEL_FILE)
    acquired = False
    tmp_path = level_path.with_suffix(".tmp")
    try:
        for _ in range(10):
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.1)
        if not acquired:
            log.warning("[btc_circuit] 获取级别文件锁超时，跳过保存")
            return
        Path(DB_DIR).mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps({"level": level, "timestamp": time.time()}))
        tmp_path.replace(level_path)
    except Exception as exc:
        log.warning("[btc_circuit] 保存触发级别失败: %s", exc)
    finally:
        if acquired:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except Exception:
                pass


def _tighten_stop_loss(
    fapi_client: BinanceFapiClient,
    trading_rule_provider: LazyBinanceTradingRuleProvider,
    tighten_ratio: float,
) -> list[dict]:
    """
    收紧所有持仓的止损价。

    使用 close_position=True 确保止损单触发时立即平仓，
    避免旧止损单与新止损单冲突导致数量不匹配。

    Args:
        fapi_client: Binance 客户端
        trading_rule_provider: 交易规则提供者
        tighten_ratio: 止损收紧比例

    Returns:
        操作结果列表。
    """
    results = []
    if tighten_ratio <= 0:
        return results

    try:
        positions = fapi_client.get_positions()
        for pos in positions:
            if not pos.symbol or abs(pos.position_amt) <= 0:
                continue

            direction = "LONG" if pos.position_amt > 0 else "SHORT"
            current_price = float(pos.raw.get("markPrice", 0)) or pos.entry_price
            if not current_price or current_price <= 0:
                log.warning(
                    "[btc_circuit] 收紧止损: %s 持仓价格异常 %.4f，跳过",
                    pos.symbol,
                    current_price or 0,
                )
                continue

            if direction == "LONG":
                new_sl = current_price * (1 - tighten_ratio)
                side = "SELL"
            else:
                new_sl = current_price * (1 + tighten_ratio)
                side = "BUY"

            min_stop_distance = current_price * 0.005
            if abs(current_price - new_sl) < min_stop_distance:
                log.warning(
                    "[btc_circuit] 收紧止损: %s 止损距离太近 %.4f < %.4f，跳过",
                    pos.symbol,
                    abs(current_price - new_sl),
                    min_stop_distance,
                )
                continue

            log.info(
                "[btc_circuit] 设置止损: %s %s @ %s (收紧 %.0f%%, close_position=True)",
                direction,
                pos.symbol,
                f"{new_sl:.6f}",
                tighten_ratio * 100,
            )

            try:
                result = fapi_client.place_stop_market_order(
                    symbol=pos.symbol,
                    side=side,
                    quantity=0,
                    stop_price=new_sl,
                    close_position=True,
                )
                results.append(
                    {
                        "symbol": pos.symbol,
                        "action": "tighten_sl",
                        "status": "success",
                        "order_id": result.order_id,
                        "new_sl": new_sl,
                        "qty": "close_position",
                    }
                )
            except Exception as exc:
                log.warning("[btc_circuit] 设置止损失败: %s %s: %s", pos.symbol, exc)
                results.append(
                    {
                        "symbol": pos.symbol,
                        "action": "tighten_sl",
                        "status": "failed",
                        "error": str(exc),
                    }
                )

    except Exception as exc:
        log.error("[btc_circuit] 获取持仓失败: %s", exc)

    return results


def _reduce_positions(
    fapi_client: BinanceFapiClient,
    trading_rule_provider: LazyBinanceTradingRuleProvider,
    reduce_ratio: float,
) -> list[dict]:
    """
    减仓指定比例（使用 reduceOnly=True 确保只减仓不开仓）。

    Args:
        fapi_client: Binance 客户端
        trading_rule_provider: 交易规则提供者
        reduce_ratio: 减仓比例（0 < reduce_ratio <= 1.0）

    Returns:
        操作结果列表。
    """
    if reduce_ratio <= 0 or reduce_ratio > 1.0:
        log.error(
            "[btc_circuit] 减仓比例 %.2f 非法（必须 0 < ratio <= 1.0），跳过",
            reduce_ratio,
        )
        return []
    results = []
    try:
        positions = fapi_client.get_positions()
        for pos in positions:
            if not pos.symbol or abs(pos.position_amt) <= 0:
                continue

            direction = "LONG" if pos.position_amt > 0 else "SHORT"
            current_price = float(pos.raw.get("markPrice", 0)) or pos.entry_price
            if not current_price or current_price <= 0:
                log.warning(
                    "[btc_circuit] 减仓: %s 持仓价格异常 %.4f，跳过",
                    pos.symbol,
                    current_price or 0,
                )
                continue
            raw_qty = abs(pos.position_amt) * reduce_ratio

            rule = trading_rule_provider.get_rule(pos.symbol)
            if rule:
                normalized_qty = normalize_order_quantity(
                    symbol=pos.symbol,
                    quantity=raw_qty,
                    price=current_price,
                    rule=rule,
                )
            else:
                normalized_qty = raw_qty

            if not normalized_qty or normalized_qty <= 0:
                log.warning(
                    "[btc_circuit] 减仓数量规范化失败: %s qty=%.4f",
                    pos.symbol,
                    raw_qty,
                )
                continue

            log.info(
                "[btc_circuit] 减仓: %s %s %.4f (%.0f%%)",
                direction,
                pos.symbol,
                normalized_qty,
                reduce_ratio * 100,
            )

            try:
                result = fapi_client.place_market_order(
                    symbol=pos.symbol,
                    side="SELL" if direction == "LONG" else "BUY",
                    quantity=normalized_qty,
                    reduce_only=True,
                )
                results.append(
                    {
                        "symbol": pos.symbol,
                        "action": "reduce",
                        "status": "success",
                        "qty": normalized_qty,
                        "ratio": reduce_ratio,
                        "order_id": result.order_id,
                    }
                )
            except Exception as exc:
                log.warning("[btc_circuit] 减仓失败: %s %s: %s", pos.symbol, exc)
                results.append(
                    {
                        "symbol": pos.symbol,
                        "action": "reduce",
                        "status": "failed",
                        "error": str(exc),
                    }
                )

    except Exception as exc:
        log.error("[btc_circuit] 获取持仓失败: %s", exc)

    return results


def _close_all_positions(
    fapi_client: BinanceFapiClient,
    trading_rule_provider: LazyBinanceTradingRuleProvider,
) -> list[dict]:
    """
    全平所有持仓（使用 reduceOnly=True 确保只平仓不开仓）。

    Args:
        fapi_client: Binance 客户端
        trading_rule_provider: 交易规则提供者

    Returns:
        操作结果列表。
    """
    results = []
    try:
        positions = fapi_client.get_positions()
        for pos in positions:
            if not pos.symbol or abs(pos.position_amt) <= 0:
                continue

            direction = "LONG" if pos.position_amt > 0 else "SHORT"
            current_price = float(pos.raw.get("markPrice", 0)) or pos.entry_price
            if not current_price or current_price <= 0:
                log.warning(
                    "[btc_circuit] 全平: %s 持仓价格异常 %.4f，跳过",
                    pos.symbol,
                    current_price or 0,
                )
                continue
            raw_qty = abs(pos.position_amt)

            rule = trading_rule_provider.get_rule(pos.symbol)
            if rule:
                normalized_qty = normalize_order_quantity(
                    symbol=pos.symbol,
                    quantity=raw_qty,
                    price=current_price,
                    rule=rule,
                )
            else:
                normalized_qty = raw_qty

            if not normalized_qty or normalized_qty <= 0:
                log.warning(
                    "[btc_circuit] 全平数量规范化失败: %s qty=%.4f",
                    pos.symbol,
                    raw_qty,
                )
                continue

            log.warning(
                "[btc_circuit] 🔴 全平: %s %s %.4f",
                direction,
                pos.symbol,
                normalized_qty,
            )

            try:
                result = fapi_client.place_market_order(
                    symbol=pos.symbol,
                    side="SELL" if direction == "LONG" else "BUY",
                    quantity=normalized_qty,
                    reduce_only=True,
                )
                results.append(
                    {
                        "symbol": pos.symbol,
                        "action": "close",
                        "status": "success",
                        "qty": normalized_qty,
                        "order_id": result.order_id,
                    }
                )
            except Exception as exc:
                log.error("[btc_circuit] 全平失败: %s %s: %s", pos.symbol, exc)
                results.append(
                    {
                        "symbol": pos.symbol,
                        "action": "close",
                        "status": "failed",
                        "error": str(exc),
                    }
                )

    except Exception as exc:
        log.error("[btc_circuit] 获取持仓失败: %s", exc)

    return results


def get_btc_market_info(
    public_client: BinancePublicClient,
) -> dict[str, Any]:
    """
    获取 BTC 详细市场信息。

    Returns:
        包含 BTC 市场详细数据的字典。
    """
    result = {
        "checked_at": utc_now(),
        "current_price": 0.0,
        "returns_1h": 0.0,
        "returns_6h": 0.0,
        "returns_24h": 0.0,
        "volatility_1h": 0.0,
        "volume_ratio_24h": 0.0,
        "ema_5": None,
        "ema_20": None,
        "ema_60": None,
        "sma_20": None,
        "trend": "UNKNOWN",
        "error": None,
    }

    try:
        klines_1h = public_client.get_klines("BTCUSDT", "1h", 100)

        if not klines_1h or len(klines_1h) < 60:
            result["error"] = "K线数据不足"
            return result

        closes_1h = [float(k[4]) for k in klines_1h]
        volumes_1h = [float(k[5]) for k in klines_1h]

        current_price = closes_1h[-1]

        result["current_price"] = current_price
        result["returns_1h"] = (
            (closes_1h[-1] - closes_1h[-2]) / closes_1h[-2] * 100
            if len(closes_1h) >= 2
            else 0
        )
        result["returns_6h"] = (
            (closes_1h[-1] - closes_1h[-7]) / closes_1h[-7] * 100
            if len(closes_1h) >= 7
            else 0
        )
        result["returns_24h"] = (
            (closes_1h[-1] - closes_1h[-25]) / closes_1h[-25] * 100
            if len(closes_1h) >= 25
            else 0
        )

        if len(closes_1h) >= 2:
            returns = [
                (closes_1h[i] - closes_1h[i - 1]) / closes_1h[i - 1] * 100
                for i in range(1, len(closes_1h))
            ]
            result["volatility_1h"] = (
                (max(returns[-24:]) - min(returns[-24:])) if len(returns) >= 24 else 0
            )

        avg_volume_24h = sum(volumes_1h[-25:-1]) / 24 if len(volumes_1h) >= 25 else 1
        current_volume = volumes_1h[-1] if volumes_1h else 0
        result["volume_ratio_24h"] = (
            current_volume / avg_volume_24h if avg_volume_24h > 0 else 0
        )

        result["ema_5"] = _calc_ema(closes_1h, 5)
        result["ema_20"] = _calc_ema(closes_1h, 20)
        result["ema_60"] = _calc_ema(closes_1h, 60) if len(closes_1h) >= 60 else None
        result["sma_20"] = sum(closes_1h[-20:]) / 20 if len(closes_1h) >= 20 else None

        if result["ema_5"] and result["ema_20"]:
            if result["ema_5"] > result["ema_20"]:
                result["trend"] = "UPTREND"
            elif result["ema_5"] < result["ema_20"]:
                result["trend"] = "DOWNTREND"
            else:
                result["trend"] = "NEUTRAL"

    except Exception as exc:
        result["error"] = str(exc)
        log.error("[btc_circuit] BTC市场信息获取失败: %s", exc)

    return result


def check_btc_circuit_breaker(
    cb: CircuitBreaker,
    public_client: BinancePublicClient,
) -> dict[str, Any]:
    """
    执行 BTC 熔断检查。

    Returns:
        包含检查结果的字典。
    """
    result = {
        "checked_at": utc_now(),
        "level": None,
        "level_name": None,
        "btc_price": 0.0,
        "btc_1h_return_pct": 0.0,
        "short_term_drop": False,
        "tighten_ratio": 0.0,
        "reduce_ratio": 0.0,
        "paper_mode_before": False,
        "paper_mode_after": False,
        "action_taken": None,
        "error": None,
    }

    try:
        cb_result = cb.check_from_klines(
            lambda sym, iv, lm: public_client.get_klines(sym, iv, lm)
        )
    except Exception as exc:
        result["error"] = str(exc)
        log.error("[btc_circuit] BTC K线获取失败: %s", exc)
        return result

    result["level"] = cb_result.level.value
    result["level_name"] = cb_result.level.name
    result["btc_price"] = cb_result.btc_price
    result["btc_1h_return_pct"] = cb_result.btc_1h_return_pct
    result["short_term_drop"] = cb_result.short_term_drop
    result["tighten_ratio"] = cb_result.tighten_ratio
    result["reduce_ratio"] = cb_result.reduce_ratio

    return result


def run_report(
    interval_minutes: int = 5,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    执行一次 BTC 熔断检查并生成报告。

    Args:
        interval_minutes: 检查间隔（分钟），仅用于报告输出
        dry_run: True 时只检查不实际操作
    """
    errors: list[str] = []
    actions_results: list[dict] = []

    try:
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            raise RuntimeError("缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET 环境变量")

        os.makedirs(DB_DIR, exist_ok=True)
        rate_limiter = RateLimiter()
        public_client = BinancePublicClient(rate_limiter=rate_limiter)
        fapi_client = BinanceFapiClient(api_key, api_secret, rate_limiter=rate_limiter)
        trading_rule_provider = LazyBinanceTradingRuleProvider(public_client)
        risk_controller = RiskController(
            db_path=os.path.join(DB_DIR, "trading_state.db")
        )
        cb = CircuitBreaker(db_path=DB_DIR)

        paper_mode_before = risk_controller.is_paper_mode()
        market_info = get_btc_market_info(public_client)
        check_result = check_btc_circuit_breaker(cb, public_client)
        check_result["paper_mode_before"] = paper_mode_before

        last_level = _load_last_triggered_level()
        current_level = check_result["level"] or 0
        action_taken = None
        paper_mode_after = paper_mode_before

        if current_level > CircuitLevel.NORMAL.value:
            if current_level > last_level:
                if dry_run:
                    action_taken = (
                        f"[DRY RUN] Would execute Level {current_level} protection"
                    )
                    log.info(
                        "[btc_circuit] [DRY RUN] Level %d 触发（上次 %d），将执行保护操作",
                        current_level,
                        last_level,
                    )
                else:
                    if current_level >= CircuitLevel.CLOSE_ALL.value:
                        log.warning(
                            "[btc_circuit] 🔴 Level %d 触发，执行全平 + Paper Mode",
                            current_level,
                        )
                        close_results = _close_all_positions(
                            fapi_client, trading_rule_provider
                        )
                        actions_results.extend(close_results)
                        risk_controller.enable_paper_mode("btc_circuit_breaker_cron")
                        paper_mode_after = True
                        action_taken = (
                            f"CLOSE_ALL + Paper Mode ({len(close_results)} 持仓)"
                        )
                        _save_last_triggered_level(current_level)

                    elif current_level >= CircuitLevel.TIGHTEN.value:
                        tighten_r = check_result["tighten_ratio"]
                        reduce_r = check_result["reduce_ratio"]
                        level_icon = (
                            "🟠" if current_level >= CircuitLevel.REDUCE.value else "🟡"
                        )
                        log.warning(
                            "[btc_circuit] %s Level %d 触发，减仓%.0f%% + 收紧止损%.0f%%",
                            level_icon,
                            current_level,
                            reduce_r * 100,
                            tighten_r * 100,
                        )
                        reduce_results = _reduce_positions(
                            fapi_client,
                            trading_rule_provider,
                            reduce_r,
                        )
                        tighten_results = _tighten_stop_loss(
                            fapi_client,
                            trading_rule_provider,
                            tighten_r,
                        )
                        actions_results.extend(reduce_results)
                        actions_results.extend(tighten_results)
                        action_taken = (
                            f"TIGHTEN {tighten_r * 100:.0f}% + REDUCE {reduce_r * 100:.0f}% "
                            f"(减仓{len(reduce_results)}持仓, 止损{len(tighten_results)}持仓)"
                        )
                        _save_last_triggered_level(current_level)

            else:
                action_taken = (
                    f"Level {current_level} (上次 {last_level}), 级别未升级，跳过操作"
                )
                log.info(
                    "[btc_circuit] Level %d 触发，但级别未升级（上次 %d），跳过操作",
                    current_level,
                    last_level,
                )
        else:
            if last_level > 0:
                _save_last_triggered_level(0)
                action_taken = f"Market recovered, reset level (was {last_level})"
            else:
                action_taken = "NORMAL, no action"

        check_result["action_taken"] = action_taken
        check_result["paper_mode_after"] = paper_mode_after
        check_result["last_level"] = last_level
        check_result["level_escalated"] = current_level > last_level

        report = {
            "task_name": "btc_circuit_breaker",
            "status": "ok" if not check_result["error"] else "error",
            "finished_at": utc_now(),
            "interval_minutes": interval_minutes,
            "dry_run": dry_run,
            "market": {
                "current_price": market_info.get("current_price", 0),
                "returns_1h": market_info.get("returns_1h", 0),
                "returns_6h": market_info.get("returns_6h", 0),
                "returns_24h": market_info.get("returns_24h", 0),
                "volatility_1h": market_info.get("volatility_1h", 0),
                "volume_ratio_24h": market_info.get("volume_ratio_24h", 0),
                "ema_5": market_info.get("ema_5"),
                "ema_20": market_info.get("ema_20"),
                "ema_60": market_info.get("ema_60"),
                "sma_20": market_info.get("sma_20"),
                "trend": market_info.get("trend", "UNKNOWN"),
            },
            "circuit_breaker": {
                "level": check_result["level_name"],
                "btc_price": check_result["btc_price"],
                "btc_1h_return_pct": check_result["btc_1h_return_pct"],
                "short_term_drop": check_result["short_term_drop"],
                "tighten_ratio": check_result["tighten_ratio"],
                "reduce_ratio": check_result["reduce_ratio"],
                "last_level": last_level,
                "level_escalated": check_result["level_escalated"],
            },
            "paper_mode": {
                "before": paper_mode_before,
                "after": paper_mode_after,
            },
            "actions": actions_results,
            "action_taken": action_taken,
            "errors": errors,
        }

        return report

    except Exception as exc:
        errors.append(str(exc))
        log.error("[btc_circuit] 执行失败: %s", exc)
        raise
    finally:
        pass


def render_markdown(report: dict) -> str:
    """渲染 Markdown 格式报告。"""
    market = report.get("market", {})
    cb = report.get("circuit_breaker", {})
    pm = report.get("paper_mode", {})
    level = cb.get("level", "N/A")
    action = report.get("action_taken", "None")
    pm_after = pm.get("after", False)

    current_price = market.get("current_price", 0)
    trend = market.get("trend", "UNKNOWN")

    level_icon = {
        "NORMAL": "🟢",
        "TIGHTEN": "🟡",
        "REDUCE": "🟠",
        "CLOSE_ALL": "🔴",
    }.get(level, "⚪")

    trend_icon = {
        "UPTREND": "📈",
        "DOWNTREND": "📉",
        "NEUTRAL": "➡️",
        "UNKNOWN": "❓",
    }.get(trend, "❓")

    lines = [
        f"## 🛡️ BTC 熔断器 {level_icon} **{level}** | {trend_icon} {trend} | ${current_price:,.0f}",
    ]

    if level != "NORMAL" or pm_after:
        lines.append(f"**动作**: {action}")

    actions = report.get("actions", [])
    if actions:
        lines.append("")
        for a in actions:
            status_icon = "✅" if a.get("status") == "success" else "❌"
            if a.get("action") == "tighten_sl":
                detail = f"止损→{a.get('new_sl', 0):.4f}"
            elif a.get("action") == "reduce":
                detail = f"减{a.get('ratio', 0) * 100:.0f}%"
            elif a.get("action") == "close":
                detail = f"平仓"
            else:
                detail = a.get("error", "-")
            lines.append(
                f"{status_icon} {a.get('symbol', '-')}: {a.get('action', '-')} | {detail}"
            )

    if report.get("errors"):
        lines.append(f"❌ 错误: {report['errors'][0]}")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BTC 熔断器独立定时任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 每 5 分钟运行一次（默认）
  python run_btc_circuit_breaker_cron.py

  # 每 1 分钟运行一次
  python run_btc_circuit_breaker_cron.py --interval 1

  # Dry run 模式（只检查不实际操作）
  python run_btc_circuit_breaker_cron.py --dry-run

  # JSON 格式输出
  python run_btc_circuit_breaker_cron.py --format json
        """,
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="检查间隔分钟数（默认 5）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只检查不实际操作（不启用 Paper Mode）",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="输出格式（默认 markdown）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        report = run_report(
            interval_minutes=args.interval,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        failure = {
            "task_name": "btc_circuit_breaker",
            "status": "failed",
            "finished_at": utc_now(),
            "errors": [str(exc)],
        }
        if args.format == "json":
            print(json.dumps(failure, ensure_ascii=False, indent=2))
        else:
            print("## 🛡️ BTC 熔断器报告")
            print("")
            print(f"**状态**: ❌ Failed")
            print(f"**错误**: {exc}")
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))


if __name__ == "__main__":
    main()
