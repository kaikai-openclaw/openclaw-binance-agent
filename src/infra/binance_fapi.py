"""
Binance_Fapi_Client 合约客户端模块

封装 Binance U本位合约 fapi 接口，集成指数退避重试、超时控制和限流。
"""

import hashlib
import hmac
import logging
import os
import threading
import time
from decimal import Decimal
from urllib.parse import urlencode

from typing import Dict, List, Optional, Set

import requests

from src.infra.rate_limiter import RateLimiter, GLOBAL_RATE_LIMITER

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


def format_decimal_param(value: float | int | str | Decimal) -> str:
    """
    将下单价格/数量格式化为 Binance 友好的十进制字符串。

    避免把整数数量 33216.0 序列化成 "33216.0"，导致
    quantityPrecision=0 的交易对被 Binance 拒绝。
    """
    decimal_value = Decimal(str(value))
    if decimal_value == decimal_value.to_integral_value():
        return str(decimal_value.quantize(Decimal("1")))
    return format(decimal_value.normalize(), "f")


def _extract_response_detail(response) -> str:
    """提取 Binance 错误响应正文，便于定位精度/余额等 400 问题。"""
    try:
        return response.text[:500]
    except Exception:
        return ""


# ============================================================
# 返回值数据类型（轻量 dict 封装）
# ============================================================

class OrderResult:
    """下单结果（普通订单 + Algo 条件单通用）。"""
    def __init__(self, order_id: str, symbol: str, side: str, price: float,
                 quantity: float, status: str, raw: Optional[dict] = None):
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
                 unrealized_pnl: float, leverage: int, raw: Optional[dict] = None):
        self.symbol = symbol
        self.position_amt = position_amt
        self.entry_price = entry_price
        self.unrealized_pnl = unrealized_pnl
        self.leverage = leverage
        self.raw = raw or {}


class AccountInfo:
    """账户信息。"""
    def __init__(self, total_balance: float, available_balance: float,
                 total_unrealized_pnl: float, raw: Optional[dict] = None):
        self.total_balance = total_balance
        self.available_balance = available_balance
        self.total_unrealized_pnl = total_unrealized_pnl
        self.raw = raw or {}


