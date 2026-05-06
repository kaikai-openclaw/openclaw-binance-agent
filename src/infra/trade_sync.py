"""
Binance 服务端成交同步。

服务端 STOP_MARKET / TAKE_PROFIT_MARKET 触发后，本地 Skill-4 不一定阻塞
监控到平仓结果。本模块从 Binance userTrades 拉取真实 realizedPnl 成交，
幂等写入 MemoryStore，补齐自我进化的数据闭环。
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.memory_store import MemoryStore
from src.models.types import TradeDirection, TradeRecord

log = logging.getLogger(__name__)


DEFAULT_SYNC_LOOKBACK_HOURS = 72
# 用于兜底扫收时的回溯窗口（7天，捕捉跨多天持仓后被强平的币种）
SWEEP_LOOKBACK_HOURS = 168


@dataclass
class _ClosedOrder:
    symbol: str
    order_id: str
    side: str
    quantity: float
    exit_price: float
    realized_pnl: float
    closed_at: datetime


class BinanceTradeSyncer:
    """把 Binance 真实平仓成交同步到 MemoryStore。"""

    def __init__(
        self,
        client: BinanceFapiClient,
        memory_store: MemoryStore,
        lookback_hours: int = DEFAULT_SYNC_LOOKBACK_HOURS,
        risk_controller: Optional[Any] = None,
    ) -> None:
        self._client = client
        self._memory_store = memory_store
        self._lookback_hours = lookback_hours
        self._risk_controller = risk_controller  # 可选，用于记录止损冷却期

    def sync_closed_trades(
        self,
        symbols: Iterable[str],
        metadata_by_symbol: Optional[dict[str, dict[str, Any]]] = None,
    ) -> int:
        """
        同步指定交易对最近的已实现盈亏成交。

        metadata_by_symbol 可携带本地计划侧元数据，如 rating_score /
        position_size_pct；缺失时使用保守默认值。
        """
        metadata_by_symbol = metadata_by_symbol or {}
        synced_count = 0
        for symbol in sorted({s for s in symbols if s}):
            try:
                trades = self._client.get_user_trades(
                    symbol=symbol,
                    start_time=self._start_time_ms(),
                    limit=1000,
                )
            except Exception as exc:
                log.warning(f"同步 {symbol} Binance 成交失败: {exc}")
                continue

            for closed_order in self._extract_closed_orders(symbol, trades):
                metadata = metadata_by_symbol.get(symbol, {})
                # 若 metadata 为空或 strategy_tag 缺失，用 risk_controller 兜底查询
                if self._risk_controller is not None and (
                    not metadata.get("strategy_tag")
                    or metadata.get("strategy_tag") == "unknown"
                ):
                    direction = "long" if closed_order.side == "SELL" else "short"
                    tag = self._risk_controller._get_position_strategy_tag(
                        closed_order.symbol, direction
                    )
                    if tag and tag != "existing_position":
                        metadata = {
                            "strategy_tag": tag,
                            "rating_score": metadata.get("rating_score", 6),
                            "position_size_pct": metadata.get("position_size_pct", 0.0),
                            "hold_duration_hours": metadata.get(
                                "hold_duration_hours", 0.0
                            ),
                            "close_reason": metadata.get(
                                "close_reason", "server_close"
                            ),
                        }
                closed_at_ms = int(closed_order.closed_at.timestamp() * 1000)
                direction_str = "long" if closed_order.side == "SELL" else "short"
                open_ms = self._memory_store.get_position_open_time(
                    closed_order.symbol, direction_str
                )
                if open_ms is not None and open_ms > 0:
                    hold_hours = max(0.0, (closed_at_ms - open_ms) / 3600000.0)
                    metadata["hold_duration_hours"] = hold_hours
                    self._memory_store.remove_position_open_time(
                        closed_order.symbol, direction_str
                    )
                record = self._to_trade_record(closed_order, metadata)
                # ── 去重：检查是否已被 partial_tp.py 或之前的同步写入 ──
                # partial_tp.py 用 partial_tp 前缀，trade_sync 用 binance_user_order 前缀
                # 两者 order_id 相同时说明是同一笔 Binance 成交，应跳过
                if self._memory_store.has_order_synced(
                    closed_order.symbol, closed_order.order_id
                ):
                    log.info(
                        "跳过 %s order=%s（已同步过）",
                        closed_order.symbol,
                        closed_order.order_id,
                    )
                    continue
                sync_key = (
                    f"binance_user_order:{closed_order.symbol}:"
                    f"{closed_order.order_id}:{closed_at_ms}"
                )
                if self._memory_store.record_trade_once(record, sync_key):
                    synced_count += 1
                    log.info(
                        "同步 Binance 平仓成交: %s order=%s pnl=%.8f",
                        closed_order.symbol,
                        closed_order.order_id,
                        closed_order.realized_pnl,
                    )
                    # ── 亏损成交：记录止损冷却期，防止立即重新开仓 ──
                    # 当 realized_pnl < 0 时，说明是止损触发（服务端保护单平仓）。
                    # 需要记录冷却期，防止下一轮 pipeline 立即重新开仓。
                    if (
                        self._risk_controller is not None
                        and closed_order.realized_pnl < 0
                    ):
                        direction = "long" if closed_order.side == "SELL" else "short"
                        strategy_tag = str(
                            metadata.get("strategy_tag", "server_close")
                            or "server_close"
                        )
                        if strategy_tag in ("unknown", ""):
                            strategy_tag = "server_close"
                        self._risk_controller.record_stop_loss(
                            closed_order.symbol,
                            direction,
                            strategy_tag=strategy_tag,
                        )
                        log.warning(
                            "同步到亏损成交 %s %s pnl=%.8f，已记录冷却期 %.1f 小时",
                            closed_order.symbol,
                            direction,
                            closed_order.realized_pnl,
                            self._risk_controller.STOP_LOSS_COOLDOWN_HOURS,
                        )

        return synced_count

    def _start_time_ms(self) -> int:
        return int((time.time() - self._lookback_hours * 3600) * 1000)

    @staticmethod
    def _extract_closed_orders(
        symbol: str,
        trades: list[dict[str, Any]],
    ) -> list[_ClosedOrder]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for trade in trades:
            realized_pnl = _to_float(trade.get("realizedPnl"))
            quantity = _to_float(trade.get("qty"))
            price = _to_float(trade.get("price"))
            order_id = str(trade.get("orderId", ""))
            side = str(trade.get("side", "")).upper()
            if realized_pnl == 0 or quantity <= 0 or price <= 0 or not order_id:
                continue
            if side not in {"BUY", "SELL"}:
                continue

            key = (order_id, side)
            item = grouped.setdefault(
                key,
                {
                    "symbol": str(trade.get("symbol", symbol)),
                    "order_id": order_id,
                    "side": side,
                    "quantity": 0.0,
                    "weighted_exit": 0.0,
                    "realized_pnl": 0.0,
                    "closed_ms": 0,
                },
            )
            item["quantity"] += quantity
            item["weighted_exit"] += price * quantity
            item["realized_pnl"] += realized_pnl
            item["closed_ms"] = max(item["closed_ms"], int(trade.get("time", 0) or 0))

        closed_orders: list[_ClosedOrder] = []
        for item in grouped.values():
            quantity = item["quantity"]
            if quantity <= 0:
                continue
            exit_price = item["weighted_exit"] / quantity
            closed_at = datetime.fromtimestamp(
                item["closed_ms"] / 1000,
                tz=timezone.utc,
            )
            closed_orders.append(
                _ClosedOrder(
                    symbol=item["symbol"],
                    order_id=item["order_id"],
                    side=item["side"],
                    quantity=quantity,
                    exit_price=exit_price,
                    realized_pnl=item["realized_pnl"],
                    closed_at=closed_at,
                )
            )
        return closed_orders

    @staticmethod
    def _to_trade_record(
        closed_order: _ClosedOrder,
        metadata: dict[str, Any],
    ) -> TradeRecord:
        direction = (
            TradeDirection.LONG if closed_order.side == "SELL" else TradeDirection.SHORT
        )
        if direction == TradeDirection.LONG:
            entry_price = (
                closed_order.exit_price
                - closed_order.realized_pnl / closed_order.quantity
            )
        else:
            entry_price = (
                closed_order.exit_price
                + closed_order.realized_pnl / closed_order.quantity
            )

        return TradeRecord(
            symbol=closed_order.symbol,
            direction=direction,
            entry_price=max(entry_price, 0.0),
            exit_price=closed_order.exit_price,
            pnl_amount=closed_order.realized_pnl,
            hold_duration_hours=float(metadata.get("hold_duration_hours", 0.0) or 0.0),
            rating_score=int(metadata.get("rating_score", 6) or 6),
            position_size_pct=float(metadata.get("position_size_pct", 0.0) or 0.0),
            closed_at=closed_order.closed_at,
            strategy_tag=metadata.get("strategy_tag", "unknown") or "unknown",
            close_reason=str(
                metadata.get("close_reason", "server_close") or "server_close"
            ),
        )

    def sync_all_candidates(self) -> int:
        """
        兜底扫收：主动从 Binance 账户持仓发现所有近期有交易的币种，
        同步其已实现盈亏成交。不依赖 pipeline 传入的 symbols 列表，
        能捕捉到跨 pipeline 运行周期被服务端强平的币种（如 DEXEUSDT/COMPUSDT）。

        发现范围：
        1. 当前账户持仓中的所有币种
        2. 历史上有过交易但当前已无持仓的币种（从 position_strategy_tags 表读取）

        回溯窗口：SWEEP_LOOKBACK_HOURS（默认7天），确保跨多天持仓后被止损的情况不漏记。
        """
        # ── Step 1: 收集候选币种 ──────────────────────────────
        candidate_symbols: set[str] = set()

        # 1a. 当前持仓（使用 get_positions 返回 List[PositionInfo]）
        try:
            positions = self._client.get_positions()
            for pos in positions:
                if pos.position_amt != 0 and pos.symbol:
                    candidate_symbols.add(pos.symbol)
        except Exception as exc:
            log.warning(f"[BinanceTradeSyncer] 获取持仓列表失败: {exc}")

        # 1b. 历史交易过但已无持仓的币种（从 MemoryStore 读取）
        try:
            historical = self._memory_store.get_all_traded_symbols()
            candidate_symbols.update(historical)
        except Exception as exc:
            log.warning(f"[BinanceTradeSyncer] 读取历史交易币种失败: {exc}")

        if not candidate_symbols:
            log.info("[BinanceTradeSyncer] 无候选币种，跳过兜底扫收")
            return 0

        log.info(
            f"[BinanceTradeSyncer] 兜底扫收候选币种 {len(candidate_symbols)} 个: "
            f"{sorted(candidate_symbols)}"
        )

        # ── Step 2: 用更长回溯窗口同步所有候选 ───────────────────
        # 构建 metadata_by_symbol：用 risk_controller 查 position_strategy_tags 表
        # 确保服务端平仓时能获取正确的 strategy_tag，避免回退到 crypto_generic
        metadata_by_symbol: dict[str, dict[str, Any]] = {}
        if self._risk_controller is not None:
            for sym in candidate_symbols:
                tag = self._risk_controller._get_position_strategy_tag(sym, "long")
                if tag and tag != "existing_position":
                    metadata_by_symbol[sym] = {
                        "strategy_tag": tag,
                        "rating_score": 6,
                        "position_size_pct": 0.0,
                        "hold_duration_hours": 0.0,
                        "close_reason": "server_close",
                    }
                else:
                    tag = self._risk_controller._get_position_strategy_tag(sym, "short")
                    if tag and tag != "existing_position":
                        metadata_by_symbol[sym] = {
                            "strategy_tag": tag,
                            "rating_score": 6,
                            "position_size_pct": 0.0,
                            "hold_duration_hours": 0.0,
                            "close_reason": "server_close",
                        }

        original_lookback = self._lookback_hours
        self._lookback_hours = SWEEP_LOOKBACK_HOURS
        try:
            synced = self.sync_closed_trades(
                symbols=candidate_symbols,
                metadata_by_symbol=metadata_by_symbol if metadata_by_symbol else None,
            )
        finally:
            self._lookback_hours = original_lookback

        log.info(f"[BinanceTradeSyncer] 兜底扫收完成，新增同步 {synced} 笔")
        return synced


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
