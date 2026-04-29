"""
Binance_Fapi_Client 单元测试模块

测试 BinanceFapiClient 的核心功能：
- 指数退避序列计算
- HTTP 429/418 处理
- 重试耗尽异常
- 各 API 方法的正确解析
- Rate_Limiter 集成
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from src.infra.binance_fapi import (
    BinanceFapiClient,
    IPBannedError,
    MaxRetryExceededError,
    OrderResult,
    PositionInfo,
    AccountInfo,
    PositionRisk,
    calculate_backoff,
)
from src.infra.rate_limiter import RateLimiter


# ============================================================
# calculate_backoff 测试
# ============================================================

class TestCalculateBackoff:
    """指数退避序列计算测试。"""

    def test_backoff_sequence_values(self):
        """退避序列应为 [1, 2, 4, 8, 16]"""
        assert calculate_backoff(0) == 1
        assert calculate_backoff(1) == 2
        assert calculate_backoff(2) == 4
        assert calculate_backoff(3) == 8
        assert calculate_backoff(4) == 16

    def test_backoff_beyond_sequence_uses_last(self):
        """超出序列长度时使用最后一个值 16"""
        assert calculate_backoff(5) == 16
        assert calculate_backoff(100) == 16


# ============================================================
# BinanceFapiClient 初始化测试
# ============================================================

class TestBinanceFapiClientInit:
    """客户端初始化测试。"""

    def test_default_init(self):
        """默认初始化参数正确"""
        client = BinanceFapiClient(api_key="test_key", api_secret="test_secret")
        assert client.api_key == "test_key"
        assert client.api_secret == "test_secret"
        assert client.base_url == "https://fapi.binance.com"
        assert isinstance(client.rate_limiter, RateLimiter)
        assert client.REQUEST_TIMEOUT == 10
        assert client.MAX_RETRIES == 5
        assert client.BACKOFF_SEQUENCE == [1, 2, 4, 8, 16]

    def test_custom_rate_limiter(self):
        """可注入自定义 RateLimiter"""
        rl = RateLimiter()
        client = BinanceFapiClient("k", "s", rate_limiter=rl)
        assert client.rate_limiter is rl

    def test_base_url_trailing_slash_stripped(self):
        """base_url 尾部斜杠被去除"""
        client = BinanceFapiClient("k", "s", base_url="https://example.com/")
        assert client.base_url == "https://example.com"


# ============================================================
# _request_with_retry 核心逻辑测试
# ============================================================

class TestRequestWithRetry:
    """带重试的请求核心逻辑测试。"""

    def _make_client(self):
        """创建一个用于测试的客户端，mock 掉 rate_limiter.acquire"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)
        return client, rl

    @patch("time.sleep", return_value=None)  # 跳过实际等待
    def test_successful_request(self, mock_sleep):
        """成功请求直接返回 JSON"""
        client, rl = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"orderId": 12345}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            result = client._request_with_retry("GET", "/fapi/v1/test")

        assert result == {"orderId": 12345}
        rl.acquire.assert_called_once()

    @patch("time.sleep", return_value=None)
    def test_http_429_triggers_pause_and_retry(self, mock_sleep):
        """HTTP 429 应调用 rate_limiter.pause(30) 并重试"""
        client, rl = self._make_client()

        # 第一次返回 429，第二次返回 200
        resp_429 = MagicMock()
        resp_429.status_code = 429

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}
        resp_200.raise_for_status = MagicMock()

        with patch.object(client._session, "request", side_effect=[resp_429, resp_200]):
            result = client._request_with_retry("GET", "/fapi/v1/test")

        assert result == {"ok": True}
        rl.pause.assert_called_once_with(30)
        assert rl.acquire.call_count == 2  # 两次请求各调用一次

    @patch("time.sleep", return_value=None)
    def test_http_418_triggers_stop_and_raises(self, mock_sleep):
        """HTTP 418 应调用 rate_limiter.stop() 并抛出 IPBannedError"""
        client, rl = self._make_client()

        resp_418 = MagicMock()
        resp_418.status_code = 418

        with patch.object(client._session, "request", return_value=resp_418):
            with pytest.raises(IPBannedError, match="IP 被 Binance 封禁"):
                client._request_with_retry("GET", "/fapi/v1/test")

        rl.stop.assert_called_once()

    @patch("time.sleep", return_value=None)
    def test_http_418_not_retried(self, mock_sleep):
        """HTTP 418 不应重试，直接抛出"""
        client, rl = self._make_client()

        resp_418 = MagicMock()
        resp_418.status_code = 418

        with patch.object(client._session, "request", return_value=resp_418) as mock_req:
            with pytest.raises(IPBannedError):
                client._request_with_retry("GET", "/fapi/v1/test")

        # 只调用了一次请求
        assert mock_req.call_count == 1

    @patch("time.sleep", return_value=None)
    def test_timeout_retries_with_backoff(self, mock_sleep):
        """超时异常应按指数退避重试"""
        client, rl = self._make_client()

        # 前两次超时，第三次成功
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"data": "ok"}
        resp_ok.raise_for_status = MagicMock()

        with patch.object(
            client._session, "request",
            side_effect=[
                requests.exceptions.Timeout("timeout"),
                requests.exceptions.Timeout("timeout"),
                resp_ok,
            ],
        ):
            result = client._request_with_retry("GET", "/fapi/v1/test")

        assert result == {"data": "ok"}
        # 验证退避等待：第 0 次重试等 1s，第 1 次重试等 2s
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)

    @patch("time.sleep", return_value=None)
    def test_max_retries_exceeded_raises(self, mock_sleep):
        """重试 5 次后仍失败应抛出 MaxRetryExceededError"""
        client, rl = self._make_client()

        with patch.object(
            client._session, "request",
            side_effect=requests.exceptions.Timeout("timeout"),
        ):
            with pytest.raises(MaxRetryExceededError, match="重试 5 次后仍失败"):
                client._request_with_retry("GET", "/fapi/v1/test")

        # 应该尝试了 5 次
        assert rl.acquire.call_count == 5

    @patch("time.sleep", return_value=None)
    def test_connection_error_retries(self, mock_sleep):
        """ConnectionError 也应触发重试"""
        client, rl = self._make_client()

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"ok": True}
        resp_ok.raise_for_status = MagicMock()

        with patch.object(
            client._session, "request",
            side_effect=[
                requests.exceptions.ConnectionError("conn err"),
                resp_ok,
            ],
        ):
            result = client._request_with_retry("GET", "/fapi/v1/test")

        assert result == {"ok": True}


