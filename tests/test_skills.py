"""
Skill 链路集成测试

测试完整 Pipeline 流程：Skill-1 → Skill-2 → Skill-3 → Skill-4 → Skill-5，
验证 state_id 在各 Skill 间正确串联，以及降级路径（no_opportunity 跳过 Skill-4）。

使用 mock 模拟 Binance API（BinanceFapiClient）和 TradingAgents（analyzer 回调），
不依赖任何外部服务。

需求: 6.4, 9.6
"""

import json
import os
import uuid
from unittest.mock import MagicMock

import pytest

from src.infra.binance_fapi import BinanceFapiClient, OrderResult, PositionRisk
from src.infra.memory_store import MemoryStore
from src.infra.risk_controller import RiskController
from src.infra.state_store import StateStore
from src.models.types import AccountState, ValidationResult
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.skills.skill3_strategy import Skill3Strategy
from src.skills.skill4_execute import Skill4Execute
from src.skills.skill5_evolve import Skill5Evolve


# ── 加载 Schema ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


SCHEMAS = {
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


# ── 默认账户状态 ──────────────────────────────────────────

def _make_account(
    total_balance: float = 10000.0,
    available_margin: float = 8000.0,
    daily_realized_pnl: float = 0.0,
    positions: list | None = None,
) -> AccountState:
    return AccountState(
        total_balance=total_balance,
        available_margin=available_margin,
        daily_realized_pnl=daily_realized_pnl,
        positions=positions or [],
        is_paper_mode=False,
    )


# ── Skill-1 输入数据（符合 skill1_input.json Schema） ──

SKILL1_INPUT = {
    "trigger_time": "2025-01-15T08:00:00Z",
}


def _make_bullish_klines(n: int = 100, base: float = 100, step: float = 0.3, noise_seed: int = 0) -> list[list]:
    """构造温和上涨 K 线，后 5 根放量。noise_seed 不同则产生不同波动。"""
    import random
    rng = random.Random(noise_seed)
    closes = [base + i * step + rng.gauss(0, step * 2) for i in range(n)]
    # 确保价格为正
    closes = [max(c, 1.0) for c in closes]
    volumes = [1000.0] * (n - 5) + [3000.0] * 5
    result = []
    for c, v in zip(closes, volumes):
        result.append([0, str(c), str(c * 1.01), str(c * 0.99), str(c), str(v), 0, "0", 0, "0", "0", "0"])
    return result


def _make_mock_binance_public(symbols: list[str], tickers: list[dict] | None = None, klines=None):
    """构造 mock BinancePublicClient。"""
    client = MagicMock()
    client.get_exchange_info.return_value = {
        "symbols": [
            {"symbol": s, "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"}
            for s in symbols
        ]
    }
    if tickers is None:
        tickers = [
            {"symbol": s, "quoteVolume": "100000000", "highPrice": "110",
             "lowPrice": "100", "priceChangePercent": "5.0"}
            for s in symbols
        ]
    client.get_tickers_24hr.return_value = tickers
    if klines is not None:
        client.get_klines.return_value = klines
    else:
        # 每个 symbol 用不同的 noise_seed 避免相关性去重
        _kline_cache = {}
        for idx, s in enumerate(symbols):
            _kline_cache[s] = _make_bullish_klines(noise_seed=idx + 1)

        def _get_klines(symbol, interval=None, limit=None):
            return _kline_cache.get(symbol, _make_bullish_klines())
        client.get_klines.side_effect = _get_klines
    return client


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    """临时 StateStore 实例。"""
    db = os.path.join(str(tmp_path), "state.db")
    store = StateStore(db_path=db)
    yield store
    store.close()


@pytest.fixture
def memory_store(tmp_path):
    """临时 MemoryStore 实例。"""
    db = os.path.join(str(tmp_path), "memory.db")
    store = MemoryStore(db_path=db)
    yield store
    store.close()


# ══════════════════════════════════════════════════════════
# 正常路径：Skill-1 → 2 → 3 → 4 → 5 完整执行
# ══════════════════════════════════════════════════════════

class TestFullPipelineNormalPath:
    """
    正常路径集成测试。

    Skill-1 输出候选币种 → Skill-2 评级过滤 → Skill-3 生成交易计划 →
    Skill-4 执行交易 → Skill-5 展示与进化。
    验证 state_id 在各 Skill 间正确串联。

    需求: 6.4, 9.6
    """

    def test_full_pipeline_state_id_chain(self, state_store, memory_store):
        """
        完整 Pipeline 正常路径：
        - Skill-1 输出 state_id_1
        - Skill-2 通过 state_id_1 读取数据，输出 state_id_2
        - Skill-3 通过 state_id_2 读取数据，输出 state_id_3
        - Skill-4 通过 state_id_3 读取数据，输出 state_id_4
        - Skill-5 通过 state_id_4 读取数据，输出最终结果
        所有 state_id 均为有效 UUID v4 且互不相同。
        """
        account = _make_account()
        account_provider = lambda: account

        # ── Skill-1：信息收集 ──
        client1 = _make_mock_binance_public(["BTCUSDT", "ETHUSDT"])
        skill1 = Skill1Collect(
            state_store=state_store,
            input_schema=SCHEMAS["s1_in"],
            output_schema=SCHEMAS["s1_out"],
            client=client1,
        )
        # 先存入 Skill-1 的输入数据
        s1_input_id = state_store.save("pipeline_trigger", SKILL1_INPUT)
        state_id_1 = skill1.execute(input_state_id=s1_input_id)
        # 验证 state_id_1 是有效 UUID
        uuid.UUID(state_id_1, version=4)

        # 验证 Skill-1 输出数据已存入 StateStore
        s1_data = state_store.load(state_id_1)
        assert len(s1_data["candidates"]) >= 1

        # ── Skill-2：深度分析 ──
        # mock analyzer：BTCUSDT 评 8 分做多，ETHUSDT 评 4 分（将被过滤）
        def mock_analyzer(symbol, market_data):
            if symbol == "BTCUSDT":
                return {"rating_score": 8, "signal": "long", "confidence": 80.0}
            return {"rating_score": 4, "signal": "hold", "confidence": 30.0}

        trading_agents = TradingAgentsModule(analyzer=mock_analyzer)
        skill2 = Skill2Analyze(
            state_store=state_store,
            input_schema=SCHEMAS["s2_in"],
            output_schema=SCHEMAS["s2_out"],
            trading_agents=trading_agents,
        )
        # Skill-2 的 execute() 从 StateStore 加载 input_data，
        # 其中需要 input_state_id 字段指向 Skill-1 的输出
        s2_input_id = state_store.save("skill1_to_skill2", {
            "input_state_id": state_id_1,
        })
        state_id_2 = skill2.execute(input_state_id=s2_input_id)
        uuid.UUID(state_id_2, version=4)

        s2_data = state_store.load(state_id_2)
        # ETHUSDT 评级 4 分 < 6 分阈值，应被过滤
        assert len(s2_data["ratings"]) == 1
        assert s2_data["ratings"][0]["symbol"] == "BTCUSDT"
        assert s2_data["filtered_count"] == 1

        # ── Skill-3：策略制定 ──
        risk_controller = RiskController()
        skill3 = Skill3Strategy(
            state_store=state_store,
            input_schema=SCHEMAS["s3_in"],
            output_schema=SCHEMAS["s3_out"],
            risk_controller=risk_controller,
            account_state_provider=account_provider,
        )
        s3_input_id = state_store.save("skill2_to_skill3", {
            "input_state_id": state_id_2,
        })
        state_id_3 = skill3.execute(input_state_id=s3_input_id)
        uuid.UUID(state_id_3, version=4)

        s3_data = state_store.load(state_id_3)
        assert s3_data["pipeline_status"] == "has_trades"
        assert len(s3_data["trade_plans"]) >= 1

        # ── Skill-4：交易执行 ──
        mock_binance = MagicMock(spec=BinanceFapiClient)
        mock_binance.place_limit_order.return_value = OrderResult(
            order_id="ORD001", symbol="BTCUSDT", side="BUY",
            price=100.0, quantity=10.0, status="NEW",
        )
        # 持仓监控：第一次轮询即触发止盈
        mock_binance.get_position_risk.return_value = PositionRisk(
            symbol="BTCUSDT", position_amt=10.0, entry_price=100.0,
            mark_price=110.0, unrealized_pnl=100.0,
            liquidation_price=80.0, leverage=10,
        )
        mock_binance.place_market_order.return_value = OrderResult(
            order_id="ORD002", symbol="BTCUSDT", side="SELL",
            price=110.0, quantity=10.0, status="FILLED",
        )

        skill4 = Skill4Execute(
            state_store=state_store,
            input_schema=SCHEMAS["s4_in"],
            output_schema=SCHEMAS["s4_out"],
            binance_client=mock_binance,
            risk_controller=risk_controller,
            account_state_provider=account_provider,
            poll_interval=0,  # 测试时不等待
        )
        s4_input_id = state_store.save("skill3_to_skill4", {
            "input_state_id": state_id_3,
        })
        state_id_4 = skill4.execute(input_state_id=s4_input_id)
        uuid.UUID(state_id_4, version=4)

        s4_data = state_store.load(state_id_4)
        assert len(s4_data["execution_results"]) >= 1
        assert s4_data["is_paper_mode"] is False

        # ── Skill-5：展示与进化 ──
        skill5 = Skill5Evolve(
            state_store=state_store,
            input_schema=SCHEMAS["s5_in"],
            output_schema=SCHEMAS["s5_out"],
            memory_store=memory_store,
            account_state_provider=account_provider,
        )
        s5_input_id = state_store.save("skill4_to_skill5", {
            "input_state_id": state_id_4,
        })
        state_id_5 = skill5.execute(input_state_id=s5_input_id)
        uuid.UUID(state_id_5, version=4)

        s5_data = state_store.load(state_id_5)
        assert "account_summary" in s5_data
        assert "positions" in s5_data
        assert "evolution" in s5_data

        # ── 验证所有 state_id 互不相同 ──
        all_ids = [state_id_1, state_id_2, state_id_3, state_id_4, state_id_5]
        assert len(set(all_ids)) == 5, "所有 state_id 应互不相同"


# ══════════════════════════════════════════════════════════
# 降级路径：Skill-3 输出 no_opportunity 时跳过 Skill-4
# ══════════════════════════════════════════════════════════

class TestDegradationPath:
    """
    降级路径集成测试。

    当 Skill-3 输出 pipeline_status=no_opportunity 时，
    应跳过 Skill-4 直接进入 Skill-5。

    需求: 6.4
    """

    def test_no_opportunity_skips_skill4(self, state_store, memory_store):
        """
        降级路径：
        - Skill-1 输出候选币种
        - Skill-2 所有币种评级低于阈值，输出空 ratings
        - Skill-3 无目标币种，输出 no_opportunity
        - 跳过 Skill-4，直接执行 Skill-5
        - Skill-5 正常输出账户状态
        """
        account = _make_account()
        account_provider = lambda: account

        # ── Skill-1 ──
        client1 = _make_mock_binance_public(["XRPUSDT"])
        skill1 = Skill1Collect(
            state_store=state_store,
            input_schema=SCHEMAS["s1_in"],
            output_schema=SCHEMAS["s1_out"],
            client=client1,
        )
        s1_input_id = state_store.save("trigger", SKILL1_INPUT)
        state_id_1 = skill1.execute(input_state_id=s1_input_id)

        # ── Skill-2：所有币种评级低于 6 分 ──
        def low_rating_analyzer(symbol, market_data):
            return {"rating_score": 3, "signal": "hold", "confidence": 20.0}

        trading_agents = TradingAgentsModule(analyzer=low_rating_analyzer)
        skill2 = Skill2Analyze(
            state_store=state_store,
            input_schema=SCHEMAS["s2_in"],
            output_schema=SCHEMAS["s2_out"],
            trading_agents=trading_agents,
        )
        s2_input_id = state_store.save("s1_to_s2", {
            "input_state_id": state_id_1,
        })
        state_id_2 = skill2.execute(input_state_id=s2_input_id)

        s2_data = state_store.load(state_id_2)
        assert s2_data["ratings"] == [], "所有币种应被过滤"

        # ── Skill-3：空评级 → no_opportunity ──
        risk_controller = RiskController()
        skill3 = Skill3Strategy(
            state_store=state_store,
            input_schema=SCHEMAS["s3_in"],
            output_schema=SCHEMAS["s3_out"],
            risk_controller=risk_controller,
            account_state_provider=account_provider,
        )
        s3_input_id = state_store.save("s2_to_s3", {
            "input_state_id": state_id_2,
        })
        state_id_3 = skill3.execute(input_state_id=s3_input_id)

        s3_data = state_store.load(state_id_3)
        assert s3_data["pipeline_status"] == "no_opportunity"
        assert s3_data["trade_plans"] == []

        # ── 跳过 Skill-4，直接执行 Skill-5 ──
        skill5 = Skill5Evolve(
            state_store=state_store,
            input_schema=SCHEMAS["s5_in"],
            output_schema=SCHEMAS["s5_out"],
            memory_store=memory_store,
            account_state_provider=account_provider,
        )
        # Skill-5 的 input_state_id 是可选的，不传时使用空字典
        state_id_5 = skill5.execute(input_state_id=None)
        uuid.UUID(state_id_5, version=4)

        s5_data = state_store.load(state_id_5)
        assert s5_data["account_summary"]["total_balance"] == 10000.0
        assert s5_data["evolution"]["trade_count"] == 0
        assert s5_data["evolution"]["adjustment_applied"] is False


# ══════════════════════════════════════════════════════════
# Pipeline.run() 模拟：验证返回 success=True
# ══════════════════════════════════════════════════════════

class TestPipelineRunSuccess:
    """
    模拟 Pipeline.run() 完整流程，验证最终返回 success=True。

    手动编排 Skill 执行顺序，模拟 Pipeline 的核心逻辑。

    需求: 6.4, 9.6
    """

    def test_pipeline_run_returns_success(self, state_store, memory_store):
        """
        模拟 Pipeline.run()：
        1. 执行 Skill-1 ~ Skill-5
        2. 验证最终结果 success=True
        """
        account = _make_account()
        account_provider = lambda: account

        # ── 构建所有 Skill ──
        client1 = _make_mock_binance_public(["SOLUSDT"])
        skill1 = Skill1Collect(
            state_store=state_store,
            input_schema=SCHEMAS["s1_in"],
            output_schema=SCHEMAS["s1_out"],
            client=client1,
        )

        def analyzer(symbol, market_data):
            return {"rating_score": 9, "signal": "short", "confidence": 85.0}

        trading_agents = TradingAgentsModule(analyzer=analyzer)
        skill2 = Skill2Analyze(
            state_store=state_store,
            input_schema=SCHEMAS["s2_in"],
            output_schema=SCHEMAS["s2_out"],
            trading_agents=trading_agents,
        )

        risk_controller = RiskController()
        skill3 = Skill3Strategy(
            state_store=state_store,
            input_schema=SCHEMAS["s3_in"],
            output_schema=SCHEMAS["s3_out"],
            risk_controller=risk_controller,
            account_state_provider=account_provider,
        )

        mock_binance = MagicMock(spec=BinanceFapiClient)
        mock_binance.place_limit_order.return_value = OrderResult(
            order_id="ORD100", symbol="SOLUSDT", side="SELL",
            price=100.0, quantity=5.0, status="NEW",
        )
        mock_binance.get_position_risk.return_value = PositionRisk(
            symbol="SOLUSDT", position_amt=-5.0, entry_price=100.0,
            mark_price=90.0, unrealized_pnl=50.0,
            liquidation_price=120.0, leverage=10,
        )
        mock_binance.place_market_order.return_value = OrderResult(
            order_id="ORD101", symbol="SOLUSDT", side="BUY",
            price=90.0, quantity=5.0, status="FILLED",
        )

        skill4 = Skill4Execute(
            state_store=state_store,
            input_schema=SCHEMAS["s4_in"],
            output_schema=SCHEMAS["s4_out"],
            binance_client=mock_binance,
            risk_controller=risk_controller,
            account_state_provider=account_provider,
            poll_interval=0,
        )

        skill5 = Skill5Evolve(
            state_store=state_store,
            input_schema=SCHEMAS["s5_in"],
            output_schema=SCHEMAS["s5_out"],
            memory_store=memory_store,
            account_state_provider=account_provider,
        )

        # ── 模拟 Pipeline.run() ──
        success = True
        try:
            # Skill-1
            s1_in = state_store.save("pipe_trigger", SKILL1_INPUT)
            sid1 = skill1.execute(input_state_id=s1_in)

            # Skill-2
            s2_in = state_store.save("pipe_s1_s2", {"input_state_id": sid1})
            sid2 = skill2.execute(input_state_id=s2_in)

            # Skill-3
            s3_in = state_store.save("pipe_s2_s3", {"input_state_id": sid2})
            sid3 = skill3.execute(input_state_id=s3_in)

            # 检查是否需要跳过 Skill-4
            s3_data = state_store.load(sid3)
            if s3_data["pipeline_status"] == "no_opportunity":
                # 跳过 Skill-4，直接 Skill-5
                skill5.execute(input_state_id=None)
            else:
                # Skill-4
                s4_in = state_store.save("pipe_s3_s4", {"input_state_id": sid3})
                sid4 = skill4.execute(input_state_id=s4_in)

                # Skill-5
                s5_in = state_store.save("pipe_s4_s5", {"input_state_id": sid4})
                skill5.execute(input_state_id=s5_in)

        except Exception:
            success = False

        assert success is True, "Pipeline.run() 应返回 success=True"
