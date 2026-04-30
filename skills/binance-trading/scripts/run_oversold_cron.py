#!/usr/bin/env python3
"""
超跌交易定时任务固定报告入口。

脚本直接执行超跌扫描和交易流水线，并输出稳定结构的 JSON 或 Markdown。
定时任务应调用本脚本，而不是让模型根据零散命令输出临场总结。
"""
import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.binance_kline_cache import BinanceKlineCache
from src.infra.binance_public import BinancePublicClient
from src.infra.daily_pnl import calculate_daily_realized_pnl
from src.infra.exchange_rules import LazyBinanceTradingRuleProvider
from src.infra.memory_store import MemoryStore
from src.infra.rate_limiter import RateLimiter
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.infra.trade_sync import BinanceTradeSyncer
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.models.types import AccountState
from src.skills.crypto_oversold import LongTermOversoldSkill, ShortTermOversoldSkill
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.skills.skill3_strategy import Skill3Strategy
from src.skills.skill4_execute import Skill4Execute
from src.skills.skill5_evolve import Skill5Evolve


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("oversold_cron")

DB_DIR = os.path.join(PROJECT_ROOT, "data")


def load_schema(name: str) -> dict:
    path = os.path.join(PROJECT_ROOT, "config", "schemas", name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def make_account_provider(
    fapi_client: BinanceFapiClient,
    paper_mode: bool,
    tracked_symbols: Optional[set[str]] = None,
) -> AccountState:
    """创建供 Skill-3/5 使用的账户状态快照。"""
    tracked_symbols = tracked_symbols or set()
    info = fapi_client.get_account_info()
    positions = []
    for p in fapi_client.get_positions():
        raw = p.raw or {}
        mark_price = safe_float(raw.get("markPrice"), p.entry_price)
        positions.append({
            "symbol": p.symbol,
            "direction": "long" if p.position_amt > 0 else "short",
            "quantity": abs(p.position_amt),
            "entry_price": p.entry_price,
            "current_price": mark_price if mark_price > 0 else p.entry_price,
            "unrealized_pnl": p.unrealized_pnl,
        })
        tracked_symbols.add(p.symbol)
    daily_realized_pnl = calculate_daily_realized_pnl(
        fapi_client,
        tracked_symbols,
    )
    return AccountState(
        total_balance=info.total_balance,
        available_margin=info.available_balance,
        daily_realized_pnl=daily_realized_pnl,
        positions=positions,
        is_paper_mode=paper_mode,
    )


def make_market_price_provider(public_client: BinancePublicClient):
    cache: dict[str, Optional[float]] = {}

    def provider(symbol: str) -> Optional[float]:
        if symbol in cache:
            return cache[symbol]
        try:
            for ticker in public_client.get_tickers_24hr():
                last = safe_float(ticker.get("lastPrice"))
                cache[ticker["symbol"]] = last if last > 0 else None
        except Exception as exc:
            log.warning("拉取 ticker 失败: %s", exc)
            return None
        return cache.get(symbol)

    return provider


def build_position_snapshots(
    total_balance: float,
    positions: list[Any],
    source_map: Optional[dict[str, str]] = None,
) -> list[dict]:
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


def build_protection_report(
    positions: list[dict],
    algo_orders: list[dict],
) -> dict:
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


def classify_protection_label(side: str, trigger: float, entry: float) -> str:
    if entry <= 0 or trigger <= 0:
        return "条件单"
    if side == "SELL":
        return "止盈" if trigger > entry else "止损"
    if side == "BUY":
        return "止盈" if trigger < entry else "止损"
    return "条件单"


def build_account_summary(account: Any, positions: list[dict], paper_mode: bool) -> dict:
    total_balance = safe_float(getattr(account, "total_balance", 0))
    total_margin = sum(p["initial_margin"] for p in positions)
    total_notional = sum(p["notional_value"] for p in positions)
    daily_realized_pnl = safe_float(getattr(account, "daily_realized_pnl", 0))
    daily_loss_pct = (
        abs(min(daily_realized_pnl, 0.0)) / total_balance * 100
        if total_balance > 0
        else 0.0
    )
    return {
        "total_balance": total_balance,
        "available_margin": safe_float(getattr(account, "available_balance", 0)),
        "total_unrealized_pnl": safe_float(getattr(account, "total_unrealized_pnl", 0)),
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


def build_decision(
    ratings: list[dict],
    plans: list[dict],
    execution_results: list[dict],
    rating_threshold: int,
) -> dict:
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


def render_markdown(report: dict) -> str:
    scan = report["scan"]
    analysis = report["analysis"]
    decision = report["decision"]
    execution = report["triggered_trades"]
    account = report["account"]
    risk = report["risk"]
    lines = [
        "超跌交易定时任务报告",
        "",
        f"任务状态: {report['status']}",
        f"交易决策: {decision['action']}",
        f"原因: {decision['reason']}",
        f"Paper Mode: {str(account['paper_mode']).lower()}",
        "",
        "扫描结果:",
        f"- 市场状态: {scan.get('market_regime', {}).get('status', 'unknown')} "
        f"({scan.get('market_regime', {}).get('reason', '')})",
        f"- 全部交易对: {scan['filter_summary'].get('total_tickers', 0)}",
        f"- 基础过滤后: {scan['filter_summary'].get('after_base_filter', 0)}",
        f"- 超跌候选: {scan['filter_summary'].get('after_oversold_filter', 0)}",
        f"- 最终输出: {scan['filter_summary'].get('output_count', 0)}",
    ]

    for item in scan["candidates"][:10]:
        lines.append(
            "- {symbol}: 超跌评分 {score}/100, 24h {change:+.2f}%, "
            "RSI {rsi}, 费率 {funding}, 信号 {signals}".format(
                symbol=item.get("symbol", ""),
                score=item.get("oversold_score", 0),
                change=safe_float(item.get("price_change_pct")),
                rsi=_fmt_optional(item.get("rsi")),
                funding=_fmt_optional(item.get("funding_rate"), suffix="%"),
                signals=item.get("signal_details", ""),
            )
        )

    lines.extend([
        "",
        "分析评级:",
        f"- 分析币种: {analysis['analyzed_count']}",
        f"- 达标币种: {analysis['passed_count']}",
        f"- 评级门槛: {analysis['rating_threshold']}",
    ])
    display_ratings = analysis.get("all_ratings") or analysis["ratings"]
    for rating in display_ratings[:10]:
        passed = "达标" if rating.get("rating_score", 0) >= analysis["rating_threshold"] else "未达标"
        lines.append(
            f"- {rating.get('symbol')}: {rating.get('rating_score')}/10, "
            f"信号 {rating.get('signal')}, 置信度 {rating.get('confidence', 0):.0f}%, "
            f"{passed}"
        )

    lines.extend([
        "",
        "已触发/已执行交易:",
        f"- 本轮开仓/成交: {decision['executed_count']}",
        f"- 本轮风控拒绝: {decision['risk_blocked_count']}",
        f"- 本轮执行失败: {decision['execution_failed_count']}",
        f"- 服务端已平仓同步: {execution['closed_since_last_run_count']}",
    ])
    for item in execution["this_run"]:
        lines.append(
            f"- {item.get('symbol')} {item.get('direction')} → {item.get('status')} "
            f"数量 {item.get('executed_quantity', 0)} 价格 {item.get('executed_price', 0)} "
            f"原因 {item.get('reason', '')}"
        )

    lines.append("")
    lines.append("当前持仓:")
    if report["positions"]:
        for pos in report["positions"]:
            lines.extend([
                f"- {pos['symbol']} {pos['direction']} ({pos['source']})",
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
    else:
        lines.append("- 当前无持仓")

    lines.append("")
    lines.append("保护单状态:")
    for symbol, health in report["protection_orders"]["health"].items():
        duplicate = health["duplicate_protection_orders"]
        detail = (
            f"止损 {health['stop_loss_count']} 张, "
            f"止盈 {health['take_profit_count']} 张"
        )
        if duplicate:
            detail += f", 重复保护单 {duplicate} 张"
        lines.append(f"- {symbol}: {health['status']} ({detail})")

    lines.extend([
        "",
        "账户状态:",
        f"- 总资金: {account['total_balance']:.2f} USDT",
        f"- 可用保证金: {account['available_margin']:.2f} USDT",
        f"- 未实现盈亏: {account['total_unrealized_pnl']:+.2f} USDT",
        f"- 持仓保证金: {account['total_position_margin']:.2f} USDT",
        f"- 持仓资金占比: {account['total_position_margin_pct']:.2f}%",
        f"- 日亏损: {account['daily_loss_pct']:.2f}%",
        "",
        "风险状态:",
        f"- 单笔保证金上限: {risk['single_trade_margin_limit_pct']}%",
        f"- 单币种持仓上限: {risk['single_symbol_position_limit_pct']}%",
        f"- 日亏损停止阈值: {risk['daily_loss_stop_pct']}%",
        f"- 当前状态: {risk['risk_status']}",
    ])

    if report["warnings"] or report["errors"]:
        lines.append("")
        lines.append("异常与注意事项:")
        for warning in report["warnings"]:
            lines.append(f"- WARNING: {warning}")
        for error in report["errors"]:
            lines.append(f"- ERROR: {error}")

    return "\n".join(lines)


def _fmt_optional(value: Any, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def run_report(args: argparse.Namespace) -> dict:
    started_at = utc_now()
    monotonic_start = time.monotonic()
    run_id = str(uuid.uuid4())
    errors: list[str] = []
    warnings: list[str] = []
    ta_module: Optional[TradingAgentsModule] = None

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET 环境变量")

    os.makedirs(DB_DIR, exist_ok=True)
    state_store = StateStore(db_path=os.path.join(DB_DIR, "state_store.db"))
    memory_store = MemoryStore(db_path=os.path.join(DB_DIR, "trading_state.db"))
    risk_controller = RiskController(db_path=os.path.join(DB_DIR, "trading_state.db"))
    cache = BinanceKlineCache(os.path.join(DB_DIR, "binance_kline_cache.db"))
    rate_limiter = RateLimiter()
    public_client = BinancePublicClient(rate_limiter=rate_limiter, kline_cache=cache)
    fapi_client = BinanceFapiClient(api_key, api_secret, rate_limiter=rate_limiter)

    try:
        if args.paper:
            risk_controller.enable_paper_mode("oversold_cron_paper_flag")
        paper_mode = risk_controller.is_paper_mode()

        if args.mode == "long":
            oversold_skill = LongTermOversoldSkill(
                state_store, load_schema("crypto_oversold_input.json"),
                load_schema("crypto_oversold_output.json"), public_client
            )
        else:
            oversold_skill = ShortTermOversoldSkill(
                state_store, load_schema("crypto_oversold_input.json"),
                load_schema("crypto_oversold_output.json"), public_client
            )

        scan_input = {
            "trigger_time": started_at,
            "min_oversold_score": args.min_score,
            "max_candidates": args.max_candidates,
        }
        if args.symbols:
            scan_input["target_symbols"] = [
                s.strip().upper() for s in args.symbols.split(",") if s.strip()
            ]
        scan_data = oversold_skill.run(scan_input)
        scan_symbols = [c["symbol"] for c in scan_data.get("candidates", [])]
        source_map = {symbol: f"超跌{args.mode}" for symbol in scan_symbols}

        rating_threshold, risk_ratio = memory_store.get_evolved_params()
        s1_data: dict = {"candidates": [], "filter_summary": {}}
        s2_data: dict = {"ratings": [], "filtered_count": 0, "failed_symbols": []}
        s3_data: dict = {"trade_plans": [], "pipeline_status": "no_opportunity"}
        s4_data: dict = {"execution_results": [], "is_paper_mode": paper_mode}
        s5_data: dict = {}
        synced_closed_count = 0

        tracked_symbols = set(scan_symbols)
        account_provider = lambda: make_account_provider(
            fapi_client,
            paper_mode,
            tracked_symbols=tracked_symbols,
        )
        market_price_provider = make_market_price_provider(public_client)
        trading_rule_provider = LazyBinanceTradingRuleProvider(public_client)

        if scan_symbols:
            # 直接使用底层扫描器的结果，避免 Skill1 二次处理丢失深度指标
            s1_data = scan_data
            s1_id = state_store.save("skill1_output", s1_data)

            analyzer_fn = create_trading_agents_analyzer(fast_mode=args.fast)
            ta_module = TradingAgentsModule(analyzer=analyzer_fn)
            skill2 = Skill2Analyze(
                state_store=state_store,
                input_schema=load_schema("skill2_input.json"),
                output_schema=load_schema("skill2_output.json"),
                trading_agents=ta_module,
                rating_threshold=rating_threshold,
            )
            s2_input_id = state_store.save("skill2_input", {"input_state_id": s1_id})
            s2_id = skill2.execute(s2_input_id)
            s2_data = state_store.load(s2_id)

            if s2_data.get("ratings"):
                tracked_symbols.update(
                    rating.get("symbol", "")
                    for rating in s2_data.get("ratings", [])
                )
                skill3 = Skill3Strategy(
                    state_store=state_store,
                    input_schema=load_schema("skill3_input.json"),
                    output_schema=load_schema("skill3_output.json"),
                    risk_controller=risk_controller,
                    account_state_provider=account_provider,
                    market_price_provider=market_price_provider,
                    trading_rule_provider=trading_rule_provider,
                    risk_ratio=risk_ratio,
                    require_market_price=True,
                )
                s3_input_id = state_store.save("skill3_input", {"input_state_id": s2_id})
                s3_id = skill3.execute(s3_input_id)
                s3_data = state_store.load(s3_id)

                skill4 = Skill4Execute(
                    state_store=state_store,
                    input_schema=load_schema("skill4_input.json"),
                    output_schema=load_schema("skill4_output.json"),
                    binance_client=fapi_client,
                    risk_controller=risk_controller,
                    account_state_provider=account_provider,
                    poll_interval=30,
                    trading_rule_provider=trading_rule_provider,
                )
                s4_input_id = state_store.save("skill4_input", {"input_state_id": s3_id})
                s4_id = skill4.execute(s4_input_id)
                s4_data = state_store.load(s4_id)
                tracked_symbols.update(
                    result.get("symbol", "")
                    for result in s4_data.get("execution_results", [])
                )

                syncer = BinanceTradeSyncer(fapi_client, memory_store)
                sync_symbols = set(scan_symbols)
                sync_symbols.update(
                    r.get("symbol", "") for r in s4_data.get("execution_results", [])
                )
                synced_closed_count = syncer.sync_closed_trades(
                    symbols=sync_symbols,
                    metadata_by_symbol=_metadata_by_symbol(s4_data.get("execution_results", [])),
                )

                skill5 = Skill5Evolve(
                    state_store=state_store,
                    input_schema=load_schema("skill5_input.json"),
                    output_schema=load_schema("skill5_output.json"),
                    memory_store=memory_store,
                    account_state_provider=account_provider,
                    trade_syncer=None,
                    risk_controller=risk_controller,
                )
                s5_input_id = state_store.save("skill5_input", {"input_state_id": s4_id})
                s5_id = skill5.execute(s5_input_id)
                s5_data = state_store.load(s5_id)

        account_state = account_provider()
        account_info = fapi_client.get_account_info()
        raw_positions = fapi_client.get_positions()
        positions = build_position_snapshots(
            account_info.total_balance, raw_positions, source_map
        )
        algo_orders = fapi_client.get_open_algo_orders()
        protection = build_protection_report(positions, algo_orders)
        warnings.extend(_protection_warnings(protection))

        decision = build_decision(
            s2_data.get("ratings", []),
            s3_data.get("trade_plans", []),
            s4_data.get("execution_results", []),
            rating_threshold,
        )
        account_info.daily_realized_pnl = account_state.daily_realized_pnl
        account_summary = build_account_summary(account_info, positions, paper_mode)

        finished_at = utc_now()
        report = {
            "task_name": "超跌交易",
            "run_id": run_id,
            "mode": args.mode,
            "status": "success" if not errors else "partial_failed",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(time.monotonic() - monotonic_start, 3),
            "scan": {
                "filter_summary": scan_data.get("filter_summary", {}),
                "candidates": scan_data.get("candidates", []),
                "pipeline_run_id": scan_data.get("pipeline_run_id", ""),
                "market_regime": scan_data.get("market_regime", {}),
            },
            "analysis": {
                "rating_threshold": rating_threshold,
                "analyzed_count": len(s2_data.get("all_ratings", [])),
                "passed_count": len(s2_data.get("ratings", [])),
                "filtered_count": s2_data.get("filtered_count", 0),
                "failed_symbols": s2_data.get("failed_symbols", []),
                "ratings": s2_data.get("ratings", []),
                "all_ratings": s2_data.get("all_ratings", []),
                "summary": s2_data.get("analysis_summary", ""),
            },
            "decision": decision,
            "trade_plans": s3_data.get("trade_plans", []),
            "triggered_trades": {
                "this_run": s4_data.get("execution_results", []),
                "closed_since_last_run_count": synced_closed_count,
            },
            "positions": positions,
            "protection_orders": protection,
            "account": account_summary,
            "risk": {
                "single_trade_margin_limit_pct": 35,
                "single_symbol_position_limit_pct": 40,
                "daily_loss_stop_pct": 5,
                "risk_status": "paper_mode" if paper_mode else "normal",
            },
            "evolution": s5_data.get("evolution", {}),
            "warnings": warnings,
            "errors": errors,
        }
        report["markdown"] = render_markdown(report)
        return report
    except Exception as exc:
        errors.append(str(exc))
        raise
    finally:
        if ta_module:
            ta_module.shutdown()
        state_store.close()
        memory_store.close()
        risk_controller.close()
        cache.close()


def _metadata_by_symbol(execution_results: list[dict]) -> dict[str, dict[str, Any]]:
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


def _protection_warnings(protection: dict) -> list[str]:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="超跌交易定时任务固定报告")
    parser.add_argument("--mode", choices=["short", "long"], default="short")
    parser.add_argument("--min-score", type=int, default=25)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--fast", action="store_true", help="使用快速 LLM 分析")
    parser.add_argument("--paper", action="store_true", help="强制模拟盘")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        report = run_report(args)
    except Exception as exc:
        failure = {
            "task_name": "超跌交易",
            "status": "failed",
            "finished_at": utc_now(),
            "errors": [str(exc)],
        }
        if args.format == "json":
            print(json.dumps(failure, ensure_ascii=False, indent=2))
        else:
            print("超跌交易定时任务报告")
            print("")
            print("任务状态: failed")
            print(f"错误: {exc}")
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["markdown"])


if __name__ == "__main__":
    main()
