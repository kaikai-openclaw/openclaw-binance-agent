# 长期记忆

## 系统架构

### 数据基础设施（2026-04-08 建立）

- 本地 SQLite K 线缓存：`data/kline_cache.db`
- 全市场预加载完成：5271 只股票，248 万行，324 MB
- 数据源优先级：腾讯日线（最稳定）→ 新浪 → 东方财富
- baostock 服务器（www.baostock.com:10030）当前网络环境连接不稳定
- 所有数据消费方共享同一缓存：AkshareClient / SkillDataProvider / TradingAgents

### 缓存链路

```text
Skill-1A/1B (get_klines)       → AkshareClient → SQLite 缓存
TradingAgents (get_stock_data) → SQLite 缓存 → CSV fallback → akshare
快速分析 (_fetch_astock_quote) → 实时行情 → SQLite 缓存 → AkshareClient
SkillDataProvider (run)        → SQLite 缓存 → akshare 增量拉取
```

## 策略参数

### 加密货币

- 评级阈值: 默认 6 分，可由 MemoryStore 自我进化调整。
- 风险比例: 默认 2%，可由历史交易表现调整。
- 杠杆: 默认 10x，下单前显式同步 Binance symbol 杠杆。
- 持仓上限: 默认 24 小时。
- 止损止盈: 优先 ATR 动态止损止盈；无 ATR 时回退固定百分比。
- 高波动过滤: ATR 原始止损距离超过 `max_stop_pct` 时跳过交易，不强行截断。

### A 股超跌反弹（Skill-1B，2026-04-09 重构为双模式）

#### 短期超跌反弹（ShortTermAStockOversold）— 3~5 天持仓

- RSI < 25（极端超卖）、BIAS(20) < -8%、连跌 ≥ 3 天、累跌 < -12%（10 天内）
- A 股独有：跌停板计数（权重 13）、底部放量（权重 13，T+1 恐慌盘释放）
- 核心逻辑：恐慌抛售 → 超卖极值 → 抛压出尽 → V 型反弹

#### 长期超跌蓄能（LongTermAStockOversold）— 2~4 周持仓

- RSI < 35、BIAS(60) < -15%、连跌 ≥ 5 天、累跌 < -25%（30 天内）
- A 股独有：缩量企稳（权重 13，地量见地价）、60 日乖离率（权重 18）
- MACD 底背离回看 60 天（权重 18）、距 120 日高点回撤 > 30%（权重 15）
- 核心逻辑：持续阴跌 → 深度偏离均线 → 缩量筑底 → 趋势反转

## 关键决策记录

- 2025-06-26: 系统初始化，Binance fapi 连通测试通过（香港 IP）
- 2025-06-26: TradingAgents + Gemini 2.5 Flash 测试通过
- 2026-04-08: 建立 A 股本地 K 线缓存基础设施
- 2026-04-08: 重设计 Skill-1B 超跌筛选条件，去掉当日预筛
- 2026-04-08: TradingAgents 接入共享 SQLite 缓存
- 2026-04-29: 加固 Binance 执行链路：签名重试刷新 timestamp、请求携带 `recvWindow`、下单数量/价格统一十进制格式化。
- 2026-04-29: 风控状态持久化：`RiskController` 支持 `enable_paper_mode()` / `disable_paper_mode()` 并写入 SQLite runtime state。
- 2026-04-29: 服务端保护单改为 `closePosition=true`，并在每轮执行开始清理无持仓残留 Algo 条件单。
- 2026-04-29: 新增 `BinanceTradeSyncer`，从 Binance `userTrades.realizedPnl` 同步服务端触发后的真实平仓成交，幂等写入 MemoryStore。
- 2026-04-29: 新增超跌定时任务固定报告入口 `skills/binance-trading/scripts/run_oversold_cron.py`，稳定输出扫描、评级、持仓、保护单、账户和风险状态。
- 2026-05-03: 风控扩展为六大约束，新增总敞口上限（总资金 × 4x）和最大同时持仓数（30）。
- 2026-05-03: Skill-4 新增止损上移（Break-even + 阶梯锁利，3步）和时间衰减止盈（持仓超时后下调止盈，2步）。
- 2026-05-03: `scripts/manage_positions.py` 支持做空持仓管理，新增进程锁（fcntl.flock）防止 cron 重叠，原子写入状态文件。
- 2026-05-03: 超买做空策略优化：评分门槛 40→30（回测胜率 63.6%），顶部确认扩展为 4 个信号（MACD 顶背离、RSI 顶背离、KDJ 死叉、量价背离），4h/1d 最大回撤门槛收紧至 15%。
- 2026-05-03: 新增 A 股大盘环境过滤模块 `src/infra/market_regime.py`（MarketRegimeFilter），牛/熊/横盘三态，各策略独立开关，横盘自动提高评分门槛。
- 2026-05-03: A 股反转策略新增 T+1 跳空风险提示，权重优化，大盘环境过滤集成。
- 2026-05-03: 新增回测框架：`scripts/backtest_crypto.py`（加密货币）和 `scripts/backtest_astock.py`（A 股），用于策略参数验证。

