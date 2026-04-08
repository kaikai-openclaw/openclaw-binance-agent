"""
Skill-5：展示与自我进化

从 State_Store 读取账户状态和持仓信息，输出格式化 Markdown 表格展示，
提取平仓交易数据存入 Memory_Store，计算策略胜率和平均盈亏比，
基于反思日志调整 Skill-2 评级阈值和 Skill-3 风险比例。

MemoryStore 和 account_state_provider 通过构造函数注入，便于测试时 mock。

需求: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, List

from src.infra.memory_store import MemoryStore
from src.infra.state_store import StateStore
from src.models.types import (
    AccountState,
    TradeDirection,
    TradeRecord,
    calculate_pnl_ratio,
    compute_evolution_adjustment,
)
from src.skills.base import BaseSkill

log = logging.getLogger(__name__)

# 默认策略参数
DEFAULT_RATING_THRESHOLD = 6
DEFAULT_RISK_RATIO = 0.02

# 账户状态提供者类型
AccountStateProvider = Callable[[], AccountState]


class Skill5Evolve(BaseSkill):
    """
    展示与自我进化 Skill。

    从 State_Store 读取账户状态和持仓信息，输出格式化 Markdown 表格，
    提取平仓交易数据存入 Memory_Store，计算策略统计并执行策略调优。

    需求: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
    """

    def __init__(
        self,
        state_store: StateStore,
        input_schema: dict,
        output_schema: dict,
        memory_store: MemoryStore,
        account_state_provider: AccountStateProvider,
    ) -> None:
        """
        初始化 Skill-5。

        参数:
            state_store: 状态存储实例
            input_schema: 输入 JSON Schema
            output_schema: 输出 JSON Schema
            memory_store: 长期记忆库（注入）
            account_state_provider: 账户状态提供回调
        """
        super().__init__(state_store, input_schema, output_schema)
        self.name = "skill5_evolve"
        self._memory_store = memory_store
        self._account_state_provider = account_state_provider

    def run(self, input_data: dict) -> dict:
        """
        执行展示与自我进化。

        流程:
        1. 读取账户状态（需求 5.1）
        2. 构建持仓展示数据，计算盈亏比例（需求 5.2）
        3. 提取平仓交易数据存入 Memory_Store（需求 5.3）
        4. 计算策略胜率和平均盈亏比（需求 5.4）
        5. 执行策略调优逻辑（需求 5.5, 5.6, 5.7）
        6. 生成格式化 Markdown 表格

        参数:
            input_data: 经 Schema 校验的输入，可包含 input_state_id

        返回:
            符合 skill5_output.json Schema 的输出字典
        """
        # 步骤 1：读取账户状态（需求 5.1）
        account = self._account_state_provider()

        # 如果有上游 state_id，读取执行结果用于提取平仓交易
        input_state_id = input_data.get("input_state_id")
        execution_results = []
        if input_state_id:
            try:
                upstream_data = self.state_store.load(input_state_id)
                execution_results = upstream_data.get(
                    "execution_results", []
                )
            except Exception as exc:
                log.warning(
                    f"[{self.name}] 读取上游数据失败: {exc}"
                )

        # 步骤 2：构建持仓展示数据（需求 5.2）
        positions_display = self._build_positions_display(account)

        # 计算未实现盈亏总额
        unrealized_pnl = sum(
            p.get("unrealized_pnl", 0.0)
            for p in (account.positions or [])
        )

        # 步骤 3：提取平仓交易数据存入 Memory_Store（需求 5.3）
        self._record_closed_trades(execution_results)

        # 步骤 4 & 5：计算策略统计并执行调优（需求 5.4-5.7）
        evolution = self._compute_evolution()

        # 生成 Markdown 表格（需求 5.2）
        markdown = self._generate_markdown(
            account, positions_display, evolution
        )
        log.info(f"[{self.name}] Markdown 报告:\n{markdown}")

        output = {
            "state_id": str(uuid.uuid4()),
            "account_summary": {
                "total_balance": account.total_balance,
                "available_margin": account.available_margin,
                "unrealized_pnl": unrealized_pnl,
                "daily_realized_pnl": account.daily_realized_pnl,
                "is_paper_mode": account.is_paper_mode,
            },
            "positions": positions_display,
            "evolution": evolution,
        }

        log.info(
            f"[{self.name}] 执行完成: "
            f"持仓数={len(positions_display)}, "
            f"胜率={evolution['win_rate']:.1f}%, "
            f"调优={evolution['adjustment_applied']}"
        )

        return output

    def _build_positions_display(
        self, account: AccountState
    ) -> List[dict]:
        """
        构建持仓展示数据，计算每笔持仓的盈亏比例。

        需求 5.2: 展示各持仓币种的方向、数量、入场价格、当前价格、盈亏比例。

        参数:
            account: 账户状态

        返回:
            持仓展示列表
        """
        positions_display = []
        for pos in account.positions or []:
            symbol = pos.get("symbol", "")
            direction_str = pos.get("direction", "long")
            quantity = pos.get("quantity", 0.0)
            entry_price = pos.get("entry_price", 0.0)
            current_price = pos.get("current_price", 0.0)

            # 计算盈亏比例（需求 5.2）
            if entry_price > 0 and current_price > 0:
                direction = TradeDirection(direction_str)
                pnl_ratio = calculate_pnl_ratio(
                    entry_price, current_price, direction
                )
            else:
                pnl_ratio = 0.0

            positions_display.append({
                "symbol": symbol,
                "direction": direction_str,
                "quantity": quantity,
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_ratio": round(pnl_ratio, 4),
            })

        return positions_display

    def _record_closed_trades(
        self, execution_results: List[dict]
    ) -> None:
        """
        提取平仓交易数据存入 Memory_Store。

        需求 5.3: 提取已成交交易的核心数据（币种、方向、入场价格、
        平仓价格、盈亏金额、持仓时长、评级分、策略参数）存入 Memory_Store。

        参数:
            execution_results: Skill-4 输出的执行结果列表
        """
        for result in execution_results:
            status = result.get("status", "")
            # 仅处理已成交或模拟盘的交易
            if status not in ("filled", "paper_trade"):
                continue

            entry_price = result.get("executed_price", 0.0)
            quantity = result.get("executed_quantity", 0.0)
            if entry_price <= 0 or quantity <= 0:
                continue

            symbol = result.get("symbol", "")
            direction_str = result.get("direction", "long")
            direction = TradeDirection(direction_str)

            # 优先使用 Skill-4 传递的入场/平仓价格，缺失时回退到 executed_price
            entry_price = result.get("entry_price", entry_price)
            exit_price = result.get("exit_price", entry_price)

            # 优先使用上游计算的 pnl；缺失时按价格差重算
            pnl_amount = result.get("pnl_amount")
            if pnl_amount is None:
                if direction == TradeDirection.LONG:
                    pnl_amount = (exit_price - entry_price) * quantity
                else:
                    pnl_amount = (entry_price - exit_price) * quantity

            hold_duration = result.get("hold_duration_hours", 0.0)
            rating_score = result.get("rating_score", 6)
            position_size_pct = result.get("position_size_pct", 0.0)

            trade_record = TradeRecord(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl_amount=pnl_amount,
                hold_duration_hours=hold_duration,
                rating_score=rating_score,
                position_size_pct=position_size_pct,
                closed_at=datetime.now(timezone.utc),
            )

            try:
                self._memory_store.record_trade(trade_record)
                log.info(
                    f"[{self.name}] 记录交易: {symbol} "
                    f"pnl={pnl_amount}"
                )
            except Exception as exc:
                log.error(
                    f"[{self.name}] 记录交易失败: {exc}"
                )

    def _compute_evolution(self) -> dict:
        """
        计算策略统计并执行调优逻辑。

        需求 5.4: 基于最近 50 笔交易计算胜率和平均盈亏比
        需求 5.5: 胜率低于 40% 时收紧参数，高于 60% 时放松参数
        需求 5.6: 基于反思日志调整评级阈值和风险比例
        需求 5.7: 交易记录不足 10 笔时跳过进化计算

        返回:
            进化数据字典，符合 skill5_output.json 中 evolution 的 Schema
        """
        # 获取最近 50 笔交易记录
        recent_trades = self._memory_store.get_recent_trades(limit=50)
        trade_count = len(recent_trades)

        # 需求 5.7: 交易记录不足 10 笔时跳过
        if trade_count < 10:
            log.info(
                f"[{self.name}] 交易记录不足 10 笔 "
                f"({trade_count} 笔)，跳过进化计算"
            )
            return {
                "win_rate": 0.0,
                "avg_pnl_ratio": 0.0,
                "trade_count": trade_count,
                "adjustment_applied": False,
                "adjustment_detail": (
                    f"交易记录不足 10 笔（当前 {trade_count} 笔），"
                    "使用默认策略参数"
                ),
                "current_rating_threshold": DEFAULT_RATING_THRESHOLD,
                "current_risk_ratio": DEFAULT_RISK_RATIO,
            }

        # 需求 5.4: 计算策略统计
        stats = self._memory_store.compute_stats(recent_trades)

        # 从上一轮反思日志读取当前参数（闭合反馈环）
        latest_reflection = self._memory_store.get_latest_reflection()
        if latest_reflection is not None:
            current_threshold = latest_reflection.suggested_rating_threshold
            current_risk = latest_reflection.suggested_risk_ratio
        else:
            current_threshold = DEFAULT_RATING_THRESHOLD
            current_risk = DEFAULT_RISK_RATIO

        # 需求 5.5 & 5.6: 渐进式双向调优
        reflection = compute_evolution_adjustment(
            recent_trades,
            current_rating_threshold=current_threshold,
            current_risk_ratio=current_risk,
        )

        adjustment_applied = False
        adjustment_detail = "胜率正常，维持当前策略参数"

        if reflection is not None:
            # 保存反思日志到 Memory_Store
            try:
                self._memory_store.save_reflection(reflection)
            except Exception as exc:
                log.error(
                    f"[{self.name}] 保存反思日志失败: {exc}"
                )

            # 需求 5.6: 判断是否需要调整参数
            if (
                reflection.suggested_rating_threshold != current_threshold
                or reflection.suggested_risk_ratio != current_risk
            ):
                adjustment_applied = True
                adjustment_detail = reflection.reasoning
            else:
                adjustment_detail = reflection.reasoning

        return {
            "win_rate": round(stats.win_rate, 2),
            "avg_pnl_ratio": round(stats.avg_pnl_ratio, 4),
            "trade_count": trade_count,
            "adjustment_applied": adjustment_applied,
            "adjustment_detail": adjustment_detail,
            "current_rating_threshold": (
                reflection.suggested_rating_threshold
                if reflection else current_threshold
            ),
            "current_risk_ratio": (
                reflection.suggested_risk_ratio
                if reflection else current_risk
            ),
        }

    @staticmethod
    def _generate_markdown(
        account: AccountState,
        positions: List[dict],
        evolution: dict,
    ) -> str:
        """
        生成格式化 Markdown 表格展示。

        需求 5.2: 展示账户总资金、可用保证金、未实现盈亏、
        当日已实现盈亏、各持仓详情。

        参数:
            account: 账户状态
            positions: 持仓展示列表
            evolution: 进化数据

        返回:
            格式化 Markdown 字符串
        """
        mode_tag = " 🟡 模拟盘" if account.is_paper_mode else ""
        lines = [
            f"## 账户状态概览{mode_tag}",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 账户总资金 | {account.total_balance:.2f} USDT |",
            f"| 可用保证金 | {account.available_margin:.2f} USDT |",
            f"| 当日已实现盈亏 | {account.daily_realized_pnl:.2f} USDT |",
            "",
        ]

        if positions:
            lines.extend([
                "## 持仓明细",
                "",
                "| 币种 | 方向 | 数量 | 入场价 | 当前价 | 盈亏比例 |",
                "|------|------|------|--------|--------|----------|",
            ])
            for p in positions:
                pnl_str = f"{p['pnl_ratio']:+.2f}%"
                lines.append(
                    f"| {p['symbol']} | {p['direction']} | "
                    f"{p['quantity']:.4f} | {p['entry_price']:.2f} | "
                    f"{p['current_price']:.2f} | {pnl_str} |"
                )
            lines.append("")
        else:
            lines.extend(["## 持仓明细", "", "当前无持仓。", ""])

        lines.extend([
            "## 策略进化",
            "",
            f"- 交易笔数: {evolution['trade_count']}",
            f"- 胜率: {evolution['win_rate']:.1f}%",
            f"- 平均盈亏比: {evolution['avg_pnl_ratio']:.4f}",
            f"- 参数调整: {'是' if evolution['adjustment_applied'] else '否'}",
        ])

        if evolution.get("adjustment_detail"):
            lines.append(
                f"- 调整详情: {evolution['adjustment_detail']}"
            )

        return "\n".join(lines)
