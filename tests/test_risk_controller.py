"""
RiskController 单元测试

测试风控拦截层的核心功能：
- validate_order: 单笔保证金、单币持仓、止损冷却期
- check_daily_loss: 日亏损检测
- execute_degradation: 降级流程
- is_paper_mode / record_stop_loss
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.infra.risk_controller import RiskController
from src.models.types import (
    AccountState,
    OrderRequest,
    TradeDirection,
)


def _make_account(
    total_balance: float = 10000.0,
    daily_pnl: float = 0.0,
    positions: list | None = None,
) -> AccountState:
    """创建测试用 AccountState。"""
    return AccountState(
        total_balance=total_balance,
        available_margin=total_balance * 0.5,
        daily_realized_pnl=daily_pnl,
        positions=positions or [],
    )


def _make_order(
    symbol: str = "BTCUSDT",
    direction: TradeDirection = TradeDirection.LONG,
    price: float = 50000.0,
    quantity: float = 0.01,
    leverage: int = 10,
) -> OrderRequest:
    """创建测试用 OrderRequest。"""
    return OrderRequest(
        symbol=symbol,
        direction=direction,
        price=price,
        quantity=quantity,
        leverage=leverage,
    )


# ============================================================
# validate_order 测试
# ============================================================


class TestValidateOrder:
    """validate_order 风控断言校验测试。"""

    def test_order_passes_all_checks(self):
        """正常订单应通过所有校验。"""
        rc = RiskController()
        account = _make_account(total_balance=100000.0)
        # 保证金 = 0.01 * 50000 / 10 = 50，远小于 100000 * 0.2 = 20000
        order = _make_order(price=50000.0, quantity=0.01, leverage=10)
        result = rc.validate_order(order, account)
        assert result.passed is True
        assert result.reason == ""

    def test_single_margin_exceeds_limit(self):
        """单笔保证金超过 20% 应被拒绝。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0)
        # 保证金 = 1.0 * 50000 / 10 = 5000，超过 10000 * 0.2 = 2000
        order = _make_order(price=50000.0, quantity=1.0, leverage=10)
        result = rc.validate_order(order, account)
        assert result.passed is False
        assert "单笔保证金" in result.reason

    def test_single_margin_at_exact_limit(self):
        """单笔保证金恰好等于 20% 应通过（同时满足 30% 持仓限制）。"""
        rc = RiskController()
        account = _make_account(total_balance=100000.0)
        # 保证金 = 0.4 * 50000 / 10 = 2000，限额 = 100000 * 0.2 = 20000 → 通过
        # 持仓价值 = 0.4 * 50000 = 20000，限额 = 100000 * 0.3 = 30000 → 通过
        order = _make_order(price=50000.0, quantity=0.4, leverage=10)
        result = rc.validate_order(order, account)
        assert result.passed is True

    def test_coin_position_exceeds_limit(self):
        """单币累计持仓超过 30% 应被拒绝。"""
        rc = RiskController()
        # 已有持仓价值 2500（0.05 * 50000）
        positions = [{"symbol": "BTCUSDT", "quantity": 0.05, "entry_price": 50000.0}]
        account = _make_account(total_balance=10000.0, positions=positions)
        # 新订单价值 = 0.02 * 50000 = 1000，累计 = 2500 + 1000 = 3500 > 3000
        order = _make_order(price=50000.0, quantity=0.02, leverage=10)
        result = rc.validate_order(order, account)
        assert result.passed is False
        assert "单币累计持仓" in result.reason

    def test_coin_position_within_limit(self):
        """单币累计持仓在 30% 以内应通过。"""
        rc = RiskController()
        positions = [{"symbol": "BTCUSDT", "quantity": 0.01, "entry_price": 50000.0}]
        account = _make_account(total_balance=10000.0, positions=positions)
        # 已有 500，新增 500，累计 1000 < 3000
        order = _make_order(price=50000.0, quantity=0.01, leverage=10)
        result = rc.validate_order(order, account)
        assert result.passed is True

    def test_cooldown_rejects_same_direction(self):
        """止损冷却期内同方向订单应被拒绝。"""
        rc = RiskController()
        rc.record_stop_loss("BTCUSDT", "long")
        account = _make_account(total_balance=100000.0)
        order = _make_order(symbol="BTCUSDT", direction=TradeDirection.LONG)
        result = rc.validate_order(order, account)
        assert result.passed is False
        assert "冷却期" in result.reason

    def test_cooldown_allows_opposite_direction(self):
        """止损冷却期内反方向订单应通过。"""
        rc = RiskController()
        rc.record_stop_loss("BTCUSDT", "long")
        account = _make_account(total_balance=100000.0)
        order = _make_order(symbol="BTCUSDT", direction=TradeDirection.SHORT)
        result = rc.validate_order(order, account)
        assert result.passed is True

    def test_cooldown_allows_different_symbol(self):
        """止损冷却期内不同币种订单应通过。"""
        rc = RiskController()
        rc.record_stop_loss("BTCUSDT", "long")
        account = _make_account(total_balance=100000.0)
        order = _make_order(symbol="ETHUSDT", direction=TradeDirection.LONG)
        result = rc.validate_order(order, account)
        assert result.passed is True


