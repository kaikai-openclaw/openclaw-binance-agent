"""News data for Binance crypto assets.

Binance doesn't have a public news API, so we fall back to yfinance Search
for crypto-related news, which works well for major tokens.
"""

from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Annotated

try:
    import yfinance as yf
    from .stockstats_utils import yf_retry
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False


def get_news(
    ticker: Annotated[str, "ticker symbol, e.g. BTC-USD, ETHUSDT"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get crypto news using yfinance Search as a backend."""
    if not _HAS_YFINANCE:
        return f"News fetching requires yfinance. No news available for {ticker}."

    # Normalize ticker for yfinance search (BTC-USD works, BTCUSDT doesn't)
    search_term = _normalize_for_search(ticker)

    try:
        search = yf_retry(lambda: yf.Search(
            query=search_term,
            news_count=20,
            enable_fuzzy_query=True,
        ))

        if not search.news:
            return f"No news found for {ticker}"

        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        news_str = ""
        count = 0
        for article in search.news:
            data = _extract(article)
            if data["pub_date"]:
                pub_naive = data["pub_date"].replace(tzinfo=None)
                if not (start_dt <= pub_naive <= end_dt + relativedelta(days=1)):
                    continue
            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            count += 1

        if count == 0:
            return f"No news found for {ticker} between {start_date} and {end_date}"

        return f"## {ticker} Crypto News, from {start_date} to {end_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching news for {ticker}: {str(e)}"


def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: int = 7,
    limit: int = 10,
) -> str:
    """Get global crypto market news."""
    if not _HAS_YFINANCE:
        return "News fetching requires yfinance."

    queries = [
        "cryptocurrency market",
        "bitcoin ethereum crypto",
        "DeFi blockchain trading",
    ]
    all_news = []
    seen = set()

    try:
        for q in queries:
            search = yf_retry(lambda query=q: yf.Search(
                query=query, news_count=limit, enable_fuzzy_query=True,
            ))
            if search.news:
                for article in search.news:
                    data = _extract(article)
                    if data["title"] and data["title"] not in seen:
                        seen.add(data["title"])
                        all_news.append(data)
            if len(all_news) >= limit:
                break

        if not all_news:
            return f"No global crypto news found for {curr_date}"

        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_date = (curr_dt - relativedelta(days=look_back_days)).strftime("%Y-%m-%d")

        news_str = ""
        for data in all_news[:limit]:
            if data.get("pub_date"):
                pub_naive = data["pub_date"].replace(tzinfo=None) if hasattr(data["pub_date"], "replace") else data["pub_date"]
                if pub_naive > curr_dt + relativedelta(days=1):
                    continue
            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"

        return f"## Global Crypto News, from {start_date} to {curr_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching global crypto news: {str(e)}"


def _normalize_for_search(ticker: str) -> str:
    """Convert BTCUSDT / BTC-USD style to a search-friendly term."""
    s = ticker.upper().replace("-", "")
    for suffix in ("USDT", "USD", "BUSD"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Map common symbols to full names for better search results
    names = {
        "BTC": "Bitcoin BTC",
        "ETH": "Ethereum ETH",
        "SOL": "Solana SOL",
        "BNB": "BNB Binance",
        "XRP": "Ripple XRP",
        "ADA": "Cardano ADA",
        "DOGE": "Dogecoin DOGE",
        "DOT": "Polkadot DOT",
        "AVAX": "Avalanche AVAX",
        "MATIC": "Polygon MATIC",
    }
    return names.get(s, f"{s} crypto")


def _extract(article: dict) -> dict:
    """Extract article data from yfinance news format."""
    if "content" in article:
        c = article["content"]
        url_obj = c.get("canonicalUrl") or c.get("clickThroughUrl") or {}
        pub_date = None
        if c.get("pubDate"):
            try:
                pub_date = datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        return {
            "title": c.get("title", ""),
            "summary": c.get("summary", ""),
            "publisher": c.get("provider", {}).get("displayName", "Unknown"),
            "link": url_obj.get("url", ""),
            "pub_date": pub_date,
        }
    return {
        "title": article.get("title", ""),
        "summary": article.get("summary", ""),
        "publisher": article.get("publisher", "Unknown"),
        "link": article.get("link", ""),
        "pub_date": None,
    }
