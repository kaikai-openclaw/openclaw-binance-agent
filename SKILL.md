---
name: binance-agent
description: "OpenClaw AI Agent for Binance — market analysis, trading signals, portfolio monitoring, and automated trade execution via Binance API. Use when user wants AI-powered crypto trading insights, portfolio check, or trade execution on Binance."
---

# Binance AI Agent & Skill

OpenClaw AI Agent for Binance — combining market analysis, trading signals, and automated execution.

## Features

- **Market Analysis** — Technical indicators, price action, funding rate, open interest
- **Trading Signals** — AI-generated buy/sell/hold signals with confidence score
- **Portfolio Monitor** — Balance, positions, P&L, unrealized gains
- **Trade Execution** — Spot & futures order placement (with user confirmation)
- **Alert System** — Price alerts, funding rate alerts, whale movement alerts

## Requirements

- Binance API Key & Secret (Spot and/or Futures)
- Store in `~/.openclaw/.env`:
  ```
  BINANCE_API_KEY=your_key
  BINANCE_API_SECRET=your_secret
  BINANCE_TESTNET=true   # set to false for live trading
  ```

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/analyze.py` | Market analysis — symbols, indicators, signals |
| `scripts/portfolio.py` | Portfolio overview — balances, positions, P&L |
| `scripts/trade.py` | Execute trades (requires confirmation) |
| `scripts/alerts.py` | Price & funding rate alerts |

## Usage

```
# Analyze a symbol
python3 scripts/analyze.py --symbol BTCUSDT --interval 1h

# Check portfolio
python3 scripts/portfolio.py

# Execute a trade (dry-run by default)
python3 scripts/trade.py --symbol BTCUSDT --side BUY --quantity 0.01 --type LIMIT

# Set price alert
python3 scripts/alerts.py --symbol BTCUSDT --condition above --price 70000
```

## Safety

- **Always confirm** before executing real trades
- Use [Binance Testnet](https://testnet.binance.vision/) for backtesting
- Never commit API keys — use environment variables
- Start with small amounts

## License

MIT
