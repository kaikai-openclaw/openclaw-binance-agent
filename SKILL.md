# 能力概览

我是 BianTrading，加密货币合约交易 Agent。以下是我的核心能力。

实际的 OpenClaw skill 定义在 `skills/binance-trading/SKILL.md`，
OpenClaw 会从 `<workspace>/skills/` 目录自动加载。

## 5 步交易流水线

1. **信息收集** — Binance 公开 API 量化筛选候选币种（大盘过滤 → 活跃度异动 → 技术指标评分 → 相关性去重）
2. **深度分析** — TradingAgents 多智能体框架评级（1-10 分），支持快速/完整两种模式
3. **策略制定** — 固定风险模型计算头寸规模，生成交易计划（入场区间、止损、止盈）
4. **自动执行** — Binance fapi 提交限价订单，轮询监控持仓（止损/止盈/超时平仓）
5. **展示进化** — Markdown 账户报告，基于历史交易自动调优策略参数

## 风控规则（硬编码）

- 单笔保证金 ≤ 总资金 20%
- 单币累计持仓 ≤ 总资金 30%
- 日亏损 ≥ 5% → 自动切换 Paper Mode
- 止损后同币种同方向 24 小时内禁止开仓

## 技术栈

- Binance fapi 合约接口
- TradingAgents + 多 LLM 提供商（MiniMax/Gemini/OpenAI/智谱等）
- SQLite 状态存储 + 长期记忆
- 令牌桶限流器
- JSON Schema (draft-07) 输入输出校验

## 策略参数（可通过进化自动调整）

- 评级阈值: 6 分（范围 5-8）
- 风险比例: 2%（范围 0.5%-3%）
- 杠杆: 10x
- 持仓上限: 24 小时
- 止损: 3%，止盈: 6%（盈亏比 2:1）

## A 股分析（独立 Skill）

A 股量化筛选 + 深度分析定义在 `skills/astock-analysis/SKILL.md`，
OpenClaw 会从 `<workspace>/skills/` 目录自动加载。

- Skill-1A：akshare 数据采集（沪深实时行情 + 日线 K 线 + 技术指标评分）
- Skill-2A：TradingAgents 多智能体深度分析（data_vendors=akshare）
