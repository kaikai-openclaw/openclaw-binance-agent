"""
Skill-5 展示与自我进化 单元测试。

覆盖场景：
1. 正常流程：读取账户状态 → 构建持仓展示 → 记录交易 → 计算进化
2. 交易记录不足 20 笔 → 跳过进化计算
3. 胜率低于 38% → 生成调优建议（需连续两轮确认）
4. 胜率正常 → 维持默认参数
5. 空持仓列表
6. Paper Mode 标记
7. 无上游 state_id（定时触发）
8. 平仓交易数据提取
9. Markdown 表格生成
10. 盈亏比例计算

需求: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""

import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.infra.memory_store import MemoryStore
from src.infra.state_store import StateStore
from src.models.types import (
    AccountState,
    ReflectionLog,
    StrategyStats,
    TradeDirection,
    TradeRecord,
)
from src.skills.skill5_evolve import Skill5Evolve


# ── 加载 Schema ──────────────────────────────────────────

def _load_schema(name: str) -> dict:
    path = os.path.join("config", "schemas", name)
    with open(path) as f:
        return json.load(f)


INPUT_SCHEMA = _load_schema("skill5_input.json")
OUTPUT_SCHEMA = _load_schema("skill5_output.json")


# ── 辅助函数 ──────────────────────────────────────────────

def _make_account(
    total_balance: float = 10000.0,
    available_margin: float = 8000.0,
    daily_realized_pnl: float = -100.0,
    positions: list | None = None,
    is_paper_mode: bool = False,
) -> AccountState:
    """构造 AccountState。"""
    return AccountState(
        total_balance=total_balance,
        available_margin=available_margin,
        daily_realized_pnl=daily_realized_pnl,
        positions=positions or [],
        is_paper_mode=is_paper_mode,
    )


def _make_position(
    symbol: str = "BTCUSDT",
    direction: str = "long",
    quantity: float = 1.0,
    entry_price: float = 100.0,
    current_price: float = 110.0,
    unrealized_pnl: float = 10.0,
) -> dict:
    """构造持仓字典。"""
    return {
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "entry_price": entry_price,
        "current_price": current_price,
        "unrealized_pnl": unrealized_pnl,
    }


def _make_execution_result(
    symbol: str = "BTCUSDT",
    direction: str = "long",
    status: str = "filled",
    executed_price: float = 100.0,
    executed_quantity: float = 1.0,
    pnl_amount: float = 10.0,
    hold_duration_hours: float = 2.0,
    rating_score: int = 7,
    position_size_pct: float = 10.0,
) -> dict:
    """构造执行结果字典。"""
    return {
        "order_id": str(uuid.uuid4()),
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "executed_price": executed_price,
        "executed_quantity": executed_quantity,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "fee": 0.1,
        "pnl_amount": pnl_amount,
        "hold_duration_hours": hold_duration_hours,
        "rating_score": rating_score,
        "position_size_pct": position_size_pct,
    }


def _make_trade_record(
    pnl_amount: float = 10.0,
    symbol: str = "BTCUSDT",
) -> TradeRecord:
    """构造 TradeRecord。"""
    return TradeRecord(
        symbol=symbol,
        direction=TradeDirection.LONG,
        entry_price=100.0,
        exit_price=110.0,
        pnl_amount=pnl_amount,
        hold_duration_hours=2.0,
        rating_score=7,
        position_size_pct=10.0,
        closed_at=datetime.now(timezone.utc),
    )


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def state_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_state.db")
    store = StateStore(db_path=db_path)
    yield store
    store.close()


@pytest.fixture
def memory_store(tmp_path):
    db_path = os.path.join(str(tmp_path), "test_memory.db")
    store = MemoryStore(db_path=db_path)
    yield store
    store.close()


def _make_skill(
    state_store,
    memory_store,
    account=None,
    trade_syncer=None,
) -> Skill5Evolve:
    """创建 Skill5Evolve 实例。"""
    if account is None:
        account = _make_account()

    return Skill5Evolve(
        state_store=state_store,
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        memory_store=memory_store,
        account_state_provider=lambda: account,
        trade_syncer=trade_syncer,
    )


# ══════════════════════════════════════════════════════════
# 1. 正常执行流程
# ══════════════════════════════════════════════════════════

class TestNormalExecution:
    """测试正常执行流程。"""

    def test_basic_execution_with_positions(
        self, state_store, memory_store
    ):
        """有持仓时应正确构建展示数据。"""
        positions = [
            _make_position(
                symbol="BTCUSDT", direction="long",
                quantity=1.0, entry_price=100.0, current_price=110.0,
            ),
        ]
        account = _make_account(positions=positions)

        # 存储上游数据
        upstream = {
            "state_id": str(uuid.uuid4()),
            "execution_results": [_make_execution_result()],
            "is_paper_mode": False,
        }
        sid = state_store.save("skill4_execute", upstream)

        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({"input_state_id": sid})

        assert "state_id" in result
        assert result["account_summary"]["total_balance"] == 10000.0
        assert len(result["positions"]) == 1
        assert result["positions"][0]["symbol"] == "BTCUSDT"
        # 做多 100→110，盈亏比例 = 10%
        assert abs(result["positions"][0]["pnl_ratio"] - 10.0) < 0.01

    def test_skill_name(self, state_store, memory_store):
        """Skill 名称应为 skill5_evolve。"""
        skill = _make_skill(state_store, memory_store)
        assert skill.name == "skill5_evolve"

    def test_state_id_is_uuid(self, state_store, memory_store):
        """输出的 state_id 应为有效 UUID。"""
        skill = _make_skill(state_store, memory_store)
        result = skill.run({})
        uuid.UUID(result["state_id"], version=4)

    def test_syncs_server_closed_trades_before_evolution(
        self, state_store, memory_store
    ):
        """Skill-5 应在计算进化前同步 Binance 服务端平仓成交。"""
        upstream = {
            "state_id": str(uuid.uuid4()),
            "execution_results": [
                _make_execution_result(
                    symbol="BTCUSDT",
                    status="open",
                    rating_score=8,
                    position_size_pct=12.0,
                )
            ],
            "is_paper_mode": False,
        }
        sid = state_store.save("skill4_execute", upstream)
        trade_syncer = MagicMock()
        trade_syncer.sync_closed_trades.return_value = 1

        skill = _make_skill(
            state_store,
            memory_store,
            trade_syncer=trade_syncer,
        )
        skill.run({"input_state_id": sid})

        trade_syncer.sync_closed_trades.assert_called_once()
        kwargs = trade_syncer.sync_closed_trades.call_args.kwargs
        assert kwargs["symbols"] == {"BTCUSDT"}
        assert kwargs["metadata_by_symbol"]["BTCUSDT"]["rating_score"] == 8
        assert kwargs["metadata_by_symbol"]["BTCUSDT"]["position_size_pct"] == 12.0

    def test_sync_symbols_include_current_positions(
        self, state_store, memory_store
    ):
        """没有本轮执行结果时，也应尝试同步当前持仓币种。"""
        account = _make_account(positions=[_make_position(symbol="ETHUSDT")])
        trade_syncer = MagicMock()
        trade_syncer.sync_closed_trades.return_value = 0

        skill = _make_skill(
            state_store,
            memory_store,
            account=account,
            trade_syncer=trade_syncer,
        )
        skill.run({})

        kwargs = trade_syncer.sync_closed_trades.call_args.kwargs
        assert kwargs["symbols"] == {"ETHUSDT"}


# ══════════════════════════════════════════════════════════
# 2. 交易记录不足 20 笔 → 跳过进化
# ══════════════════════════════════════════════════════════

class TestInsufficientTrades:
    """测试交易记录不足 20 笔的场景（需求 5.7）。"""

    def test_less_than_20_trades_skips_evolution(
        self, state_store, memory_store
    ):
        """不足 20 笔交易时应跳过进化计算。"""
        # 插入 5 笔交易
        for _ in range(5):
            memory_store.record_trade(_make_trade_record())

        skill = _make_skill(state_store, memory_store)
        result = skill.run({})

        assert result["evolution"]["adjustment_applied"] is False
        assert result["evolution"]["trade_count"] == 5
        assert "不足 20 笔" in result["evolution"]["adjustment_detail"]

    def test_zero_trades_skips_evolution(
        self, state_store, memory_store
    ):
        """零笔交易时应跳过进化计算。"""
        skill = _make_skill(state_store, memory_store)
        result = skill.run({})

        assert result["evolution"]["trade_count"] == 0
        assert result["evolution"]["adjustment_applied"] is False
        assert result["evolution"]["win_rate"] == 0.0


# ══════════════════════════════════════════════════════════
# 3. 胜率低于 38% → 调优建议（需连续两轮确认）
# ══════════════════════════════════════════════════════════

class TestLowWinRate:
    """测试胜率低于 38% 时的调优逻辑（需求 5.5, 5.6）。"""

    def test_low_win_rate_triggers_adjustment(
        self, state_store, memory_store
    ):
        """胜率低于 38% 且连续两轮确认后应触发参数调整。"""
        # 插入 20 笔交易：4 笔盈利，16 笔亏损 → 胜率 20%
        for i in range(20):
            pnl = 10.0 if i < 4 else -5.0
            memory_store.record_trade(_make_trade_record(pnl_amount=pnl))

        # 先写入一条上轮同向反思日志（模拟连续确认）
        from src.models.types import ReflectionLog
        from datetime import datetime, timezone
        memory_store.save_reflection(ReflectionLog(
            created_at=datetime.now(timezone.utc),
            win_rate=25.0,
            avg_pnl_ratio=-1.0,
            suggested_rating_threshold=6,
            suggested_risk_ratio=0.02,
            reasoning="上轮低胜率",
            strategy_tag="unknown",
        ))

        skill = _make_skill(state_store, memory_store)
        result = skill.run({})

        assert result["evolution"]["adjustment_applied"] is True
        assert result["evolution"]["trade_count"] == 20
        assert result["evolution"]["win_rate"] == 20.0

    def test_adjustment_saves_reflection(
        self, state_store, memory_store
    ):
        """调优时应保存反思日志到 Memory_Store。"""
        for i in range(20):
            pnl = 10.0 if i < 4 else -5.0
            memory_store.record_trade(_make_trade_record(pnl_amount=pnl))

        # 写入上轮同向反思日志
        from src.models.types import ReflectionLog
        from datetime import datetime, timezone
        memory_store.save_reflection(ReflectionLog(
            created_at=datetime.now(timezone.utc),
            win_rate=25.0,
            avg_pnl_ratio=-1.0,
            suggested_rating_threshold=6,
            suggested_risk_ratio=0.02,
            reasoning="上轮低胜率",
            strategy_tag="unknown",
        ))

        skill = _make_skill(state_store, memory_store)
        skill.run({})

        reflection = memory_store.get_latest_reflection(strategy_tag="unknown")
        assert reflection is not None
        assert reflection.win_rate == 20.0


# ══════════════════════════════════════════════════════════
# 4. 胜率正常 → 维持默认参数
# ══════════════════════════════════════════════════════════

class TestNormalWinRate:
    """测试胜率正常时维持默认参数（需求 5.4）。"""

    def test_normal_win_rate_no_adjustment(
        self, state_store, memory_store
    ):
        """胜率在 38%-62% 死区内时不应触发参数调整。"""
        # 插入 20 笔交易：10 笔盈利，10 笔亏损 → 胜率 50%
        for i in range(20):
            pnl = 10.0 if i < 10 else -5.0
            memory_store.record_trade(_make_trade_record(pnl_amount=pnl))

        skill = _make_skill(state_store, memory_store)
        result = skill.run({})

        assert result["evolution"]["adjustment_applied"] is False
        assert result["evolution"]["trade_count"] == 20
        assert result["evolution"]["win_rate"] == 50.0


# ══════════════════════════════════════════════════════════
# 5. 空持仓列表
# ══════════════════════════════════════════════════════════

class TestEmptyPositions:
    """测试空持仓列表场景。"""

    def test_no_positions(self, state_store, memory_store):
        """无持仓时 positions 应为空列表。"""
        account = _make_account(positions=[])
        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({})

        assert result["positions"] == []
        assert result["account_summary"]["unrealized_pnl"] == 0.0


# ══════════════════════════════════════════════════════════
# 6. Paper Mode 标记
# ══════════════════════════════════════════════════════════

class TestPaperMode:
    """测试 Paper Mode 标记。"""

    def test_paper_mode_flag(self, state_store, memory_store):
        """Paper Mode 应在输出中正确标记。"""
        account = _make_account(is_paper_mode=True)
        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({})

        assert result["account_summary"]["is_paper_mode"] is True


# ══════════════════════════════════════════════════════════
# 7. 无上游 state_id（定时触发）
# ══════════════════════════════════════════════════════════

class TestNoUpstreamStateId:
    """测试无上游 state_id 的场景。"""

    def test_no_input_state_id(self, state_store, memory_store):
        """无 input_state_id 时应正常执行。"""
        skill = _make_skill(state_store, memory_store)
        result = skill.run({})

        assert "state_id" in result
        assert "account_summary" in result
        assert "positions" in result
        assert "evolution" in result


# ══════════════════════════════════════════════════════════
# 8. 平仓交易数据提取
# ══════════════════════════════════════════════════════════

class TestClosedTradeRecording:
    """测试平仓交易数据提取存入 Memory_Store（需求 5.3）。"""

    def test_filled_trade_recorded(self, state_store, memory_store):
        """已成交交易应被记录到 Memory_Store。"""
        upstream = {
            "state_id": str(uuid.uuid4()),
            "execution_results": [
                _make_execution_result(status="filled"),
            ],
            "is_paper_mode": False,
        }
        sid = state_store.save("skill4_execute", upstream)

        skill = _make_skill(state_store, memory_store)
        skill.run({"input_state_id": sid})

        trades = memory_store.get_recent_trades(limit=10)
        assert len(trades) == 1
        assert trades[0].symbol == "BTCUSDT"

    def test_rejected_trade_not_recorded(
        self, state_store, memory_store
    ):
        """被风控拒绝的交易不应被记录。"""
        upstream = {
            "state_id": str(uuid.uuid4()),
            "execution_results": [
                _make_execution_result(status="rejected_by_risk"),
            ],
            "is_paper_mode": False,
        }
        sid = state_store.save("skill4_execute", upstream)

        skill = _make_skill(state_store, memory_store)
        skill.run({"input_state_id": sid})

        trades = memory_store.get_recent_trades(limit=10)
        assert len(trades) == 0

    def test_paper_trade_recorded(self, state_store, memory_store):
        """模拟盘交易应被记录到 Memory_Store。"""
        upstream = {
            "state_id": str(uuid.uuid4()),
            "execution_results": [
                _make_execution_result(status="paper_trade"),
            ],
            "is_paper_mode": True,
        }
        sid = state_store.save("skill4_execute", upstream)

        skill = _make_skill(state_store, memory_store)
        skill.run({"input_state_id": sid})

        trades = memory_store.get_recent_trades(limit=10)
        assert len(trades) == 1


# ══════════════════════════════════════════════════════════
# 9. Markdown 表格生成
# ══════════════════════════════════════════════════════════

class TestMarkdownGeneration:
    """测试 Markdown 表格生成（需求 5.2）。"""

    def test_markdown_with_positions(self, state_store, memory_store):
        """有持仓时 Markdown 应包含持仓明细表格。"""
        positions = [
            _make_position(
                symbol="BTCUSDT", direction="long",
                quantity=1.0, entry_price=100.0, current_price=110.0,
            ),
        ]
        account = _make_account(positions=positions)

        md = Skill5Evolve._generate_markdown(
            account,
            [{"symbol": "BTCUSDT", "direction": "long",
              "quantity": 1.0, "entry_price": 100.0,
              "current_price": 110.0, "pnl_ratio": 10.0}],
            {"win_rate": 60.0, "avg_pnl_ratio": 5.0,
             "trade_count": 10, "adjustment_applied": False,
             "adjustment_detail": ""},
        )

        assert "账户状态概览" in md
        assert "BTCUSDT" in md
        assert "持仓明细" in md
        assert "10000.00" in md

    def test_markdown_no_positions(self, state_store, memory_store):
        """无持仓时 Markdown 应显示"当前无持仓"。"""
        account = _make_account()

        md = Skill5Evolve._generate_markdown(
            account, [],
            {"win_rate": 0.0, "avg_pnl_ratio": 0.0,
             "trade_count": 0, "adjustment_applied": False,
             "adjustment_detail": ""},
        )

        assert "当前无持仓" in md

    def test_markdown_paper_mode_tag(self, state_store, memory_store):
        """Paper Mode 时 Markdown 应包含模拟盘标记。"""
        account = _make_account(is_paper_mode=True)

        md = Skill5Evolve._generate_markdown(
            account, [],
            {"win_rate": 0.0, "avg_pnl_ratio": 0.0,
             "trade_count": 0, "adjustment_applied": False,
             "adjustment_detail": ""},
        )

        assert "模拟盘" in md


# ══════════════════════════════════════════════════════════
# 10. 盈亏比例计算
# ══════════════════════════════════════════════════════════

class TestPnlRatioCalculation:
    """测试持仓盈亏比例计算（需求 5.2）。"""

    def test_long_profit(self, state_store, memory_store):
        """做多盈利时盈亏比例应为正数。"""
        positions = [
            _make_position(
                direction="long",
                entry_price=100.0,
                current_price=120.0,
            ),
        ]
        account = _make_account(positions=positions)
        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({})

        # (120 - 100) / 100 * 100 = 20%
        assert abs(result["positions"][0]["pnl_ratio"] - 20.0) < 0.01

    def test_long_loss(self, state_store, memory_store):
        """做多亏损时盈亏比例应为负数。"""
        positions = [
            _make_position(
                direction="long",
                entry_price=100.0,
                current_price=90.0,
            ),
        ]
        account = _make_account(positions=positions)
        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({})

        # (90 - 100) / 100 * 100 = -10%
        assert abs(result["positions"][0]["pnl_ratio"] - (-10.0)) < 0.01

    def test_short_profit(self, state_store, memory_store):
        """做空盈利时盈亏比例应为正数。"""
        positions = [
            _make_position(
                direction="short",
                entry_price=100.0,
                current_price=90.0,
            ),
        ]
        account = _make_account(positions=positions)
        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({})

        # (100 - 90) / 100 * 100 = 10%
        assert abs(result["positions"][0]["pnl_ratio"] - 10.0) < 0.01

    def test_short_loss(self, state_store, memory_store):
        """做空亏损时盈亏比例应为负数。"""
        positions = [
            _make_position(
                direction="short",
                entry_price=100.0,
                current_price=110.0,
            ),
        ]
        account = _make_account(positions=positions)
        skill = _make_skill(state_store, memory_store, account)
        result = skill.run({})

        # (100 - 110) / 100 * 100 = -10%
        assert abs(result["positions"][0]["pnl_ratio"] - (-10.0)) < 0.01