# ============================================================
# 公开 API 方法测试
# ============================================================

class TestSetLeverage:
    """set_leverage 测试。"""

    @patch("time.sleep", return_value=None)
    def test_set_leverage_posts_symbol_and_leverage(self, mock_sleep):
        """设置杠杆应调用 Binance leverage 端点。"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "symbol": "BTCUSDT",
            "leverage": 10,
            "maxNotionalValue": "1000000",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp) as req:
            result = client.set_leverage("BTCUSDT", 10)

        assert result["symbol"] == "BTCUSDT"
        assert result["leverage"] == 10
        _, kwargs = req.call_args
        assert kwargs["method"] == "POST"
        assert kwargs["url"].endswith("/fapi/v1/leverage")
        assert kwargs["data"]["symbol"] == "BTCUSDT"
        assert kwargs["data"]["leverage"] == 10


class TestPlaceLimitOrder:
    """place_limit_order 测试。"""

    @patch("time.sleep", return_value=None)
    def test_place_limit_order_returns_order_result(self, mock_sleep):
        """限价订单返回正确的 OrderResult"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "orderId": 99001,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "50000.00",
            "origQty": "0.1",
            "status": "NEW",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            result = client.place_limit_order("BTCUSDT", "BUY", 50000.0, 0.1)

        assert isinstance(result, OrderResult)
        assert result.order_id == "99001"
        assert result.symbol == "BTCUSDT"
        assert result.side == "BUY"
        assert result.price == 50000.0
        assert result.quantity == 0.1
        assert result.status == "NEW"


