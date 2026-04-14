---
name: binance-data
description: Binance U本位合约数据基础设施与超跌反弹分析。本地SQLite缓存优先、增量联网拉取。超跌反弹扫描支持短期(4h)和长期(1d)双模式，八维度评分含资金费率。当用户说"币圈超跌"、"加密货币反弹"、"预加载K线"时使用。
user-invocable: true
metadata: {"openclaw":{"requires":{"bins":[".venv/bin/python3"]}}}
---

# Binance 合约历史数据服务（Data Provider）

底层数据基础设施 Skill，专门负责 Binance U本位合约历史 K 线数据的采集、缓存与分发。
为下游的量化筛选、深度分析、策略回测等 Skill 提供标准化数据源。

## 核心能力

- 本地 SQLite 缓存优先，增量联网拉取（仅拉缺失段）
- 支持所有 Binance 合约 K 线周期：1m/3m/5m/15m/30m/1h/2h/4h/6h/8h/12h/1d/3d/1w/1M
- 自动分页拉取（单次最多 1500 条，长区间自动分段）
- 标准化 JSON 输出：status_code + meta_info + data
- 集成 RateLimiter 限流 + 指数退避重试

## 用法

```bash
# 查询指定交易对的历史 K 线
.venv/bin/python3 {baseDir}/scripts/fetch_data.py BTCUSDT --start 2024-01-01 --end 2024-06-30

# 指定周期
.venv/bin/python3 {baseDir}/scripts/fetch_data.py ETHUSDT --start 2024-01-01 --end 2024-06-30 --interval 1d

# 输出原始 JSON
.venv/bin/python3 {baseDir}/scripts/fetch_data.py BTCUSDT --start 2024-01-01 --end 2024-06-30 --json

# 全市场预加载（所有 USDT 永续合约）
.venv/bin/python3 {baseDir}/scripts/preload_klines.py

# 指定交易对预加载
.venv/bin/python3 {baseDir}/scripts/preload_klines.py --symbols BTCUSDT ETHUSDT SOLUSDT

# 断点续传
.venv/bin/python3 {baseDir}/scripts/preload_klines.py --skip-existing
```

## 输出格式

### 成功响应（200）

```json
{
  "status_code": 200,
  "message": "success",
  "meta_info": {
    "symbol": "BTCUSDT",
    "interval": "4h",
    "data_source": "local_cache",
    "row_count": 180
  },
  "data": [
    {"open_time": 1704067200000, "open": 42000.0, "high": 42500.0, "low": 41800.0, "close": 42300.0, "volume": 1250.5, "quote_volume": 52625000.0, "trades": 85000}
  ]
}
```

## 数据字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `open_time` | int | K 线开盘时间 (ms) |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | float | 成交量 |
| `close_time` | int | K 线收盘时间 (ms) |
| `quote_volume` | float | 成交额 (USDT) |
| `trades` | int | 成交笔数 |

## 缓存策略

- 缓存存储在 `data/binance_kline_cache.db`（SQLite）
- 按 (symbol, interval, open_time) 三元组唯一索引
- 首次请求联网拉取后自动缓存
- 后续请求优先命中本地缓存
- API 限流：内置令牌桶 + 指数退避

## 下游编程调用

```python
from src.infra.binance_public import BinancePublicClient
from src.infra.binance_kline_cache import BinanceKlineCache
from src.infra.rate_limiter import RateLimiter

cache = BinanceKlineCache("data/binance_kline_cache.db")
client = BinancePublicClient(rate_limiter=RateLimiter(), kline_cache=cache)

# 带缓存的 K 线获取（Skill-1 无缝替换）
klines = client.get_klines_cached("BTCUSDT", "4h", 100)

# 按时间范围拉取（自动分页 + 缓存）
klines = client.get_klines_range("BTCUSDT", "4h", start_time_ms, end_time_ms)

cache.close()
```

## 超跌反弹扫描

全市场扫描超跌反弹候选币种，支持短期（4h）和长期（1d）两种模式。

```bash
# 短期超跌（4h，默认）— 捕捉恐慌抛售后的 V 型反转
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --mode short

# 长期超跌（1d 日线）— 捕捉中期超跌后的均值回归
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --mode long

# 指定币种
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --mode short --symbols BTC,ETH,SOL

# 调整评分阈值
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --mode long --min-score 30

# JSON 输出
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --mode short --json
```

### 短期超跌评分（4h K 线，满分 100）

侧重即时超卖信号和资金费率，适合日内/隔日超短线。

