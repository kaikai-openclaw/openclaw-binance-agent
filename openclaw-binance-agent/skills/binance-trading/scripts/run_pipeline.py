#!/usr/bin/env python3
"""
完整 Pipeline 入口脚本（OpenClaw skill 调用入口）

注意：这是通用候选 Pipeline，不是 4h 右侧反转专用入口。
4h 反转实盘/定时任务应使用 run_reversal_cron.py --mode 4h。

5 步流水线：信息收集 → 深度分析 → 策略制定 → 自动执行 → 展示进化

用法:
    python3 run_pipeline.py [--paper] [--fast] [--symbols BTC,SOL]
"""

import argparse
import json
import logging
import os
import sys

# 项目根目录 = scripts/ 的上两级（skills/binance-trading/scripts/ → 项目根）
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.binance_public import BinancePublicClient
from src.infra.daily_pnl import calculate_daily_realized_pnl
from src.infra.exchange_rules import LazyBinanceTradingRuleProvider
from src.infra.memory_store import MemoryStore
from src.infra.rate_limiter import RateLimiter
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.infra.trade_sync import BinanceTradeSyncer
from src.integrations.trading_agents_adapter import (
    create_trading_agents_analyzer,
)
from src.models.types import AccountState
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.skills.skill3_strategy import Skill3Strategy
from src.infra.circuit_breaker import CircuitBreaker
from src.skills.skill4_execute import Skill4Execute
from src.skills.skill5_evolve import Skill5Evolve

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)
log = logging.getLogger("pipeline")

DB_DIR = os.path.join(PROJECT_ROOT, "data")


def load_schema(name: str) -> dict:
    path = os.path.join(PROJECT_ROOT, "config", "schemas", name)
    with open(path) as f:
        return json.load(f)


def make_account_provider(
    fapi_client: BinanceFapiClient,
    paper_mode: bool,
    tracked_symbols: set[str] | None = None,
):
    """创建账户状态提供回调。"""
    tracked_symbols = tracked_symbols or set()

    def provider() -> AccountState:
        try:
            info = fapi_client.get_account_info()
            positions_raw = fapi_client.get_positions()
            positions = []
            for p in positions_raw:
                positions.append(
                    {
                        "symbol": p.symbol,
                        "direction": "long" if p.position_amt > 0 else "short",
                        "quantity": abs(p.position_amt),
                        "entry_price": p.entry_price,
                        "current_price": p.entry_price,  # mark_price 需要额外查询
                    }
                )
            pnl_symbols = set(tracked_symbols)
            pnl_symbols.update(p.symbol for p in positions_raw)
            daily_realized_pnl = calculate_daily_realized_pnl(
                fapi_client,
                pnl_symbols,
            )
            return AccountState(
                total_balance=info.total_balance,
                available_margin=info.available_balance,
                daily_realized_pnl=daily_realized_pnl,
                positions=positions,
                is_paper_mode=paper_mode,
            )
        except Exception as e:
            log.warning(f"获取账户状态失败: {e}，使用默认值")
            return AccountState(
                total_balance=0.0,
                available_margin=0.0,
                daily_realized_pnl=0.0,
                positions=[],
                is_paper_mode=True,
            )

    return provider


def make_market_price_provider(public_client: BinancePublicClient):
    """创建市场价格提供回调。

    P0-2 改造：价格不可用时返回 None（而不是 0.0），
    让 Skill-3 在 require_market_price=True 下正确跳过该币种，
    避免用 0 或 100 魔数算出失真的止损/头寸。
    """
    _cache: dict = {}

    def provider(symbol: str):
        if symbol in _cache:
            return _cache[symbol]
        try:
            tickers = public_client.get_tickers_24hr()
            for t in tickers:
                last = t.get("lastPrice", 0)
                try:
                    last = float(last)
                except (TypeError, ValueError):
                    last = 0.0
                _cache[t["symbol"]] = last if last > 0 else None
        except Exception as exc:
            log.warning(f"拉取 ticker 失败: {exc}")
            return None
        return _cache.get(symbol)

    return provider


