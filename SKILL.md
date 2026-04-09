# 能力概览

我是 BianTrading，量化交易 Agent。覆盖加密货币和 A 股两个市场。

OpenClaw 从 `skills/` 目录自动加载技能，每个技能独立运作、职责清晰。

## 技能清单

| 技能 | 目录 | 职责 |
|------|------|------|
| 加密货币交易 | `skills/binance-trading/` | Binance U本位合约 5 步交易流水线 |
| 加密货币数据 | `skills/binance-data/` | 合约 K 线缓存 + 超跌反弹扫描（短期/长期） |
| A 股分析 | `skills/astock-analysis/` | 量化筛选 + 超跌反弹 + 深度分析 + 交易计划 |
| A 股数据服务 | `skills/astock-data/` | 本地缓存 + 增量拉取 + 批量预加载 |

## 加密货币交易（binance-trading）

5 步自动化流水线：
1. 信息收集 — Binance 公开 API 量化筛选候选币种
2. 深度分析 — TradingAgents 多智能体评级（1-10 分）
3. 策略制定 — 固定风险模型，头寸/止损/止盈
4. 自动执行 — Binance fapi 下单 + 持仓监控
5. 展示进化 — 账户报告 + 策略参数自动调优

风控硬编码：单笔 ≤20%、单币 ≤30%、日亏 ≥5% 切模拟盘、止损后 24h 禁同向开仓

## 加密货币数据（binance-data）

底层数据基础设施 + 超跌反弹分析：
- 本地 SQLite K 线缓存（538 个 USDT 永续合约，4h + 1d）
- 缓存优先 + 增量联网拉取，零重复请求
- 超跌反弹扫描（双模式）：
  - 短期超跌（4h）：RSI 极端超卖 + 资金费率 + 底部放量
  - 长期超跌（1d）：BIAS 深度偏离 + MACD 底背离 + 距高点回撤
- 八维度评分体系，含币圈独有的资金费率和持仓量信号

## A 股分析（astock-analysis）

全链路分析 + 交易计划生成：
- Skill-1A：趋势/动量量化筛选（RSI/EMA/MACD/ADX 多因子评分）
- Skill-1B：超跌反弹筛选（双模式）：
  - 短期超跌反弹（3~5 天）：跌停板计数 + 底部放量 + RSI 极端超卖
  - 长期超跌蓄能（2~4 周）：缩量企稳 + 60 日 BIAS + MACD 底背离
- Skill-2A：TradingAgents 多智能体深度分析（完整报告自动保存）
- 交易计划生成：入场区间 / 止损止盈 / 仓位管理 / 分批建仓
- 历史分析报告查询

## A 股数据服务（astock-data）

底层数据基础设施：
- 本地 SQLite K 线缓存（5000+ 只股票，250 万行）
- 缓存优先 + 增量联网拉取，零重复请求
- 批量预加载全市场历史数据
- 支持前复权/后复权/不复权

## 技术栈

- Binance fapi 合约接口（公开 + 签名端点）
- TradingAgents 多智能体框架 + 多 LLM 提供商（Gemini/MiniMax/OpenAI/智谱/Qwen）
- akshare / 腾讯 / 新浪 / 东方财富（A 股数据）
- SQLite（K 线缓存 + 状态存储 + 分析报告）
- JSON Schema draft-07 输入输出校验

## 数据资产

| 数据库 | 路径 | 内容 |
|--------|------|------|
| A 股 K 线缓存 | `data/kline_cache.db` | 5000+ 只股票日线，250 万行 |
| 币安 K 线缓存 | `data/binance_kline_cache.db` | 538 个合约 4h+1d，64 万行 |
| 状态存储 | `data/state_store.db` | Skill 输入输出快照 |
| 分析报告 | `data/reports/` | TradingAgents 完整分析报告（markdown） |
