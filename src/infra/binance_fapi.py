"""
Binance_Fapi_Client 合约客户端模块

封装 Binance U本位合约 fapi 接口，集成指数退避重试、超时控制和限流。
"""

import hashlib
import hmac
import logging
import threading
import time
from urllib.parse import urlencode

import requests

from src.infra.rate_limiter import RateLimiter

log = logging.getLogger(__name__)


class IPBannedError(Exception):
    """当 Binance 返回 HTTP 418（IP 被封禁）时抛出。"""
    pass


class MaxRetryExceededError(Exception):
    """当 API 请求重试次数耗尽后抛出。"""
    pass


def calculate_backoff(attempt: int) -> int:
    """
    计算第 attempt 次重试的退避等待时间（秒）。

    退避序列为 [1, 2, 4, 8, 16]，即 2^attempt 秒。
    attempt 从 0 开始，超出序列长度时使用最后一个值。

    参数:
        attempt: 重试次数（从 0 开始）

    返回:
        等待秒数
    """
    sequence = [1, 2, 4, 8, 16]
    index = min(attempt, len(sequence) - 1)
    return sequence[index]


# ============================================================
# 返回值数据类型（轻量 dict 封装）
# ============================================================

class OrderResult:
    """下单结果。"""
    def __init__(self, order_id: str, symbol: str, side: str, price: float,
                 quantity: float, status: str, raw: dict | None = None):
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.price = price
        self.quantity = quantity
        self.status = status
        self.raw = raw or {}


class PositionInfo:
    """持仓信息。"""
    def __init__(self, symbol: str, position_amt: float, entry_price: float,
                 unrealized_pnl: float, leverage: int, raw: dict | None = None):
        self.symbol = symbol
        self.position_amt = position_amt
        self.entry_price = entry_price
        self.unrealized_pnl = unrealized_pnl
        self.leverage = leverage
        self.raw = raw or {}


class AccountInfo:
    """账户信息。"""
    def __init__(self, total_balance: float, available_balance: float,
                 total_unrealized_pnl: float, raw: dict | None = None):
        self.total_balance = total_balance
        self.available_balance = available_balance
        self.total_unrealized_pnl = total_unrealized_pnl
        self.raw = raw or {}


class PositionRisk:
    """持仓风险信息。"""
    def __init__(self, symbol: str, position_amt: float, entry_price: float,
                 mark_price: float, unrealized_pnl: float,
                 liquidation_price: float, leverage: int,
                 raw: dict | None = None):
        self.symbol = symbol
        self.position_amt = position_amt
        self.entry_price = entry_price
        self.mark_price = mark_price
        self.unrealized_pnl = unrealized_pnl
        self.liquidation_price = liquidation_price
        self.leverage = leverage
        self.raw = raw or {}


# ============================================================
# BinanceFapiClient 主类
# ============================================================

