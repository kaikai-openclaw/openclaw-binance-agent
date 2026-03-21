#!/usr/bin/env python3
"""
Binance Market Analyzer
Fetches klines, calculates technical indicators, generates trading signals.
"""

import sys
import json
import argparse
import os
from datetime import datetime

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────

TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

if TESTNET:
    BASE_URL = "https://testnet.binance.vision/api"
else:
    BASE_URL = "https://api.binance.com/api"

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# ── Helpers ─────────────────────────────────────────────────────────────────

def get_klines(symbol, interval="1h", limit=100):
    url = f"{BASE_URL}/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(f"{BASE_URL}/v3/klines", params=params)
    r.raise_for_status()
    return r.json()


def get_ticker(symbol):
    url = f"{BASE_URL}/v3/ticker/24hr"
    r = requests.get(url, params={"symbol": symbol})
    r.raise_for_status()
    return r.json()


def get_funding_rate(symbol):
    url = f"{BASE_URL}/v3/premiumIndex"
    r = requests.get(url, params={"symbol": symbol})
    r.raise_for_status()
    return r.json()


def calc_sma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return None, None, None
    ema = lambda data, n: sum(data[-n:]) / n  # simplified EMA
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd_line = ema_fast - ema_slow
    return macd_line, 0, 0  # signal line simplified


def analyze_symbol(symbol, interval="1h", limit=100):
    klines = get_klines(symbol, interval, limit)
    closes = [float(k[4]) for k in klines]

    price = closes[-1]
    high = max(closes)
    low = min(closes)
    volume = float(klines[-1][5])

    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)
    rsi = calc_rsi(closes)

    ticker = get_ticker(symbol)
    change = float(ticker.get("priceChangePercent", 0))
    volume_24h = float(ticker.get("quoteVolume", 0))

    # Funding rate (futures only)
    funding = None
    try:
        fr = get_funding_rate(symbol)
        funding = float(fr.get("lastFundingRate", 0)) * 100
    except Exception:
        pass

    # Signal generation
    signals = []
    if rsi and rsi < 30:
        signals.append(("RSI", "BUY", f"RSI={rsi:.1f} oversold"))
    elif rsi and rsi > 70:
        signals.append(("RSI", "SELL", f"RSI={rsi:.1f} overbought"))

    if sma20 and sma50 and price > sma20 > sma50:
        signals.append(("SMA", "BUY", "Price above golden cross"))
    elif sma20 and sma50 and price < sma20 < sma50:
        signals.append(("SMA", "SELL", "Death cross"))

    if change > 5:
        signals.append(("MOMENTUM", "SELL", f"+{change:.2f}% in 24h — overbought risk"))
    elif change < -5:
        signals.append(("MOMENTUM", "BUY", f"{change:.2f}% in 24h — oversold opportunity"))

    # Overall signal
    buy_signals = sum(1 for _, s, _ in signals if s == "BUY")
    sell_signals = sum(1 for _, s, _ in signals if s == "SELL")

    if buy_signals > sell_signals:
        overall = "🟢 BUY"
        confidence = min(buy_signals / max(sell_signals, 1) * 40 + 40, 95)
    elif sell_signals > buy_signals:
        overall = "🔴 SELL"
        confidence = min(sell_signals / max(buy_signals, 1) * 40 + 40, 95)
    else:
        overall = "🟡 HOLD"
        confidence = 50

    return {
        "symbol": symbol,
        "price": price,
        "high_24h": high,
        "low_24h": low,
        "change_24h_pct": change,
        "volume_24h": volume_24h,
        "interval": interval,
        "sma20": sma20,
        "sma50": sma50,
        "rsi": rsi,
        "funding_rate_pct": funding,
        "signals": signals,
        "overall_signal": overall,
        "confidence": confidence,
        "timestamp": datetime.now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="Binance Market Analyzer")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair, e.g. BTCUSDT")
    parser.add_argument("--interval", default="1h", help="Kline interval: 1m, 5m, 15m, 1h, 4h, 1d")
    parser.add_argument("--limit", type=int, default=100, help="Number of candles")
    parser.add_argument("--format", default="json", choices=["json", "md"])
    args = parser.parse_args()

    try:
        result = analyze_symbol(args.symbol.upper(), args.interval, args.limit)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

    if args.format == "md":
        r = result
        lines = [
            f"## {r['symbol']} Analysis ({r['interval']})",
            f"",
            f"**Price:** ${r['price']:,.2f}  |  **24h Change:** {r['change_24h_pct']:+.2f}%",
            f"**High:** ${r['high_24h']:,.2f}  |  **Low:** ${r['low_24h']:,.2f}",
            f"**Volume 24h:** ${r['volume_24h']:,.0f}",
            f"",
            f"### Indicators",
            f"- RSI(14): {r['rsi']:.1f}" if r['rsi'] else "- RSI(14): N/A",
            f"- SMA(20): ${r['sma20']:,.2f}" if r['sma20'] else "- SMA(20): N/A",
            f"- SMA(50): ${r['sma50']:,.2f}" if r['sma50'] else "- SMA(50): N/A",
            f"- Funding Rate: {r['funding_rate_pct']:+.4f}% / 8h" if r['funding_rate_pct'] else "- Funding Rate: N/A (spot)",
            f"",
            f"### Signals",
        ]
        for name, direction, detail in r["signals"]:
            lines.append(f"- **{direction}** ({name}): {detail}")
        lines.append("")
        lines.append(f"### Overall: {r['overall_signal']}  |  Confidence: {r['confidence']:.0f}%")
        print("\n".join(lines))
    else:
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
