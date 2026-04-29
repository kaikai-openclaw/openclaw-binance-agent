"""
核心数据模型定义

包含所有 dataclass 和枚举类型，以及头寸规模计算、盈亏比例计算、
进化评分与策略调优等核心计算函数。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
import logging

log = logging.getLogger(__name__)


# ============================================================
# 枚举类型
# ============================================================

class TradeDirection(str, Enum):
    """交易方向：做多或做空"""
    LONG = "long"
    SHORT = "short"


class Signal(str, Enum):
    """多空或观望信号"""
    LONG = "long"
    SHORT = "short"
    HOLD = "hold"


class OrderStatus(str, Enum):
    """订单状态"""
    FILLED = "filled"                        # 已成交
    OPEN = "open"                            # 已开仓/挂单，等待后续轮询管理
    REJECTED_BY_RISK = "rejected_by_risk"    # 被风控拒绝
    EXECUTION_FAILED = "execution_failed"    # 执行失败
    PAPER_TRADE = "paper_trade"              # 模拟盘交易


class PipelineStatus(str, Enum):
    """Pipeline 状态：本轮是否有交易机会"""
    HAS_TRADES = "has_trades"
    NO_OPPORTUNITY = "no_opportunity"


class AlertLevel(str, Enum):
    """告警级别"""
    INFO = "INFO"            # 一般信息（如 Skill 执行完成）
    WARNING = "WARNING"      # 警告（如重试中、头寸裁剪）
    HIGH = "HIGH"            # 高优先级（如 API 重试耗尽、订单执行失败）
    CRITICAL = "CRITICAL"    # 严重（如日亏损触发降级）
    EMERGENCY = "EMERGENCY"  # 紧急（如 IP 被封禁）


# ============================================================
# 数据类（Dataclass）
# ============================================================

@dataclass
class Candidate:
    """Skill-1 输出的候选币种"""
    symbol: str           # 币种交易对符号，如 "BTCUSDT"
    heat_score: float     # 市场热度评分（0-100）
    source_url: str       # 数据来源 URL
    collected_at: datetime  # 采集时间戳


@dataclass
class Rating:
    """Skill-2 输出的评级结果"""
    symbol: str           # 币种交易对符号
    rating_score: int     # 评级分（1-10）
    signal: Signal        # 多空或观望信号
    confidence: float     # 置信度百分比（0-100）


@dataclass
class TradePlan:
    """Skill-3 输出的交易计划"""
    symbol: str                    # 币种交易对符号
    direction: TradeDirection      # 交易方向
    entry_price_upper: float       # 入场价格区间上限
    entry_price_lower: float       # 入场价格区间下限
    position_size_pct: float       # 头寸规模百分比（不超过 20%）
    stop_loss_price: float         # 止损价格
    take_profit_price: float       # 止盈价格
    max_hold_hours: float          # 持仓时间上限（小时）


@dataclass
class ExecutionResult:
    """Skill-4 输出的执行结果"""
    order_id: str                  # Binance 订单 ID
    symbol: str                    # 币种交易对符号
    direction: TradeDirection      # 交易方向
    executed_price: float          # 成交价格
    executed_quantity: float       # 成交数量
    fee: float                     # 手续费
    status: OrderStatus            # 订单状态
    executed_at: datetime          # 成交时间戳


@dataclass
class TradeRecord:
    """Memory_Store 中的交易记录"""
    symbol: str                    # 币种交易对符号
    direction: TradeDirection      # 交易方向
    entry_price: float             # 入场价格
    exit_price: float              # 平仓价格
    pnl_amount: float              # 盈亏金额
    hold_duration_hours: float     # 持仓时长（小时）
    rating_score: int              # 评级分
    position_size_pct: float       # 头寸规模百分比
    closed_at: datetime            # 平仓时间戳


@dataclass
class StrategyStats:
    """策略统计数据"""
    win_rate: float          # 胜率百分比
    avg_pnl_ratio: float     # 平均盈亏比
    total_trades: int        # 总交易笔数
    winning_trades: int      # 盈利笔数
    losing_trades: int       # 亏损笔数


@dataclass
class ReflectionLog:
    """反思日志"""
    created_at: datetime                  # 创建时间
    win_rate: float                       # 胜率百分比
    avg_pnl_ratio: float                  # 平均盈亏比
    suggested_rating_threshold: int       # 建议的评级过滤阈值
    suggested_risk_ratio: float           # 建议的风险比例
    reasoning: str                        # 调优推理过程


@dataclass
class AccountState:
    """账户状态"""
    total_balance: float           # 账户总资金
    available_margin: float        # 可用保证金
    daily_realized_pnl: float      # 当日已实现盈亏
    positions: list = field(default_factory=list)  # 持仓列表
    is_paper_mode: bool = False    # 是否处于模拟盘模式


@dataclass
class OrderRequest:
    """订单请求"""
    symbol: str                    # 币种交易对符号
    direction: TradeDirection      # 交易方向
    price: float                   # 价格
    quantity: float                # 数量
    leverage: int                  # 杠杆倍数
    order_type: str = "limit"      # 订单类型："limit" | "market"


@dataclass
class ValidationResult:
    """风控校验结果"""
    passed: bool                   # 是否通过校验
    reason: str = ""               # 拒绝原因（通过时为空）


# ============================================================
# 核心计算函数
# ============================================================

def calculate_position_size(
    account_balance: float,
    risk_ratio: float,
    entry_price: float,
    stop_loss_price: float,
) -> float:
    """
    固定风险模型头寸规模计算。

    公式：头寸规模 = (账户风险比例 × 账户总资金) / |入场价格 - 止损价格|

    参数:
        account_balance: 账户总资金（必须为正数）
        risk_ratio: 账户风险比例，范围 (0, 0.20]
        entry_price: 入场价格（必须为正数）
        stop_loss_price: 止损价格（必须为正数，且不等于入场价格）

    返回:
        头寸数量（非百分比）

    异常:
        ValueError: 参数不满足边界约束时抛出
    """
    # 边界校验
    if account_balance <= 0:
        raise ValueError("账户余额必须为正数")
    if risk_ratio <= 0 or risk_ratio > 0.20:
        raise ValueError("风险比例必须在 (0, 0.20] 范围内")
    if entry_price <= 0:
        raise ValueError("入场价格必须为正数")
    if stop_loss_price <= 0:
        raise ValueError("止损价格必须为正数")
    if entry_price == stop_loss_price:
        raise ValueError("入场价格不能等于止损价格")

    # 计算头寸规模
    risk_amount = risk_ratio * account_balance
    price_distance = abs(entry_price - stop_loss_price)
    position_size = risk_amount / price_distance

    # 转换为头寸规模百分比，检查风控上限
    position_value = position_size * entry_price
    position_pct = (position_value / account_balance) * 100

    # 风控上限裁剪：单笔不超过 20%
    if position_pct > 20.0:
        position_pct = 20.0
        position_size = (account_balance * 0.20) / entry_price
        log.info(f"头寸规模超限，已裁剪至 20%: {position_size}")

    return position_size


def calculate_pnl_ratio(
    entry_price: float,
    current_price: float,
    direction: TradeDirection,
) -> float:
    """
    计算持仓盈亏比例（百分比）。

    做多：(当前价格 - 入场价格) / 入场价格 × 100
    做空：(入场价格 - 当前价格) / 入场价格 × 100

    参数:
        entry_price: 入场价格（必须为正数）
        current_price: 当前价格（必须为正数）
        direction: 交易方向（做多或做空）

    返回:
        盈亏比例百分比

    异常:
        ValueError: 价格不为正数时抛出
    """
    if entry_price <= 0:
        raise ValueError("入场价格必须为正数")
    if current_price <= 0:
        raise ValueError("当前价格必须为正数")

    if direction == TradeDirection.LONG:
        return ((current_price - entry_price) / entry_price) * 100
    else:
        return ((entry_price - current_price) / entry_price) * 100


def compute_evolution_adjustment(
    trades: List[TradeRecord],
    current_rating_threshold: int = 6,
    current_risk_ratio: float = 0.02,
) -> Optional[ReflectionLog]:
    """
    基于最近 50 笔交易计算策略调优建议（渐进式双向调整）。

    规则：
    - 交易记录不足 10 笔时跳过，返回 None
    - 胜率 < 40%：收紧（阈值 +1，风险 ×0.8）
    - 胜率 > 60%：放松（阈值 -1，风险 ×1.1）
    - 40%-60%：维持当前参数
    - 附带按评级分段的胜率分析，辅助决策

    参数:
        trades: 交易记录列表（按平仓时间倒序排列）
        current_rating_threshold: 当前评级过滤阈值（从上一轮反思日志读取）
        current_risk_ratio: 当前风险比例（从上一轮反思日志读取）

    返回:
        ReflectionLog 调优建议，或 None（记录不足 10 笔时）
    """
    if len(trades) < 10:
        return None

    # 取最近 50 笔
    recent = trades[:50]
    winning = [t for t in recent if t.pnl_amount > 0]
    win_rate = len(winning) / len(recent) * 100

    total_pnl = sum(t.pnl_amount for t in recent)
    avg_pnl_ratio = total_pnl / len(recent)

    # 按评级分段分析胜率
    rating_analysis = _analyze_by_rating(recent)

    now = datetime.now(timezone.utc)

    if win_rate < 40:
        # 收紧：阈值 +1（上限 8），风险 ×0.8（下限 0.005）
        new_threshold = min(8, current_rating_threshold + 1)
        new_risk_ratio = max(0.005, round(current_risk_ratio * 0.8, 4))
        reasoning = (
            f"胜率 {win_rate:.1f}% 低于 40% 阈值，"
            f"收紧评级过滤阈值 {current_rating_threshold}→{new_threshold}，"
            f"降低风险比例 {current_risk_ratio:.4f}→{new_risk_ratio:.4f}"
        )
    elif win_rate > 60:
        # 放松：阈值 -1（下限 5），风险 ×1.1（上限 0.03）
        new_threshold = max(5, current_rating_threshold - 1)
        new_risk_ratio = min(0.03, round(current_risk_ratio * 1.1, 4))
        reasoning = (
            f"胜率 {win_rate:.1f}% 高于 60% 阈值，"
            f"放松评级过滤阈值 {current_rating_threshold}→{new_threshold}，"
            f"提高风险比例 {current_risk_ratio:.4f}→{new_risk_ratio:.4f}"
        )
    else:
        # 40%-60%：维持当前参数
        new_threshold = current_rating_threshold
        new_risk_ratio = current_risk_ratio
        reasoning = (
            f"胜率 {win_rate:.1f}% 处于正常区间(40%-60%)，"
            f"维持当前策略参数"
        )

    if rating_analysis:
        reasoning += f"；评级分段分析: {rating_analysis}"

    return ReflectionLog(
        created_at=now,
        win_rate=win_rate,
        avg_pnl_ratio=avg_pnl_ratio,
        suggested_rating_threshold=new_threshold,
        suggested_risk_ratio=new_risk_ratio,
        reasoning=reasoning,
    )


def _analyze_by_rating(trades: List[TradeRecord]) -> str:
    """
    按评级分段分析胜率，辅助进化决策。

    参数:
        trades: 交易记录列表

    返回:
        格式化的分段分析字符串，如 "6分:3胜/5笔(60.0%), 7分:2胜/3笔(66.7%)"
    """
    buckets: dict[int, list[TradeRecord]] = {}
    for t in trades:
        buckets.setdefault(t.rating_score, []).append(t)

    parts = []
    for score in sorted(buckets.keys()):
        group = buckets[score]
        wins = sum(1 for t in group if t.pnl_amount > 0)
        wr = wins / len(group) * 100
        parts.append(f"{score}分:{wins}胜/{len(group)}笔({wr:.1f}%)")

    return ", ".join(parts)
