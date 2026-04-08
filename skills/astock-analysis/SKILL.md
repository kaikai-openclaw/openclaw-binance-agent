---
name: astock-analysis
description: A股量化数据采集与深度分析。通过 akshare 获取沪深A股实时行情和日线数据，执行量化筛选（大盘过滤→量比→技术指标→相关性去重），再调用 TradingAgents 多智能体框架进行深度分析评级。用于A股行情分析、选股、技术面评估。
user-invocable: true
metadata: {"openclaw":{"requires":{"bins":[".venv/bin/python3"]}}}
---

# A 股分析 Agent

沪深 A 股量化筛选 + TradingAgents 深度分析，Skill-1A / Skill-1B / Skill-2A 独立调用。
数据源为 akshare（东方财富），无需 API Key。

## Skill-1A：量化筛选（趋势/动量）

对指定个股或全市场执行量化筛选，输出候选列表和 state_id。

```bash
.venv/bin/python3 {baseDir}/scripts/analyze_astock.py 600519
.venv/bin/python3 {baseDir}/scripts/analyze_astock.py SH600519
.venv/bin/python3 {baseDir}/scripts/analyze_astock.py --scan
```

输出包含 state_id，用于后续手动调用 Skill-2A。

## Skill-1B：超跌反弹筛选

多维度超跌信号检测（均值回归策略），捕捉市场情绪错杀后的短期修复机会。

```bash
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan                    # 全市场扫描
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py 600519                    # 指定个股
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py 600519 000001 300750      # 多个个股
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --rsi 30           # 自定义 RSI 阈值
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --bias -8          # 自定义乖离率阈值
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --min-score 40     # 降低评分门槛
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --scan --volume-confirm   # 要求底部放量确认
```

输出包含 state_id，可手动调用 Skill-2A 深度分析。

### 超跌信号维度（六维评分，满分 100）

1. 价格偏离度（20分）— 20日乖离率 BIAS < -10%
2. 动量极值（20分）— RSI(14) < 25，超卖区
3. 连续杀跌（15分）— 连续下跌天数 + 近N日累计跌幅
4. 通道突破（15分）— 收盘价跌破布林带(20,2)下轨
5. 动量背离（15分）— MACD 底背离（价格新低但 MACD 柱未新低）
6. KDJ 极值（10分）— J 值 < 0
7. 底部放量（5分加分）— 最后一根成交量 ≥ 前5日均量 1.5 倍

### 基础过滤（排雷）

- 排除 ST / *ST / 退市 / 北交所
- 排除股价 < 3 元低价股
- 排除日均成交额 < 5000 万流动性枯竭股
- 相关性去重（Pearson > 0.85）

### 风险提示

超跌反弹本质是左侧交易（接飞刀），必须严格止损。
单一指标无效，需多维共振确认。适用于震荡市和牛市回调期，单边熊市中失效风险高。

## Skill-2A：深度分析（手动调用）

接收 Skill-1A 或 Skill-1B 输出的 state_id，执行 TradingAgents 深度分析评级。

```bash
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py <state_id>
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py <state_id> --fast
.venv/bin/python3 {baseDir}/scripts/deep_analyze.py <state_id> --threshold 7
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