class PositionRisk:
    """持仓风险信息。"""
    def __init__(self, symbol: str, position_amt: float, entry_price: float,
                 mark_price: float, unrealized_pnl: float,
                 liquidation_price: float, leverage: int,
                 raw: Optional[dict] = None):
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
    DEFAULT_RECV_WINDOW = 5000  # Binance 签名请求的接收窗口（毫秒）

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://fapi.binance.com",
        rate_limiter: Optional[RateLimiter] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter or GLOBAL_RATE_LIMITER
        self._session = requests.Session()
        self._session.headers.update({
            "X-MBX-APIKEY": self.api_key,
        })
        # 代理配置：支持 HTTP/SOCKS5 代理
        # 优先使用传入参数，其次读取环境变量 HTTPS_PROXY
        _proxy = proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if _proxy:
            self._session.proxies = {
                "http": _proxy,
                "https": _proxy,
            }
            log.info(f"Binance API 代理已配置: {_proxy}")
        # 网络故障标记：用于检测网络恢复后触发自动同步（线程安全）
        self._network_lock = threading.Lock()
        self._network_was_down: bool = False

    # ------------------------------------------------------------------
    # 签名与请求基础设施
    # ------------------------------------------------------------------

    def _sign(self, params: dict) -> dict:
        """对请求参数进行 HMAC-SHA256 签名。"""
        signed_params = dict(params)
        signed_params.setdefault("recvWindow", self.DEFAULT_RECV_WINDOW)
        signed_params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(signed_params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed_params["signature"] = signature
        return signed_params

    def _request_with_retry(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        """
        带指数退避重试的 HTTP 请求。

        - 每次请求前调用 rate_limiter.acquire()
        - HTTP 429 → rate_limiter.pause(30)，继续重试
        - HTTP 418 → rate_limiter.stop()，抛出 IPBannedError
        - 超时/网络错误 → 指数退避后重试
        - 重试耗尽 → 抛出 MaxRetryExceededError
        """
        url = f"{self.base_url}{path}"
        request_params = dict(params or {})
        last_error_detail = ""

        for attempt in range(self.MAX_RETRIES):
            try:
                # 限流：获取令牌
                self.rate_limiter.acquire()
                signed_params = self._sign(request_params)

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
                try:
                    response.raise_for_status()
                except requests.exceptions.HTTPError as exc:
                    detail = _extract_response_detail(response)
                    if detail:
                        raise requests.exceptions.HTTPError(
                            f"{exc}; Binance response: {detail}",
                            response=response,
                        ) from exc
                    raise

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
                last_error_detail = str(e)
                backoff = calculate_backoff(attempt)
                log.warning(
                    f"请求 {path} 失败（{type(e).__name__}），"
                    f"第 {attempt + 1} 次重试，等待 {backoff}s"
                )
                time.sleep(backoff)

            except requests.exceptions.HTTPError as e:
                # 非 429/418 的 HTTP 错误，也进行退避重试
                backoff = calculate_backoff(attempt)
                detail = str(e)
                last_error_detail = detail
                status_code = e.response.status_code if e.response is not None else "unknown"
                log.warning(
                    f"请求 {path} HTTP 错误（{status_code}），"
                    f"详情={detail}，"
                    f"第 {attempt + 1} 次重试，等待 {backoff}s"
                )
                time.sleep(backoff)

        # 重试耗尽
        log.error(f"API 请求 {path} 重试 {self.MAX_RETRIES} 次后仍失败")
        detail_suffix = f": {last_error_detail}" if last_error_detail else ""
        raise MaxRetryExceededError(
            f"API 请求 {path} 重试 {self.MAX_RETRIES} 次后仍失败{detail_suffix}"
        )

    # ------------------------------------------------------------------
    # 公开 API 方法
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """
        设置指定 U 本位合约交易对杠杆。

        Binance 的杠杆是交易所侧按 symbol 持久保存的状态；下单参数本身
        不携带 leverage，因此每次自动执行前需要显式同步目标杠杆。
        """
        params = {
            "symbol": symbol,
            "leverage": leverage,
        }
        return self._request_with_retry("POST", "/fapi/v1/leverage", params)

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
            "price": format_decimal_param(price),
            "quantity": format_decimal_param(quantity),
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
            "quantity": format_decimal_param(quantity),
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

    def place_stop_market_order(self, symbol: str, side: str,
                                quantity: float, stop_price: float,
                                close_position: bool = False) -> OrderResult:
        """
        提交止损市价单（STOP_MARKET）。触及 stop_price 后以市价成交。

        注意：自 2025-12-09 起，条件单已迁移至 Algo Service，
        使用 POST /fapi/v1/algoOrder 端点。

        参数:
            symbol: 交易对符号（如 "BTCUSDT"）
            side: 买卖方向（"BUY" 或 "SELL"）
            quantity: 下单数量（close_position=True 时忽略）
            stop_price: 触发价格
            close_position: 是否全部平仓（True 时忽略 quantity）
        """
        params: dict = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "triggerPrice": format_decimal_param(stop_price),
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = format_decimal_param(quantity)
        data = self._request_with_retry("POST", "/fapi/v1/algoOrder", params)
        return OrderResult(
            order_id=str(data.get("algoId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("triggerPrice", stop_price)),
            quantity=float(data.get("quantity", quantity)),
            status=data.get("algoStatus", ""),
            raw=data,
        )

    def place_take_profit_market_order(self, symbol: str, side: str,
                                       quantity: float, stop_price: float,
                                       close_position: bool = False) -> OrderResult:
        """
        提交止盈市价单（TAKE_PROFIT_MARKET）。触及 stop_price 后以市价成交。

        注意：自 2025-12-09 起，条件单已迁移至 Algo Service，
        使用 POST /fapi/v1/algoOrder 端点。

        参数:
            symbol: 交易对符号
            side: 买卖方向
            quantity: 下单数量（close_position=True 时忽略）
            stop_price: 触发价格
            close_position: 是否全部平仓
        """
        params: dict = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": format_decimal_param(stop_price),
        }
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = format_decimal_param(quantity)
        data = self._request_with_retry("POST", "/fapi/v1/algoOrder", params)
        return OrderResult(
            order_id=str(data.get("algoId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("triggerPrice", stop_price)),
            quantity=float(data.get("quantity", quantity)),
            status=data.get("algoStatus", ""),
            raw=data,
        )

    def place_stop_limit_order(self, symbol: str, side: str,
                               quantity: float, price: float,
                               stop_price: float) -> OrderResult:
        """
        提交止损限价单（STOP）。触及 stop_price 后以 price 挂限价单。

        注意：自 2025-12-09 起，条件单已迁移至 Algo Service，
        使用 POST /fapi/v1/algoOrder 端点。

        参数:
            symbol: 交易对符号
            side: 买卖方向
            quantity: 下单数量
            price: 限价价格（触发后的挂单价）
            stop_price: 触发价格
        """
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "STOP",
            "timeInForce": "GTC",
            "price": format_decimal_param(price),
            "quantity": format_decimal_param(quantity),
            "triggerPrice": format_decimal_param(stop_price),
        }
        data = self._request_with_retry("POST", "/fapi/v1/algoOrder", params)
        return OrderResult(
            order_id=str(data.get("algoId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("price", price)),
            quantity=float(data.get("quantity", quantity)),
            status=data.get("algoStatus", ""),
            raw=data,
        )

    def place_take_profit_limit_order(self, symbol: str, side: str,
                                      quantity: float, price: float,
                                      stop_price: float) -> OrderResult:
        """
        提交止盈限价单（TAKE_PROFIT）。触及 stop_price 后以 price 挂限价单。

        注意：自 2025-12-09 起，条件单已迁移至 Algo Service，
        使用 POST /fapi/v1/algoOrder 端点。

        参数:
            symbol: 交易对符号
            side: 买卖方向
            quantity: 下单数量
            price: 限价价格
            stop_price: 触发价格
        """
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT",
            "timeInForce": "GTC",
            "price": format_decimal_param(price),
            "quantity": format_decimal_param(quantity),
            "triggerPrice": format_decimal_param(stop_price),
        }
        data = self._request_with_retry("POST", "/fapi/v1/algoOrder", params)
        return OrderResult(
            order_id=str(data.get("algoId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("price", price)),
            quantity=float(data.get("quantity", quantity)),
            status=data.get("algoStatus", ""),
            raw=data,
        )

    def place_trailing_stop_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        callback_rate: float,
        activation_price: Optional[float] = None,
        close_position: bool = False,
    ) -> OrderResult:
        """
        提交移动止损市价单（TRAILING_STOP_MARKET）。

        价格在激活价触发后，跟踪最优价格，当回调幅度达到 callback_rate 时
        以市价平仓，锁住已实现盈利。

        注意：Binance 强制 TRAILING_STOP_MARKET 走 Algo Service 端点，
        该端点不支持 closePosition=true 也不支持 reduceOnly=true。
        为防止触发后开反向新仓，必须确保 quantity 精确等于当前持仓量。

        参数:
            symbol: 交易对符号（如 \"BTCUSDT\"）
            side: 买卖方向（做多持仓平仓用 \"SELL\"，做空持仓平仓用 \"BUY\"）
            quantity: 下单数量（必须精确等于持仓量，防止反向开仓）
            callback_rate: 回调比例（%），取值范围 0.1 ~ 5.0
            activation_price: 激活价格（可选），达到此价格才开始追踪；
                              不传则立即从当前价格开始追踪
            close_position: 未使用（Algo Service 不支持），保留参数兼容性
        """
        params: dict = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": "TRAILING_STOP_MARKET",
            "callbackRate": format_decimal_param(callback_rate),
            "quantity": format_decimal_param(quantity),
        }
        if activation_price is not None and activation_price > 0:
            params["activationPrice"] = format_decimal_param(activation_price)

        data = self._request_with_retry("POST", "/fapi/v1/algoOrder", params)
        return OrderResult(
            order_id=str(data.get("algoId", "")),
            symbol=data.get("symbol", symbol),
            side=data.get("side", side),
            price=float(data.get("activationPrice", activation_price or 0)),
            quantity=float(data.get("quantity", quantity)),
            status=data.get("algoStatus", ""),
            raw=data,
        )

    def place_oco_stop_take_profit(self, symbol: str, side: str,
                                   quantity: float, stop_price: float,
                                   take_profit_price: float) -> List[OrderResult]:
        """
        一次性挂止损 + 止盈两张市价条件单（模拟 OCO）。

        注意：Binance 合约不支持原生 OCO，这里分别下两张 STOP_MARKET 和
        TAKE_PROFIT_MARKET 单。其中一张触发成交后，需要手动取消另一张。

        参数:
            symbol: 交易对符号
            side: 平仓方向（做多持仓用 "SELL"，做空持仓用 "BUY"）
            quantity: 下单数量
            stop_price: 止损触发价
            take_profit_price: 止盈触发价

        返回:
            [止损单 OrderResult, 止盈单 OrderResult]
        """
        sl = self.place_stop_market_order(symbol, side, quantity, stop_price)
        tp = self.place_take_profit_market_order(symbol, side, quantity, take_profit_price)
        return [sl, tp]

    def get_positions(self) -> List[PositionInfo]:
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

    def get_user_trades(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List[dict]:
        """
        获取指定交易对的账户成交历史。

        用于同步 Binance 服务端条件单触发后的真实平仓成交，供本地
        MemoryStore 和策略进化使用。
        """
        params: dict = {
            "symbol": symbol,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        data = self._request_with_retry("GET", "/fapi/v1/userTrades", params)
        return data if isinstance(data, list) else []

    def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
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

        # 无指定 symbol 时，按 openOrders 覆盖所有有挂单币种
        cancelled = 0
        symbols_to_cancel: Set[str] = set()

        try:
            open_orders = self.get_open_orders()
            symbols_to_cancel.update(
                str(order.get("symbol", ""))
                for order in open_orders
                if order.get("symbol")
            )
        except Exception as e:
            log.warning(f"获取 open orders 失败，回退持仓币种取消: {e}")

        # 回退逻辑：若 openOrders 为空或失败，至少覆盖当前持仓币种
        if not symbols_to_cancel:
            try:
                positions = self.get_positions()
                symbols_to_cancel.update(pos.symbol for pos in positions if pos.symbol)
            except Exception as e:
                log.warning(f"获取持仓列表失败: {e}")

        for sym in symbols_to_cancel:
            try:
                result = self._request_with_retry(
                    "DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym}
                )
                if result.get("code") == 200:
                    cancelled += 1
            except Exception as e:
                log.warning(f"取消 {sym} 挂单失败: {e}")
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

    def get_open_orders(self, symbol: Optional[str] = None) -> List[dict]:
        """
        获取未完成订单列表（普通订单）。

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

    def get_open_algo_orders(self, symbol: Optional[str] = None) -> List[dict]:
        """
        获取未完成 Algo 条件单列表。

        参数:
            symbol: 交易对符号。若为 None，则获取所有 Algo 挂单。

        返回:
            Algo 挂单列表（原始 dict）
        """
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._request_with_retry("GET", "/fapi/v1/openAlgoOrders", params)
        if isinstance(data, dict):
            return data.get("orders", [])
        return data if isinstance(data, list) else []

    def cancel_algo_order(self, symbol: str, algo_id: int) -> dict:
        """
        取消指定 Algo 条件单。

        参数:
            symbol: 交易对符号
            algo_id: Algo 订单 ID

        返回:
            Binance 原始响应
        """
        params = {"symbol": symbol, "algoId": algo_id}
        return self._request_with_retry("DELETE", "/fapi/v1/algoOrder", params)

    def cancel_all_algo_orders(self, symbol: str) -> int:
        """
        取消指定币种的所有 Algo 条件单。

        参数:
            symbol: 交易对符号

        返回:
            取消的订单数量
        """
        data = self._request_with_retry(
            "DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}
        )
        return 1 if data.get("code") == 200 or data.get("msg") else 0

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
