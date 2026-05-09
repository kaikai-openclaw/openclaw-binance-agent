from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor
import logging

_logger = logging.getLogger(__name__)


def _safe_route(method: str, *args, **kwargs) -> str:
    """route_to_vendor 的安全包装：异常时返回降级文本而非抛出。"""
    try:
        return route_to_vendor(method, *args, **kwargs)
    except Exception as e:
        _logger.warning("数据获取降级 [%s]: %s", method, e)
        return f"⚠️ {method} 数据获取失败（{type(e).__name__}），本次分析缺少该数据，请基于其他可用信息做出判断。"


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    return _safe_route("get_stock_data", symbol, start_date, end_date)
