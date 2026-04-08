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

### A 股超跌反弹（Skill-1B）

- RSI 阈值: 35（原 25，2026-04-08 放宽）
- BIAS 阈值: -6%（原 -10%）
- 近 10 日跌幅阈值: -8%（原 -15%）
- 最低评分: 25（原 50）
- 当日预筛: 已禁用（超跌是历史累积状态，不应只看当天）
- 效果: 筛选漏斗从 50→0 提升到 4252→475→30

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

## 待办

- [ ] 用户转入 USDT 后开始加密货币实盘
- [ ] 先用 Paper Mode 跑几轮验证策略
- [ ] 定期更新本地 K 线缓存（每日增量）
