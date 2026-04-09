# 长期记忆

## 系统架构

### 数据基础设施（2026-04-08 建立）

- 本地 SQLite K 线缓存：`data/kline_cache.db`
- 全市场预加载完成：5271 只股票，248 万行，324 MB
- 数据源优先级：腾讯日线（最稳定）→ 新浪 → 东方财富
- baostock 服务器（www.baostock.com:10030）当前网络环境连接不稳定
- 所有数据消费方共享同一缓存：AkshareClient / SkillDataProvider / TradingAgents

### 缓存链路

```
Skill-1A/1B (get_klines)       → AkshareClient → SQLite 缓存
TradingAgents (get_stock_data) → SQLite 缓存 → CSV fallback → akshare
快速分析 (_fetch_astock_quote) → 实时行情 → SQLite 缓存 → AkshareClient
SkillDataProvider (run)        → SQLite 缓存 → akshare 增量拉取
```

## 策略参数

### 加密货币

- 评级阈值: 6 分（范围 5-8）
- 风险比例: 2%（范围 0.5%-3%）
- 杠杆: 10x
- 持仓上限: 24 小时
- 止损: 3%，止盈: 6%（盈亏比 2:1）

### A 股超跌反弹（Skill-1B，2026-04-09 重构为双模式）

**短期超跌反弹（ShortTermAStockOversold）— 3~5 天持仓**
- RSI < 25（极端超卖）、BIAS(20) < -8%、连跌 ≥ 3 天、累跌 < -12%（10 天内）
- A 股独有：跌停板计数（权重 13）、底部放量（权重 13，T+1 恐慌盘释放）
- 核心逻辑：恐慌抛售 → 超卖极值 → 抛压出尽 → V 型反弹

**长期超跌蓄能（LongTermAStockOversold）— 2~4 周持仓**
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

### 缓存链路（更新）

```
Skill-1 (get_klines)           → BinancePublicClient → Binance fapi（无缓存，原有行为）
Skill-1 (get_klines_cached)    → BinanceKlineCache → Binance fapi（缓存优先）
预加载 (get_klines_range)      → Binance fapi → BinanceKlineCache（自动分页+回写）
fetch_data.py                  → BinanceKlineCache → BinancePublicClient（CLI 查询）
```

## 待办

- [ ] 用户转入 USDT 后开始加密货币实盘
- [ ] 先用 Paper Mode 跑几轮验证策略
- [ ] 定期更新本地 K 线缓存（每日增量）
- [ ] Skill-1 切换到 get_klines_cached() 以利用本地缓存
- [ ] 运行一次全市场预加载验证缓存链路
