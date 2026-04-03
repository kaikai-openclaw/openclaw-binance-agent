---
name: astock-analysis
description: A股量化数据采集与深度分析。通过 akshare 获取沪深A股实时行情和日线数据，执行量化筛选（大盘过滤→量比→技术指标→相关性去重），再调用 TradingAgents 多智能体框架进行深度分析评级。用于A股行情分析、选股、技术面评估。
user-invocable: true
metadata: {"openclaw":{"requires":{"bins":["python3"]}}}
---

# A 股分析 Agent

沪深 A 股量化筛选 + TradingAgents 深度分析，基于 Skill-1A / Skill-2A 架构。
数据源为 akshare（东方财富），无需 API Key。

## 分析指定个股（Skill-1A + Skill-2A）

对指定 A 股执行量化筛选 + 深度分析评级。

```bash
python3 {baseDir}/scripts/analyze_astock.py 600519
python3 {baseDir}/scripts/analyze_astock.py 000001 --fast
python3 {baseDir}/scripts/analyze_astock.py SH600519
```

## 全市场扫描

从沪深全市场量化筛选候选股票并深度分析。

```bash
python3 {baseDir}/scripts/analyze_astock.py --scan
python3 {baseDir}/scripts/analyze_astock.py --scan --fast
```

## 筛选流程（Skill-1A）

1. 大盘过滤 — 成交额 ≥5亿、振幅 ≥3%、|涨跌幅| 1.5%-9.9%
2. 活跃度异动 — 日线量比 ≥1.3
3. 技术指标评分 — RSI/EMA/MACD/ADX/流动性 多因子双向评分（满分100）
4. 相关性去重 — Pearson 相关系数 >0.85 的候选去重

自动排除：ST / *ST / 退市 / 北交所标的

## 深度分析（Skill-2A）

- 完整模式（默认）：TradingAgents 多智能体辩论，约 5-10 分钟/股
- 快速模式（`--fast`）：单次 LLM 调用，约 10-30 秒/股

TradingAgents 通过 akshare 获取：
- 日线 OHLCV 行情（stock_zh_a_hist）
- 技术指标（stockstats 本地计算）
- 基本面数据（stock_individual_info_em + 财务分析指标）
- 个股新闻（stock_news_em）

评级输出：1-10 分，≥6 分通过，附带多空信号和置信度。

## 配置

环境变量（在项目 `.env` 中设置）：
- `LLM_PROVIDER` — LLM 提供商（默认 minimax）
- `FAST_LLM_MODEL` — 快速模式模型名称
- 对应 provider 的 API Key（如 `MINIMAX_API_KEY`）

无需 Binance API Key。

## 注意事项

- akshare 有频率限制，全市场扫描时会自动指数退避重试
- A 股交易时间外也可分析，使用最近交易日数据
- 所有状态存储在本地 SQLite（data/ 目录）