class BinanceFapiClient:
    """
    Binance U本位合约 fapi 客户端。

    - 集成 Rate_Limiter 进行请求限流
    - 指数退避重试（退避序列 [1, 2, 4, 8, 16]，最多 5 次）
    - 处理 HTTP 429（暂停 30s）和 HTTP 418（紧急停止）
    - 请求签名使用 HMAC-SHA256
    """

    REQUEST_TIMEOUT = 10  # 请求超时（秒）
    MAX_RETRIES = 5
    BACKOFF_SEQUENCE = [1, 2, 4, 8, 16]  # 指数退避序列（秒）

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://fapi.binance.com",
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or RateLimiter()
        self._session = requests.Session()
        self._session.headers.update({
            "X-MBX-APIKEY": self.api_key,
        })
        # 网络故障标记：用于检测网络恢复后触发自动同步（线程安全）
        self._network_lock = threading.Lock()
        self._network_was_down: bool = False

    # ------------------------------------------------------------------
    # 签名与请求基础设施
    # ------------------------------------------------------------------

    def _sign(self, params: dict) -> dict:
        """对请求参数进行 HMAC-SHA256 签名。"""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request_with_retry(self, method: str, path: str, params: dict | None = None) -> dict:
        """
        带指数退避重试的 HTTP 请求。

        - 每次请求前调用 rate_limiter.acquire()
        - HTTP 429 → rate_limiter.pause(30)，继续重试
        - HTTP 418 → rate_limiter.stop()，抛出 IPBannedError
        - 超时/网络错误 → 指数退避后重试
        - 重试耗尽 → 抛出 MaxRetryExceededError
        """
        url = f"{self.base_url}{path}"
        signed_params = self._sign(params or {})

        for attempt in range(self.MAX_RETRIES):
            try:
                # 限流：获取令牌
                self.rate_limiter.acquire()

                response = self._session.request(
                    method=method,
                    url=url,
                    params=signed_params if method.upper() == "GET" else None,
                    data=signed_params if method.upper() != "GET" else None,
                    timeout=self.REQUEST_TIMEOUT,
                )

                # HTTP 429：请求过多，暂停 30 秒
                if response.status_code == 429:
                    self.rate_limiter.pause(30)
                    log.warning(f"HTTP 429，暂停 30 秒后重试（第 {attempt + 1} 次）")
                    backoff = calculate_backoff(attempt)
                    time.sleep(backoff)
                    continue

                # HTTP 418：IP 被封禁，紧急停止
                if response.status_code == 418:
                    self.rate_limiter.stop()
                    log.critical("HTTP 418，IP 被 Binance 封禁，紧急停止所有请求")
                    raise IPBannedError("IP 被 Binance 封禁（HTTP 418）")

                # 其他错误状态码
                response.raise_for_status()

                # 检测网络恢复：之前有网络故障，现在请求成功
                with self._network_lock:
                    was_down = self._network_was_down
                    self._network_was_down = False
                if was_down:
                    log.info(f"网络已恢复（请求 {path} 成功），将触发自动同步")
                    # 注意：不在此处直接调用 sync_after_reconnect()，
                    # 避免递归调用。由调用方在捕获到恢复信号后主动调用。

                return response.json()

            except IPBannedError:
                raise  # 不重试，直接抛出

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                # 标记网络故障（线程安全）
                with self._network_lock:
                    self._network_was_down = True
                backoff = calculate_backoff(attempt)
                log.warning(
                    f"请求 {path} 失败（{type(e).__name__}），"
                    f"第 {attempt + 1} 次重试，等待 {backoff}s"
                )
                time.sleep(backoff)

            except requests.exceptions.HTTPError as e:
                # 非 429/418 的 HTTP 错误，也进行退避重试
                backoff = calculate_backoff(attempt)
                log.warning(
                    f"请求 {path} HTTP 错误（{e.response.status_code}），"
                    f"第 {attempt + 1} 次重试，等待 {backoff}s"
                )
                time.sleep(backoff)

        # 重试耗尽
        log.error(f"API 请求 {path} 重试 {self.MAX_RETRIES} 次后仍失败")
        raise MaxRetryExceededError(
            f"API 请求 {path} 重试 {self.MAX_RETRIES} 次后仍失败"
        )

    # ------------------------------------------------------------------
    # 公开 API 方法
    # ------------------------------------------------------------------

    def place_limit_order(self, symbol: str, side: str, price: float,
                          quantity: float) -> OrderResult:
        """
        提交限价订单。

        参数:
            symbol: 交易对符号（如 "BTCUSDT"）
            side: 买卖方向（"BUY" 或 "SELL"）
            price: 限价价格
            quantity: 下单数量
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": str(price),
            "quantity": str(quantity),
        }
        data = self._request_with_retry("POST", "/fapi/v1/order", params)
        return OrderResult(
            order_id=str(data.get("orderId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("price", price)),
            quantity=float(data.get("origQty", quantity)),
            status=data.get("status", ""),
            raw=data,
        )

    def place_market_order(self, symbol: str, side: str,
                           quantity: float) -> OrderResult:
        """
        提交市价订单（用于止损/止盈平仓）。

        参数:
            symbol: 交易对符号
            side: 买卖方向
            quantity: 下单数量
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": str(quantity),
        }
        data = self._request_with_retry("POST", "/fapi/v1/order", params)
        return OrderResult(
            order_id=str(data.get("orderId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("avgPrice", 0)),
            quantity=float(data.get("origQty", quantity)),
            status=data.get("status", ""),
            raw=data,
        )

    def get_positions(self) -> list[PositionInfo]:
        """获取当前所有未平仓持仓。"""
        data = self._request_with_retry("GET", "/fapi/v2/positionRisk")
        positions = []
        for item in data:
            amt = float(item.get("positionAmt", 0))
            if amt != 0:
                positions.append(PositionInfo(
                    symbol=item.get("symbol", ""),
                    position_amt=amt,
                    entry_price=float(item.get("entryPrice", 0)),
                    unrealized_pnl=float(item.get("unRealizedProfit", 0)),
                    leverage=int(item.get("leverage", 1)),
                    raw=item,
                ))
        return positions

    def get_account_info(self) -> AccountInfo:
        """获取账户余额和保证金信息。"""
        data = self._request_with_retry("GET", "/fapi/v2/account")
        return AccountInfo(
            total_balance=float(data.get("totalWalletBalance", 0)),
            available_balance=float(data.get("availableBalance", 0)),
            total_unrealized_pnl=float(data.get("totalUnrealizedProfit", 0)),
            raw=data,
        )

    def cancel_all_orders(self, symbol: str | None = None) -> int:
        """
        取消指定币种或全部未成交订单，返回取消数量。

        参数:
            symbol: 交易对符号。若为 None，则取消所有持仓币种的挂单。
        """
        if symbol:
            data = self._request_with_retry(
                "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}
            )
            # Binance 返回 {"code": 200, "msg": "The operation of cancel all open order is done."}
            return 1 if data.get("code") == 200 else 0

        # 无指定 symbol 时，先获取持仓列表，逐个取消
        positions = self.get_positions()
        cancelled = 0
        symbols_seen = set()
        for pos in positions:
            if pos.symbol not in symbols_seen:
                symbols_seen.add(pos.symbol)
                try:
                    result = self._request_with_retry(
                        "DELETE", "/fapi/v1/allOpenOrders", {"symbol": pos.symbol}
                    )
                    if result.get("code") == 200:
                        cancelled += 1
                except Exception as e:
                    log.warning(f"取消 {pos.symbol} 挂单失败: {e}")
        return cancelled

    def get_position_risk(self, symbol: str) -> PositionRisk:
        """
        获取指定币种的持仓风险信息（含未实现盈亏）。

        参数:
            symbol: 交易对符号
        """
        data = self._request_with_retry(
            "GET", "/fapi/v2/positionRisk", {"symbol": symbol}
        )
        # 返回列表，取第一个匹配项
        item = data[0] if data else {}
        return PositionRisk(
            symbol=item.get("symbol", symbol),
            position_amt=float(item.get("positionAmt", 0)),
            entry_price=float(item.get("entryPrice", 0)),
            mark_price=float(item.get("markPrice", 0)),
            unrealized_pnl=float(item.get("unRealizedProfit", 0)),
            liquidation_price=float(item.get("liquidationPrice", 0)),
            leverage=int(item.get("leverage", 1)),
            raw=item,
        )

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """
        获取未完成订单列表。

        参数:
            symbol: 交易对符号。若为 None，则获取所有未完成订单。

        返回:
            未完成订单列表（原始 dict）
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request_with_retry("GET", "/fapi/v1/openOrders", params)
        return data if isinstance(data, list) else []

    def sync_after_reconnect(self) -> dict:
        """
        网络恢复后自动重新同步账户持仓和未完成订单状态。

        在网络断线恢复后调用，重新获取：
        1. 账户信息（余额、保证金）
        2. 当前所有未平仓持仓
        3. 所有未完成订单

        返回:
            {
                "account": AccountInfo,
                "positions": list[PositionInfo],
                "open_orders": list[dict],
            }

        异常:
            MaxRetryExceededError: 同步请求重试耗尽时抛出

        需求: 7.7
        """
        log.info("网络恢复，开始重新同步账户状态...")

        # 步骤 1：重新获取账户信息
        account_info = self.get_account_info()
        log.info(
            f"账户同步完成: 总余额={account_info.total_balance}, "
            f"可用余额={account_info.available_balance}"
        )

        # 步骤 2：重新获取所有未平仓持仓
        positions = self.get_positions()
        log.info(f"持仓同步完成: {len(positions)} 笔未平仓持仓")

        # 步骤 3：重新获取所有未完成订单
        open_orders = self.get_open_orders()
        log.info(f"订单同步完成: {len(open_orders)} 笔未完成订单")

        log.info("网络恢复同步全部完成")

        return {
            "account": account_info,
            "positions": positions,
            "open_orders": open_orders,
        }