## 网络环境备忘

- 腾讯 qt.gtimg.cn / web.ifzq.gtimg.cn：稳定可用
- 东方财富 push2his.eastmoney.com：不稳定，频繁断连
- akshare stock_info_a_code_name：不稳定，经常中途断连
- baostock TCP 10030：端口可达但协议握手卡住

### Binance 合约 K 线缓存（2026-04-09 建立）

- 本地 SQLite 缓存：`data/binance_kline_cache.db`
- 缓存模块：`src/infra/binance_kline_cache.py`（BinanceKlineCache）
- 按 (symbol, interval, open_time) 三元组唯一索引
- BinancePublicClient 新增 `kline_cache` 参数 + `get_klines_cached()` / `get_klines_range()` 方法
- BinancePublicClient 新增 `get_funding_rates_all()` / `get_open_interest()` 资金费率和持仓量接口
- 预加载完成：538 个 USDT 永续合约，552,132 行 4h K 线，84.3 MB
- 预加载脚本：`skills/binance-data/scripts/preload_klines.py`
- 数据查询脚本：`skills/binance-data/scripts/fetch_data.py`
- 设计与 A 股 KlineCache 对齐：缓存优先、WAL 模式、增量拉取

### 加密货币超跌反弹筛选（2026-04-09 建立）

- Skill 模块：`src/skills/crypto_oversold.py`
  - `ShortTermOversoldSkill` — 短期超跌（4h），捕捉恐慌抛售 V 型反转
  - `LongTermOversoldSkill` — 长期超跌（1d），捕捉中期均值回归
  - `CryptoOversoldSkill` — 向后兼容别名，指向短期版本
- CLI 脚本：`skills/binance-data/scripts/scan_oversold.py --mode short|long`
- 短期核心信号：RSI<20 + 资金费率(权重20) + 底部放量(权重10)
- 长期核心信号：BIAS<-15% + MACD底背离(权重15) + 距高点回撤(权重15)
- 首次扫描：短期 5 候选，长期 11 候选（PIPPINUSDT 评分 47 最高）

### Binance 实盘执行安全（2026-04-29 加固）

- `src/infra/binance_fapi.py`
  - 所有签名请求重试时重新签名，避免 timestamp 过期。
  - 默认携带 `recvWindow=5000`。
  - HTTP 错误会保留 Binance 原始响应正文，便于定位精度、余额、签名问题。
  - 下单价格和数量通过 `format_decimal_param()` 输出，避免整数精度币种提交 `33216.0`。
- `src/skills/skill4_execute.py`
  - 执行前按交易所规则规整 entry price、quantity 和触发价。
  - 服务端 STOP_MARKET / TAKE_PROFIT_MARKET 使用 `closePosition=true`。
  - 入场成交但服务端保护单全部失败时立即平仓，避免裸仓。
  - 每轮开始清理无持仓残留保护条件单，减少反向开仓风险。
- `src/infra/trade_sync.py`
  - 同步 Binance `userTrades` 的已实现盈亏成交。
  - 按订单聚合部分成交。
  - 使用 `trade_sync_keys` 幂等去重，避免重复写入交易记录。

### 超跌定时任务报告（2026-04-29 建立）