| 维度 | 权重 | 阈值 | 说明 |
|------|------|------|------|
| 资金费率 | 20 | < -0.1% | 币圈独有，空头拥挤=反弹概率高 |
| RSI(14) 超卖 | 18 | < 20 | 极端超卖 |
| BIAS(20) | 12 | < -10% | 短期偏离 |
| 底部放量 | 10 | ≥ 2.0x | 恐慌盘涌出 |
| 距高点回撤 | 10 | > -20% | 短期回撤 |
| 连续杀跌 | 10 | ≥5根/< -15% | 3 天内 |
| 布林带 | 8 | 跌破下轨 | |
| KDJ J值 | 7 | < 0 | |
| MACD 底背离 | 5 | | 4h 级别可靠性一般 |

### 长期超跌评分（1d 日线，满分 100）

侧重趋势偏离和背离信号，适合波段交易（3天~2周）。

| 维度 | 权重 | 阈值 | 说明 |
|------|------|------|------|
| BIAS(20) | 15 | < -15% | 日线偏离 |
| MACD 底背离 | 15 | 60 天回看 | 日线级别可靠性高 |
| 距高点回撤 | 15 | > -40% | 180 天回看，覆盖完整中期下跌 |
| 连续杀跌+累跌 | 12 | ≥3天/< -30% | 14 天内 |
| RSI(14) | 12 | < 30 | |
| 布林带 | 10 | 跌破下轨 | |
| 资金费率 | 8 | < -0.1% | 长期看权重降低 |
| KDJ J值 | 8 | < 0 | |
| 底部放量 | 5 | ≥ 1.5x | |

### 意图匹配指南

| 用户说的 | 应该调用 |
|---------|---------|
| "币圈超跌扫描" | `scan_oversold.py --mode short` |
| "长期超跌币种" | `scan_oversold.py --mode long` |
| "BTC 超跌了吗" | `scan_oversold.py --mode short --symbols BTC` |
| "预加载 K 线" | `preload_klines.py` |
| "查询 BTCUSDT 历史" | `fetch_data.py BTCUSDT --start 2024-01-01 --end 2024-06-30` |

## 底部放量反转扫描

全市场扫描底部放量反转候选币种，支持短期（4h）和长期（1d）两种模式。
与超跌反弹的区别：超跌 = "接飞刀"（暴跌中抄底），反转 = "确认转向"（底部构筑完成后入场）。

```bash
# 短期反转（4h，默认）— 捕捉 4h 级别底部放量后的 V/U 型反转
.venv/bin/python3 {baseDir}/scripts/scan_reversal.py --mode short

# 长期反转（1d 日线）— 捕捉日线级别底部构筑完成后的趋势反转
.venv/bin/python3 {baseDir}/scripts/scan_reversal.py --mode long

# 指定币种
.venv/bin/python3 {baseDir}/scripts/scan_reversal.py --mode short --symbols BTC,ETH,SOL

# 调整评分阈值
.venv/bin/python3 {baseDir}/scripts/scan_reversal.py --mode long --min-score 25

# JSON 输出
.venv/bin/python3 {baseDir}/scripts/scan_reversal.py --mode short --json
```

### 短期反转评分（4h K 线，满分 100）

侧重即时反转确认信号和资金费率回归，适合日内/隔日波段。

| 维度 | 权重 | 说明 |
|------|------|------|
| 底部放量 | 18 | 核心信号，近期量能 vs 前期地量（≥2x 触发，≥3.5x 满分）|
| 资金费率回归 | 15 | 币圈独有，从极端负值回归正常 = 空头平仓 = 反转信号 |
| 价格企稳 | 15 | 不再创新低 + 波动收窄 |
| 均线拐头 | 12 | EMA5 上穿 EMA10（金叉）或 EMA 拐头向上 |
| 距底部距离 | 10 | 距近期最低点 3%-12% 最佳（刚离开底部，还有空间）|
| MACD 反转 | 8 | 零轴下方金叉（4h 级别可靠性一般）|
| KDJ 低位金叉 | 8 | K 上穿 D 且都在 50 以下 |
| 前期跌幅 | 7 | 跌得越深反转空间越大（≥25% 满分）|
| 长下影线 | 7 | 下方有强支撑 |

### 长期反转评分（1d 日线，满分 100）

侧重趋势反转确认和 MACD 信号，适合波段交易（3天~2周）。

