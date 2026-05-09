# A股量化策略整理

> 基于代码实现整理，覆盖所有现有 A 股分析 Skill 的策略逻辑、指标体系和参数配置。

---

## 目录

1. [整体架构](#整体架构)
2. [数据基础设施（astock-data）](#数据基础设施)
3. [策略一：趋势选股（Skill-1A）](#策略一趋势选股)
4. [策略二：超跌反弹（Skill-1B）](#策略二超跌反弹)
5. [策略三：底部放量反转](#策略三底部放量反转)
6. [策略四：深度分析评级（Skill-2A）](#策略四深度分析评级)
7. [交易计划生成](#交易计划生成)
8. [风控体系](#风控体系)
9. [快速命令参考](#快速命令参考)
---

## 整体架构

A 股分析由两个 Skill 协作完成，数据源为 akshare（东方财富），无需 API Key，缓存在本地 SQLite。

```
astock-data (数据层)        astock-analysis (分析层)
   fetch_data.py              analyze_astock.py   -- 趋势选股 Skill-1A
   preload_klines.py          scan_oversold.py    -- 超跌反弹 Skill-1B
                              scan_reversal.py    -- 底部放量反转
                              deep_analyze.py     -- 深度评级 Skill-2A
                              make_trade_plan.py  -- 交易计划生成
                              view_reports.py     -- 历史报告查看
```

各 Skill 通过 state_id（UUID）在 SQLite 中传递数据，避免上下文膨胀。

---

## 数据基础设施

**Skill**：`astock-data` | **脚本**：`skills/astock-data/scripts/fetch_data.py`

| 能力 | 说明 |
|------|------|
| 复权方式 | 前复权 qfq（默认）/ 后复权 hfq / 不复权 none |
| 代码格式 | 必须带交易所前缀：sh.600519 / sz.000001 / bj.830799 |
| 缓存策略 | 按 (symbol, adjust, date) 三元组唯一索引，仅拉缺失段 |
| 防封控 | API 调用间隔 ≥ 300ms，指数退避重试 |
| 降级 | API 不可用时返回已有缓存 + 警告 |

输出字段：`date / open / high / low / close / volume / amount`

---

## 策略一：趋势选股（Skill-1A）

**脚本**：`skills/astock-analysis/scripts/analyze_astock.py`
**类**：`Skill1ACollect`
**定位**：右侧趋势跟踪，选出正在运行中的强势股。

### 六维度评分体系（满分 100）

| 维度 | 权重 | 核心逻辑 |
|------|------|----------|
| 均线多头排列 | 25 | MA5>MA10>MA20>MA60 完美排列 25 分，三线多头 18 分，站上 MA20 6 分 |
| MACD 持续性 | 20 | MACD 线>0 且柱状图>0 且放大 20 分；金叉状态 10 分 |
| ADX 趋势强度 | 15 | ADX≥40 满分；ADX 25-40 线性映射；ADX<20 得 0 分 |
| 量价配合 | 15 | 量比>1.5 得 5 分；量价同向 5 分；换手率 2%-15% 5 分；无大阳线基因减半 |
| 突破确认 | 15 | 放量突破 20 日高点 15 分；温和突破 8 分；缩量假突破 0 分 |
| RSI 趋势区间 | 10 | RSI 55-75 满分；RSI<50 或>80 得 0 分 |

### 方向判断

均线多头排列（得分≥10）+ MACD 看多 → `long`，否则 `neutral`。

### 基础过滤条件

- 成交额过滤（剔除流动性不足标的）
- 价格下限过滤（剔除仙股）

### 用法

```bash
# 全市场扫描
.venv/bin/python3 skills/astock-analysis/scripts/analyze_astock.py --scan
# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/analyze_astock.py 600519
```

---

## 策略二：超跌反弹（Skill-1B）

**脚本**：`skills/astock-analysis/scripts/scan_oversold.py`
**类**：`ShortTermAStockOversold` / `LongTermAStockOversold`
**定位**：左侧交易，捕捉超跌后的反弹机会。支持短期（3~5天）和长期（2~4周）两种模式。

### 短期超跌反弹评分（满分 100）

| 维度 | 权重 | 核心逻辑 |
|------|------|----------|
| RSI 极端超卖 | 20 | RSI < 阈值（默认 25），越低得分越高 |
| 20 日乖离率 | 12 | BIAS(20) < 阈值（默认 -6%），偏离越大得分越高 |
| 连续杀跌+累计跌幅 | 12 | 连跌天数 + 近 N 日累计跌幅双维度 |
| 布林带下轨突破 | 10 | 收盘价跌破 BOLL 下轨 |
| KDJ J 值极值 | 10 | KDJ J < 0，越低得分越高 |
| 跌停板计数 | 13 | A 股独有：近期跌停数，流动性枯竭后反弹概率高 |
| 底部放量 | 13 | 近 5 日均量 vs 前期均量，放量≥1.5x 得分 |
| MACD 底背离 | 5 | 短期可靠性一般，回看 20 日 |
| 距高点回撤 | 5 | 距近期高点跌幅 > 15% |

### 长期超跌蓄能评分（满分 100）

| 维度 | 权重 | 核心逻辑 |
|------|------|----------|
| RSI 偏弱 | 10 | RSI < 阈值，权重低于短期（长期不追求极端超卖）|
| 60 日乖离率 | 18 | BIAS(60) < 阈值，中期偏离度核心指标 |
| 连续杀跌+累计跌幅 | 10 | 同短期 |
| 布林带下轨突破 | 8 | 同短期 |
| MACD 底背离 | 18 | 长期核心：日线级别底背离，回看 60 日，A 股可靠性高 |
| KDJ J 值 | 5 | 同短期 |
| 跌停板计数 | 3 | 长期意义不大，权重低 |
| 缩量企稳 | 13 | A 股独有：地量见地价，量比 < 0.5 = 抛压枯竭（与短期放量信号相反）|
| 距 120 日高点回撤 | 15 | 长期核心：距 120 日高点跌幅越深得分越高 |

### 关键参数（可调）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| --rsi | 25 | RSI 超跌阈值 |
| --bias | -6 | 20 日乖离率阈值（%）|
| --drop | -8 | 近 N 日累计跌幅阈值（%）|
| --drop-days | 10 | 累计跌幅回看天数 |
| --min-score | 25 | 综合评分最低门槛 |
| --max | 30 | 最大输出数量 |

### 用法

```bash
# 短期超跌（默认）
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py --scan --mode short
# 长期超跌蓄能
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py --scan --mode long
# 自定义参数
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py --scan --rsi 30 --min-score 40
# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/scan_oversold.py 600519 --mode short
```

---

## 策略三：底部放量反转

**脚本**：`skills/astock-analysis/scripts/scan_reversal.py`
**类**：`AStockReversalSkill`
**定位**：捕捉底部放量启动信号，比超跌策略更强调"已经开始反转"而非"还在下跌"。

### 九维度评分体系（满分 100）

| 维度 | 权重 | 核心逻辑 |
|------|------|----------|
| 底部放量 | 20 | 近 3 日均量 vs 前 15 日均量；放量必须伴随阳线，巨量阴线得分减半并标注警告 |
| 价格企稳 | 15 | 近 5 日不再创新低（8 分）+ 波动收窄 ATR 缩小（7 分）|
| 均线拐头 | 15 | MA5 上穿 MA10 金叉 15 分；MA5 拐头向上 10 分；MA10 拐头 7 分 |
| MACD 反转信号 | 12 | MACD 金叉 + 柱状图由负转正 |
| 距底部距离 | 10 | 距近期低点 3%-10% 为理想区间（刚离底部，还有空间）|
| 前期跌幅深度 | 8 | 前期跌幅 > 20% 满分，> 10% 部分得分 |
| 换手率异常 | 8 | 换手率 3%-15% 满分；> 25% 减半（可能是出货）|
| KDJ 低位金叉 | 7 | KDJ 在低位（J<20）发生金叉 |
| 长下影线 | 5 | 当日下影线长度 > 实体 2 倍，表明下方有强支撑 |

### 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| --min-score | 40 | 最低评分门槛（比超跌策略高）|
| --max | 20 | 最大输出数量 |
| --exclude-kcb | false | 排除科创板（688 开头）|

### 用法

```bash
# 全市场扫描
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py --scan
# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py 600519
# 调整门槛
.venv/bin/python3 skills/astock-analysis/scripts/scan_reversal.py --scan --min-score 50
```

---

## 策略四：深度分析评级（Skill-2A）

**脚本**：`skills/astock-analysis/scripts/deep_analyze.py`
**类**：`Skill2AAnalyze` + `AStockTradingAgentsModule`
**定位**：对筛选出的候选股进行 LLM 深度评级，输出 1-10 分 + 多空信号 + 置信度。

### 两种分析模式

| 模式 | 耗时 | 原理 | 适用场景 |
|------|------|------|----------|
| 完整模式（默认）| 5-10 分钟/股 | TradingAgents 多智能体辩论 | 重要个股深度研究 |
| 快速模式（--fast）| 10-30 秒/股 | 单次 LLM 调用 | 批量快速筛选 |

### 评级输出

- 评分：1-10 分，≥6 分通过
- 信号：`bullish` / `bearish` / `neutral`
- 置信度：0-100%
- 评论：分析摘要（完整模式含多智能体辩论过程）

### 两种调用方式

```bash
# 方式一：直接传股票代码（独立调用）
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py 600519
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py 600519 000001 300750 --fast

# 方式二：接上游筛选结果（传 state_id）
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py --state-id <state_id>
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py --state-id <state_id> --fast
.venv/bin/python3 skills/astock-analysis/scripts/deep_analyze.py --state-id <state_id> --threshold 7
```

### 历史报告查看

```bash
# 列出所有 A 股报告
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --market astock
# 查看指定股票最新报告
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --symbol 600519
# 查看指定日期报告
.venv/bin/python3 skills/astock-analysis/scripts/view_reports.py --symbol 600519 --date 2026-04-09
```

---

## 交易计划生成

**脚本**：`skills/astock-analysis/scripts/make_trade_plan.py`
**定位**：基于 Skill-1B 超跌筛选结果，一步生成量化交易计划（入场/止损/止盈/仓位）。

### 短期超跌反弹策略参数

| 参数 | 值 |
|------|----|
| 最低入场评分 | 35 分 |
| 止损方式 | 1.5 倍 ATR 动态止损，最大 -8% |
| ATR 不可用时 | 固定 -5% 止损 |
| 止盈目标一 | +5%，平仓 50% |
| 止盈目标二 | +10%，平仓剩余 50% |
| 仓位范围 | 5%~15%（评分越高仓位越大）|
| 最大持仓 | 5 个交易日 |
| 入场时机 | 尾盘低吸或次日开盘限价单 |

### 长期超跌蓄能策略参数

| 参数 | 值 |
|------|----|
| 最低入场评分 | 40 分 |
| 止损方式 | 近期低点下方 3%，最大 -12% |
| 止盈目标一 | +8%，平仓 30% |
| 止盈目标二 | +15%，平仓 30% |
| 止盈目标三 | +25%，平仓剩余 40% |
| 仓位范围 | 8%~20%（评分越高仓位越大）|
| 最大持仓 | 20 个交易日 |
| 入场时机 | 分批建仓：首次 40%，缩量企稳确认后加仓 60% |

### 仓位计算公式

```
评分比例 = min(1.0, (评分 - 最低入场分) / 40)
目标仓位 = 最小仓位 + 评分比例 × (最大仓位 - 最小仓位)
目标仓位 = min(目标仓位, 剩余可用仓位, 单只上限 20%)
买入股数 = int(仓位金额 / 收盘价 / 100) × 100  # A 股最小单位 100 股
```

### 用法

```bash
# 短期反弹计划（默认 10 万资金）
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short
# 长期蓄能计划
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode long
# 指定资金
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short --capital 500000
# 指定个股
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short --symbols 600519 000001
# JSON 输出
.venv/bin/python3 skills/astock-analysis/scripts/make_trade_plan.py --scan --mode short --json
```

---

## 风控体系

### A 股特有约束（硬编码）

| 规则 | 值 | 说明 |
|------|----|------|
| 单只仓位上限 | 20% | 超出自动裁剪 |
| 总持仓上限 | 80% | 留 20% 现金应对极端行情 |
| 单日最大亏损 | 3% | 触发后当日不再开仓 |
| 止损冷却期 | 5 个交易日 | 止损后同股票 5 日内不重复开仓 |

### A 股 vs 加密货币策略差异

| 维度 | A 股 | 加密货币 |
|------|------|----------|
| 方向 | 只能做多（普通账户）| 多空双向 |
| 交易规则 | T+1（当天买次日才能卖）| 实时 |
| 涨跌幅限制 | 主板 ±10%，创业板/科创板 ±20% | 无限制 |
| 杠杆 | 无（满仓=100%）| 最高 10 倍 |
| 止损执行 | 最快 T+1 | 实时 |
| 冷却期 | 5 个交易日 | 24 小时 |

### 涨跌停处理

- 止损价不能低于跌停价（主板 -10%，创业板/科创板 -20%）
- 短期策略第一止盈不超过涨停价（T+1 当天最多涨停）
- 长期策略第二止盈允许两天涨停空间

---

## 快速命令参考

| 用户意图 | 命令 |
|---------|------|
| 趋势选股 / 全市场扫描 | `analyze_astock.py --scan` |
| 看某只股票趋势 | `analyze_astock.py 600519` |
| 短期超跌扫描 | `scan_oversold.py --scan --mode short` |
| 长期超跌蓄能 | `scan_oversold.py --scan --mode long` |
| 底部反转 / 放量反转 | `scan_reversal.py --scan` |
| 某只股票底部反转了吗 | `scan_reversal.py 600519` |
| 深度分析某只股票 | `deep_analyze.py 600519` |
| 快速分析（批量）| `deep_analyze.py 600519 000001 --fast` |
| 接上游筛选结果分析 | `deep_analyze.py --state-id <id>` |
| 短期交易计划 | `make_trade_plan.py --scan --mode short` |
| 长期交易计划 | `make_trade_plan.py --scan --mode long` |
| 指定资金交易计划 | `make_trade_plan.py --scan --mode short --capital 500000` |
| 查看历史报告 | `view_reports.py --market astock` |
| 查看某只股票报告 | `view_reports.py --symbol 600519` |

> 所有脚本路径前缀：`.venv/bin/python3 skills/astock-analysis/scripts/`
> 数据脚本路径前缀：`.venv/bin/python3 skills/astock-data/scripts/`

---

## 策略选择指南

| 场景 | 推荐策略 | 原因 |
|------|----------|------|
| 市场处于上升趋势 | 趋势选股（Skill-1A）| 顺势而为，胜率更高 |
| 市场急跌后寻找反弹 | 超跌反弹短期（Skill-1B short）| 捕捉恐慌盘释放后的反弹 |
| 市场长期低迷寻底 | 超跌蓄能长期（Skill-1B long）| 等待底部构筑完成 |
| 寻找已经启动的底部股 | 底部放量反转 | 信号更确定，但机会更少 |
| 对某只股票做深度研究 | 深度分析（Skill-2A）| LLM 多维度综合判断 |

**组合使用建议**：先用量化筛选（Skill-1A/1B/反转）缩小范围，再用深度分析（Skill-2A）对候选股做最终评级，最后用交易计划生成器输出具体操作方案。

---

## 回测验证记录

> 回测工具：`scripts/backtest_astock.py`
> 数据：kline_cache.db，5412 只股票，2021-01-01 ~ 2026-05-03
> 样本：随机抽取 500-1000 只，seed=42

### 超跌反弹短期（oversold_short）✅ 有效

| 评分区间 | 样本 | 胜率 | 均收益 |
|---------|------|------|--------|
| 0-25 | 225,816 | 47.6% | +0.27% |
| 25-40 | 4,958 | 59.6% | +2.64% |
| 40-55 | 1,393 | 60.6% | +2.03% |
| 55-70 | 171 | 61.4% | +3.77% |

**结论**：评分有效，高分组显著跑赢低分组。**默认门槛从 25 调整为 35**。

### 超跌蓄能长期（oversold_long）✅ 有效，但有上限

| 评分区间 | 样本 | 胜率 | 均收益 |
|---------|------|------|--------|
| 25-40 | 10,485 | 55.7% | +2.63% |
| 40-55 | 1,784 | 65.1% | +7.12% |
| 55-70 | 365 | 59.5% | +4.87% |
| 70-85 | 24 | 41.7% | -11.46% |

**结论**：40-55 分是甜蜜区间，70 分以上反而崩塌（极端信号 = 危险信号）。**加评分上限 70**。

### 趋势选股（trend）❌ 市场环境依赖

| 评分区间 | 样本 | 胜率 | 均收益 |
|---------|------|------|--------|
| 40-55 | 24,073 | 49.1% | +0.76% |
| 55-70 | 28,765 | 48.4% | +0.76% |
| 70-85 | 16,511 | 44.1% | +0.33% |
| 85+ | 1,621 | 37.2% | -1.09% |

**结论**：高分组（85+）反而最差，胜率 37%。根本原因是 2021-2024 震荡熊市，趋势策略天然失效。**不是权重问题，是市场环境问题**，大盘过滤已处理（熊市/横盘时自动暂停或提高门槛）。

### 底部放量反转（reversal）⚠️ 弱有效，市场环境依赖

子维度分析（500只，115,606个样本点）：

| 维度 | 原权重 | 新权重 | 有效性 | 差值 |
|------|--------|--------|--------|------|
| KDJ金叉 | 7 | **22** | ✅ | +0.54% |
| MACD反转 | 12 | **22** | ⚠️ | +0.19% |
| 前期跌幅 | 8 | **18** | ⚠️ | +0.20% |
| 底部放量 | 20 | 10 | ❌ | -0.09% |
| 均线拐头 | 15 | 8 | ❌ | -0.08% |
| 价格企稳 | 15 | 5 | ❌ | -0.66% |
| 距底部距离 | 10 | 5 | ❌ | -0.37% |
| 长下影线 | 5 | 5 | ❌ | -0.14% |
| 换手率 | 8 | 5 | ? | 无信号 |

按年份分层验证市场环境影响：

| 年份 | 胜率 | 均收益 | 评分≥40子集 |
|------|------|--------|------------|
| 2021 | 49% | +0.86% | 胜率50% +0.87% |
| 2022 | 46% | -0.20% | 胜率50% +0.00% |
| 2023 | 46% | +0.07% | 胜率48% +0.22% |
| 2024 | 45% | +0.20% | 胜率48% +1.28% |
| **2025** | **56%** | **+1.54%** | **胜率61% +2.21%** |

**结论**：2025 年（反弹年）评分≥40 胜率 61%，均收益 +2.21%，策略有效。2022-2024 熊市拉低整体数据。**权重已按回测结果调整，大盘过滤确保只在合适环境下运行**。