class TestPlaceMarketOrder:
    """place_market_order 测试。"""

    @patch("time.sleep", return_value=None)
    def test_place_market_order_returns_order_result(self, mock_sleep):
        """市价订单返回正确的 OrderResult"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "orderId": 99002,
            "symbol": "ETHUSDT",
            "side": "SELL",
            "avgPrice": "3000.50",
            "origQty": "1.0",
            "status": "FILLED",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            result = client.place_market_order("ETHUSDT", "SELL", 1.0)

        assert isinstance(result, OrderResult)
        assert result.order_id == "99002"
        assert result.price == 3000.50
        assert result.status == "FILLED"


class TestGetPositions:
    """get_positions 测试。"""

    @patch("time.sleep", return_value=None)
    def test_get_positions_filters_zero_amt(self, mock_sleep):
        """get_positions 应过滤掉 positionAmt 为 0 的持仓"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "50000",
             "unRealizedProfit": "100", "leverage": "10"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "3000",
             "unRealizedProfit": "0", "leverage": "5"},
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            positions = client.get_positions()

        assert len(positions) == 1
        assert isinstance(positions[0], PositionInfo)
        assert positions[0].symbol == "BTCUSDT"
        assert positions[0].position_amt == 0.5


class TestGetAccountInfo:
    """get_account_info 测试。"""

    @patch("time.sleep", return_value=None)
    def test_get_account_info_returns_account_info(self, mock_sleep):
        """get_account_info 返回正确的 AccountInfo"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "totalWalletBalance": "10000.00",
            "availableBalance": "5000.00",
            "totalUnrealizedProfit": "200.50",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            info = client.get_account_info()

        assert isinstance(info, AccountInfo)
        assert info.total_balance == 10000.0
        assert info.available_balance == 5000.0
        assert info.total_unrealized_pnl == 200.50


class TestCancelAllOrders:
    """cancel_all_orders 测试。"""

    @patch("time.sleep", return_value=None)
    def test_cancel_all_orders_with_symbol(self, mock_sleep):
        """指定 symbol 取消挂单"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 200, "msg": "The operation of cancel all open order is done."}
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            count = client.cancel_all_orders("BTCUSDT")

        assert count == 1

    @patch("time.sleep", return_value=None)
    def test_cancel_all_orders_without_symbol_uses_open_orders(self, mock_sleep):
        """未指定 symbol 时，应按 open orders 的 symbol 全量取消。"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        open_orders = [
            {"orderId": 1, "symbol": "BTCUSDT", "status": "NEW"},
            {"orderId": 2, "symbol": "ETHUSDT", "status": "NEW"},
            {"orderId": 3, "symbol": "BTCUSDT", "status": "PARTIALLY_FILLED"},
        ]

        def _request_side_effect(*args, **kwargs):
            method = kwargs.get("method")
            path = kwargs.get("url", "")
            if method == "GET" and path.endswith("/fapi/v1/openOrders"):
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = open_orders
                resp.raise_for_status = MagicMock()
                return resp
            if method == "DELETE" and path.endswith("/fapi/v1/allOpenOrders"):
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"code": 200, "msg": "ok"}
                resp.raise_for_status = MagicMock()
                return resp
            raise AssertionError("unexpected request")

        with patch.object(client._session, "request", side_effect=_request_side_effect):
            count = client.cancel_all_orders()

        assert count == 2


class TestGetPositionRisk:
    """get_position_risk 测试。"""

    @patch("time.sleep", return_value=None)
    def test_get_position_risk_returns_position_risk(self, mock_sleep):
        """get_position_risk 返回正确的 PositionRisk"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.5",
                "entryPrice": "50000",
                "markPrice": "51000",
                "unRealizedProfit": "500",
                "liquidationPrice": "45000",
                "leverage": "10",
            }
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            risk = client.get_position_risk("BTCUSDT")

        assert isinstance(risk, PositionRisk)
        assert risk.symbol == "BTCUSDT"
        assert risk.position_amt == 0.5
        assert risk.mark_price == 51000.0
        assert risk.liquidation_price == 45000.0
        assert risk.leverage == 10


