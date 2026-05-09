"""AKShare A股基本面数据获取模块。

通过 AKShare 获取A股上市公司的财务数据，包括：
- 个股基本信息（stock_individual_info_em）
- 财务分析主要指标（stock_financial_analysis_indicator_em）
- 资产负债表（stock_balance_sheet_by_report_em → 新浪 fallback）
- 利润表（stock_profit_sheet_by_report_em → 新浪 fallback）
- 现金流量表（stock_cash_flow_sheet_by_report_em → 新浪 fallback）

主数据源为东方财富，三大报表在东方财富失败时自动降级到新浪。
"""

import logging
import pandas as pd
from datetime import datetime
from typing import Annotated
from .akshare_stock import (
    _ensure_akshare,
    _symbol_to_akshare,
    _symbol_to_em,
    _akshare_retry,
    AKShareError,
    AKShareInvalidSymbolError,
)

logger = logging.getLogger(__name__)


def get_fundamentals(
    ticker: Annotated[str, "A股股票代码，如 600519、000001"],
    curr_date: Annotated[str, "当前日期（用于过滤未来数据）"] = None,
) -> str:
    """获取A股个股基本面综合数据。

    合并个股信息（stock_individual_info_em）和财务分析主要指标
    （stock_financial_analysis_indicator_em）两个接口的数据。
    """
    ak = _ensure_akshare()
    code = _symbol_to_akshare(ticker)
    em_code = _symbol_to_em(ticker)

    try:
        # 获取个股基本信息
        info_df = _akshare_retry(lambda: ak.stock_individual_info_em(symbol=code))
        info_lines = []
        if info_df is not None and not info_df.empty:
            for _, row in info_df.iterrows():
                info_lines.append(f"{row.get('item', '')}: {row.get('value', '')}")

        # 获取财务分析主要指标（最近几期）
        fin_df = _akshare_retry(
            lambda: ak.stock_financial_analysis_indicator_em(symbol=em_code, indicator="按报告期")
        )
        fin_lines = []
        if fin_df is not None and not fin_df.empty:
            # 按报告日期过滤，防止前瞻偏差
            if curr_date and "REPORT_DATE" in fin_df.columns:
                fin_df["REPORT_DATE"] = fin_df["REPORT_DATE"].astype(str)
                fin_df = fin_df[fin_df["REPORT_DATE"] <= curr_date]

            # 只取最近3期
            recent = fin_df.head(3)
            # 选取关键财务指标列
            key_cols = [
                "REPORT_DATE", "SECURITY_NAME_ABBR",
                "EPSJB",       # 基本每股收益
                "EPSKCJB",     # 扣非每股收益
                "MGJZC",       # 每股净资产
                "MGGJJ",       # 每股公积金
                "XSMLL",       # 销售毛利率
                "XSJLL",       # 销售净利率
                "JZCSYL",      # 净资产收益率
                "ZZCJLL",      # 总资产净利率
                "ZCFZL",       # 资产负债率
                "LDBL",        # 流动比率
                "SDBL",        # 速动比率
            ]
            available_cols = [c for c in key_cols if c in recent.columns]
            if available_cols:
                for _, row in recent.iterrows():
                    period = row.get("REPORT_DATE", "未知")
                    fin_lines.append(f"\n--- 报告期: {period} ---")
                    col_names = {
                        "SECURITY_NAME_ABBR": "股票简称",
                        "EPSJB": "基本每股收益(元)",
                        "EPSKCJB": "扣非每股收益(元)",
                        "MGJZC": "每股净资产(元)",
                        "MGGJJ": "每股公积金(元)",
                        "XSMLL": "销售毛利率(%)",
                        "XSJLL": "销售净利率(%)",
                        "JZCSYL": "净资产收益率(%)",
                        "ZZCJLL": "总资产净利率(%)",
                        "ZCFZL": "资产负债率(%)",
                        "LDBL": "流动比率",
                        "SDBL": "速动比率",
                    }
                    for col in available_cols:
                        if col == "REPORT_DATE":
                            continue
                        label = col_names.get(col, col)
                        val = row.get(col)
                        if val is not None:
                            fin_lines.append(f"  {label}: {val}")

        header = f"# A股基本面数据: {code}\n"
        header += f"# 数据获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        result = header
        if info_lines:
            result += "## 个股基本信息\n" + "\n".join(info_lines) + "\n\n"
        if fin_lines:
            result += "## 财务分析主要指标\n" + "\n".join(fin_lines) + "\n"

        if not info_lines and not fin_lines:
            result += f"未找到 {code} 的基本面数据\n"

        return result

    except AKShareInvalidSymbolError:
        raise
    except Exception as e:
        return f"获取 {ticker} 基本面数据出错: {str(e)}"