- 固定入口：`skills/binance-trading/scripts/run_oversold_cron.py`
- Markdown 输出用于 Telegram/定时任务，JSON 输出用于调试和后续自动化。
- 报告固定包含：
  - 扫描漏斗和超跌候选。
  - 所有已分析评级，包括未达标币种的评分、方向、置信度。
  - 本轮交易计划、开仓、风控拒绝、执行失败。
  - 服务端已平仓同步数量。
  - 当前持仓涨跌、浮盈亏、名义价值、保证金、资金占比、杠杆、保证金收益率。
  - 止损/止盈保护单健康状态，含重复保护单和残留条件单告警。
  - 账户总资金、可用保证金、持仓资金占比、日亏损和风控阈值。

### 缓存链路（更新）

```text
Skill-1 (get_klines)           → BinancePublicClient → Binance fapi（无缓存，原有行为）
Skill-1 (get_klines_cached)    → BinanceKlineCache → Binance fapi（缓存优先）
预加载 (get_klines_range)      → Binance fapi → BinanceKlineCache（自动分页+回写）
fetch_data.py                  → BinanceKlineCache → BinancePublicClient（CLI 查询）
```

### 止损上移与时间衰减止盈（2026-05-03 新增）

- 止损上移（Break-even + 阶梯锁利）：
  - step 0 → 1：盈利达 1.3x sl_dist，止损移至保本（entry_price）
  - step 1 → 2：盈利达 1.8x sl_dist，止损锁住 0.5x 利润
  - step 2 → 3：盈利达 2.3x sl_dist，止损锁住 1x 利润
  - 仅在 `position_opened && sl_dist > 0 && server_sl_tp_placed` 时执行
- 时间衰减止盈：
  - step 0 → 1：持仓超过 max_hold_hours × 75%，止盈下调 40%
  - 仅在 `sl_step == 0`（止损未上移，仍在亏损区）时执行，避免已保本后催促止盈
  - 仅在 `sl_step == 0`（止损未上移，仍在亏损区）时执行，避免已保本后催促止盈

### 超买做空策略优化（2026-05-03 回测验证）

- 评分门槛：40 → 30（4h/1d 回测胜率 63.6%，均收益 +2.09%；1h 胜率 70.1%，均收益 +3.49%）
- 顶部确认扩展为 4 个信号（满足任一即通过）：
  1. MACD 顶背离（修复后的双峰检测）
  2. RSI 顶背离（短周期上比 MACD 更稳定）
  3. KDJ 高位死叉（1h 阈值 70，4h/1d 阈值 80）
  4. 量价背离（价涨量缩，动能衰竭直接证据）
- 4h/1d 最大回撤门槛：-20% → -15%（收紧，减少追空空间已消耗的情况）

### A 股大盘环境过滤（2026-05-03 新增）

- 模块：`src/infra/market_regime.py`（MarketRegimeFilter）
- 进程级单例，TTL 内存缓存（10 分钟），K 线走 SQLite 缓存
- 三态分类：bull（多头排列）/ bear（空头排列）/ sideways（横盘）
- 各策略开关：
  - `allow_trend`：牛市/横盘且近 5 日未加速下跌
  - `allow_oversold`：牛市正常；横盘且近 5 日未加速下跌；熊市关闭
  - `allow_reversal`：牛市/横盘均可；熊市仅高分才入场
- 横盘时自动提高评分门槛：超跌 +15，趋势 +10

### 参数调整与Bug修复（2026-05-09）

- ATR 止盈乘数调整：`DEFAULT_ATR_TP_MULT` 2.8 → 2.3（盈亏比 1.87:1 → 1.53:1）
- skill3_strategy: wick_tip 路径改用动态乘数计算止盈距离
- skill3_strategy: 做空硬顶检查优先级提升，避免被高波动跳过逻辑绕过
- manage_positions: tp_improved 时正确保存 original_tp 为当前止盈价（修复时间衰减止盈基准错误）
- risk_controller: db_path 为空时黑名单初始化兜底（修复测试环境 AttributeError）

## 待办

- [ ] 定期检查保护单重复和残留条件单告警
- [ ] 定期更新 Binance K 线缓存（每日增量）
- [ ] 继续扩大服务端成交同步的元数据覆盖，例如真实持仓时长和策略来源
- [ ] 评估是否将 `check_account.py` 也改造成结构化 JSON + Markdown 双输出
- [ ] 定期运行回测脚本验证策略参数是否需要再调整
