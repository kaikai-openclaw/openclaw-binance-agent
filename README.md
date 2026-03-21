# 🤖 OpenClaw Binance Agent

> AI Agent + Skill for Binance — market analysis, trading signals, portfolio monitoring, and automated trade execution.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)

## 🎯 Overview

This project provides an OpenClaw AI Agent skill for Binance, combining:

- 📊 **Market Analysis** — Technical indicators, price action, whale tracking
- 📈 **Trading Signals** — AI-generated buy/sell/hold signals with confidence scores
- 💼 **Portfolio Monitor** — Balances, positions, P&L tracking
- ⚡ **Trade Execution** — Spot & futures orders (with safety confirmations)
- 🔔 **Alert System** — Price, funding rate, and whale movement alerts

## 🚀 Quick Start

### 1. Prerequisites

```bash
pip install requests
```

### 2. Configure API Keys

Edit `~/.openclaw/.env`:

```env
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret
BINANCE_TESTNET=true   # set to false for live trading
DRY_RUN=true           # always start with dry-run!
```

### 3. Analyze Markets

```bash
python3 scripts/analyze.py --symbol BTCUSDT --interval 1h --format md
```

### 4. Check Portfolio

```bash
python3 scripts/portfolio.py --format md
```

### 5. Execute Trades

```bash
# Dry-run (always start here!)
python3 scripts/trade.py --symbol BTCUSDT --side BUY --quantity 0.01 --type MARKET

# Real trade (after testing!)
DRY_RUN=false python3 scripts/trade.py --symbol BTCUSDT --side BUY --quantity 0.01 --type MARKET
```

## 📁 Project Structure

```
openclaw-binance-agent/
├── SKILL.md              # OpenClaw skill definition
├── scripts/
│   ├── analyze.py        # Market analysis + signals
│   ├── portfolio.py      # Portfolio & positions monitor
│   ├── trade.py          # Trade executor
│   └── alerts.py         # Price & funding alerts
├── prompts/
│   └── trading.md        # AI trading system prompts
└── README.md
```

## ⚠️ Safety Guidelines

1. **Always start with dry-run mode** — `DRY_RUN=true`
2. **Use Testnet first** — `BINANCE_TESTNET=true`
3. **Never commit API keys** — use environment variables only
4. **Confirm before executing** — the script asks for confirmation in non-dry-run mode
5. **Start small** — test with amounts you're comfortable losing

## 🔧 OpenClaw Skill Installation

```bash
# Clone into your OpenClaw skills directory
git clone https://github.com/kaikai-openclaw/openclaw-binance-agent.git \
  ~/path/to/your/openclaw/skills/binance-agent

# Or use clawhub (once published)
clawhub install openclaw-binance-agent
```

## 📜 License

MIT — free to use, modify, and distribute.
