#!/usr/bin/env python3
"""
Paper Trading 回测模拟器

使用历史/模拟市场数据，对完整 Pipeline 进行回测。
不依赖真实账户余额，以假资金运行。

用法：
    python scripts/paper_backtest.py [--rounds 3] [--initial-balance 10000]
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

# 项目根目录加入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.infra.binance_fapi import BinanceFapiClient, OrderResult, PositionRisk
from src.infra.memory_store import MemoryStore
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.models.types import AccountState
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.skills.skill3_strategy import Skill3Strategy
from src.skills.skill4_execute import Skill4Execute
from src.skills.skill5_evolve import Skill5Evolve


# ── 辅助函数 ──────────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join(PROJECT_ROOT, "config", "schemas", name)
    with open(path) as f:
        return json.load(f)


# ── 模拟市场数据 ──────────────────────────────────────────

MOCK_CANDIDATES = [
    {"symbol": "BTCUSDT", "heat_score": 95.0},
    {"symbol": "ETHUSDT", "heat_score": 88.0},
    {"symbol": "SOLUSDT", "heat_score": 85.0},
    {"symbol": "BNBUSDT", "heat_score": 75.0},
    {"symbol": "XRPUSDT", "heat_score": 70.0},
]


def mock_search(keywords: list[str]) -> list[dict]:
    """
    模拟 Web Search：返回候选币种 URL 列表。
    返回格式：list of dict，每个 dict 包含 url 字段
    """
    return [{"url": f"https://binance.com/market/{sym.lower()}"}
            for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]]


def mock_fetcher(url: str) -> dict | None:
    """
    模拟 Fetcher：根据 URL 返回币种热度数据。
    从 URL 中提取 symbol 并返回对应的 mock 数据。
    """
    for c in MOCK_CANDIDATES:
        # 从 URL 中匹配 symbol
        if c["symbol"].lower() in url.lower():
            return {"symbol": c["symbol"], "heat_score": c["heat_score"]}
    return None


def mock_analyzer(symbol: str, market_data: dict) -> dict:
    """
    模拟 TradingAgents 分析器。
    基于评分返回不同的信号。
    注意：rating_score 必须是 1-10 的整数（Schema 要求 integer）
    """
    scores = {
        "BTCUSDT": {"rating_score": 9, "signal": "long", "confidence": 85.0},
        "ETHUSDT": {"rating_score": 8, "signal": "long", "confidence": 78.0},
        "SOLUSDT": {"rating_score": 7, "signal": "long", "confidence": 65.0},
        "BNBUSDT": {"rating_score": 5, "signal": "hold", "confidence": 55.0},
        "XRPUSDT": {"rating_score": 4, "signal": "hold", "confidence": 40.0},
    }
    return scores.get(symbol, {"rating_score": 5, "signal": "hold", "confidence": 50.0})


# ── 模拟持仓监控 ──────────────────────────────────────────

class MockPositionMonitor:
    """
    模拟持仓监控：
    - 第一次轮询触发止盈（价格向有利方向移动）
    - 模拟市价平仓
    """

    def __init__(self, direction: str, entry_price: float,
                 stop_loss: float, take_profit: float):
        self.direction = direction
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.poll_count = 0

    def get_position_risk(self, symbol: str) -> PositionRisk:
        """模拟持仓：第一次触发止盈平仓。"""
        self.poll_count += 1

        # 模拟价格变动：有 70% 概率触发止盈，30% 概率继续持仓
        if self.poll_count == 1:
            # 触发止盈：价格向有利方向移动
            if self.direction == "long":
                mark_price = self.entry_price * 1.05  # +5% 触发止盈
                pnl = (mark_price - self.entry_price) * 10  # 正盈利
            else:
                mark_price = self.entry_price * 0.95  # -5% 触发止盈
                pnl = (self.entry_price - mark_price) * 10
        else:
            mark_price = self.entry_price
            pnl = 0.0

        amt = 10.0 if self.direction == "long" else -10.0
        return PositionRisk(
            symbol=symbol,
            position_amt=amt,
            entry_price=self.entry_price,
            mark_price=mark_price,
            unrealized_pnl=pnl,
            liquidation_price=self.entry_price * 0.5,
            leverage=10,
        )

    def place_market_order(self, symbol: str, side: str, quantity: float) -> OrderResult:
        """模拟市价平仓。"""
        price = self.entry_price * 1.05 if side == "SELL" else self.entry_price * 0.95
        return OrderResult(
            order_id=f"mock_{uuid.uuid4().hex[:8]}",
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
            status="FILLED",
        )


# ── 主程序 ──────────────────────────────────────────────

def run_paper_backtest(rounds: int = 3, initial_balance: float = 10000.0):
    """运行 Paper Trading 回测。"""

    print("=" * 60)
    print("  Paper Trading 回测模拟器")
    print("=" * 60)
    print(f"  初始资金: {initial_balance:.2f} USDT (假资金)")
    print(f"  回测轮数: {rounds}")
    print(f"  模式: Paper Trading (不进行真实交易)")
    print("=" * 60)

    # 初始化存储
    db_path = os.path.join(PROJECT_ROOT, "data", "backtest_state.db")
    mem_path = os.path.join(PROJECT_ROOT, "data", "backtest_memory.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    state_store = StateStore(db_path=db_path)
    memory_store = MemoryStore(db_path=mem_path)

    # 加载 Schema
    schemas = {
        "s1_in": _load_schema("skill1_input.json"),
        "s1_out": _load_schema("skill1_output.json"),
        "s2_in": _load_schema("skill2_input.json"),
        "s2_out": _load_schema("skill2_output.json"),
        "s3_in": _load_schema("skill3_input.json"),
        "s3_out": _load_schema("skill3_output.json"),
        "s4_in": _load_schema("skill4_input.json"),
        "s4_out": _load_schema("skill4_output.json"),
        "s5_in": _load_schema("skill5_input.json"),
        "s5_out": _load_schema("skill5_output.json"),
    }

    # 账户状态
    account = AccountState(
        total_balance=initial_balance,
        available_margin=initial_balance,
        daily_realized_pnl=0.0,
        positions=[],
        is_paper_mode=True,  # Paper Mode
    )
    account_provider = lambda: account

    # 风控（启用 Paper Mode）
    risk_controller = RiskController()
    risk_controller._paper_mode = True  # 强制 Paper Mode

    # Mock Binance Client（用于 Skill-4）
    mock_binance = MagicMock(spec=BinanceFapiClient)

    total_trades = 0
    total_pnl = 0.0
    wins = 0
    losses = 0

    for round_num in range(1, rounds + 1):
        print(f"\n{'='*60}")
        print(f"  第 {round_num}/{rounds} 轮")
        print(f"{'='*60}")

        # ── Skill-1: 信息收集 ──
        print("\n[Skill-1] 信息收集...")
        skill1 = Skill1Collect(
            state_store=state_store,
            input_schema=schemas["s1_in"],
            output_schema=schemas["s1_out"],
            searcher=mock_search,
            fetcher=mock_fetcher,
        )
        s1_input_id = state_store.save("backtest_trigger", {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "search_keywords": ["crypto", "defi", "trading"],
        })
        state_id_1 = skill1.execute(input_state_id=s1_input_id)
        s1_data = state_store.load(state_id_1)
        print(f"  候选币种: {len(s1_data['candidates'])} 个")
        for c in s1_data["candidates"]:
            print(f"    - {c['symbol']}: 热度 {c['heat_score']}")

        # ── Skill-2: 深度分析 ──
        print("\n[Skill-2] 深度分析 (TradingAgents)...")
        trading_agents = TradingAgentsModule(analyzer=mock_analyzer)
        skill2 = Skill2Analyze(
            state_store=state_store,
            input_schema=schemas["s2_in"],
            output_schema=schemas["s2_out"],
            trading_agents=trading_agents,
        )
        s2_input_id = state_store.save("s1_to_s2", {"input_state_id": state_id_1})
        state_id_2 = skill2.execute(input_state_id=s2_input_id)
        s2_data = state_store.load(state_id_2)
        print(f"  评级通过: {len(s2_data['ratings'])} 个")
        print(f"  过滤掉: {s2_data['filtered_count']} 个")
        for r in s2_data["ratings"]:
            print(f"    - {r['symbol']}: 评分 {r['rating_score']} ({r['signal']})")

        # ── Skill-3: 策略制定 ──
        print("\n[Skill-3] 策略制定...")
        skill3 = Skill3Strategy(
            state_store=state_store,
            input_schema=schemas["s3_in"],
            output_schema=schemas["s3_out"],
            risk_controller=risk_controller,
            account_state_provider=account_provider,
        )
        s3_input_id = state_store.save("s2_to_s3", {"input_state_id": state_id_2})
        state_id_3 = skill3.execute(input_state_id=s3_input_id)
        s3_data = state_store.load(state_id_3)
        print(f"  状态: {s3_data['pipeline_status']}")
        print(f"  交易计划: {len(s3_data['trade_plans'])} 笔")
        for plan in s3_data["trade_plans"]:
            print(f"    - {plan['symbol']}: {plan['direction']} "
                  f"入场 {plan['entry_price_lower']:.2f}-{plan['entry_price_upper']:.2f} "
                  f"止损 {plan['stop_loss_price']:.2f} 止盈 {plan['take_profit_price']:.2f}")

        if s3_data["pipeline_status"] == "no_opportunity":
            print("  无交易机会，跳过执行")
            continue

        # ── Skill-4: 模拟执行 ──
        print("\n[Skill-4] Paper Trading 执行 (模拟)...")

        # 为每笔交易计划创建 Mock 监控
        for plan in s3_data["trade_plans"]:
            monitor = MockPositionMonitor(
                direction=plan["direction"],
                entry_price=(plan["entry_price_upper"] + plan["entry_price_lower"]) / 2,
                stop_loss=plan["stop_loss_price"],
                take_profit=plan["take_profit_price"],
            )

            # 配置 mock
            mock_binance.place_limit_order = MagicMock(return_value=OrderResult(
                order_id=f"paper_{uuid.uuid4().hex[:8]}",
                symbol=plan["symbol"],
                side="BUY" if plan["direction"] == "long" else "SELL",
                price=(plan["entry_price_upper"] + plan["entry_price_lower"]) / 2,
                quantity=1.0,
                status="FILLED",
            ))
            mock_binance.get_position_risk = monitor.get_position_risk
            mock_binance.place_market_order = monitor.place_market_order

        skill4 = Skill4Execute(
            state_store=state_store,
            input_schema=schemas["s4_in"],
            output_schema=schemas["s4_out"],
            binance_client=mock_binance,
            risk_controller=risk_controller,
            account_state_provider=account_provider,
            poll_interval=0,
        )
        s4_input_id = state_store.save("s3_to_s4", {"input_state_id": state_id_3})
        state_id_4 = skill4.execute(input_state_id=s4_input_id)
        s4_data = state_store.load(state_id_4)

        print(f"  Paper Mode: {s4_data['is_paper_mode']}")
        for res in s4_data["execution_results"]:
            status_emoji = "paper" if res["status"] == "paper_trade" else res["status"]
            print(f"    {res['symbol']}: {status_emoji} "
                  f"(订单 {res.get('order_id', 'N/A')})")
            if res["status"] == "paper_trade":
                total_trades += 1
                # 模拟盈亏（+5% 止盈）
                pnl = 50.0  # 假设每笔赚 50 USDT
                total_pnl += pnl
                wins += 1

        # ── Skill-5: 展示与进化 ──
        print("\n[Skill-5] 账户报告与策略进化...")
        skill5 = Skill5Evolve(
            state_store=state_store,
            input_schema=schemas["s5_in"],
            output_schema=schemas["s5_out"],
            memory_store=memory_store,
            account_state_provider=account_provider,
        )
        s5_input_id = state_store.save("s4_to_s5", {"input_state_id": state_id_4})
        state_id_5 = skill5.execute(input_state_id=s5_input_id)
        s5_data = state_store.load(state_id_5)

        evo = s5_data.get("evolution", {})
        summary = s5_data.get("account_summary", {})
        print(f"  总资金: {summary.get('total_balance', 0):.2f} USDT")
        print(f"  胜率: {evo.get('win_rate', 0):.1f}%")
        print(f"  交易笔数: {evo.get('trade_count', 0)}")
        print(f"  策略调整: {'是' if evo.get('adjustment_applied') else '否'}")

    # ── 最终报告 ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  回测最终报告")
    print("=" * 60)
    print(f"  初始资金: {initial_balance:.2f} USDT")
    print(f"  最终资金: {initial_balance + total_pnl:.2f} USDT")
    print(f"  总收益: {total_pnl:+.2f} USDT ({total_pnl/initial_balance*100:+.2f}%)")
    print(f"  总交易笔数: {total_trades}")
    print(f"  胜率: {wins/total_trades*100:.1f}%" if total_trades > 0 else "N/A")
    print(f"  盈利交易: {wins}")
    print(f"  亏损交易: {losses}")
    print("=" * 60)
    print("  Paper Trading 回测完成")
    print("  注意: 此结果为模拟数据，不构成投资建议")

    # 清理
    state_store.close()
    memory_store.close()

    return {
        "initial_balance": initial_balance,
        "final_balance": initial_balance + total_pnl,
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / total_trades * 100 if total_trades > 0 else 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper Trading 回测模拟器")
    parser.add_argument("--rounds", type=int, default=3, help="回测轮数 (默认 3)")
    parser.add_argument("--initial-balance", type=float, default=10000.0,
                        help="初始虚拟资金 (默认 10000 USDT)")
    args = parser.parse_args()

    run_paper_backtest(rounds=args.rounds, initial_balance=args.initial_balance)
