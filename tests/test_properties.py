"""
属性测试模块

使用 hypothesis 库对核心计算函数进行属性测试，
验证系统在所有合法输入下的正确性不变量。
"""

import math
from datetime import datetime, timedelta

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.models.types import (
    TradeDirection,
    TradeRecord,
    calculate_pnl_ratio,
    calculate_position_size,
    compute_evolution_adjustment,
)


# Feature: openclaw-binance-agent, Property 19: 盈亏比例计算正确性
# **Validates: Requirements 5.2**
class TestPnlRatioCalculation:
    """盈亏比例计算正确性属性测试。

    对于任意正数的入场价格和正数的当前价格，
    做多方向的盈亏比例应等于 (current - entry) / entry × 100，
    做空方向的盈亏比例应等于 (entry - current) / entry × 100。
    """

    @given(
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        current_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_pnl_ratio_long_formula(self, entry_price: float, current_price: float) -> None:
        """做多方向：盈亏比例 = (当前价格 - 入场价格) / 入场价格 × 100"""
        result = calculate_pnl_ratio(entry_price, current_price, TradeDirection.LONG)
        expected = ((current_price - entry_price) / entry_price) * 100
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"做多盈亏比例不匹配: got {result}, expected {expected}"
        )

    @given(
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        current_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_pnl_ratio_short_formula(self, entry_price: float, current_price: float) -> None:
        """做空方向：盈亏比例 = (入场价格 - 当前价格) / 入场价格 × 100"""
        result = calculate_pnl_ratio(entry_price, current_price, TradeDirection.SHORT)
        expected = ((entry_price - current_price) / entry_price) * 100
        assert math.isclose(result, expected, rel_tol=1e-9), (
            f"做空盈亏比例不匹配: got {result}, expected {expected}"
        )

    @given(
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        direction=st.sampled_from([TradeDirection.LONG, TradeDirection.SHORT]),
    )
    @settings(max_examples=20)
    def test_pnl_ratio_same_price_is_zero(self, entry_price: float, direction: TradeDirection) -> None:
        """当入场价格等于当前价格时，盈亏比例应为 0"""
        result = calculate_pnl_ratio(entry_price, entry_price, direction)
        assert result == 0.0, f"相同价格时盈亏比例应为 0, got {result}"

    @given(
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        current_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_pnl_ratio_long_short_symmetry(self, entry_price: float, current_price: float) -> None:
        """做多和做空的盈亏比例互为相反数"""
        long_ratio = calculate_pnl_ratio(entry_price, current_price, TradeDirection.LONG)
        short_ratio = calculate_pnl_ratio(entry_price, current_price, TradeDirection.SHORT)
        assert math.isclose(long_ratio + short_ratio, 0.0, abs_tol=1e-9), (
            f"做多({long_ratio}) + 做空({short_ratio}) 应为 0"
        )

# Feature: openclaw-binance-agent, Property 16: 数值参数边界校验
# **Validates: Requirements 3.8**
class TestNumericBoundaryValidation:
    """数值参数边界校验属性测试。

    对于任意包含非正数价格或非正数头寸规模的交易计划输入，
    系统应拒绝该输入并抛出校验错误（ValueError）。
    """

    # ------------------------------------------------------------------
    # calculate_position_size 边界校验
    # ------------------------------------------------------------------

    @given(
        account_balance=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
        risk_ratio=st.floats(min_value=0.001, max_value=0.20, allow_nan=False, allow_infinity=False),
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_non_positive_account_balance_rejected(
        self, account_balance: float, risk_ratio: float, entry_price: float, stop_loss_price: float
    ) -> None:
        """非正数的账户余额（0 或负数）应被拒绝"""
        import pytest
        from hypothesis import assume

        assume(entry_price != stop_loss_price)
        with pytest.raises(ValueError, match="账户余额必须为正数"):
            calculate_position_size(account_balance, risk_ratio, entry_price, stop_loss_price)

    @given(
        account_balance=st.floats(min_value=100, max_value=1e8, allow_nan=False, allow_infinity=False),
        entry_price=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_non_positive_entry_price_rejected(
        self, account_balance: float, entry_price: float, stop_loss_price: float
    ) -> None:
        """非正数的入场价格（0 或负数）应被拒绝"""
        import pytest

        with pytest.raises(ValueError, match="入场价格必须为正数"):
            calculate_position_size(account_balance, 0.02, entry_price, stop_loss_price)

    @given(
        account_balance=st.floats(min_value=100, max_value=1e8, allow_nan=False, allow_infinity=False),
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_non_positive_stop_loss_price_rejected(
        self, account_balance: float, entry_price: float, stop_loss_price: float
    ) -> None:
        """非正数的止损价格（0 或负数）应被拒绝"""
        import pytest

        with pytest.raises(ValueError, match="止损价格必须为正数"):
            calculate_position_size(account_balance, 0.02, entry_price, stop_loss_price)

    @given(
        account_balance=st.floats(min_value=100, max_value=1e8, allow_nan=False, allow_infinity=False),
        risk_ratio=st.one_of(
            st.floats(max_value=0, allow_nan=False, allow_infinity=False),
            st.floats(min_value=0.201, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_risk_ratio_out_of_range_rejected(
        self, account_balance: float, risk_ratio: float, entry_price: float, stop_loss_price: float
    ) -> None:
        """risk_ratio 超出 (0, 0.20] 范围应被拒绝"""
        import pytest
        from hypothesis import assume

        assume(entry_price != stop_loss_price)
        with pytest.raises(ValueError, match="风险比例必须在"):
            calculate_position_size(account_balance, risk_ratio, entry_price, stop_loss_price)

    @given(
        account_balance=st.floats(min_value=100, max_value=1e8, allow_nan=False, allow_infinity=False),
        price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_entry_equals_stop_loss_rejected(
        self, account_balance: float, price: float
    ) -> None:
        """入场价格等于止损价格应被拒绝"""
        import pytest

        with pytest.raises(ValueError, match="入场价格不能等于止损价格"):
            calculate_position_size(account_balance, 0.02, price, price)

    # ------------------------------------------------------------------
    # calculate_pnl_ratio 边界校验
    # ------------------------------------------------------------------

    @given(
        entry_price=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
        current_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        direction=st.sampled_from([TradeDirection.LONG, TradeDirection.SHORT]),
    )
    @settings(max_examples=20)
    def test_pnl_ratio_non_positive_entry_rejected(
        self, entry_price: float, current_price: float, direction: TradeDirection
    ) -> None:
        """calculate_pnl_ratio 对非正数入场价格应抛出 ValueError"""
        import pytest

        with pytest.raises(ValueError, match="入场价格必须为正数"):
            calculate_pnl_ratio(entry_price, current_price, direction)

    @given(
        entry_price=st.floats(min_value=1e-8, max_value=1e12, allow_nan=False, allow_infinity=False),
        current_price=st.floats(max_value=0, allow_nan=False, allow_infinity=False),
        direction=st.sampled_from([TradeDirection.LONG, TradeDirection.SHORT]),
    )
    @settings(max_examples=20)
    def test_pnl_ratio_non_positive_current_rejected(
        self, entry_price: float, current_price: float, direction: TradeDirection
    ) -> None:
        """calculate_pnl_ratio 对非正数当前价格应抛出 ValueError"""
        import pytest

        with pytest.raises(ValueError, match="当前价格必须为正数"):
            calculate_pnl_ratio(entry_price, current_price, direction)


# Feature: openclaw-binance-agent, Property 1: State_Store 存取 round-trip
# **Validates: Requirements 1.6, 2.1, 2.5, 3.1, 3.6, 4.1, 4.13, 5.1, 5.3, 6.1, 6.2**
class TestStateStoreRoundTrip:
    """State_Store 存取 round-trip 属性测试。

    对于任意 Skill 名称和任意合法的 JSON 数据对象，
    将其通过 State_Store.save() 存储后，使用返回的 state_id
    调用 State_Store.load() 应当返回与原始数据完全一致的 JSON 对象，
    且 state_id 符合 UUID v4 格式。
    """

    @staticmethod
    def _json_strategy():
        """生成随机 JSON 对象的策略（使用 st.recursive 生成嵌套结构）。"""
        # 基础类型：字符串、整数、浮点数、布尔值、None
        base = st.one_of(
            st.text(min_size=0, max_size=50),
            st.integers(min_value=-(2**53), max_value=2**53),
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
            st.none(),
        )
        # 递归构建嵌套结构：列表和字典
        return st.recursive(
            base,
            lambda children: st.one_of(
                st.lists(children, max_size=5),
                st.dictionaries(st.text(min_size=1, max_size=20), children, max_size=5),
            ),
            max_leaves=15,
        )

    @staticmethod
    def _skill_name_strategy():
        """生成随机 Skill 名称的策略。"""
        return st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
            min_size=1,
            max_size=30,
        )

    @given(data=st.data())
    @settings(max_examples=20)
    def test_state_store_round_trip(self, data):
        """存取 round-trip：save 后 load 返回的数据与原始数据完全一致，
        且 state_id 符合 UUID v4 格式。"""
        import re
        import tempfile
        import uuid

        from src.infra.state_store import StateStore

        # 生成随机 Skill 名称和 JSON 数据对象
        skill_name = data.draw(self._skill_name_strategy(), label="skill_name")
        # 确保顶层是 dict（符合 State_Store.save 的 data: dict 签名）
        json_data = data.draw(
            st.dictionaries(
                st.text(min_size=1, max_size=20),
                self._json_strategy(),
                min_size=0,
                max_size=5,
            ),
            label="json_data",
        )

        # 使用 tempfile 创建临时数据库（每次迭代独立）
        tmp_dir = tempfile.mkdtemp()
        db_path = str(tmp_dir + "/test_state.db")
        store = StateStore(db_path=db_path)

        try:
            # 存储数据
            state_id = store.save(skill_name, json_data)

            # 验证 state_id 符合 UUID v4 格式
            # UUID v4 格式：xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx
            uuid_v4_pattern = re.compile(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
                re.IGNORECASE,
            )
            assert uuid_v4_pattern.match(state_id), (
                f"state_id 不符合 UUID v4 格式: {state_id}"
            )

            # 额外验证：使用 uuid 模块解析确认版本号为 4
            parsed_uuid = uuid.UUID(state_id)
            assert parsed_uuid.version == 4, (
                f"state_id UUID 版本应为 4, 实际为 {parsed_uuid.version}"
            )

            # 读取数据
            loaded_data = store.load(state_id)

            # 验证存取一致性：load 返回的数据与原始数据完全一致
            assert loaded_data == json_data, (
                f"存取数据不一致:\n原始: {json_data}\n读取: {loaded_data}"
            )
        finally:
            store.close()


# Feature: openclaw-binance-agent, Property 11: 限流速率不变量
# **Validates: Requirements 4.8, 7.1**
class TestRateLimiterInvariant:
    """限流速率不变量属性测试。

    验证 RateLimiter 在任意队列大小下，_get_current_rate() 返回的速率上限
    始终不超过 NORMAL_RATE（1000 次/分钟），从而保证任意 60 秒窗口内
    请求数不超过速率上限。
    """

    @given(
        queue_size=st.integers(min_value=0, max_value=10000),
    )
    @settings(max_examples=20)
    def test_rate_limiter_invariant(self, queue_size: int) -> None:
        """任意队列大小下，当前速率不超过正常速率上限（1000/min）"""
        from src.infra.rate_limiter import RateLimiter

        limiter = RateLimiter()
        limiter._queue_size = queue_size

        rate = limiter._get_current_rate()

        # 不变量 1：速率始终 <= NORMAL_RATE（1000）
        assert rate <= RateLimiter.NORMAL_RATE, (
            f"速率 {rate} 超过正常速率上限 {RateLimiter.NORMAL_RATE}"
        )

        # 不变量 2：速率始终为正数（确保限流器不会返回 0 或负数速率）
        assert rate > 0, f"速率必须为正数，实际为 {rate}"

        # 不变量 3：速率只能是 NORMAL_RATE 或 DEGRADED_RATE 之一
        assert rate in (RateLimiter.NORMAL_RATE, RateLimiter.DEGRADED_RATE), (
            f"速率 {rate} 不在合法值集合 {{{RateLimiter.NORMAL_RATE}, {RateLimiter.DEGRADED_RATE}}} 中"
        )


# Feature: openclaw-binance-agent, Property 12: 限流自动降速
# **Validates: Requirements 7.2**
class TestRateLimiterAutoDegrade:
    """限流自动降速属性测试。

    验证当待发送请求队列超过 800 时，RateLimiter 自动降速至 500/min；
    当队列 <= 800 时，保持正常速率 1000/min。
    """

    @given(
        queue_size=st.integers(min_value=801, max_value=100000),
    )
    @settings(max_examples=20)
    def test_rate_limiter_auto_degrade(self, queue_size: int) -> None:
        """队列超过 800 时自动降速至 500/min"""
        from src.infra.rate_limiter import RateLimiter

        limiter = RateLimiter()
        limiter._queue_size = queue_size

        rate = limiter._get_current_rate()

        # 队列 > 800 时，速率必须降至 DEGRADED_RATE（500）
        assert rate == RateLimiter.DEGRADED_RATE, (
            f"队列大小 {queue_size} > 800 时，速率应为 {RateLimiter.DEGRADED_RATE}，实际为 {rate}"
        )

    @given(
        queue_size=st.integers(min_value=0, max_value=800),
    )
    @settings(max_examples=20)
    def test_rate_limiter_normal_when_below_threshold(self, queue_size: int) -> None:
        """队列 <= 800 时保持正常速率 1000/min"""
        from src.infra.rate_limiter import RateLimiter

        limiter = RateLimiter()
        limiter._queue_size = queue_size

        rate = limiter._get_current_rate()

        # 队列 <= 800 时，速率必须为 NORMAL_RATE（1000）
        assert rate == RateLimiter.NORMAL_RATE, (
            f"队列大小 {queue_size} <= 800 时，速率应为 {RateLimiter.NORMAL_RATE}，实际为 {rate}"
        )


# Feature: openclaw-binance-agent, Property 7: 风控断言不变量
# **Validates: Requirements 3.4, 4.2, 8.1, 8.2, 8.8**
class TestRiskControllerInvariant:
    """风控断言不变量属性测试。

    对于任意订单请求和账户状态，若订单通过 validate_order() 校验，则：
    (a) 单笔保证金 ≤ 总资金 × 20%
    (b) 单币累计持仓 ≤ 总资金 × 30%
    (c) 该币种该方向不在止损冷却期内
    """

    @given(
        total_balance=st.floats(min_value=1000.0, max_value=1e8, allow_nan=False, allow_infinity=False),
        price=st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        quantity=st.floats(min_value=0.001, max_value=100.0, allow_nan=False, allow_infinity=False),
        leverage=st.integers(min_value=1, max_value=125),
        direction=st.sampled_from([TradeDirection.LONG, TradeDirection.SHORT]),
        existing_qty=st.floats(min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False),
        has_cooldown=st.booleans(),
    )
    @settings(max_examples=20)
    def test_risk_controller_invariant(
        self,
        total_balance: float,
        price: float,
        quantity: float,
        leverage: int,
        direction: TradeDirection,
        existing_qty: float,
        has_cooldown: bool,
    ) -> None:
        """通过校验的订单必须满足所有硬编码风控约束。"""
        from datetime import datetime, timedelta

        from src.infra.risk_controller import RiskController
        from src.models.types import AccountState, OrderRequest

        rc = RiskController()

        # 构造已有持仓
        positions = []
        if existing_qty > 0:
            positions.append({
                "symbol": "BTCUSDT",
                "quantity": existing_qty,
                "entry_price": price,
            })

        account = AccountState(
            total_balance=total_balance,
            available_margin=total_balance * 0.5,
            daily_realized_pnl=0.0,
            positions=positions,
        )

        order = OrderRequest(
            symbol="BTCUSDT",
            direction=direction,
            price=price,
            quantity=quantity,
            leverage=leverage,
        )

        # 如果设置了冷却期，记录止损
        direction_str = direction.value
        if has_cooldown:
            rc.record_stop_loss("BTCUSDT", direction_str)

        result = rc.validate_order(order, account)

        # 核心不变量：若订单通过校验，则三个约束必须全部满足
        if result.passed:
            # (a) 单笔保证金 ≤ 总资金 × 20%
            single_margin = quantity * price / leverage
            margin_limit = total_balance * RiskController.MAX_SINGLE_MARGIN_RATIO
            assert single_margin <= margin_limit + 1e-9, (
                f"通过校验但单笔保证金 {single_margin:.4f} > 限额 {margin_limit:.4f}"
            )

            # (b) 单币累计持仓 ≤ 总资金 × 30%
            existing_value = existing_qty * price if existing_qty > 0 else 0.0
            new_total = existing_value + quantity * price
            coin_limit = total_balance * RiskController.MAX_SINGLE_COIN_RATIO
            assert new_total <= coin_limit + 1e-9, (
                f"通过校验但单币累计持仓 {new_total:.4f} > 限额 {coin_limit:.4f}"
            )

            # (c) 该币种该方向不在止损冷却期内
            assert not has_cooldown, (
                "通过校验但该币种该方向处于止损冷却期内"
            )


# Feature: openclaw-binance-agent, Property 8: 止损冷却期
# **Validates: Requirements 8.3**
class TestStopLossCooldown:
    """止损冷却期属性测试。

    对于任意币种和方向，在记录止损事件后的 24 小时内，
    该币种同方向的新开仓订单必须被拒绝；
    超过 24 小时后，同方向订单应不再因冷却期被拒绝。
    """

    @given(
        symbol=st.sampled_from(["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]),
        direction=st.sampled_from(["long", "short"]),
        hours_elapsed=st.floats(min_value=0.0, max_value=23.99, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_stop_loss_cooldown(
        self,
        symbol: str,
        direction: str,
        hours_elapsed: float,
    ) -> None:
        """24 小时内同方向订单被拒绝。"""
        from datetime import datetime, timedelta

        from src.infra.risk_controller import RiskController
        from src.models.types import AccountState, OrderRequest

        rc = RiskController()

        # 手动插入一条止损记录，时间为 hours_elapsed 小时前
        record_time = datetime.now() - timedelta(hours=hours_elapsed)
        rc._stop_loss_records.append((symbol, direction, record_time))

        # 构造一个保证金和持仓都在限额内的订单
        trade_dir = TradeDirection.LONG if direction == "long" else TradeDirection.SHORT
        account = AccountState(
            total_balance=1_000_000.0,
            available_margin=500_000.0,
            daily_realized_pnl=0.0,
            positions=[],
        )
        order = OrderRequest(
            symbol=symbol,
            direction=trade_dir,
            price=100.0,
            quantity=0.01,
            leverage=10,
        )

        result = rc.validate_order(order, account)

        # 24 小时内（hours_elapsed < 24），同方向订单必须被拒绝
        assert result.passed is False, (
            f"止损后 {hours_elapsed:.2f} 小时内同方向订单应被拒绝，但通过了校验"
        )
        assert "冷却期" in result.reason

    @given(
        symbol=st.sampled_from(["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]),
        direction=st.sampled_from(["long", "short"]),
        hours_elapsed=st.floats(min_value=24.01, max_value=720.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_stop_loss_cooldown_expired(
        self,
        symbol: str,
        direction: str,
        hours_elapsed: float,
    ) -> None:
        """超过 24 小时后同方向订单应放行。"""
        from datetime import datetime, timedelta

        from src.infra.risk_controller import RiskController
        from src.models.types import AccountState, OrderRequest

        rc = RiskController()

        # 手动插入一条止损记录，时间为 hours_elapsed 小时前（已过期）
        record_time = datetime.now() - timedelta(hours=hours_elapsed)
        rc._stop_loss_records.append((symbol, direction, record_time))

        trade_dir = TradeDirection.LONG if direction == "long" else TradeDirection.SHORT
        account = AccountState(
            total_balance=1_000_000.0,
            available_margin=500_000.0,
            daily_realized_pnl=0.0,
            positions=[],
        )
        order = OrderRequest(
            symbol=symbol,
            direction=trade_dir,
            price=100.0,
            quantity=0.01,
            leverage=10,
        )

        result = rc.validate_order(order, account)

        # 超过 24 小时后，订单不应因冷却期被拒绝
        assert result.passed is True, (
            f"止损后 {hours_elapsed:.2f} 小时（已过期）同方向订单应放行，"
            f"但被拒绝: {result.reason}"
        )


# Feature: openclaw-binance-agent, Property 9: 日亏损降级触发
# **Validates: Requirements 4.11, 4.12, 8.5**
class TestDailyLossDegradation:
    """日亏损降级触发属性测试。

    对于任意账户状态，当 daily_realized_pnl 为负且
    |daily_realized_pnl| / total_balance >= 0.05 时，
    check_daily_loss() 必须返回 True，
    且执行降级后系统必须处于 Paper_Trading_Mode。
    """

    @given(
        total_balance=st.floats(min_value=100.0, max_value=1e8, allow_nan=False, allow_infinity=False),
        loss_ratio=st.floats(min_value=0.0501, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_daily_loss_degradation(
        self,
        total_balance: float,
        loss_ratio: float,
    ) -> None:
        """亏损 ≥ 5% 时触发降级并进入 Paper Mode。"""
        from src.infra.risk_controller import RiskController
        from src.models.types import AccountState

        rc = RiskController()

        # 构造亏损达到阈值的账户状态（使用略高于 5% 的值避免浮点精度边界问题）
        daily_pnl = -(loss_ratio * total_balance)
        account = AccountState(
            total_balance=total_balance,
            available_margin=total_balance * 0.5,
            daily_realized_pnl=daily_pnl,
            positions=[],
        )

        # check_daily_loss 必须返回 True
        assert rc.check_daily_loss(account) is True, (
            f"亏损比例 {loss_ratio:.4f} >= 5% 但 check_daily_loss 返回 False"
        )

        # 执行降级后必须进入 Paper Mode
        assert rc.is_paper_mode() is False  # 降级前不是 Paper Mode
        rc.execute_degradation(account)
        assert rc.is_paper_mode() is True, (
            "执行降级后系统未进入 Paper_Trading_Mode"
        )

    @given(
        total_balance=st.floats(min_value=100.0, max_value=1e8, allow_nan=False, allow_infinity=False),
        loss_ratio=st.floats(min_value=0.0, max_value=0.0499, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_daily_loss_below_threshold_no_degradation(
        self,
        total_balance: float,
        loss_ratio: float,
    ) -> None:
        """亏损 < 5% 时不触发降级。"""
        from src.infra.risk_controller import RiskController
        from src.models.types import AccountState

        rc = RiskController()

        daily_pnl = -(loss_ratio * total_balance)
        account = AccountState(
            total_balance=total_balance,
            available_margin=total_balance * 0.5,
            daily_realized_pnl=daily_pnl,
            positions=[],
        )

        # check_daily_loss 必须返回 False
        assert rc.check_daily_loss(account) is False, (
            f"亏损比例 {loss_ratio:.4f} < 5% 但 check_daily_loss 返回 True"
        )


# Feature: openclaw-binance-agent, Property 13: 指数退避序列正确性
# **Validates: Requirements 7.6**
class TestExponentialBackoffSequence:
    """指数退避序列正确性属性测试。

    对于任意连续失败的 API 请求序列，第 N 次重试（N 从 0 开始）的
    等待时间应为 2^N 秒，序列为 [1, 2, 4, 8, 16]，最多重试 5 次。
    超出序列长度时使用最后一个值（16 秒）。
    """

    @given(
        attempt=st.integers(min_value=0, max_value=4),
    )
    @settings(max_examples=20)
    def test_backoff_equals_power_of_two(self, attempt: int) -> None:
        """第 N 次重试（0 <= N <= 4）的退避时间应为 2^N 秒。"""
        from src.infra.binance_fapi import calculate_backoff

        result = calculate_backoff(attempt)
        expected = 2 ** attempt

        # 不变量：退避时间 = 2^attempt
        assert result == expected, (
            f"第 {attempt} 次重试退避时间应为 {expected}s，实际为 {result}s"
        )

    @given(
        attempt=st.integers(min_value=5, max_value=1000),
    )
    @settings(max_examples=20)
    def test_backoff_clamped_beyond_sequence(self, attempt: int) -> None:
        """超出序列范围（attempt >= 5）时，退避时间固定为最大值 16 秒。"""
        from src.infra.binance_fapi import calculate_backoff

        result = calculate_backoff(attempt)

        # 不变量：超出序列长度时使用最后一个值
        assert result == 16, (
            f"第 {attempt} 次重试（超出序列）退避时间应为 16s，实际为 {result}s"
        )

    @given(
        attempt=st.integers(min_value=0, max_value=1000),
    )
    @settings(max_examples=20)
    def test_backoff_is_positive_integer(self, attempt: int) -> None:
        """任意重试次数下，退避时间始终为正整数。"""
        from src.infra.binance_fapi import calculate_backoff

        result = calculate_backoff(attempt)

        # 不变量 1：退避时间为正数
        assert result > 0, f"退避时间必须为正数，实际为 {result}"

        # 不变量 2：退避时间为整数
        assert isinstance(result, int), f"退避时间应为整数，实际类型为 {type(result)}"

        # 不变量 3：退避时间在合法序列 [1, 2, 4, 8, 16] 中
        assert result in (1, 2, 4, 8, 16), (
            f"退避时间 {result} 不在合法序列 [1, 2, 4, 8, 16] 中"
        )


# ============================================================
# 交易记录生成策略（供 Property 15 使用）
# ============================================================

def _trade_record_strategy(pnl_positive: bool | None = None):
    """
    生成随机 TradeRecord 的策略。

    参数:
        pnl_positive: None=随机盈亏, True=强制盈利, False=强制亏损
    """
    if pnl_positive is True:
        pnl_st = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
    elif pnl_positive is False:
        pnl_st = st.floats(min_value=-1e6, max_value=-0.01, allow_nan=False, allow_infinity=False)
    else:
        pnl_st = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)

    return st.builds(
        TradeRecord,
        symbol=st.sampled_from(["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]),
        direction=st.sampled_from([TradeDirection.LONG, TradeDirection.SHORT]),
        entry_price=st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        exit_price=st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        pnl_amount=pnl_st,
        hold_duration_hours=st.floats(min_value=0.1, max_value=720.0, allow_nan=False, allow_infinity=False),
        rating_score=st.integers(min_value=1, max_value=10),
        position_size_pct=st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False),
        closed_at=st.builds(
            lambda days: datetime(2025, 1, 1) + timedelta(days=days),
            days=st.floats(min_value=0, max_value=365, allow_nan=False, allow_infinity=False),
        ),
    )


# Feature: openclaw-binance-agent, Property 15: 策略统计与调优触发
# **Validates: Requirements 5.4, 5.5, 5.6**
class TestStrategyStatsAndTuning:
    """策略统计与调优触发属性测试。

    对于任意包含至少 10 笔交易记录的列表，
    胜率计算结果应等于 盈利笔数 / 总笔数 × 100；
    当胜率低于 40% 时，系统必须生成包含新评级阈值和新风险比例的调优建议，
    且新评级阈值 ≥ 默认阈值（6），新风险比例 ≤ 默认风险比例（0.02）。
    """

    # ------------------------------------------------------------------
    # 胜率计算公式正确性
    # ------------------------------------------------------------------

    @given(
        trades=st.lists(
            _trade_record_strategy(),
            min_size=10,
            max_size=50,
        ),
    )
    @settings(max_examples=20)
    def test_win_rate_formula_correct(self, trades: list[TradeRecord]) -> None:
        """胜率 = 盈利笔数 / 总笔数 × 100，使用 compute_stats 验证"""
        from src.infra.memory_store import MemoryStore
        import tempfile

        # 使用临时数据库
        tmp_dir = tempfile.mkdtemp()
        store = MemoryStore(db_path=f"{tmp_dir}/test_mem.db")

        try:
            stats = store.compute_stats(trades)

            # 手动计算期望胜率
            winning_count = sum(1 for t in trades if t.pnl_amount > 0)
            total_count = len(trades)
            expected_win_rate = winning_count / total_count * 100

            # 验证胜率公式
            assert math.isclose(stats.win_rate, expected_win_rate, rel_tol=1e-9), (
                f"胜率不匹配: got {stats.win_rate}, expected {expected_win_rate}"
            )

            # 验证盈利/亏损笔数
            assert stats.winning_trades == winning_count
            assert stats.total_trades == total_count

            # 验证平均盈亏比
            expected_avg = sum(t.pnl_amount for t in trades) / total_count
            assert math.isclose(stats.avg_pnl_ratio, expected_avg, rel_tol=1e-9), (
                f"平均盈亏比不匹配: got {stats.avg_pnl_ratio}, expected {expected_avg}"
            )
        finally:
            store.close()

    # ------------------------------------------------------------------
    # 交易记录不足 10 笔时返回 None
    # ------------------------------------------------------------------

    @given(
        trades=st.lists(
            _trade_record_strategy(),
            min_size=0,
            max_size=9,
        ),
    )
    @settings(max_examples=20)
    def test_insufficient_trades_returns_none(self, trades: list[TradeRecord]) -> None:
        """交易记录不足 10 笔时，compute_evolution_adjustment 返回 None"""
        result = compute_evolution_adjustment(trades)
        assert result is None, (
            f"交易记录仅 {len(trades)} 笔（< 10），应返回 None，实际返回 {result}"
        )

    # ------------------------------------------------------------------
    # 胜率 < 40% 时生成调优建议
    # ------------------------------------------------------------------

    @given(data=st.data())
    @settings(max_examples=20)
    def test_low_win_rate_triggers_tuning(self, data) -> None:
        """胜率 < 40% 时，必须生成调优建议：
        - 新评级阈值 >= 默认阈值（6）
        - 新风险比例 <= 默认风险比例（0.02）
        """
        # 生成 10~50 笔交易，控制胜率 < 40%
        total = data.draw(st.integers(min_value=10, max_value=50), label="total")
        # 盈利笔数严格 < 40%
        max_winning = int(total * 0.4) - 1
        if max_winning < 0:
            max_winning = 0
        winning_count = data.draw(
            st.integers(min_value=0, max_value=max_winning), label="winning_count"
        )
        losing_count = total - winning_count

        # 生成盈利交易和亏损交易
        winning_trades = data.draw(
            st.lists(_trade_record_strategy(pnl_positive=True), min_size=winning_count, max_size=winning_count),
            label="winning_trades",
        )
        losing_trades = data.draw(
            st.lists(_trade_record_strategy(pnl_positive=False), min_size=losing_count, max_size=losing_count),
            label="losing_trades",
        )

        trades = winning_trades + losing_trades

        result = compute_evolution_adjustment(trades)

        # 必须返回非 None 的 ReflectionLog
        assert result is not None, "胜率 < 40% 时应生成调优建议"

        # 验证胜率计算正确
        expected_win_rate = winning_count / total * 100
        assert math.isclose(result.win_rate, expected_win_rate, rel_tol=1e-9), (
            f"胜率不匹配: got {result.win_rate}, expected {expected_win_rate}"
        )

        # 核心不变量：新评级阈值 >= 默认阈值（6）
        assert result.suggested_rating_threshold >= 6, (
            f"调优后评级阈值 {result.suggested_rating_threshold} 应 >= 默认值 6"
        )

        # 核心不变量：新风险比例 <= 默认风险比例（0.02）
        assert result.suggested_risk_ratio <= 0.02, (
            f"调优后风险比例 {result.suggested_risk_ratio} 应 <= 默认值 0.02"
        )

        # 新评级阈值不超过上限 8
        assert result.suggested_rating_threshold <= 8, (
            f"调优后评级阈值 {result.suggested_rating_threshold} 应 <= 上限 8"
        )

        # 新风险比例不低于下限 0.005
        assert result.suggested_risk_ratio >= 0.005, (
            f"调优后风险比例 {result.suggested_risk_ratio} 应 >= 下限 0.005"
        )

    # ------------------------------------------------------------------
    # 胜率 >= 40% 时维持默认参数
    # ------------------------------------------------------------------

    @given(data=st.data())
    @settings(max_examples=20)
    def test_normal_win_rate_keeps_defaults(self, data) -> None:
        """胜率在 40%-60% 区间时，使用默认参数调用应维持默认策略参数：
        - 评级阈值 = 6
        - 风险比例 = 0.02
        胜率 > 60% 时应放松参数（阈值下降，风险上升）。
        """
        # 生成 10~50 笔交易，控制胜率 >= 40%
        total = data.draw(st.integers(min_value=10, max_value=50), label="total")
        min_winning = int(math.ceil(total * 0.4))
        winning_count = data.draw(
            st.integers(min_value=min_winning, max_value=total), label="winning_count"
        )
        losing_count = total - winning_count

        # 生成盈利交易和亏损交易
        winning_trades = data.draw(
            st.lists(_trade_record_strategy(pnl_positive=True), min_size=winning_count, max_size=winning_count),
            label="winning_trades",
        )
        losing_trades = data.draw(
            st.lists(_trade_record_strategy(pnl_positive=False), min_size=losing_count, max_size=losing_count),
            label="losing_trades",
        )

        trades = winning_trades + losing_trades

        result = compute_evolution_adjustment(trades)

        # 必须返回非 None 的 ReflectionLog
        assert result is not None, "胜率 >= 40% 时也应返回 ReflectionLog"

        win_rate = winning_count / total * 100

        if win_rate > 60:
            # 放松：阈值应 <= 默认值 6，风险应 >= 默认值 0.02
            assert result.suggested_rating_threshold <= 6, (
                f"胜率 {win_rate:.1f}% > 60% 时评级阈值应 <= 6，"
                f"实际为 {result.suggested_rating_threshold}"
            )
            assert result.suggested_risk_ratio >= 0.02, (
                f"胜率 {win_rate:.1f}% > 60% 时风险比例应 >= 0.02，"
                f"实际为 {result.suggested_risk_ratio}"
            )
            # 阈值下限 5
            assert result.suggested_rating_threshold >= 5, (
                f"放松后评级阈值 {result.suggested_rating_threshold} 应 >= 下限 5"
            )
            # 风险上限 0.03
            assert result.suggested_risk_ratio <= 0.03, (
                f"放松后风险比例 {result.suggested_risk_ratio} 应 <= 上限 0.03"
            )
        else:
            # 40%-60% 区间：维持默认参数
            assert result.suggested_rating_threshold == 6, (
                f"胜率 {win_rate:.1f}% 在正常区间时评级阈值应为默认值 6，"
                f"实际为 {result.suggested_rating_threshold}"
            )
            assert result.suggested_risk_ratio == 0.02, (
                f"胜率 {win_rate:.1f}% 在正常区间时风险比例应为默认值 0.02，"
                f"实际为 {result.suggested_risk_ratio}"
            )


# ============================================================
# Schema 文件加载辅助函数（供 Property 2 使用）
# ============================================================

import json
import os

def _load_all_schemas() -> dict[str, dict]:
    """
    加载 config/schemas/ 目录下所有 JSON Schema 文件。
    返回 {文件名: schema_dict} 的映射。
    """
    schema_dir = os.path.join(os.path.dirname(__file__), "..", "config", "schemas")
    schema_dir = os.path.normpath(schema_dir)
    schemas = {}
    for fname in sorted(os.listdir(schema_dir)):
        if fname.endswith(".json"):
            with open(os.path.join(schema_dir, fname), "r", encoding="utf-8") as f:
                schemas[fname] = json.load(f)
    return schemas


# 预加载所有 Schema（模块级别，避免每次测试重复读取文件）
_ALL_SCHEMAS = _load_all_schemas()


# Feature: openclaw-binance-agent, Property 2: Schema 校验通过——合法数据
# **Validates: Requirements 1.5, 2.3, 3.3, 9.2, 9.3**
class TestSchemaValidDataPasses:
    """Schema 校验通过——合法数据属性测试。

    对于任意符合 Skill 输入/输出 JSON Schema 定义的合法数据，
    使用对应的 Schema 进行校验应当通过（jsonschema.validate 不抛异常）。
    使用 hypothesis-jsonschema 的 from_schema() 基于 Schema 自动生成合法数据。
    """

    @given(data=st.data())
    @settings(max_examples=20)
    def test_schema_valid_data_passes(self, data) -> None:
        """对所有 10 个 Schema，生成的合法数据必须通过 jsonschema.validate 校验。"""
        from hypothesis_jsonschema import from_schema
        from jsonschema import validate, ValidationError

        # 随机选择一个 Schema 文件
        schema_name = data.draw(
            st.sampled_from(sorted(_ALL_SCHEMAS.keys())),
            label="schema_name",
        )
        schema = _ALL_SCHEMAS[schema_name]

        # 使用 from_schema 生成符合该 Schema 的合法数据
        valid_data = data.draw(from_schema(schema), label="valid_data")

        # 核心断言：合法数据必须通过校验，validate 不抛异常即为通过
        try:
            validate(instance=valid_data, schema=schema)
        except ValidationError as e:
            raise AssertionError(
                f"Schema '{schema_name}' 校验失败，但数据应为合法:\n"
                f"数据: {valid_data}\n"
                f"错误: {e.message}"
            )


# Feature: openclaw-binance-agent, Property 3: Schema 校验拒绝——非法数据
# **Validates: Requirements 9.4, 9.5**
class TestSchemaInvalidDataRejected:
    """Schema 校验拒绝——非法数据属性测试。

    对于任意不符合 Skill 输入/输出 JSON Schema 定义的数据
    （缺少必填字段、类型错误、包含额外字段），
    使用对应的 Schema 进行校验应当失败（jsonschema.validate 抛出 ValidationError）。
    """

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _schemas_with_required() -> list[tuple[str, dict]]:
        """返回所有包含 required 字段的 Schema（名称, schema）列表。"""
        return [
            (name, schema)
            for name, schema in _ALL_SCHEMAS.items()
            if schema.get("required")
        ]

    @staticmethod
    def _get_dummy_value(prop_schema: dict):
        """根据属性 Schema 生成一个符合类型的占位值。"""
        t = prop_schema.get("type", "string")
        if t == "string":
            # 处理 format 字段
            fmt = prop_schema.get("format")
            if fmt == "uuid":
                return "550e8400-e29b-41d4-a716-446655440000"
            if fmt == "date-time":
                return "2025-01-01T00:00:00Z"
            if fmt == "uri":
                return "https://example.com"
            # 处理 enum
            if "enum" in prop_schema:
                return prop_schema["enum"][0]
            return "test_value"
        if t == "integer":
            # 尊重 minimum/maximum
            lo = prop_schema.get("minimum", 0)
            return lo
        if t == "number":
            lo = prop_schema.get("minimum", 0)
            exc_lo = prop_schema.get("exclusiveMinimum")
            if exc_lo is not None:
                return exc_lo + 1.0
            return float(lo)
        if t == "boolean":
            return True
        if t == "array":
            return []
        if t == "object":
            return {}
        return "fallback"

    @staticmethod
    def _build_valid_object(schema: dict) -> dict:
        """根据 Schema 的 properties 和 required 构建一个最小合法对象。"""
        props = schema.get("properties", {})
        required = schema.get("required", [])
        obj: dict = {}
        for key in required:
            if key in props:
                prop_schema = props[key]
                obj[key] = TestSchemaInvalidDataRejected._get_dummy_value(prop_schema)
            else:
                obj[key] = "placeholder"
        return obj

    # ------------------------------------------------------------------
    # 测试 1：缺少必填字段的数据应被拒绝
    # ------------------------------------------------------------------

    @given(data=st.data())
    @settings(max_examples=20)
    def test_missing_required_field_rejected(self, data) -> None:
        """对每个 Schema，随机移除一个必填字段后，校验应失败。"""
        from jsonschema import validate, ValidationError

        # 仅选择有 required 字段的 Schema
        schemas_with_req = self._schemas_with_required()
        assert len(schemas_with_req) > 0, "没有包含 required 字段的 Schema"

        schema_name, schema = data.draw(
            st.sampled_from(schemas_with_req), label="schema"
        )
        required_fields = schema["required"]
        assert len(required_fields) > 0

        # 随机选择一个必填字段移除
        field_to_remove = data.draw(
            st.sampled_from(required_fields), label="field_to_remove"
        )

        # 构建一个包含所有必填字段的最小合法对象，然后移除选中的字段
        obj = self._build_valid_object(schema)
        del obj[field_to_remove]

        # 核心断言：缺少必填字段的数据必须被拒绝
        with_error = False
        try:
            validate(instance=obj, schema=schema)
        except ValidationError:
            with_error = True

        assert with_error, (
            f"Schema '{schema_name}' 缺少必填字段 '{field_to_remove}' "
            f"但校验通过了，数据: {obj}"
        )

    # ------------------------------------------------------------------
    # 测试 2：包含额外字段的数据应被拒绝
    # ------------------------------------------------------------------

    @given(data=st.data())
    @settings(max_examples=20)
    def test_additional_properties_rejected(self, data) -> None:
        """对每个设置了 additionalProperties: false 的 Schema，
        添加一个额外字段后，校验应失败。"""
        from jsonschema import validate, ValidationError

        # 所有 Schema 都设置了 additionalProperties: false
        schema_name = data.draw(
            st.sampled_from(sorted(_ALL_SCHEMAS.keys())), label="schema_name"
        )
        schema = _ALL_SCHEMAS[schema_name]

        # 构建最小合法对象
        obj = self._build_valid_object(schema)

        # 生成一个不在 properties 中的随机字段名
        existing_keys = set(schema.get("properties", {}).keys())
        extra_key = data.draw(
            st.text(
                alphabet=st.characters(whitelist_categories=("Ll",)),
                min_size=3,
                max_size=15,
            ).filter(lambda k: k not in existing_keys),
            label="extra_key",
        )
        obj[extra_key] = "unexpected_value"

        # 核心断言：包含额外字段的数据必须被拒绝
        with_error = False
        try:
            validate(instance=obj, schema=schema)
        except ValidationError:
            with_error = True

        assert with_error, (
            f"Schema '{schema_name}' 包含额外字段 '{extra_key}' "
            f"但校验通过了，数据: {obj}"
        )

    # ------------------------------------------------------------------
    # 测试 3：类型错误的数据应被拒绝
    # ------------------------------------------------------------------

    @given(data=st.data())
    @settings(max_examples=20)
    def test_wrong_type_rejected(self, data) -> None:
        """对每个 Schema，将某个必填字段的值替换为错误类型后，校验应失败。"""
        from jsonschema import validate, ValidationError

        # 仅选择有 required 字段的 Schema
        schemas_with_req = self._schemas_with_required()
        schema_name, schema = data.draw(
            st.sampled_from(schemas_with_req), label="schema"
        )
        props = schema.get("properties", {})
        required_fields = schema["required"]

        # 选择一个有明确类型定义的必填字段
        typed_fields = [
            f for f in required_fields
            if f in props and "type" in props[f]
        ]
        assert len(typed_fields) > 0, (
            f"Schema '{schema_name}' 没有带类型定义的必填字段"
        )

        field_to_corrupt = data.draw(
            st.sampled_from(typed_fields), label="field_to_corrupt"
        )
        original_type = props[field_to_corrupt]["type"]

        # 根据原始类型选择一个不兼容的值
        # JSON Schema 类型：string, number, integer, boolean, array, object, null
        wrong_value_map = {
            "string": st.one_of(
                st.integers(min_value=-1000, max_value=1000),
                st.booleans(),
                st.lists(st.integers(), max_size=2),
            ),
            "number": st.one_of(
                st.text(min_size=1, max_size=10),
                st.booleans(),
                st.lists(st.integers(), max_size=2),
            ),
            "integer": st.one_of(
                st.text(min_size=1, max_size=10),
                st.booleans(),
                st.lists(st.integers(), max_size=2),
            ),
            "boolean": st.one_of(
                st.text(min_size=1, max_size=10),
                st.integers(min_value=-1000, max_value=1000),
                st.lists(st.integers(), max_size=2),
            ),
            "array": st.one_of(
                st.text(min_size=1, max_size=10),
                st.integers(min_value=-1000, max_value=1000),
                st.booleans(),
            ),
            "object": st.one_of(
                st.text(min_size=1, max_size=10),
                st.integers(min_value=-1000, max_value=1000),
                st.booleans(),
            ),
        }

        # 获取错误类型的值生成策略
        wrong_strategy = wrong_value_map.get(
            original_type,
            st.text(min_size=1, max_size=10),  # 默认回退
        )
        wrong_value = data.draw(wrong_strategy, label="wrong_value")

        # 构建合法对象后替换目标字段为错误类型
        obj = self._build_valid_object(schema)
        obj[field_to_corrupt] = wrong_value

        # 核心断言：类型错误的数据必须被拒绝
        with_error = False
        try:
            validate(instance=obj, schema=schema)
        except ValidationError:
            with_error = True

        assert with_error, (
            f"Schema '{schema_name}' 字段 '{field_to_corrupt}' "
            f"(期望类型 {original_type}) 被替换为 {type(wrong_value).__name__} "
            f"值 {wrong_value!r} 但校验通过了"
        )


# Feature: openclaw-binance-agent, Property 18: 执行日志完整性
# **Validates: Requirements 6.6**
class TestExecutionLogCompleteness:
    """执行日志完整性属性测试。

    对于任意 Skill 名称和任意合法输入数据，每次 Skill 执行前后
    各记录一条日志，包含必要字段：
    - 开始日志：Skill 名称、input_state_id
    - 成功日志：Skill 名称、output_state_id、耗时、状态=success
    - 失败日志：Skill 名称、耗时、状态=failed、失败原因
    """

    @given(
        skill_name=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
            min_size=1,
            max_size=30,
        ),
        input_value=st.integers(min_value=0, max_value=10000),
    )
    @settings(max_examples=20)
    def test_execution_log_completeness_success(
        self, skill_name: str, input_value: int
    ) -> None:
        """成功执行时，开始日志和完成日志均包含必要字段。"""
        import logging
        import logging.handlers
        import tempfile

        from src.infra.state_store import StateStore
        from src.skills.base import BaseSkill

        # 构造一个简单的子类 Skill，run() 返回合法输出
        input_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"value": {"type": "integer", "minimum": 0}},
            "required": ["value"],
            "additionalProperties": True,
        }
        output_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": True,
        }

        class _TestSkill(BaseSkill):
            def __init__(self, store):
                super().__init__(store, input_schema, output_schema)
                self.name = skill_name

            def run(self, input_data: dict) -> dict:
                return {"result": str(input_data.get("value", 0))}

        # 使用临时数据库
        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_log.db")

        try:
            # 存入输入数据
            input_state_id = store.save("upstream", {"value": input_value})

            skill = _TestSkill(store)

            # 捕获日志
            logger = logging.getLogger("src.skills.base")
            handler = logging.handlers.MemoryHandler(capacity=100)
            handler.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            old_level = logger.level
            logger.setLevel(logging.DEBUG)

            try:
                output_state_id = skill.execute(input_state_id=input_state_id)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(old_level)

            # 收集日志消息
            messages = [r.message for r in handler.buffer]

            # 验证开始日志
            start_msgs = [m for m in messages if "开始执行" in m]
            assert len(start_msgs) >= 1, (
                f"缺少开始执行日志，所有日志: {messages}"
            )
            start_msg = start_msgs[0]
            # 开始日志包含 Skill 名称
            assert f"[{skill_name}]" in start_msg, (
                f"开始日志缺少 Skill 名称 [{skill_name}]: {start_msg}"
            )
            # 开始日志包含 input_state_id
            assert f"input_state_id={input_state_id}" in start_msg, (
                f"开始日志缺少 input_state_id: {start_msg}"
            )

            # 验证完成日志
            end_msgs = [m for m in messages if "执行完成" in m]
            assert len(end_msgs) >= 1, (
                f"缺少执行完成日志，所有日志: {messages}"
            )
            end_msg = end_msgs[0]
            # 完成日志包含 Skill 名称
            assert f"[{skill_name}]" in end_msg, (
                f"完成日志缺少 Skill 名称 [{skill_name}]: {end_msg}"
            )
            # 完成日志包含 output_state_id
            assert f"output_state_id={output_state_id}" in end_msg, (
                f"完成日志缺少 output_state_id: {end_msg}"
            )
            # 完成日志包含耗时
            assert "耗时=" in end_msg, (
                f"完成日志缺少耗时字段: {end_msg}"
            )
            # 完成日志包含成功状态
            assert "状态=success" in end_msg, (
                f"完成日志缺少 '状态=success': {end_msg}"
            )
        finally:
            store.close()

    @given(
        skill_name=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
            min_size=1,
            max_size=30,
        ),
        error_message=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=20)
    def test_execution_log_completeness_failure(
        self, skill_name: str, error_message: str
    ) -> None:
        """执行失败时，开始日志和失败日志均包含必要字段。"""
        import logging
        import logging.handlers
        import tempfile

        from src.infra.state_store import StateStore
        from src.skills.base import BaseSkill

        # 允许空对象的输入 Schema
        input_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "additionalProperties": True,
        }
        output_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "additionalProperties": True,
        }

        class _FailSkill(BaseSkill):
            def __init__(self, store):
                super().__init__(store, input_schema, output_schema)
                self.name = skill_name

            def run(self, input_data: dict) -> dict:
                raise RuntimeError(error_message)

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_log_fail.db")

        try:
            skill = _FailSkill(store)

            # 捕获日志
            logger = logging.getLogger("src.skills.base")
            handler = logging.handlers.MemoryHandler(capacity=100)
            handler.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            old_level = logger.level
            logger.setLevel(logging.DEBUG)

            try:
                skill.execute(input_state_id=None)
            except RuntimeError:
                pass  # 预期异常
            finally:
                logger.removeHandler(handler)
                logger.setLevel(old_level)

            messages = [r.message for r in handler.buffer]

            # 验证开始日志
            start_msgs = [m for m in messages if "开始执行" in m]
            assert len(start_msgs) >= 1, (
                f"失败场景缺少开始执行日志，所有日志: {messages}"
            )
            start_msg = start_msgs[0]
            assert f"[{skill_name}]" in start_msg, (
                f"开始日志缺少 Skill 名称 [{skill_name}]: {start_msg}"
            )

            # 验证失败日志
            fail_msgs = [m for m in messages if "执行失败" in m]
            assert len(fail_msgs) >= 1, (
                f"失败场景缺少执行失败日志，所有日志: {messages}"
            )
            fail_msg = fail_msgs[0]
            # 失败日志包含 Skill 名称
            assert f"[{skill_name}]" in fail_msg, (
                f"失败日志缺少 Skill 名称 [{skill_name}]: {fail_msg}"
            )
            # 失败日志包含耗时
            assert "耗时=" in fail_msg, (
                f"失败日志缺少耗时字段: {fail_msg}"
            )
            # 失败日志包含失败状态
            assert "状态=failed" in fail_msg, (
                f"失败日志缺少 '状态=failed': {fail_msg}"
            )
            # 失败日志包含失败原因
            assert "原因=" in fail_msg, (
                f"失败日志缺少 '原因=' 字段: {fail_msg}"
            )
        finally:
            store.close()


# Feature: openclaw-binance-agent, Property 4: 数据来源标注完整性
# **Validates: Requirements 1.3**
class TestDataSourceAnnotation:
    """数据来源标注完整性属性测试。

    对于任意经过 Skill-1 处理输出的候选币种记录，
    该记录必须包含非空的 collected_at（合法 ISO 8601 时间戳）字段，
    以及完整的量化指标（signal_score, rsi, ema_bullish, macd_bullish 等）。

    通过 hypothesis 生成随机的 symbol，使用 mock 的 BinancePublicClient 注入，
    验证输出的每条候选记录都包含合法的 collected_at 和量化指标。
    """

    @given(
        symbol=st.from_regex(r"[A-Z]{2,10}USDT", fullmatch=True),
        quote_volume=st.floats(min_value=50_000_000, max_value=1e12, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_data_source_annotation(self, symbol: str, quote_volume: float) -> None:
        """每条候选记录必须包含合法 ISO 8601 格式的 collected_at 和量化指标。"""
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.skills.skill1_collect import Skill1Collect

        # 构造 mock client
        client = MagicMock()
        client.get_exchange_info.return_value = {
            "symbols": [{"symbol": symbol, "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"}]
        }
        client.get_tickers_24hr.return_value = [{
            "symbol": symbol,
            "quoteVolume": str(quote_volume),
            "highPrice": "110",
            "lowPrice": "100",
            "priceChangePercent": "5.0",
        }]
        # 构造温和上涨 K 线（确保技术指标有信号）
        closes = [100 + i * 0.3 for i in range(100)]
        volumes = [1000.0] * 95 + [3000.0] * 5
        klines = []
        for c, v in zip(closes, volumes):
            klines.append([0, str(c), str(c * 1.01), str(c * 0.99), str(c), str(v), 0, "0", 0, "0", "0", "0"])
        client.get_klines.return_value = klines

        input_schema = {"type": "object", "additionalProperties": True}
        output_schema = {"type": "object", "additionalProperties": True}

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_annotation.db")

        try:
            skill = Skill1Collect(
                state_store=store,
                input_schema=input_schema,
                output_schema=output_schema,
                client=client,
            )

            result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
            candidates = result["candidates"]

            for i, candidate in enumerate(candidates):
                # ── 验证 collected_at 存在且为合法 ISO 8601 时间戳 ──
                assert "collected_at" in candidate, f"候选记录 {i} 缺少 collected_at 字段"
                collected_at = candidate["collected_at"]
                assert isinstance(collected_at, str) and len(collected_at) > 0
                parsed_dt = datetime.fromisoformat(collected_at)
                assert parsed_dt.tzinfo is not None, f"候选记录 {i} 的 collected_at 缺少时区信息"

                # ── 验证量化指标字段存在 ──
                assert "signal_score" in candidate, f"候选记录 {i} 缺少 signal_score"
                assert isinstance(candidate["signal_score"], int)
                assert candidate["signal_score"] >= 0
        finally:
            store.close()

# Feature: openclaw-binance-agent, Property 5: 评级过滤阈值不变量
# **Validates: Requirements 2.4**
class TestRatingFilterThreshold:
    """评级过滤阈值不变量属性测试。

    对于任意经过 Skill-2 过滤后输出的评级结果列表，
    列表中每个币种的 rating_score 必须大于等于当前评级过滤阈值（默认 6 分），
    且被过滤掉的币种数量等于原始列表中低于阈值的币种数量。
    """

    @staticmethod
    def _candidate_strategy():
        """生成随机候选币种的策略。"""
        return st.fixed_dictionaries({
            "symbol": st.from_regex(r"[A-Z]{2,10}USDT", fullmatch=True),
            "heat_score": st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            "source_url": st.just("https://example.com/data"),
            "collected_at": st.just("2025-01-01T00:00:00Z"),
        })

    @staticmethod
    def _rating_result_strategy():
        """生成随机评级结果的策略（rating_score 在 1~10 之间）。"""
        return st.fixed_dictionaries({
            "rating_score": st.integers(min_value=1, max_value=10),
            "signal": st.sampled_from(["long", "short", "hold"]),
            "confidence": st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        })

    @given(
        candidates_with_ratings=st.lists(
            st.tuples(
                st.from_regex(r"[A-Z]{2,10}USDT", fullmatch=True),
                st.integers(min_value=1, max_value=10),
                st.sampled_from(["long", "short", "hold"]),
                st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
            ),
            min_size=1,
            max_size=20,
        ),
        threshold=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=20)
    def test_rating_filter_threshold(
        self,
        candidates_with_ratings: list[tuple[str, int, str, float]],
        threshold: int,
    ) -> None:
        """验证输出列表中所有评级分 >= 阈值，且 filtered_count 等于被过滤的数量。

        通过 mock 的 analyzer 注入预设的评级分数，
        验证 Skill-2 的过滤逻辑满足不变量。
        """
        import tempfile
        import uuid

        from src.infra.state_store import StateStore
        from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule

        # 构造候选币种列表和对应的分析结果映射
        candidates = []
        analyzer_results: dict[str, dict] = {}
        for symbol, rating_score, signal, confidence in candidates_with_ratings:
            # 确保每个 symbol 唯一（追加索引后缀）
            unique_symbol = f"{symbol}{len(candidates)}"
            candidates.append({
                "symbol": unique_symbol,
                "heat_score": 50.0,
                "source_url": "https://example.com/data",
                "collected_at": "2025-01-01T00:00:00Z",
            })
            analyzer_results[unique_symbol] = {
                "rating_score": rating_score,
                "signal": signal,
                "confidence": confidence,
            }

        # 构造 mock analyzer：根据 symbol 返回预设的评级结果
        def mock_analyzer(symbol: str, market_data: dict) -> dict:
            return analyzer_results[symbol]

        # 使用宽松的 Schema（避免 Schema 校验干扰属性测试）
        input_schema = {"type": "object", "additionalProperties": True}
        output_schema = {"type": "object", "additionalProperties": True}

        # 创建临时 StateStore 并存入上游数据
        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_filter.db")

        try:
            # 存入 Skill-1 输出数据
            upstream_data = {
                "state_id": str(uuid.uuid4()),
                "candidates": candidates,
                "pipeline_run_id": str(uuid.uuid4()),
            }
            input_state_id = store.save("skill1_collect", upstream_data)

            # 创建 Skill-2 实例，使用随机阈值
            trading_agents = TradingAgentsModule(analyzer=mock_analyzer)
            skill = Skill2Analyze(
                state_store=store,
                input_schema=input_schema,
                output_schema=output_schema,
                trading_agents=trading_agents,
                rating_threshold=threshold,
            )

            # 执行 Skill-2 的 run 方法
            result = skill.run({"input_state_id": input_state_id})

            ratings = result["ratings"]
            filtered_count = result["filtered_count"]

            # ── 不变量 1：输出列表中所有评级分 >= 阈值 ──
            for rating in ratings:
                assert rating["rating_score"] >= threshold, (
                    f"输出中 {rating['symbol']} 的评级分 {rating['rating_score']} "
                    f"低于阈值 {threshold}"
                )

            # ── 不变量 2：filtered_count 等于被过滤掉的币种数量 ──
            # 手动计算期望的过滤数量
            total_analyzed = len(candidates_with_ratings)
            expected_passed = sum(
                1 for _, score, _, _ in candidates_with_ratings
                if score >= threshold
            )
            expected_filtered = total_analyzed - expected_passed

            assert filtered_count == expected_filtered, (
                f"filtered_count={filtered_count} 不等于期望的过滤数量 "
                f"{expected_filtered}（总计={total_analyzed}, 通过={expected_passed}）"
            )

            # ── 不变量 3：通过的数量与输出列表长度一致 ──
            assert len(ratings) == expected_passed, (
                f"输出列表长度 {len(ratings)} 不等于期望通过数量 {expected_passed}"
            )
        finally:
            store.close()

    @given(
        num_candidates=st.integers(min_value=1, max_value=15),
    )
    @settings(max_examples=20)
    def test_all_below_threshold_filtered(self, num_candidates: int) -> None:
        """当所有候选币种评级分都低于阈值时，输出列表为空，filtered_count 等于总数。"""
        import tempfile
        import uuid

        from src.infra.state_store import StateStore
        from src.skills.skill2_analyze import (
            DEFAULT_RATING_THRESHOLD,
            Skill2Analyze,
            TradingAgentsModule,
        )

        # 所有评级分设为 1（远低于默认阈值 6）
        candidates = []
        for i in range(num_candidates):
            candidates.append({
                "symbol": f"LOW{i}USDT",
                "heat_score": 50.0,
                "source_url": "https://example.com/data",
                "collected_at": "2025-01-01T00:00:00Z",
            })

        def mock_analyzer(symbol: str, market_data: dict) -> dict:
            return {"rating_score": 1, "signal": "hold", "confidence": 10.0}

        input_schema = {"type": "object", "additionalProperties": True}
        output_schema = {"type": "object", "additionalProperties": True}

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_all_low.db")

        try:
            upstream_data = {
                "state_id": str(uuid.uuid4()),
                "candidates": candidates,
                "pipeline_run_id": str(uuid.uuid4()),
            }
            input_state_id = store.save("skill1_collect", upstream_data)

            trading_agents = TradingAgentsModule(analyzer=mock_analyzer)
            skill = Skill2Analyze(
                state_store=store,
                input_schema=input_schema,
                output_schema=output_schema,
                trading_agents=trading_agents,
                rating_threshold=DEFAULT_RATING_THRESHOLD,
            )

            result = skill.run({"input_state_id": input_state_id})

            # 输出列表应为空
            assert len(result["ratings"]) == 0, (
                f"所有评级分低于阈值时输出应为空，实际有 {len(result['ratings'])} 条"
            )
            # filtered_count 应等于总数
            assert result["filtered_count"] == num_candidates, (
                f"filtered_count={result['filtered_count']} 应等于 {num_candidates}"
            )
        finally:
            store.close()


    @given(
        num_results=st.integers(min_value=1, max_value=5),
        data=st.data(),
    )
    @settings(max_examples=20)
    def test_data_source_annotation_multiple_candidates(self, num_results: int, data) -> None:
        """多个币种通过筛选时，每条候选记录都必须包含合法的 collected_at 和量化指标。"""
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.skills.skill1_collect import Skill1Collect

        # 为每个币种生成随机 symbol
        symbols = []
        tickers = []
        for _ in range(num_results):
            sym = data.draw(
                st.from_regex(r"[A-Z]{2,10}USDT", fullmatch=True), label="symbol"
            )
            symbols.append(sym)
            tickers.append({
                "symbol": sym,
                "quoteVolume": "100000000",
                "highPrice": "110",
                "lowPrice": "100",
                "priceChangePercent": "5.0",
            })

        client = MagicMock()
        client.get_exchange_info.return_value = {
            "symbols": [
                {"symbol": s, "status": "TRADING", "quoteAsset": "USDT", "contractType": "PERPETUAL"}
                for s in symbols
            ]
        }
        client.get_tickers_24hr.return_value = tickers
        # 温和上涨 K 线
        closes = [100 + i * 0.3 for i in range(100)]
        volumes = [1000.0] * 95 + [3000.0] * 5
        klines = []
        for c, v in zip(closes, volumes):
            klines.append([0, str(c), str(c * 1.01), str(c * 0.99), str(c), str(v), 0, "0", 0, "0", "0", "0"])
        client.get_klines.return_value = klines

        input_schema = {"type": "object", "additionalProperties": True}
        output_schema = {"type": "object", "additionalProperties": True}

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_multi_annotation.db")

        try:
            skill = Skill1Collect(
                state_store=store,
                input_schema=input_schema,
                output_schema=output_schema,
                client=client,
            )

            result = skill.run({"trigger_time": "2025-01-01T00:00:00Z"})
            candidates = result["candidates"]

            for i, candidate in enumerate(candidates):
                # 验证 collected_at 合法 ISO 8601
                collected_at = candidate["collected_at"]
                parsed_dt = datetime.fromisoformat(collected_at)
                assert parsed_dt.tzinfo is not None, (
                    f"候选记录 {i} 的 collected_at 缺少时区信息: {collected_at}"
                )
                # 验证量化指标
                assert "signal_score" in candidate
                assert isinstance(candidate["signal_score"], int)
        finally:
            store.close()


# Feature: openclaw-binance-agent, Property 6: 头寸规模计算正确性
# **Validates: Requirements 3.2, 3.5**
class TestPositionSizeCalculation:
    """头寸规模计算正确性属性测试。

    对于任意正数的账户余额、(0, 0.20] 范围内的风险比例、
    正数的入场价格和正数的止损价格（入场价格 ≠ 止损价格），
    头寸规模计算结果应满足：
    position_size = (risk_ratio * balance) / |entry_price - stop_loss_price|，
    且最终头寸价值不超过账户余额的 20%。
    """

    @given(
        balance=st.floats(min_value=100, max_value=1e8, allow_nan=False, allow_infinity=False),
        risk_ratio=st.floats(min_value=0.001, max_value=0.20, allow_nan=False, allow_infinity=False),
        entry_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_position_size_calculation(
        self,
        balance: float,
        risk_ratio: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> None:
        """头寸规模计算公式正确且头寸价值不超过 20%。

        公式：position_size = (risk_ratio × balance) / |entry_price - stop_loss_price|
        风控上限：position_value = position_size × entry_price ≤ balance × 0.20
        """
        assume(entry_price != stop_loss_price)

        size = calculate_position_size(balance, risk_ratio, entry_price, stop_loss_price)

        # 计算未裁剪的理论头寸规模
        price_distance = abs(entry_price - stop_loss_price)
        expected_raw_size = (risk_ratio * balance) / price_distance

        # 计算头寸价值百分比
        position_value = size * entry_price
        position_pct = (position_value / balance) * 100

        # 不变量 1：头寸价值不超过账户余额的 20%（浮点容差）
        assert position_pct <= 20.0 + 1e-9, (
            f"头寸价值百分比 {position_pct:.6f}% 超过 20% 上限"
        )

        # 不变量 2：头寸规模为正数
        assert size > 0, f"头寸规模必须为正数，实际为 {size}"

        # 不变量 3：若未触发裁剪，头寸规模应等于理论值
        expected_raw_pct = (expected_raw_size * entry_price / balance) * 100
        if expected_raw_pct <= 20.0:
            # 未裁剪场景：实际值应等于理论值
            assert math.isclose(size, expected_raw_size, rel_tol=1e-9), (
                f"未裁剪时头寸规模不匹配: got {size}, expected {expected_raw_size}"
            )
        else:
            # 裁剪场景：头寸规模应被裁剪至 20% 对应的值
            expected_clipped = (balance * 0.20) / entry_price
            assert math.isclose(size, expected_clipped, rel_tol=1e-9), (
                f"裁剪后头寸规模不匹配: got {size}, expected {expected_clipped}"
            )


# Feature: openclaw-binance-agent, Property 10: 平仓条件触发
# **Validates: Requirements 4.5, 4.6, 4.7**
class TestClosePositionTrigger:
    """平仓条件触发属性测试。

    验证 Skill4Execute 的止损/止盈/超时三种平仓条件正确触发：
    - 做多：当前价 <= 止损价 → 止损；当前价 >= 止盈价 → 止盈
    - 做空：当前价 >= 止损价 → 止损；当前价 <= 止盈价 → 止盈
    - 持仓时间超过上限 → 超时平仓
    """

    # ------------------------------------------------------------------
    # 止损条件：_should_stop_loss
    # ------------------------------------------------------------------

    @given(
        current_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_long_stop_loss_trigger(self, current_price: float, stop_loss_price: float) -> None:
        """做多方向：当前价 <= 止损价时触发止损，否则不触发。"""
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.skills.skill4_execute import Skill4Execute

        # 构造最小化的 Skill4Execute 实例（仅测试 _should_stop_loss 方法）
        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_sl.db")
        try:
            skill = Skill4Execute(
                state_store=store,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                binance_client=MagicMock(),
                risk_controller=MagicMock(),
                account_state_provider=lambda: None,
                poll_interval=0,
            )

            result = skill._should_stop_loss(TradeDirection.LONG, current_price, stop_loss_price)

            # 不变量：做多时，当前价 <= 止损价 → True
            if current_price <= stop_loss_price:
                assert result is True, (
                    f"做多: 当前价 {current_price} <= 止损价 {stop_loss_price}，应触发止损"
                )
            else:
                assert result is False, (
                    f"做多: 当前价 {current_price} > 止损价 {stop_loss_price}，不应触发止损"
                )
        finally:
            store.close()

    @given(
        current_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        stop_loss_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_short_stop_loss_trigger(self, current_price: float, stop_loss_price: float) -> None:
        """做空方向：当前价 >= 止损价时触发止损，否则不触发。"""
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.skills.skill4_execute import Skill4Execute

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_sl_short.db")
        try:
            skill = Skill4Execute(
                state_store=store,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                binance_client=MagicMock(),
                risk_controller=MagicMock(),
                account_state_provider=lambda: None,
                poll_interval=0,
            )

            result = skill._should_stop_loss(TradeDirection.SHORT, current_price, stop_loss_price)

            # 不变量：做空时，当前价 >= 止损价 → True
            if current_price >= stop_loss_price:
                assert result is True, (
                    f"做空: 当前价 {current_price} >= 止损价 {stop_loss_price}，应触发止损"
                )
            else:
                assert result is False, (
                    f"做空: 当前价 {current_price} < 止损价 {stop_loss_price}，不应触发止损"
                )
        finally:
            store.close()

    # ------------------------------------------------------------------
    # 止盈条件：_should_take_profit
    # ------------------------------------------------------------------

    @given(
        current_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        take_profit_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_long_take_profit_trigger(self, current_price: float, take_profit_price: float) -> None:
        """做多方向：当前价 >= 止盈价时触发止盈，否则不触发。"""
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.skills.skill4_execute import Skill4Execute

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_tp_long.db")
        try:
            skill = Skill4Execute(
                state_store=store,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                binance_client=MagicMock(),
                risk_controller=MagicMock(),
                account_state_provider=lambda: None,
                poll_interval=0,
            )

            result = skill._should_take_profit(TradeDirection.LONG, current_price, take_profit_price)

            # 不变量：做多时，当前价 >= 止盈价 → True
            if current_price >= take_profit_price:
                assert result is True, (
                    f"做多: 当前价 {current_price} >= 止盈价 {take_profit_price}，应触发止盈"
                )
            else:
                assert result is False, (
                    f"做多: 当前价 {current_price} < 止盈价 {take_profit_price}，不应触发止盈"
                )
        finally:
            store.close()

    @given(
        current_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
        take_profit_price=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_short_take_profit_trigger(self, current_price: float, take_profit_price: float) -> None:
        """做空方向：当前价 <= 止盈价时触发止盈，否则不触发。"""
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.skills.skill4_execute import Skill4Execute

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_tp_short.db")
        try:
            skill = Skill4Execute(
                state_store=store,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                binance_client=MagicMock(),
                risk_controller=MagicMock(),
                account_state_provider=lambda: None,
                poll_interval=0,
            )

            result = skill._should_take_profit(TradeDirection.SHORT, current_price, take_profit_price)

            # 不变量：做空时，当前价 <= 止盈价 → True
            if current_price <= take_profit_price:
                assert result is True, (
                    f"做空: 当前价 {current_price} <= 止盈价 {take_profit_price}，应触发止盈"
                )
            else:
                assert result is False, (
                    f"做空: 当前价 {current_price} > 止盈价 {take_profit_price}，不应触发止盈"
                )
        finally:
            store.close()

    # ------------------------------------------------------------------
    # 超时平仓：通过 _monitor_position 集成验证
    # ------------------------------------------------------------------

    @given(
        direction=st.sampled_from([TradeDirection.LONG, TradeDirection.SHORT]),
        current_price=st.floats(min_value=50.0, max_value=150.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=20)
    def test_timeout_triggers_close(self, direction: TradeDirection, current_price: float) -> None:
        """持仓超时时应触发平仓，返回 close_reason='timeout'。

        设置 max_hold_hours 极小值（接近 0），使得首次轮询即超时。
        止损价和止盈价设置在远离当前价的位置，确保不会先触发止损/止盈。
        """
        import tempfile
        from unittest.mock import MagicMock

        from src.infra.binance_fapi import OrderResult, PositionRisk
        from src.infra.state_store import StateStore
        from src.skills.skill4_execute import Skill4Execute

        # 确保当前价不触发止损/止盈（止损价远低于当前价，止盈价远高于当前价）
        if direction == TradeDirection.LONG:
            stop_loss_price = 0.01   # 远低于任何 current_price
            take_profit_price = 1e7  # 远高于任何 current_price
        else:
            stop_loss_price = 1e7    # 远高于任何 current_price（做空止损）
            take_profit_price = 0.01 # 远低于任何 current_price（做空止盈）

        # mock binance_client
        mock_binance = MagicMock()
        mock_binance.get_position_risk.return_value = PositionRisk(
            symbol="BTCUSDT",
            position_amt=10.0,
            entry_price=100.0,
            mark_price=current_price,
            unrealized_pnl=0.0,
            liquidation_price=50.0,
            leverage=10,
        )
        mock_binance.place_market_order.return_value = OrderResult(
            order_id="close_123",
            symbol="BTCUSDT",
            side="SELL" if direction == TradeDirection.LONG else "BUY",
            price=current_price,
            quantity=10.0,
            status="FILLED",
        )

        # mock risk_controller（日亏损检查不触发）
        mock_rc = MagicMock()
        mock_rc.check_daily_loss.return_value = False

        mock_account = MagicMock()
        mock_account.total_balance = 100000.0

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_timeout.db")
        try:
            skill = Skill4Execute(
                state_store=store,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                binance_client=mock_binance,
                risk_controller=mock_rc,
                account_state_provider=lambda: mock_account,
                poll_interval=0,
            )

            # max_hold_hours 极小，首次轮询即超时
            result = skill._monitor_position(
                symbol="BTCUSDT",
                direction=direction,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                max_hold_hours=0.0,  # 0 小时 = 立即超时
                quantity=10.0,
                order_id="test_entry_123",
            )

            # 不变量：超时平仓的 reason 应为 'timeout'
            assert result["reason"] == "timeout", (
                f"持仓超时应返回 reason='timeout'，实际为 '{result['reason']}'"
            )
        finally:
            store.close()


# Feature: openclaw-binance-agent, Property 14: Paper Mode 行为一致性
# **Validates: Requirements 8.6, 8.7**
class TestPaperModeBehavior:
    """Paper Mode 行为一致性属性测试。

    验证当 RiskController.is_paper_mode() 返回 True 时，
    Skill4Execute 对所有交易计划的执行结果状态均为 'paper_trade'，
    且不调用真实的 Binance 下单接口。
    """

    @given(
        num_plans=st.integers(min_value=1, max_value=5),
        data=st.data(),
    )
    @settings(max_examples=20)
    def test_paper_mode_behavior(self, num_plans: int, data) -> None:
        """Paper Mode 下所有订单状态为 paper_trade，且不调用真实下单接口。"""
        import tempfile
        import uuid
        from unittest.mock import MagicMock

        from src.infra.state_store import StateStore
        from src.models.types import AccountState, OrderStatus, ValidationResult
        from src.skills.skill4_execute import Skill4Execute

        # 生成随机交易计划
        trade_plans = []
        for _ in range(num_plans):
            symbol_base = data.draw(
                st.from_regex(r"[A-Z]{2,6}", fullmatch=True), label="symbol_base"
            )
            symbol = f"{symbol_base}USDT"
            direction = data.draw(
                st.sampled_from(["long", "short"]), label="direction"
            )
            entry_upper = data.draw(
                st.floats(min_value=10.0, max_value=1e5, allow_nan=False, allow_infinity=False),
                label="entry_upper",
            )
            entry_lower = data.draw(
                st.floats(min_value=1.0, max_value=entry_upper, allow_nan=False, allow_infinity=False),
                label="entry_lower",
            )
            position_size_pct = data.draw(
                st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False),
                label="position_size_pct",
            )
            # 根据方向设置合理的止损/止盈价
            mid_price = (entry_upper + entry_lower) / 2
            if direction == "long":
                stop_loss = data.draw(
                    st.floats(min_value=0.01, max_value=mid_price * 0.99, allow_nan=False, allow_infinity=False),
                    label="stop_loss",
                )
                take_profit = data.draw(
                    st.floats(min_value=mid_price * 1.01, max_value=1e6, allow_nan=False, allow_infinity=False),
                    label="take_profit",
                )
            else:
                stop_loss = data.draw(
                    st.floats(min_value=mid_price * 1.01, max_value=1e6, allow_nan=False, allow_infinity=False),
                    label="stop_loss",
                )
                take_profit = data.draw(
                    st.floats(min_value=0.01, max_value=mid_price * 0.99, allow_nan=False, allow_infinity=False),
                    label="take_profit",
                )

            trade_plans.append({
                "symbol": symbol,
                "direction": direction,
                "entry_price_upper": entry_upper,
                "entry_price_lower": entry_lower,
                "position_size_pct": position_size_pct,
                "stop_loss_price": stop_loss,
                "take_profit_price": take_profit,
                "max_hold_hours": 24.0,
            })

        # 构造上游数据
        upstream_data = {
            "state_id": str(uuid.uuid4()),
            "trade_plans": trade_plans,
            "pipeline_status": "has_trades",
        }

        # mock risk_controller：Paper Mode 开启，风控校验通过
        mock_rc = MagicMock()
        mock_rc.is_paper_mode.return_value = True
        mock_rc.check_daily_loss.return_value = False
        mock_rc.validate_order.return_value = ValidationResult(passed=True)

        # mock binance_client（不应被调用）
        mock_binance = MagicMock()

        # 构造账户状态（余额足够大，确保数量计算不为零）
        account = AccountState(
            total_balance=1_000_000.0,
            available_margin=500_000.0,
            daily_realized_pnl=0.0,
            positions=[],
        )

        tmp_dir = tempfile.mkdtemp()
        store = StateStore(db_path=f"{tmp_dir}/test_paper.db")
        try:
            state_id = store.save("skill3_strategy", upstream_data)

            skill = Skill4Execute(
                state_store=store,
                input_schema={"type": "object", "additionalProperties": True},
                output_schema={"type": "object", "additionalProperties": True},
                binance_client=mock_binance,
                risk_controller=mock_rc,
                account_state_provider=lambda: account,
                poll_interval=0,
            )

            result = skill.run({"input_state_id": state_id})

            # ── 不变量 1：所有订单状态为 paper_trade ──
            execution_results = result["execution_results"]
            assert len(execution_results) == num_plans, (
                f"期望 {num_plans} 条执行结果，实际 {len(execution_results)} 条"
            )

            for i, er in enumerate(execution_results):
                assert er["status"] == OrderStatus.PAPER_TRADE.value, (
                    f"Paper Mode 下第 {i} 笔订单状态应为 '{OrderStatus.PAPER_TRADE.value}'，"
                    f"实际为 '{er['status']}'"
                )

            # ── 不变量 2：is_paper_mode 标志为 True ──
            assert result["is_paper_mode"] is True, (
                "Paper Mode 下 is_paper_mode 应为 True"
            )

            # ── 不变量 3：不调用真实下单接口 ──
            mock_binance.place_limit_order.assert_not_called()
            mock_binance.place_market_order.assert_not_called()

            # ── 不变量 4：每笔 paper_trade 订单都有 paper_ 前缀的 order_id ──
            for i, er in enumerate(execution_results):
                assert er["order_id"].startswith("paper_"), (
                    f"Paper Mode 下第 {i} 笔订单的 order_id 应以 'paper_' 开头，"
                    f"实际为 '{er['order_id']}'"
                )
        finally:
            store.close()