| 维度 | 权重 | 说明 |
|------|------|------|
| 均线拐头 | 15 | 日线级别 EMA 金叉/拐头信号强 |
| MACD 反转 | 15 | 日线 MACD 零轴下方金叉/底背离可靠性高 |
| 底部放量 | 15 | 日线放量更可靠（≥1.8x 触发，≥3x 满分）|
| 价格企稳 | 12 | 日线级别不再创新低 + 波动收窄 |
| 资金费率回归 | 10 | 长期看权重降低 |
| 距底部距离 | 10 | 距近期最低点 5%-15% 最佳 |
| 前期跌幅 | 8 | 中期跌幅深度 |
| KDJ 低位金叉 | 8 | 日线 KDJ 金叉 |
| 长下影线 | 7 | 日线长下影线 |

### 意图匹配指南（反转）

| 用户说的 | 应该调用 |
|---------|---------|
| "币圈反转扫描" | `scan_reversal.py --mode short` |
| "长期反转币种" | `scan_reversal.py --mode long` |
| "BTC 底部反转了吗" | `scan_reversal.py --mode short --symbols BTC` |
| "确认转向的币" | `scan_reversal.py --mode long` |

## 超买做空扫描

全市场扫描短期涨幅过大、多头过度拥挤的币种，寻找高胜率做空机会。
支持短期（4h）和长期（1d）两种模式。内置轧空风险排查。

```bash
# 短期超买（4h，默认）— 捕捉 FOMO 情绪见顶后的急跌
.venv/bin/python3 {baseDir}/scripts/scan_overbought.py --mode short

# 长期超买（1d 日线）— 捕捉日线级别持续上涨后的趋势衰竭
.venv/bin/python3 {baseDir}/scripts/scan_overbought.py --mode long

# 指定币种
.venv/bin/python3 {baseDir}/scripts/scan_overbought.py --mode short --symbols BTC,ETH,SOL

# 调整评分阈值
.venv/bin/python3 {baseDir}/scripts/scan_overbought.py --mode long --min-score 30

# JSON 输出
.venv/bin/python3 {baseDir}/scripts/scan_overbought.py --mode short --json
```

### 短期超买评分（4h K 线，满分 100）

侧重即时超买信号和资金费率，适合日内/隔日做空。

| 维度 | 权重 | 阈值 | 说明 |
|------|------|------|------|
| 资金费率极端正值 | 18 | > +0.1% | 做空最强信号，多头拥挤到极致 |
| RSI(14) 超买 | 15 | > 80 | 极端超买 |
| BIAS(20) | 12 | > +12% | 正向偏离均线 |
| 量价背离 | 12 | 价涨量缩 | 上涨动能衰竭 |
| 连续暴涨 | 10 | ≥5根/>+15% | 3 天内 |
| 布林带 | 8 | 突破上轨 | |
| KDJ 高位死叉 | 7 | J>100 或死叉 | |
| MACD 顶背离 | 5 | | 4h 级别可靠性一般 |
| 长上影线 | 5 | | 上方有强阻力 |
| 轧空风险 | -8 | 低流动性+高OI | 扣分，避免轧空陷阱 |

### 长期超买评分（1d 日线，满分 100）

侧重趋势衰竭和背离信号，适合波段做空（3天~2周）。

| 维度 | 权重 | 阈值 | 说明 |
|------|------|------|------|
| BIAS(20) | 15 | > +18% | 日线偏离 |
| MACD 顶背离 | 15 | 30 天回看 | 日线级别可靠性高 |
| 资金费率 | 12 | > +0.1% | 长期看权重降低 |
| 量价背离 | 12 | 价涨量缩 | 日线量价背离信号强 |
| 连续暴涨+距低点涨幅 | 12 | ≥5天/>+60% | |
| RSI(14) | 10 | > 75 | |
| 布林带 | 8 | 突破上轨 | |
| KDJ | 7 | J>100 或死叉 | |
| 长上影线 | 5 | | |
| 轧空风险 | -4 | | 长期看风险降低 |

### 意图匹配指南（做空）

| 用户说的 | 应该调用 |
|---------|---------|
| "超买做空扫描" | `scan_overbought.py --mode short` |
| "哪些币涨太多了" | `scan_overbought.py --mode short` |
| "长期超买币种" | `scan_overbought.py --mode long` |
| "BTC 超买了吗" | `scan_overbought.py --mode short --symbols BTC` |
| "适合做空的币" | `scan_overbought.py --mode short` |
| "FOMO 见顶了吗" | `scan_overbought.py --mode short` |

## 设计原则

1. 职责单一：仅负责"找数据、存数据、给数据"
2. 缓存优先：先查本地，最小化网络 I/O
3. 向后兼容：get_klines() 行为不变，get_klines_cached() 透明加速
4. 防封控：RateLimiter 限流 + 指数退避
5. 与 A 股缓存对齐：相同的设计模式，统一的开发体验
