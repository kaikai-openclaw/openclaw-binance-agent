# 工具与本机环境

这里记录本仓库特有的脚本、路径、数据文件和外部服务。不要在这里写 API Key 明文。

## 运行环境

- 工作目录：`/Users/zengkai/.openclaw/openclaw-binance-agent`
- Python：项目要求 `>=3.11`
- 虚拟环境命令前缀：`.venv/bin/python3`
- 主要配置：项目 `.env` 或 `~/.openclaw/.env`
- Telegram 账号：`BianTrading_Bot`

## 必需环境变量

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `LLM_PROVIDER`
- `FAST_LLM_MODEL`

密钥文件和 `.env` 不允许提交。

## 高频脚本

### 超跌交易定时任务

```bash
.venv/bin/python3 skills/binance-trading/scripts/run_oversold_cron.py --fast --format markdown
```

JSON 调试：

```bash
.venv/bin/python3 skills/binance-trading/scripts/run_oversold_cron.py --fast --format json
```

### 完整交易流水线

```bash
.venv/bin/python3 skills/binance-trading/scripts/run_pipeline.py --fast
.venv/bin/python3 skills/binance-trading/scripts/run_pipeline.py --paper --fast
.venv/bin/python3 skills/binance-trading/scripts/run_pipeline.py --fast --symbols BTC,SOL
```

### 账户检查

```bash
.venv/bin/python3 skills/binance-trading/scripts/check_account.py
```

用于查看总资金、可用保证金、持仓、杠杆、浮盈亏、普通挂单和 Algo 止盈止损条件单。

### Binance 数据扫描

```bash
.venv/bin/python3 skills/binance-data/scripts/scan_oversold.py --mode short
.venv/bin/python3 skills/binance-data/scripts/scan_oversold.py --mode short --json
.venv/bin/python3 skills/binance-data/scripts/scan_reversal.py --mode short
.venv/bin/python3 skills/binance-data/scripts/scan_overbought.py --mode short
```

`scan_oversold.py --json` 应保持纯 JSON 输出，供自动化消费。

## 数据文件

| 路径 | 用途 |
| ------ | ------ |
| `data/binance_kline_cache.db` | Binance K 线缓存 |
| `data/state_store.db` | Skill 输入输出状态 |
| `data/trading_state.db` | MemoryStore、交易记录、Paper Mode runtime state |
| `data/reports/` | TradingAgents 分析报告 |
| `memory/` | 运行记忆和每日记录，不提交 |

## 关键代码入口

| 文件 | 说明 |
| ------ | ------ |
| `src/infra/binance_fapi.py` | Binance 签名请求、订单、持仓、Algo 条件单、userTrades |
| `src/infra/exchange_rules.py` | 交易所数量、价格、名义金额规则 |
| `src/infra/risk_controller.py` | 风控校验、Paper Mode、止损冷却 |
| `src/infra/trade_sync.py` | Binance 服务端已平仓成交同步 |
| `src/skills/skill4_execute.py` | 实盘执行、保护单挂载、残留条件单清理 |
| `src/skills/skill5_evolve.py` | 账户展示、成交记录、自我进化 |

## 测试命令

```bash
PYTHONPATH="." python -m pytest tests/test_run_oversold_cron.py tests/test_skill2_analyze.py tests/test_skill4_execute.py
PYTHONPATH="." python -m pytest tests/test_binance_fapi.py tests/test_trade_sync.py
```

如果 pytest rootdir 识别到上层目录，先显式进入项目目录：

```bash
cd /Users/zengkai/.openclaw/openclaw-binance-agent
```
