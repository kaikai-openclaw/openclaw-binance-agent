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
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve technical indicators for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): Technical indicator to get the analysis and report of
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the specified ticker symbol and indicator.
    """
    # Handle comma-separated indicators (some models pass multiple at once)
    if "," in indicator:
        results = []
        for ind in indicator.split(","):
            ind = ind.strip()
            if ind:
                results.append(_safe_route("get_indicators", symbol, ind, curr_date, look_back_days))
        return "\n\n".join(results)
    return _safe_route("get_indicators", symbol, indicator, curr_date, look_back_days)