def _fetch_financial_sheet(ak, em_code: str, sheet_fn_name: str, curr_date: str = None) -> str:
    """通用财务报表获取函数（东方财富）。

    根据 sheet_fn_name 调用对应的 AKShare 接口，
    并按报告日期过滤以防止前瞻偏差。
    返回 None 表示失败，需要 fallback。
    """
    fn = getattr(ak, sheet_fn_name, None)
    if fn is None:
        return None

    try:
        df = _akshare_retry(lambda: fn(symbol=em_code))
    except Exception as e:
        logger.warning("东方财富 %s(%s) 失败: %s", sheet_fn_name, em_code, e)
        return None

    if df is None or df.empty:
        return None

    # 按报告日期过滤，防止前瞻偏差
    if curr_date and "REPORT_DATE" in df.columns:
        df["REPORT_DATE"] = df["REPORT_DATE"].astype(str)
        df = df[df["REPORT_DATE"] <= curr_date]

    if df.empty:
        return f"在 {curr_date} 之前未找到 {em_code} 的财务报表数据"

    # 只取最近4期报告
    recent = df.head(4)
    return recent.to_string(max_cols=20, max_colwidth=20)


# ── 新浪财务报表 fallback ────────────────────────────────

# 东方财富接口名 → 新浪 stock_financial_report_sina 的 symbol 参数
_EM_TO_SINA_SHEET = {
    "stock_balance_sheet_by_report_em": "资产负债表",
    "stock_profit_sheet_by_report_em": "利润表",
    "stock_cash_flow_sheet_by_report_em": "现金流量表",
}


def _fetch_financial_sheet_sina(ak, code: str, sheet_fn_name: str, curr_date: str = None) -> str:
    """新浪财务报表 fallback。

    使用 ak.stock_financial_report_sina(stock=code, symbol=报表类型)。
    新浪接口比东方财富稳定，但列名为中文、无标准 REPORT_DATE 列。
    """
    sina_symbol = _EM_TO_SINA_SHEET.get(sheet_fn_name)
    if not sina_symbol:
        return None

    try:
        df = _akshare_retry(
            lambda: ak.stock_financial_report_sina(stock=code, symbol=sina_symbol)
        )
    except Exception as e:
        logger.warning("新浪 %s(%s) 也失败: %s", sina_symbol, code, e)
        return None

    if df is None or df.empty:
        return None

    # 新浪报表第一列通常是 "报告日"，格式如 "20250930"
    date_col = "报告日"
    if curr_date and date_col in df.columns:
        curr_compact = curr_date.replace("-", "")
        df[date_col] = df[date_col].astype(str)
        df = df[df[date_col] <= curr_compact]

    if df.empty:
        return None

    recent = df.head(4)
    return f"[数据源: 新浪]\n{recent.to_string(max_cols=20, max_colwidth=20)}"


def _fetch_sheet_with_fallback(ticker: str, sheet_fn_name: str, curr_date: str = None) -> str:
    """获取财务报表，东方财富优先，失败自动降级到新浪。"""
    ak = _ensure_akshare()
    code = _symbol_to_akshare(ticker)
    em_code = _symbol_to_em(ticker)

    # 优先东方财富
    result = _fetch_financial_sheet(ak, em_code, sheet_fn_name, curr_date)
    if result is not None:
        return result

    # fallback 新浪
    logger.info("东方财富 %s 失败，降级到新浪 (%s)", sheet_fn_name, code)
    result = _fetch_financial_sheet_sina(ak, code, sheet_fn_name, curr_date)
    if result is not None:
        return result

    return f"⚠️ {code} 财务报表获取失败（东方财富和新浪均不可用），本次分析缺少该报表数据。"


def get_balance_sheet(
    ticker: Annotated[str, "A股股票代码"],
    freq: Annotated[str, "报告频率: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "当前日期"] = None,
) -> str:
    """获取A股资产负债表数据。

    优先东方财富 stock_balance_sheet_by_report_em，失败降级到新浪。
    """
    header = f"# A股资产负债表: {ticker}\n"
    header += f"# 数据获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + _fetch_sheet_with_fallback(ticker, "stock_balance_sheet_by_report_em", curr_date)


def get_cashflow(
    ticker: Annotated[str, "A股股票代码"],
    freq: Annotated[str, "报告频率: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "当前日期"] = None,
) -> str:
    """获取A股现金流量表数据。

    优先东方财富 stock_cash_flow_sheet_by_report_em，失败降级到新浪。
    """
    header = f"# A股现金流量表: {ticker}\n"
    header += f"# 数据获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + _fetch_sheet_with_fallback(ticker, "stock_cash_flow_sheet_by_report_em", curr_date)


def get_income_statement(
    ticker: Annotated[str, "A股股票代码"],
    freq: Annotated[str, "报告频率: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "当前日期"] = None,
) -> str:
    """获取A股利润表数据。

    优先东方财富 stock_profit_sheet_by_report_em，失败降级到新浪。
    """
    header = f"# A股利润表: {ticker}\n"
    header += f"# 数据获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + _fetch_sheet_with_fallback(ticker, "stock_profit_sheet_by_report_em", curr_date)


def get_insider_transactions(
    ticker: Annotated[str, "A股股票代码"],
) -> str:
    """获取A股股东增减持数据。

    A股没有美股式的 insider transactions，但可以通过大股东增减持来替代。
    这里返回提示信息，建议关注大股东变动和股东户数变化。
    """
    code = _symbol_to_akshare(ticker)
    return (
        f"# {code} 内部交易/大股东变动\n\n"
        "A股市场没有美股式的内部人交易披露制度。\n"
        "建议关注以下替代信息：\n"
        "1. 大股东增减持公告\n"
        "2. 股东户数变化趋势\n"
        "3. 高管持股变动\n"
        "4. 限售股解禁计划\n"
    )
