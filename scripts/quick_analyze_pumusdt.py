#!/usr/bin/env python3
import os, sys, json
os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'
os.chdir("/Users/zengkai/MyProjects/MyTradingAgents/openclaw-binance-agent")
import requests

symbol = "PUMPUSDT"

r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10)
ticker = next((t for t in r.json() if t['symbol'] == symbol), None)
last_price = float(ticker['lastPrice'])
price_chg = float(ticker['priceChangePercent'])
quote_vol = float(ticker['quoteVolume'])
high24 = float(ticker['highPrice'])
low24 = float(ticker['lowPrice'])
amplitude = (high24 - low24) / low24 * 100

print(f"📊 {symbol} 市场数据")
print(f"  价格: ${last_price:.6f}")
print(f"  24h涨跌: {price_chg:+.2f}%")
print(f"  24h成交额: ${quote_vol/1e6:.2f}M")
print(f"  24h高: ${high24:.6f} / 低: ${low24:.6f}")
print(f"  振幅: {amplitude:.2f}%")
print()

def calc_rsi(prices, period=14):
    deltas = [prices[i]-prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains)/period
    avg_loss = sum(losses)/period
    if avg_loss == 0: return 100
    return 100 - 100/(1+avg_gain/avg_loss)

def calc_ema(prices, period=20):
    if len(prices) < period: return prices[-1]
    k = 2/(period+1)
    ema = sum(prices[:period])/period
    for p in prices[period:]:
        ema = p*k + ema*(1-k)
    return ema

def analyze(klines, label):
    closes = [float(k[4]) for k in klines]
    cp = closes[-1]
    ema20 = calc_ema(closes, 20)
    ema60 = calc_ema(closes, 60)
    rsi = calc_rsi(closes)
    print(f"  [{label}] RSI:{rsi:.1f} | EMA20:${ema20:.4f} {'✓' if cp>ema20 else '✗'} | EMA60:${ema60:.4f} {'✓' if cp>ema60 else '✗'}")

kr1 = requests.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=100", timeout=10)
kr4 = requests.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=4h&limit=50", timeout=10)
kr1d = requests.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1d&limit=30", timeout=10)
analyze(kr1.json(), "1h")
analyze(kr4.json(), "4h")
analyze(kr1d.json(), "1d")