def main():
    parser = argparse.ArgumentParser(description="Binance 交易 Pipeline")
    parser.add_argument("--paper", action="store_true", help="强制模拟盘模式")
    parser.add_argument("--fast", action="store_true", help="快速 LLM 分析模式")
    parser.add_argument(
        "--symbols", type=str, default="", help="指定币种，逗号分隔（如 BTC,SOL）"
    )
    args = parser.parse_args()

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("❌ 缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET 环境变量")
        sys.exit(1)

    # 初始化基础设施
    rate_limiter = RateLimiter()
    state_store = StateStore(db_path=os.path.join(DB_DIR, "state_store.db"))
    memory_store = MemoryStore(db_path=os.path.join(DB_DIR, "trading_state.db"))
    risk_controller = RiskController(db_path=os.path.join(DB_DIR, "trading_state.db"))
    public_client = BinancePublicClient(rate_limiter=rate_limiter)
    fapi_client = BinanceFapiClient(
        api_key=api_key, api_secret=api_secret, rate_limiter=rate_limiter
    )

    if args.paper:
        risk_controller.enable_paper_mode("cli_paper_flag")
    paper_mode = risk_controller.is_paper_mode()
    if paper_mode:
        print("🟡 模拟盘模式已启用")

    tracked_symbols = set()
    if args.symbols:
        tracked_symbols.update(
            s.strip().upper() for s in args.symbols.split(",") if s.strip()
        )
    account_provider = make_account_provider(
        fapi_client,
        paper_mode,
        tracked_symbols=tracked_symbols,
    )
    market_price_provider = make_market_price_provider(public_client)
    trading_rule_provider = LazyBinanceTradingRuleProvider(public_client)
    trade_syncer = BinanceTradeSyncer(
        fapi_client, memory_store, risk_controller=risk_controller
    )

    # 从进化记忆读取调优参数
    rating_threshold, risk_ratio = memory_store.get_evolved_params()
    log.info(f"策略参数: rating_threshold={rating_threshold}, risk_ratio={risk_ratio}")

    try:
        # ── Skill-1: 信息收集 ──
        print("\n📡 Step 1/5: 信息收集...")
        skill1 = Skill1Collect(
            state_store=state_store,
            input_schema=load_schema("skill1_input.json"),
            output_schema=load_schema("skill1_output.json"),
            client=public_client,
        )
        trigger_data = {"trigger_time": datetime.now(timezone.utc).isoformat()}
        if args.symbols:
            trigger_data["target_symbols"] = [
                s.strip() for s in args.symbols.split(",") if s.strip()
            ]
        trigger_id = state_store.save("pipeline_trigger", trigger_data)
        s1_id = skill1.execute(trigger_id)
        s1_data = state_store.load(s1_id)
        candidates = s1_data.get("candidates", [])
        summary = s1_data.get("filter_summary", {})
        print(
            f"   筛选漏斗: {summary.get('total_tickers', '?')} → {summary.get('output_count', 0)} 个候选"
        )

        if not candidates:
            print("\n⚠️  当前市场无符合条件的候选币种，Pipeline 结束")
            return

        for i, c in enumerate(candidates, 1):
            print(
                f"   {i}. {c['symbol']} (评分:{c['signal_score']}, 方向:{c.get('signal_direction', '?')})"
            )

        # ── Skill-2: 深度分析 ──
        print(
            f"\n🔬 Step 2/5: 深度分析（{'快速模式' if args.fast else '完整模式'}）..."
        )
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
        ratings = s2_data.get("ratings", [])
        print(f"   {s2_data.get('analysis_summary', '')}")

        if not ratings:
            print("\n⚠️  无币种通过评级，Pipeline 结束")
            return

        for r in ratings:
            print(
                f"   ✅ {r['symbol']}: 评分={r['rating_score']}, 信号={r['signal']}, 置信度={r['confidence']:.0f}%"
            )
            tracked_symbols.add(r["symbol"])

        # ── Skill-3: 策略制定 ──
        print("\n📐 Step 3/5: 策略制定...")
        skill3 = Skill3Strategy(
            state_store=state_store,
            input_schema=load_schema("skill3_input.json"),
            output_schema=load_schema("skill3_output.json"),
            risk_controller=risk_controller,
            account_state_provider=account_provider,
            market_price_provider=market_price_provider,
            trading_rule_provider=trading_rule_provider,
            risk_ratio=risk_ratio,
            require_market_price=True,  # P0-2：生产路径禁止 100.0 魔数回退
        )
        s3_input_id = state_store.save("skill3_input", {"input_state_id": s2_id})
        s3_id = skill3.execute(s3_input_id)
        s3_data = state_store.load(s3_id)
        plans = s3_data.get("trade_plans", [])
        print(
            f"   生成 {len(plans)} 笔交易计划，状态: {s3_data.get('pipeline_status', '?')}"
        )

        if not plans:
            print("\n⚠️  无交易计划通过风控，Pipeline 结束")
            return

        print(f"\n📋 通过筛选的交易计划（共 {len(plans)} 笔）:")
        plan_map = {p["symbol"]: p for p in plans}
        for p in plans:
            entry = f"{p['entry_price_lower']:.4f}~{p['entry_price_upper']:.4f}"
            tp_info = ""
            if p.get("trailing_stop"):
                tp_info = f" | 追踪止损: 激活价{p['trailing_stop']['activation_price']:.4f} 回撤{p['trailing_stop']['trail_pct'] * 100:.1f}%"
            print(
                f"   {p['symbol']} {p['direction']} | 头寸:{p['position_size_pct']:.2f}% | 数量:{p['quantity']:.4f} | 名义值:{p['notional_value']:.2f}U | 入场:{entry} | SL:{p['stop_loss_price']:.4f} TP:{p['take_profit_price']:.4f}{tp_info}"
            )

        # ── Skill-4: 自动执行 ──
        print(f"\n⚡ Step 4/5: 自动执行（Paper: {risk_controller.is_paper_mode()}）...")
        skill4 = Skill4Execute(
            state_store=state_store,
            input_schema=load_schema("skill4_input.json"),
            output_schema=load_schema("skill4_output.json"),
            binance_client=fapi_client,
            risk_controller=risk_controller,
            account_state_provider=account_provider,
            poll_interval=30,
            trading_rule_provider=trading_rule_provider,
            circuit_breaker=CircuitBreaker(db_path=DB_DIR),
            public_client=public_client,
            memory_store=memory_store,
        )
        s4_input_id = state_store.save("skill4_input", {"input_state_id": s3_id})
        s4_id = skill4.execute(s4_input_id)
        s4_data = state_store.load(s4_id)
        results = s4_data.get("execution_results", [])

        executed_symbols = set()
        skipped_symbols = set()
        for r in results:
            if r["status"] in ("filled", "paper_trade"):
                executed_symbols.add(r["symbol"])
            else:
                skipped_symbols.add(r["symbol"])

        print(
            f"\n   执行结果: {len(executed_symbols)} 笔成功 / {len(skipped_symbols)} 笔跳过"
        )

        if executed_symbols:
            print(f"\n   ✅ 成功执行:")
            for r in results:
                if r["status"] not in ("filled", "paper_trade"):
                    continue
                plan = plan_map.get(r["symbol"], {})
                entry = f"{plan.get('entry_price_lower', 0):.4f}~{plan.get('entry_price_upper', 0):.4f}"
                print(
                    f"   {r['symbol']} {r['direction']} | 成交价:{r['executed_price']:.4f} | 数量:{r['executed_quantity']:.4f} | 手续费:{r.get('fee', 0):.4f}U"
                )
                print(
                    f"      计划入场:{entry} | SL:{plan.get('stop_loss_price', 0):.4f} TP:{plan.get('take_profit_price', 0):.4f} | 头寸:{plan.get('position_size_pct', 0):.2f}%"
                )

        if skipped_symbols:
            print(f"\n   ⚠️ 跳过/失败:")
            for r in results:
                if r["status"] in ("filled", "paper_trade"):
                    continue
                reason = r.get("reason", r["status"])
                plan = plan_map.get(r["symbol"], {})
                print(
                    f"   {r['symbol']} {r['direction']} | 状态:{reason} | 计划头寸:{plan.get('position_size_pct', 0):.2f}%"
                )

        # ── Skill-5: 展示进化 ──
        print("\n📊 Step 5/5: 展示与进化...")
        skill5 = Skill5Evolve(
            state_store=state_store,
            input_schema=load_schema("skill5_input.json"),
            output_schema=load_schema("skill5_output.json"),
            memory_store=memory_store,
            account_state_provider=account_provider,
            trade_syncer=trade_syncer,
            risk_controller=risk_controller,
        )
        s5_input_id = state_store.save("skill5_input", {"input_state_id": s4_id})
        s5_id = skill5.execute(s5_input_id)
        s5_data = state_store.load(s5_id)

        acct = s5_data.get("account_summary", {})
        evo = s5_data.get("evolution", {})
        print(f"\n{'=' * 50}")
        print(f"📊 账户总资金: {acct.get('total_balance', 0):.2f} USDT")
        print(f"   可用保证金: {acct.get('available_margin', 0):.2f} USDT")
        print(f"   当日盈亏:   {acct.get('daily_realized_pnl', 0):.2f} USDT")
        print(f"   模拟盘:     {acct.get('is_paper_mode', False)}")
        print(f"   胜率:       {evo.get('win_rate', 0):.1f}%")
        print(f"   交易笔数:   {evo.get('trade_count', 0)}")
        print(f"   参数调整:   {'是' if evo.get('adjustment_applied') else '否'}")
        print(f"{'=' * 50}")
        print("\n✅ Pipeline 完成")

    except KeyboardInterrupt:
        print("\n⏹️  Pipeline 被用户中断")
    except Exception as e:
        print(f"\n❌ Pipeline 失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        state_store.close()
        memory_store.close()
        risk_controller.close()
        ta_module_ref = locals().get("ta_module")
        if ta_module_ref:
            ta_module_ref.shutdown()


if __name__ == "__main__":
    main()
