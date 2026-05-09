"""AKShare A股新闻数据获取模块。

通过 AKShare 获取A股个股新闻和全球财经新闻：
- 个股新闻: stock_news_em（东方财富数据源）
- 全球新闻: 使用财经内容精选接口

注意: AKShare 的 stock_news_em 接口返回的是最近100条新闻，
不支持按日期范围精确筛选，因此这里在获取后进行本地日期过滤。
"""

from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Annotated
import logging

from .akshare_stock import (
    _ensure_akshare,
    _symbol_to_akshare,
    _akshare_retry,
    AKShareError,
    AKShareInvalidSymbolError,
)

logger = logging.getLogger(__name__)


def get_news(
    ticker: Annotated[str, "A股股票代码，如 600519、000001"],
    start_date: Annotated[str, "开始日期，格式 yyyy-mm-dd"],
    end_date: Annotated[str, "结束日期，格式 yyyy-mm-dd"],
) -> str:
    """获取A股个股新闻资讯。

    使用 ak.stock_news_em 接口，数据源为东方财富。
    返回指定日期范围内的新闻标题、内容摘要和来源。
    """
    ak = _ensure_akshare()
    code = _symbol_to_akshare(ticker)

    try:
        news_df = _akshare_retry(lambda: ak.stock_news_em(symbol=code))

        if news_df is None or news_df.empty:
            return f"未找到 {ticker} 的相关新闻"

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        news_str = ""
        count = 0
        for _, row in news_df.iterrows():
            title = row.get("新闻标题", "")
            content = row.get("新闻内容", "")
            pub_time = row.get("发布时间", "")
            source = row.get("文章来源", "未知")
            link = row.get("新闻链接", "")

            # 按日期过滤
            if pub_time:
                try:
                    pub_dt = datetime.strptime(str(pub_time)[:10], "%Y-%m-%d")
                    if not (start_dt <= pub_dt <= end_dt + relativedelta(days=1)):
                        continue
                except (ValueError, TypeError):
                    pass  # 日期解析失败则不过滤

            news_str += f"### {title}（来源: {source}）\n"
            if content:
                # 截取摘要，避免内容过长
                summary = content[:200] + "..." if len(str(content)) > 200 else content
                news_str += f"{summary}\n"
            if link:
                news_str += f"链接: {link}\n"
            news_str += "\n"
            count += 1

        if count == 0:
            return f"在 {start_date} 至 {end_date} 期间未找到 {ticker} 的新闻"

        return f"## {code} A股新闻资讯，{start_date} 至 {end_date}:\n\n{news_str}"

    except AKShareInvalidSymbolError:
        raise
    except Exception as e:
        return f"获取 {ticker} 新闻数据出错: {str(e)}"


def get_global_news(
    curr_date: Annotated[str, "当前日期，格式 yyyy-mm-dd"],
    look_back_days: int = 7,
    limit: int = 10,
) -> str:
    """获取全球/中国宏观财经新闻。

    尝试使用 AKShare 的财经新闻接口获取最新财经资讯。
    如果接口不可用，返回提示信息。
    """
    ak = _ensure_akshare()

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (curr_dt - relativedelta(days=look_back_days)).strftime("%Y-%m-%d")

    # 尝试多个新闻接口
    news_items = []

    # 方式1: 尝试获取东方财富全球财经快讯
    try:
        df = _akshare_retry(lambda: ak.stock_news_em(symbol="财经"))
        if df is not None and not df.empty:
            for _, row in df.head(limit).iterrows():
                title = row.get("新闻标题", "")
                content = row.get("新闻内容", "")
                source = row.get("文章来源", "未知")
                link = row.get("新闻链接", "")
                pub_time = row.get("发布时间", "")

                if title:
                    news_items.append({
                        "title": title,
                        "content": content,
                        "source": source,
                        "link": link,
                        "pub_time": pub_time,
                    })
    except Exception as e:
        logger.info(f"stock_news_em(财经) 获取失败: {e}")

    if not news_items:
        return f"在 {start_date} 至 {curr_date} 期间未找到全球财经新闻"

    news_str = ""
    for item in news_items[:limit]:
        news_str += f"### {item['title']}（来源: {item['source']}）\n"
        if item["content"]:
            summary = str(item["content"])[:200]
            if len(str(item["content"])) > 200:
                summary += "..."
            news_str += f"{summary}\n"
        if item["link"]:
            news_str += f"链接: {item['link']}\n"
        news_str += "\n"

    return f"## 全球财经新闻，{start_date} 至 {curr_date}:\n\n{news_str}"
