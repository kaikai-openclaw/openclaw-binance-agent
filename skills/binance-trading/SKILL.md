---
name: binance-trading
description: Binance U本位合约自动化交易。运行完整 Pipeline（信息收集→深度分析→策略制定→自动执行→展示进化）、查看账户状态、筛选候选币种、分析指定币种。用于加密货币合约交易、行情分析、持仓查询、策略回测相关操作。
user-invocable: true
metadata: {"openclaw":{"requires":{"env":["BINANCE_API_KEY","BINANCE_API_SECRET"],"bins":["python3"]},"primaryEnv":"BINANCE_API_KEY"}}
---

# Binance 交易 Agent

加密货币 U本位合约自动化交易技能，基于 5 步流水线架构。

## 运行完整 Pipeline

执行一轮完整的交易流水线：信息收集 → 深度分析 → 策略制定 → 自动执行 → 展示进化。

可选参数：
- `--paper` 强制使用模拟盘（不动真金白银）
- `--fast` 使用快速 LLM 分析（跳过多智能体辩论，约 30 秒/币种）
- `--symbols BTC,ETH,SOL` 指定分析币种（跳过大盘筛选）

```bash
python3 {baseDir}/scripts/run_pipeline.py
python3 {baseDir}/scripts/run_pipeline.py --paper
python3 {baseDir}/scripts/run_pipeline.py --fast --symbols BTC,SOL
```

## 查看账户状态

查询 Binance 合约账户余额、持仓、当日盈亏。

```bash
python3 {baseDir}/scripts/check_account.py
```

## 筛选候选币种（仅 Skill-1）

从全市场量化筛选候选币种，不执行交易。

```bash
python3 {baseDir}/scripts/collect_candidates.py
python3 {baseDir}/scripts/collect_candidates.py --symbols ONT,BTC
```

## 分析指定币种（Skill-1 + Skill-2）

对指定币种执行量化筛选 + 深度分析评级。

```bash
python3 {baseDir}/scripts/analyze_symbol.py BTCUSDT
python3 {baseDir}/scripts/analyze_symbol.py SOLUSDT --fast
```

## 运行测试

```bash
python3 -m pytest {baseDir}/tests/ -q
```

## 风控规则（硬编码，不可绕过）

- 单笔保证金 ≤ 总资金 20%
- 单币累计持仓 ≤ 总资金 30%
- 日亏损 ≥ 5% → 自动切换 Paper Mode（取消挂单、停止实盘、告警）
- 止损后同币种同方向 24 小时内禁止开仓

## 配置

环境变量（在 `~/.openclaw/.env` 或项目 `.env` 中设置）：
- `BINANCE_API_KEY` — Binance 合约 API Key
- `BINANCE_API_SECRET` — Binance 合约 API Secret
- `LLM_PROVIDER` — LLM 提供商（默认 minimax，可选 google/zhipu/openai 等）
- `FAST_LLM_MODEL` — 快速模式模型名称

## A 股分析（Skill-1A + Skill-2A）

对 A 股执行量化筛选 + TradingAgents 深度分析评级。
数据源为 akshare（东方财富），无需 API Key。

```bash
# 分析指定个股
python3 {baseDir}/scripts/analyze_astock.py 600519
python3 {baseDir}/scripts/analyze_astock.py 000001 --fast

# 全市场扫描
python3 {baseDir}/scripts/analyze_astock.py --scan
python3 {baseDir}/scripts/analyze_astock.py --scan --fast
```

Skill-1A 筛选流程（与 Skill-1 同构）：
1. 大盘过滤 — 成交额 ≥5亿、振幅 ≥3%、|涨跌幅| 1.5%-9.9%
2. 活跃度异动 — 日线量比 ≥1.3
3. 技术指标评分 — RSI/EMA/MACD/ADX/流动性多因子双向评分
4. 相关性去重

Skill-2A 深度分析：
- 完整模式：TradingAgents 多智能体辩论（akshare 数据源）
- 快速模式（`--fast`）：单次 LLM 调用

## 注意事项

- 首次运行前确保 Binance API Key 已开启合约交易权限
- 建议先用 `--paper` 模式验证策略
- A 股分析无需 Binance API Key，仅需 LLM 相关配置
- 所有数据存储在本地 SQLite（data/ 目录）
