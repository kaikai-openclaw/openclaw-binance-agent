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
    ) -> None:
        self._client = client
        self._memory_store = memory_store
        self._lookback_hours = lookback_hours

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
                record = self._to_trade_record(closed_order, metadata)
                # 用 orderId + 成交时间戳（ms）组合作为幂等键，
                # 防止 Binance 跨时间复用同一 orderId 导致漏记。
                closed_at_ms = int(closed_order.closed_at.timestamp() * 1000)
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
            TradeDirection.LONG
            if closed_order.side == "SELL"
            else TradeDirection.SHORT
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
            strategy_tag=(
                lambda t: t if t and t != "unknown" else "crypto_generic"
            )(str(metadata.get("strategy_tag", "unknown") or "unknown")),
        )


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