# ============================================================
# get_open_orders 测试
# ============================================================

class TestGetOpenOrders:
    """get_open_orders 测试。"""

    @patch("time.sleep", return_value=None)
    def test_get_open_orders_returns_list(self, mock_sleep):
        """get_open_orders 应返回未完成订单列表"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"orderId": 1001, "symbol": "BTCUSDT", "status": "NEW"},
            {"orderId": 1002, "symbol": "ETHUSDT", "status": "PARTIALLY_FILLED"},
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            orders = client.get_open_orders()

        assert len(orders) == 2
        assert orders[0]["orderId"] == 1001

    @patch("time.sleep", return_value=None)
    def test_get_open_orders_with_symbol(self, mock_sleep):
        """指定 symbol 时应传递参数"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"orderId": 2001, "symbol": "BTCUSDT", "status": "NEW"},
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=mock_resp):
            orders = client.get_open_orders(symbol="BTCUSDT")

        assert len(orders) == 1


# ============================================================
# sync_after_reconnect 测试（需求 7.7）
# ============================================================

class TestSyncAfterReconnect:
    """网络恢复后自动同步测试。"""

    @patch("time.sleep", return_value=None)
    def test_sync_returns_account_positions_orders(self, mock_sleep):
        """sync_after_reconnect 应返回账户、持仓和未完成订单"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        # 模拟三次 API 调用的响应
        account_resp = MagicMock()
        account_resp.status_code = 200
        account_resp.json.return_value = {
            "totalWalletBalance": "10000",
            "availableBalance": "8000",
            "totalUnrealizedProfit": "200",
        }
        account_resp.raise_for_status = MagicMock()

        positions_resp = MagicMock()
        positions_resp.status_code = 200
        positions_resp.json.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "50000",
             "unRealizedProfit": "200", "leverage": "10"},
        ]
        positions_resp.raise_for_status = MagicMock()

        orders_resp = MagicMock()
        orders_resp.status_code = 200
        orders_resp.json.return_value = [
            {"orderId": 3001, "symbol": "BTCUSDT", "status": "NEW"},
        ]
        orders_resp.raise_for_status = MagicMock()

        with patch.object(
            client._session, "request",
            side_effect=[account_resp, positions_resp, orders_resp],
        ):
            result = client.sync_after_reconnect()

        assert isinstance(result["account"], AccountInfo)
        assert result["account"].total_balance == 10000.0
        assert len(result["positions"]) == 1
        assert result["positions"][0].symbol == "BTCUSDT"
        assert len(result["open_orders"]) == 1

    @patch("time.sleep", return_value=None)
    def test_network_recovery_flag(self, mock_sleep):
        """网络故障后恢复时 _network_was_down 标记应正确切换"""
        rl = MagicMock(spec=RateLimiter)
        client = BinanceFapiClient("key", "secret", rate_limiter=rl)

        # 初始状态：无网络故障
        assert client._network_was_down is False

        # 模拟：第一次超时（标记故障），第二次成功（恢复）
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"ok": True}
        resp_ok.raise_for_status = MagicMock()

        with patch.object(
            client._session, "request",
            side_effect=[
                requests.exceptions.Timeout("timeout"),
                resp_ok,
            ],
        ):
            result = client._request_with_retry("GET", "/fapi/v1/test")

        # 恢复后标记应重置为 False
        assert client._network_was_down is False
        assert result == {"ok": True}
