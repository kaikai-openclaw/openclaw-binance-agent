---
name: astock-analysis
description: A股深度分析与量化筛选。当用户说"深度分析"、"分析XX股票"时，直接调用 deep_analyze.py 传入股票代码。当用户说"筛选"、"扫描"、"选股"时调用筛选脚本。用于A股行情分析、选股、技术面评估、深度评级。
user-invocable: true
metadata: {"openclaw":{"requires":{"bins":[".venv/bin/python3"]}}}
---

# A 股分析 Agent

沪深 A 股深度分析 + 量化筛选，数据源为 akshare（东方财富），无需 API Key。

## 深度分析（最常用）

当用户说"深度分析"、"分析某只股票"、"评级"时，直接用这个。
支持直接传股票代码（独立调用）或传 state_id（接上游筛选结果）。

```bash
# 直接分析指定股票（最常用）
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py 601615
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py 600519 000001 300750

# 快速模式（单次 LLM，10-30秒/股）
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py 601615 --fast

# 接上游筛选结果
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py --state-id <state_id>
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py --state-id <state_id> --fast
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py --state-id <state_id> --threshold 7
```

- 完整模式（默认）：TradingAgents 多智能体辩论，约 5-10 分钟/股
- 快速模式（`--fast`）：单次 LLM 调用，约 10-30 秒/股
- 评级输出：1-10 分，≥6 分通过，附带多空信号和置信度

## 量化筛选（Skill-1A：趋势/动量）

当用户说"筛选"、"扫描"、"选股"、"全市场扫描"时使用。

```bash
.venv/bin/python3 {baseDir}/scripts/analyze_astock.py 600519
.venv/bin/python3 {baseDir}/scripts/analyze_astock.py SH600519
.venv/bin/python3 {baseDir}/scripts/analyze_astock.py --scan
```

筛选流程：大盘过滤 → 量比异动 → 技术指标评分 → 相关性去重。
输出包含 state_id，可接深度分析。

## 超跌反弹筛选（Skill-1B）

当用户说"超跌"、"反弹"、"超卖"、"抄底"时使用。
支持短期（3~5天反弹）和长期（2~4周蓄能）两种模式。

```bash
# 短期超跌反弹（默认）
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --mode short
# 长期超跌蓄能
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --mode long
# 指定个股
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py 600519 --mode short
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --rsi 30 --min-score 40
```

## 交易计划生成

当用户说"交易计划"、"怎么买"、"止损止盈"、"仓位"时使用。
一步到位：超跌筛选 → 生成交易计划（入场/止损/止盈/仓位）。

```bash
# 短期反弹交易计划（默认 10 万资金）
.venv/bin/python3 {baseDir}/scripts/make_trade_plan.py --scan --mode short
# 长期蓄能交易计划
.venv/bin/python3 {baseDir}/scripts/make_trade_plan.py --scan --mode long
# 指定资金和个股
.venv/bin/python3 {baseDir}/scripts/make_trade_plan.py --scan --mode short --capital 500000
.venv/bin/python3 {baseDir}/scripts/make_trade_plan.py --scan --mode short --symbols 600519 000001
# JSON 输出（供程序调用）
.venv/bin/python3 {baseDir}/scripts/make_trade_plan.py --scan --mode short --json
```

## 历史分析报告

当用户说"历史报告"、"之前的分析"、"查看报告"时使用。
TradingAgents 深度分析的完整报告（多智能体辩论过程）会自动保存。

```bash
# 列出所有历史报告
.venv/bin/python3 {baseDir}/scripts/view_reports.py
# 只看 A 股报告
.venv/bin/python3 {baseDir}/scripts/view_reports.py --market astock
# 查看指定股票的最新报告
.venv/bin/python3 {baseDir}/scripts/view_reports.py --symbol 600519
# 查看指定日期的报告
.venv/bin/python3 {baseDir}/scripts/view_reports.py --symbol 600519 --date 2026-04-09
# 查看加密货币报告
.venv/bin/python3 {baseDir}/scripts/view_reports.py --market crypto
.venv/bin/python3 {baseDir}/scripts/view_reports.py --symbol BTCUSDT
```

## 意图匹配指南

| 用户说的 | 应该调用 |
|---------|---------|
| "深度分析 601615" | `deep_analyze.py 601615` |
| "分析一下茅台" | `deep_analyze.py 600519` |
| "快速分析 000001" | `deep_analyze.py 000001 --fast` |
| "评级 300750" | `deep_analyze.py 300750` |
| "筛选A股" / "全市场扫描" | `analyze_astock.py --scan` |
| "看看600519技术面" | `analyze_astock.py 600519` |
| "超跌扫描" / "找反弹机会" | `scan_oversold.py --scan --mode short` |
| "长期超跌蓄能" | `scan_oversold.py --scan --mode long` |
| "601615超跌了吗" | `scan_oversold.py 601615` |
| "交易计划" / "怎么买" | `make_trade_plan.py --scan --mode short` |
| "长期交易计划" | `make_trade_plan.py --scan --mode long` |
| "50万资金交易计划" | `make_trade_plan.py --scan --mode short --capital 500000` |
| "历史报告" / "之前的分析" | `view_reports.py` |
| "600519的分析报告" | `view_reports.py --symbol 600519` |
| "加密货币报告" | `view_reports.py --market crypto` |

## 配置

环境变量（在项目 `.env` 中设置）：
- `LLM_PROVIDER` — LLM 提供商（默认 minimax）
- `FAST_LLM_MODEL` — 快速模式模型名称
- 对应 provider 的 API Key（如 `MINIMAX_API_KEY`）

## 注意事项

- akshare 有频率限制，全市场扫描时会自动指数退避重试
- A 股交易时间外也可分析，使用最近交易日数据
- 所有状态存储在本地 SQLite（data/ 目录）
