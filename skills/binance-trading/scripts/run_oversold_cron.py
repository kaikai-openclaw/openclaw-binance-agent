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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

from report_utils import (
    safe_float,
    safe_int,
    fmt_optional as _fmt_optional,
    build_position_snapshots,
    build_protection_report,
    build_symbol_source_map,
    classify_protection_label,
    build_account_summary,
    build_decision,
    metadata_by_symbol as _metadata_by_symbol,
    protection_warnings as _protection_warnings,
    render_positions_markdown,
    render_protection_markdown,
    render_account_markdown,
    render_warnings_markdown,
    truncate_for_telegram,
)


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

    for item in scan["candidates"][:3]:
        lines.append(
            "- {symbol}: {score}/100, 24h {change:+.2f}%, RSI {rsi}, 费率 {funding}, {sig}".format(
                symbol=item.get("symbol", ""),
                score=item.get("oversold_score", 0),
                change=safe_float(item.get("price_change_pct")),
                rsi=_fmt_optional(item.get("rsi")),
                funding=_fmt_optional(item.get("funding_rate"), suffix="%"),
                sig=item.get("signal_details", "")[:50],
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
    for rating in display_ratings[:3]:
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
    lines.extend(render_positions_markdown(report["positions"], max_detail=3, compact=True))
    lines.append("")
    lines.extend(render_protection_markdown(report["protection_orders"], max_detail=3))
    lines.append("")
    lines.extend(render_account_markdown(account, risk))
    warn_lines = render_warnings_markdown(report.get("warnings", []), report.get("errors", []))
    if warn_lines:
        lines.append("")
        lines.extend(warn_lines)

    return truncate_for_telegram("\n".join(lines))


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

        strategy_tag = f"crypto_oversold_{args.mode}"
        rating_threshold, risk_ratio = memory_store.get_evolved_params(
            strategy_tag=strategy_tag,
        )
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
                    # ── 超跌策略参数 ──
                    # 短期(4h)：快进快出，保持默认
                    # 长期(1d)：波段反弹需要更多时间和空间
                    max_hold_hours=72.0 if args.mode == "long" else 24.0,
                    atr_tp_mult=4.5 if args.mode == "long" else 3.0,
                    trailing_stop_ratio=0.35 if args.mode == "long" else 0.5,
                    trailing_activation_mult=1.5 if args.mode == "long" else 1.0,
                    trailing_activation_mult_hv=2.0 if args.mode == "long" else 1.5,
                    high_vol_tp_mult=4.0 if args.mode == "long" else 3.0,
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
        # 合并本轮扫描来源 + 历史 StateStore 来源，确保所有持仓都有策略标签
        historical_map = build_symbol_source_map(state_store)
        full_source_map = {}
        for sym, (emoji, label) in historical_map.items():
            full_source_map[sym] = f"{emoji}{label}"
        full_source_map.update(source_map)  # 本轮扫描优先
        positions = build_position_snapshots(
            account_info.total_balance, raw_positions, full_source_map
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
