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

### 持仓管理（止损上移，支持做多/做空）

```bash
.venv/bin/python3 scripts/manage_positions.py
```

方向感知的止损/止盈单识别、规整和执行，进程锁防止 cron 重叠，原子写入状态文件。

### Binance 数据扫描

```bash
.venv/bin/python3 skills/binance-data/scripts/scan_oversold.py --mode short
.venv/bin/python3 skills/binance-data/scripts/scan_oversold.py --mode short --json
.venv/bin/python3 skills/binance-data/scripts/scan_reversal.py --mode short
.venv/bin/python3 skills/binance-data/scripts/scan_overbought.py --mode short
```

`scan_oversold.py --json` 应保持纯 JSON 输出，供自动化消费。

### 回测（策略参数验证）

```bash
.venv/bin/python3 scripts/backtest_crypto.py
.venv/bin/python3 scripts/backtest_astock.py
```

## A 股 Skill 脚本

### Skill-1A：趋势动量筛选

```bash
# 全市场扫描
.venv/bin/python3 skills/astock-analysis/scripts/analyze_astock.py --scan

# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/analyze_astock.py 600519
```

输出候选列表和 `state_id`，可接 Skill-2A 深度分析。

### Skill-1B：超跌反弹筛选

```bash
# 全市场扫描（短期超跌，3~5天持仓）
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py --scan

# 长期超跌蓄能（2~4周持仓）
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py --scan --mode long

# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py 600519 000001

# 自定义参数
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py --scan --rsi 30 --bias -8 --min-score 40
```

输出候选列表和 `state_id`，可接 Skill-2A 深度分析。

### 底部放量反转扫描

```bash
# 全市场扫描
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py --scan

# 调整评分门槛
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py --scan --min-score 50

# 纯本地缓存扫描（无网络）
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py --scan --from-cache

# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py 600519
```

### Skill-2A：深度分析（TradingAgents）

```bash
# 直接传股票代码（独立调用）
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py 600519
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py 600519 000001 300750 --fast

# 接上游 state_id（接 Skill-1A/1B 输出）
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py --state-id <state_id>
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py --state-id <state_id> --fast
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py --state-id <state_id> --threshold 7
```

`--fast` 使用快速 LLM 模式（单次调用，10~30 秒/股）；默认完整多智能体模式。

### 超跌交易计划生成

```bash
# 扫描超跌候选并生成入场/止损/止盈/仓位计划
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode long

# 指定个股 + 自定义资金
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short --symbols 600519 000001 --capital 500000

# 输出 JSON
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short --json
```

### 查看历史分析报告

```bash
# 列出所有报告
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py

# 只看 A 股 / 加密货币
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --market astock
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --market crypto

# 查看指定标的的最新报告内容
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --symbol 600519
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --symbol BTCUSDT

# 指定日期
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --symbol 600519 --date 2026-04-09
```

### A 股数据服务

```bash
# 查询本地缓存 K 线（前复权）
.venv/bin/python3 skills/astock-data/scripts/fetch_data.py sh.600519 2024-01-01 2024-12-31

# 后复权
.venv/bin/python3 skills/astock-data/scripts/fetch_data.py sz.000001 2024-01-01 2024-12-31 --adjust hfq

# 输出 JSON（供程序调用）
.venv/bin/python3 skills/astock-data/scripts/fetch_data.py sh.600519 2024-01-01 2024-06-30 --json
```

### A 股 K 线批量预加载

```bash
# 全市场预加载（腾讯数据源，最稳定）
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py

# 断点续传（跳过已有数据）
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py --skip-existing

# 指定个股
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py --symbols 600519 000001 300750

# 预加载主要指数（上证/沪深300/创业板等）
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py --index

# 自定义起始日期
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py --start 2020-01-01

# 切换数据源
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py --source baostock
.venv/bin/python3 skills/astock-data/scripts/preload_klines.py --source akshare
```

## 数据文件

| 路径 | 用途 |
| ------ | ------ |
| `data/binance_kline_cache.db` | Binance K 线缓存 |
| `data/kline_cache.db` | A 股 K 线缓存（5271 只股票，248 万行） |
| `data/state_store.db` | Skill 输入输出状态 |
| `data/trading_state.db` | MemoryStore、交易记录、Paper Mode runtime state |
| `data/reports/` | TradingAgents 分析报告 |
| `memory/` | 运行记忆和每日记录，不提交 |

## 关键代码入口

| 文件 | 说明 |
| ------ | ------ |
| `src/infra/binance_fapi.py` | Binance 签名请求、订单、持仓、Algo 条件单、userTrades |
| `src/infra/exchange_rules.py` | 交易所数量、价格、名义金额规则 |
| `src/infra/risk_controller.py` | 风控校验（6大约束）、Paper Mode、止损冷却 |
| `src/infra/market_regime.py` | A 股大盘环境过滤，牛/熊/横盘三态 |
| `src/infra/trade_sync.py` | Binance 服务端已平仓成交同步 |
| `src/skills/skill4_execute.py` | 实盘执行、保护单挂载、止损上移、时间衰减止盈、残留条件单清理 |
| `src/skills/skill5_evolve.py` | 账户展示、成交记录、自我进化 |
| `scripts/manage_positions.py` | 持仓管理，做多/做空止损上移，进程锁，原子写入 |
| `scripts/backtest_crypto.py` | 加密货币策略回测 |
| `scripts/backtest_astock.py` | A 股策略回测 |

## 测试命令

```bash
PYTHONPATH="." python -m pytest tests/test_run_oversold_cron.py tests/test_skill2_analyze.py tests/test_skill4_execute.py
PYTHONPATH="." python -m pytest tests/test_binance_fapi.py tests/test_trade_sync.py
```

如果 pytest rootdir 识别到上层目录，先显式进入项目目录：

```bash
cd /Users/zengkai/.openclaw/openclaw-binance-agent
```
