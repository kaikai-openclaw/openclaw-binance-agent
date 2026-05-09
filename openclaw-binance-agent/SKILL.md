# 能力概览

我是 BianTrading，量化交易 Agent。当前重点是 Binance U 本位合约自动交易，同时保留 A 股量化分析能力。

OpenClaw 从 `skills/` 目录自动加载技能。每个 Skill 必须输入输出结构化、可校验、可回放。

## 技能清单

| 技能 | 目录 | 职责 |
| ------ | ------ | ------ |
| 加密货币交易 | `skills/binance-trading/` | Binance 5 步流水线、账户检查、超跌/插针定时任务固定报告 |
| 加密货币数据 | `skills/binance-data/` | Binance K 线缓存、超跌/反转/超买/插针扫描、纯 JSON 输出 |
| A 股分析 | `skills/astock-analysis/` | 趋势、超跌、反转筛选和 TradingAgents 深度分析 |
| A 股数据服务 | `skills/astock-data/` | A 股本地缓存、增量拉取、批量预加载 |

## Binance 交易流水线

1. 信息收集：公开行情、量化筛选、候选币种生成。
2. 深度分析：TradingAgents 评级，输出通过评级的 `ratings` 和全部已分析的 `all_ratings`。
3. 策略制定：ATR 动态止损止盈（插针模式优先使用影线尖端止损）、波动过大跳过、风险比例和仓位计算。
4. 自动执行：杠杆同步、价格/数量规整、限价入场、服务端止损止盈保护、残留 Algo 清理。
5. 展示进化：账户报告、服务端成交同步、历史交易统计、参数自我调整。

## Binance 执行安全

- 单笔保证金 ≤20%，单币种持仓 ≤40%，总敞口 ≤总资金 × 4x，同时持仓 ≤30，日亏损 ≥5% 切 Paper Mode。
- Paper Mode 持久化到 SQLite，重启后仍生效。
- 止损后同币种同方向 24 小时冷却。
- 下单数量和价格用 Binance 规则规整并格式化为合法十进制字符串。
- 签名请求重试时重新签名，携带 `recvWindow`。
- 服务端保护单使用 `closePosition=true`，避免止盈/止损残留后反向开仓。
- 非阻塞模式下保护单全部挂载失败时立即平仓。
- 持仓期间自动执行止损上移（Break-even + 阶梯锁利，3步）和时间衰减止盈（2步）：
  - 止损上移：盈利 1.3x/1.8x/2.3x sl_dist 时分别移至保本/锁住0.5x/锁住1x利润
  - 时间衰减止盈：持仓超 50%/75% max_hold_hours 时分别下调止盈 20%/40%（仅止损未上移时执行）

## 超跌定时任务

固定入口：

```bash
.venv/bin/python3 skills/binance-trading/scripts/run_oversold_cron.py --fast --format markdown
```

报告固定包含：

- 扫描漏斗和超跌候选。
- 所有已分析评级，包括未达标币种。
- 本轮交易计划、开仓、风控拒绝、执行失败。
- Binance 服务端已平仓同步数量。
- 当前持仓涨跌、浮盈亏、名义价值、保证金、资金占比、杠杆、保证金收益率。
- 止损/止盈保护单健康状态、重复保护单、残留条件单。
- 账户资金、日亏损和风控阈值。

## 插针交易定时任务

固定入口：

```bash
.venv/bin/python3 skills/binance-trading/scripts/run_wick_cron.py --mode short --format markdown
```

与超跌/反转流水线的关键区别：
- 跳过 Skill-2（TradingAgents 评级），插针 Skill 直接输出 ratings，保证时效性。
- 更保守仓位（risk_ratio ≤ 1.5%），用仓位换速度。
- 影线尖端作为天然止损位（Skill-3 优先使用 wick_tip_price）。
- 建议 5~10 分钟调度一次（插针是分钟级事件）。

七维度评分：影线比率、插针幅度、成交量异动、价格回归度、关键价位触及、资金费率、ATR 相对幅度。

双模式：
- 短期（15m K 线）：捕捉实时插针，持仓 1h~12h。
- 长期（1h K 线）：等 K 线收线确认，持仓 4h~24h。

## Binance 数据能力

- 本地 SQLite K 线缓存：`data/binance_kline_cache.db`
- 支持 4h/1d 等周期，缓存优先、增量拉取。
- 超跌扫描：短期 4h、长期 1d。
- 底部反转扫描：短期 4h、长期 1d。
- 超买做空扫描：短期 4h、长期 1d。
- 插针检测扫描：短期 15m、长期 1h。
- `scan_oversold.py --json` 输出纯 JSON，供自动化脚本消费。

## A 股能力

- 趋势动量筛选。
- 短期/长期超跌反弹筛选。
- 底部放量反转筛选（含 T+1 跳空风险提示）。
- 大盘环境过滤（MarketRegimeFilter）：牛/熊/横盘三态，各策略独立开关，横盘自动提高评分门槛。
- TradingAgents 深度分析和报告保存。
- 本地 K 线缓存供 Skill 和 TradingAgents 共享。

## 数据资产

| 数据库/目录 | 路径 | 内容 |
| ------ | ------ | ------ |
| Binance K 线缓存 | `data/binance_kline_cache.db` | USDT 永续合约 K 线 |
| StateStore | `data/state_store.db` | Skill 输入输出快照 |
| MemoryStore / 风控状态 | `data/trading_state.db` | 历史交易、同步去重、Paper Mode runtime state |
| A 股 K 线缓存 | `data/kline_cache.db` | A 股历史 K 线 |
| 分析报告 | `data/reports/` | TradingAgents markdown 报告 |
