# 能力概览

我是 BianTrading，量化交易 Agent。以下是我的核心能力。

OpenClaw 从 `skills/` 目录自动加载技能，每个技能独立运作、职责清晰。

## 技能清单

| 技能 | 目录 | 职责 |
|------|------|------|
| 加密货币交易 | `skills/binance-trading/` | Binance U本位合约 5 步交易流水线 |
| A 股分析 | `skills/astock-analysis/` | 量化筛选 + 超跌反弹 + TradingAgents 深度分析 |
| A 股数据服务 | `skills/astock-data/` | 本地缓存 + 增量拉取 + 批量预加载 |

## 加密货币交易（binance-trading）

5 步自动化流水线：
1. 信息收集 — Binance 公开 API 量化筛选候选币种
2. 深度分析 — TradingAgents 多智能体评级（1-10 分）
3. 策略制定 — 固定风险模型，头寸/止损/止盈
4. 自动执行 — Binance fapi 下单 + 持仓监控
5. 展示进化 — 账户报告 + 策略参数自动调优

风控硬编码：单笔 ≤20%、单币 ≤30%、日亏 ≥5% 切模拟盘、止损后 24h 禁同向开仓

## A 股分析（astock-analysis）

- Skill-1A：趋势/动量量化筛选（RSI/EMA/MACD/ADX 多因子评分）
- Skill-1B：超跌反弹筛选（BIAS/RSI/BOLL/KDJ/MACD背离 六维评分）
- Skill-2A：TradingAgents 多智能体深度分析（data_vendors=akshare）

## A 股数据服务（astock-data）

底层数据基础设施，为上层所有 Skill 和 TradingAgents 提供统一数据源：
- 本地 SQLite K 线缓存（data/kline_cache.db）
- 缓存优先 + 增量联网拉取，零重复请求
- 批量预加载全市场历史数据
- 支持前复权/后复权/不复权

## 技术栈

- Binance fapi 合约接口
- TradingAgents + 多 LLM 提供商（MiniMax/Gemini/OpenAI/智谱）
- akshare / 腾讯 / 新浪 / 东方财富（A 股数据）
- SQLite（K 线缓存 + 状态存储）
- JSON Schema draft-07 输入输出校验
