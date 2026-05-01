#!/usr/bin/env python3
"""
插针交易定时任务固定报告入口。

与超跌/反转流水线的关键区别：
  - 跳过 Skill-2（TradingAgents 评级），插针 Skill 直接输出 ratings
  - 更小仓位（risk_ratio=0.015）、更短持仓（max_hold_hours=12）
  - 更高频调度（建议 5~10 分钟一次）

流水线：插针扫描 → Skill-3 策略制定 → Skill-4 执行 → Skill-5 进化
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
from src.models.types import AccountState
from src.skills.crypto_wick import LongTermWickSkill, ShortTermWickSkill
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
log = logging.getLogger("wick_cron")

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
    decision = report["decision"]
    execution = report["triggered_trades"]
    account = report["account"]
    risk = report["risk"]
    lines = [
        "插针交易定时任务报告",
        "",
        f"任务状态: {report['status']}",
        f"交易决策: {decision['action']}",
        f"原因: {decision['reason']}",
        f"Paper Mode: {str(account['paper_mode']).lower()}",
        "",
        "扫描结果:",
        f"- 全部交易对: {scan['filter_summary'].get('total_tickers', 0)}",
        f"- 基础过滤后: {scan['filter_summary'].get('after_base_filter', 0)}",
        f"- 插针候选: {scan['filter_summary'].get('after_wick_filter', 0)}",
        f"- 最终输出: {scan['filter_summary'].get('output_count', 0)}",
    ]

    for item in scan["candidates"][:3]:
        direction_emoji = "📈" if item.get("direction") == "long" else "📉"
        lines.append(
            "- {emoji} {symbol}: {wick_type} {score}/100, "
            "影线{ratio:.1f}x, 深{depth:.1f}%, 量{vol}, 费{funding}".format(
                emoji=direction_emoji,
                symbol=item.get("symbol", ""),
                wick_type="下插针" if item.get("wick_type") == "lower_wick" else "上插针",
                score=item.get("wick_score", 0),
                ratio=safe_float(item.get("shadow_ratio")),
                depth=safe_float(item.get("wick_depth_pct")),
                vol=_fmt_optional(item.get("volume_surge"), suffix="x"),
                funding=_fmt_optional(item.get("funding_rate"), suffix="%"),
            )
        )

    # 插针模式跳过 Skill-2，直接展示自动生成的 ratings
    ratings = scan.get("ratings", [])
    lines.extend([
        "",
        "自动评级（跳过 TradingAgents）:",
        f"- 评级数量: {len(ratings)}",
    ])
    for rating in ratings[:3]:
        lines.append(
            f"- {rating.get('symbol')}: {rating.get('rating_score')}/10, "
            f"信号 {rating.get('signal')}, 置信度 {rating.get('confidence', 0):.0f}%"
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
            risk_controller.enable_paper_mode("wick_cron_paper_flag")
        paper_mode = risk_controller.is_paper_mode()

        if args.mode == "long":
            wick_skill = LongTermWickSkill(
                state_store, load_schema("crypto_wick_input.json"),
                load_schema("crypto_wick_output.json"), public_client
            )
        else:
            wick_skill = ShortTermWickSkill(
                state_store, load_schema("crypto_wick_input.json"),
                load_schema("crypto_wick_output.json"), public_client
            )

        scan_input: dict[str, Any] = {
            "trigger_time": started_at,
            "min_wick_score": args.min_score,
            "max_candidates": args.max_candidates,
        }
        if args.symbols:
            scan_input["target_symbols"] = [
                s.strip().upper() for s in args.symbols.split(",") if s.strip()
            ]
        if args.direction:
            scan_input["direction_filter"] = args.direction
        scan_data = wick_skill.run(scan_input)
        scan_symbols = [c["symbol"] for c in scan_data.get("candidates", [])]
        source_map = {symbol: f"🪡插针{args.mode}" for symbol in scan_symbols}

        strategy_tag = f"crypto_wick_{args.mode}"
        rating_threshold, risk_ratio = memory_store.get_evolved_params(
            strategy_tag=strategy_tag,
        )
        # 插针策略使用更保守的仓位（跳过了 TradingAgents 评级）
        risk_ratio = min(risk_ratio, 0.015)

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

        # 关键区别：插针 Skill 直接输出 ratings，跳过 Skill-2
        ratings = scan_data.get("ratings", [])

        if ratings:
            # 包装为 Skill-2 兼容的输出格式，供 Skill-3 消费
            s2_compatible = {
                "state_id": str(uuid.uuid4()),
                "ratings": ratings,
                "all_ratings": ratings,
                "filtered_count": 0,
                "failed_symbols": [],
                "analysis_summary": f"插针自动评级: {len(ratings)} 个候选",
            }
            s2_id = state_store.save("skill2_output", s2_compatible)

            tracked_symbols.update(
                rating.get("symbol", "") for rating in ratings
            )

            # Skill-3 策略制定（插针专属参数）
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
                # ── 插针策略专属参数 ──
                max_hold_hours=12.0 if args.mode == "short" else 24.0,
                atr_stop_mult=1.0,          # 止损更紧（插针天然定义了止损位）
                atr_tp_mult=2.5,            # 盈亏比 2.5:1
                min_stop_pct=0.003,         # 最小止损 0.3%
                max_stop_pct=0.08,          # 最大止损 8%
                trailing_stop_ratio=0.6,    # 更激进的追踪止损
                trailing_activation_mult=0.8,
                trailing_activation_mult_hv=1.2,
                high_vol_tp_mult=5.0,
            )
            s3_input_id = state_store.save("skill3_input", {"input_state_id": s2_id})
            s3_id = skill3.execute(s3_input_id)
            s3_data = state_store.load(s3_id)

            # Skill-4 执行（完全复用）
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

            # 同步服务端已平仓交易
            syncer = BinanceTradeSyncer(fapi_client, memory_store)
            sync_symbols = set(scan_symbols)
            sync_symbols.update(
                r.get("symbol", "") for r in s4_data.get("execution_results", [])
            )
            synced_closed_count = syncer.sync_closed_trades(
                symbols=sync_symbols,
                metadata_by_symbol=_metadata_by_symbol(s4_data.get("execution_results", [])),
            )

            # Skill-5 进化（完全复用）
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
        historical_map = build_symbol_source_map(state_store)
        full_source_map = {}
        for sym, (emoji, label) in historical_map.items():
            full_source_map[sym] = f"{emoji}{label}"
        full_source_map.update(source_map)
        positions = build_position_snapshots(
            account_info.total_balance, raw_positions, full_source_map
        )
        algo_orders = fapi_client.get_open_algo_orders()
        protection = build_protection_report(positions, algo_orders)
        warnings.extend(_protection_warnings(protection))

        decision = build_decision(
            ratings,
            s3_data.get("trade_plans", []),
            s4_data.get("execution_results", []),
            rating_threshold,
        )
        account_info.daily_realized_pnl = account_state.daily_realized_pnl
        account_summary = build_account_summary(account_info, positions, paper_mode)

        finished_at = utc_now()
        report = {
            "task_name": "插针交易",
            "run_id": run_id,
            "mode": args.mode,
            "status": "success" if not errors else "partial_failed",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(time.monotonic() - monotonic_start, 3),
            "scan": {
                "filter_summary": scan_data.get("filter_summary", {}),
                "candidates": scan_data.get("candidates", []),
                "ratings": ratings,
                "pipeline_run_id": scan_data.get("pipeline_run_id", ""),
            },
            "analysis": {
                "rating_threshold": rating_threshold,
                "analyzed_count": len(ratings),
                "passed_count": len(ratings),
                "filtered_count": 0,
                "failed_symbols": [],
                "ratings": ratings,
                "all_ratings": ratings,
                "summary": f"插针自动评级（跳过 TradingAgents）: {len(ratings)} 个候选",
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
        state_store.close()
        memory_store.close()
        risk_controller.close()
        cache.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="插针交易定时任务固定报告")
    parser.add_argument("--mode", choices=["short", "long"], default="short")
    parser.add_argument("--min-score", type=int, default=35)
    parser.add_argument("--max-candidates", type=int, default=15)
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--direction", type=str, choices=["long", "short"], default="long",
                        help="方向过滤：long=仅下插针做多（默认），short=仅上插针做空")
    parser.add_argument("--paper", action="store_true", help="强制模拟盘")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        report = run_report(args)
    except Exception as exc:
        log.error("插针交易流水线异常: %s", exc, exc_info=True)
        report = {
            "task_name": "插针交易",
            "status": "failed",
            "errors": [str(exc)],
            "markdown": f"插针交易定时任务报告\n\n任务状态: failed\n错误: {exc}",
        }

    if args.format == "json":
        output = {k: v for k, v in report.items() if k != "markdown"}
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    else:
        print(report.get("markdown", ""))


if __name__ == "__main__":
    main()