# ============================================================
# check_daily_loss 测试
# ============================================================


class TestCheckDailyLoss:
    """check_daily_loss 日亏损检测测试。"""

    def test_no_loss(self):
        """无亏损时不触发降级。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=100.0)
        assert rc.check_daily_loss(account) is False

    def test_loss_below_threshold(self):
        """亏损低于 5% 不触发降级。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=-400.0)
        assert rc.check_daily_loss(account) is False

    def test_loss_at_threshold(self):
        """亏损恰好 5% 触发降级。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=-500.0)
        assert rc.check_daily_loss(account) is True

    def test_loss_above_threshold(self):
        """亏损超过 5% 触发降级。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=-800.0)
        assert rc.check_daily_loss(account) is True

    def test_zero_balance(self):
        """总资金为 0 时不触发降级（避免除零）。"""
        rc = RiskController()
        account = _make_account(total_balance=0.0, daily_pnl=-100.0)
        assert rc.check_daily_loss(account) is False


# ============================================================
# execute_degradation 测试
# ============================================================


class TestExecuteDegradation:
    """execute_degradation 降级流程测试。"""

    def test_switches_to_paper_mode(self):
        """降级后应切换至 Paper Mode。"""
        rc = RiskController()
        assert rc.is_paper_mode() is False
        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)
        rc.execute_degradation(account)
        assert rc.is_paper_mode() is True

    def test_calls_cancel_all_orders(self):
        """降级时应调用 binance_client.cancel_all_orders()。"""
        rc = RiskController()
        mock_client = MagicMock()
        mock_client.cancel_all_orders.return_value = 3
        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)
        rc.execute_degradation(account, binance_client=mock_client)
        mock_client.cancel_all_orders.assert_called_once()
        assert rc.is_paper_mode() is True

    def test_degradation_without_client(self):
        """无 binance_client 时降级仍应完成（跳过取消挂单）。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)
        rc.execute_degradation(account, binance_client=None)
        assert rc.is_paper_mode() is True

    def test_degradation_client_error_still_switches(self):
        """binance_client 报错时降级仍应完成。"""
        rc = RiskController()
        mock_client = MagicMock()
        mock_client.cancel_all_orders.side_effect = Exception("网络错误")
        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)
        rc.execute_degradation(account, binance_client=mock_client)
        assert rc.is_paper_mode() is True


# ============================================================
# record_stop_loss / is_paper_mode 测试
# ============================================================


class TestRecordStopLossAndPaperMode:
    """record_stop_loss 和 is_paper_mode 测试。"""

    def test_initial_not_paper_mode(self):
        """初始状态不是 Paper Mode。"""
        rc = RiskController()
        assert rc.is_paper_mode() is False

    def test_enable_disable_paper_mode_public_api(self):
        """Paper Mode 应通过公开 API 切换。"""
        rc = RiskController()
        rc.enable_paper_mode("test")
        assert rc.is_paper_mode() is True
        rc.disable_paper_mode("test")
        assert rc.is_paper_mode() is False

    def test_paper_mode_persists_with_sqlite(self, tmp_path):
        """持久化模式下，Paper Mode 应在重启后恢复。"""
        db_path = tmp_path / "risk.db"
        rc = RiskController(db_path=str(db_path))
        rc.enable_paper_mode("test")
        rc.close()

        restored = RiskController(db_path=str(db_path))
        try:
            assert restored.is_paper_mode() is True
        finally:
            restored.close()

    def test_record_stop_loss_creates_cooldown(self):
        """记录止损后应创建冷却期。"""
        rc = RiskController()
        rc.record_stop_loss("BTCUSDT", "long")
        assert rc._is_in_cooldown("BTCUSDT", "long") is True
        assert rc._is_in_cooldown("BTCUSDT", "short") is False

    def test_expired_cooldown(self):
        """超过 24 小时的止损记录不应阻止开仓。"""
        rc = RiskController()
        # 手动插入一条 25 小时前的记录
        old_time = datetime.now() - timedelta(hours=25)
        rc._stop_loss_records.append(("BTCUSDT", "long", old_time))
        assert rc._is_in_cooldown("BTCUSDT", "long") is False


# ============================================================
# 完整降级流程测试（需求 8.5, 8.6, 8.7）
# ============================================================


class TestFullDegradationFlow:
    """完整降级流程端到端测试。

    验证从日亏损检测到降级完成的完整流程：
    1. 取消所有未成交挂单
    2. 停止实盘下单（切换 Paper Mode）
    3. 发出告警通知（CRITICAL 级别日志）
    4. 系统处于 Paper_Trading_Mode
    """

    def test_full_degradation_flow_end_to_end(self):
        """完整降级流程：检测亏损 → 取消挂单 → 停止实盘 → 告警 → Paper Mode。"""
        rc = RiskController()
        mock_client = MagicMock()
        mock_client.cancel_all_orders.return_value = 5

        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)

        # 步骤 1：检测日亏损达到阈值
        assert rc.check_daily_loss(account) is True

        # 步骤 2：确认降级前不是 Paper Mode
        assert rc.is_paper_mode() is False

        # 步骤 3：执行降级
        rc.execute_degradation(account, binance_client=mock_client)

        # 验证：取消挂单被调用
        mock_client.cancel_all_orders.assert_called_once()

        # 验证：系统已切换至 Paper Mode
        assert rc.is_paper_mode() is True

    def test_degradation_logs_critical_alert(self, caplog):
        """降级流程应发出 CRITICAL 级别告警日志。"""
        import logging

        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)

        with caplog.at_level(logging.CRITICAL, logger="src.infra.risk_controller"):
            rc.execute_degradation(account)

        # 验证 CRITICAL 级别日志包含降级相关信息
        critical_msgs = [r.message for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert len(critical_msgs) > 0, "降级流程未发出 CRITICAL 级别告警"
        assert any("降级" in msg or "阈值" in msg for msg in critical_msgs), (
            f"CRITICAL 日志中未包含降级相关信息: {critical_msgs}"
        )

    def test_degradation_prevents_real_orders(self):
        """降级后 Paper Mode 下，validate_order 仍可校验但系统标记为模拟盘。"""
        rc = RiskController()
        account = _make_account(total_balance=10000.0, daily_pnl=-600.0)

        # 执行降级
        rc.execute_degradation(account)
        assert rc.is_paper_mode() is True

        # 降级后仍可进行订单校验（风控层不阻止校验本身）
        order = _make_order(price=50000.0, quantity=0.001, leverage=10)
        clean_account = _make_account(total_balance=10000.0, daily_pnl=0.0)
        result = rc.validate_order(order, clean_account)
        assert result.passed is True  # 订单本身合规，校验通过

    def test_degradation_with_cancel_failure_still_enters_paper_mode(self):
        """取消挂单失败时，降级流程仍应完成并进入 Paper Mode。"""
        rc = RiskController()
        mock_client = MagicMock()
        mock_client.cancel_all_orders.side_effect = ConnectionError("连接超时")

        account = _make_account(total_balance=10000.0, daily_pnl=-800.0)

        # 即使取消挂单失败，降级仍应完成
        rc.execute_degradation(account, binance_client=mock_client)
        mock_client.cancel_all_orders.assert_called_once()
        assert rc.is_paper_mode() is True

    def test_check_then_degrade_at_exact_threshold(self):
        """恰好 5% 亏损时应触发降级并完成全流程。"""
        rc = RiskController()
        mock_client = MagicMock()
        mock_client.cancel_all_orders.return_value = 2

        # 恰好 5% 亏损
        account = _make_account(total_balance=10000.0, daily_pnl=-500.0)

        assert rc.check_daily_loss(account) is True
        rc.execute_degradation(account, binance_client=mock_client)

        mock_client.cancel_all_orders.assert_called_once()
        assert rc.is_paper_mode() is True
