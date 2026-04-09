---
name: binance-data
description: Binance U本位合约历史K线数据基础设施服务。本地SQLite缓存优先、增量联网拉取、标准化JSON输出。为下游交易分析/回测/图表Skill提供稳定高效的数据源，避免重复联网。支持多周期（1m~1M），交易对自动校验。
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

全市场扫描超跌反弹候选币种，八维度量化评分。

```bash
# 全市场扫描
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py

# 指定币种
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --symbols BTC,ETH,SOL

# 调整评分阈值
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --min-score 30

# JSON 输出
.venv/bin/python3 {baseDir}/scripts/scan_oversold.py --json
```

### 超跌评分体系（满分 100，针对加密货币市场）

| 维度 | 权重 | 阈值 | 说明 |
|------|------|------|------|
| RSI(14) 超卖 | 15 | < 25 | 比 A 股(35)更极端，币圈波动大 |
| 乖离率 BIAS(20) | 15 | < -12% | 比 A 股(-6%)更极端 |
| 连续杀跌+累计跌幅 | 12 | ≥5根/< -20% | 4h K 线，5根≈20小时 |
| 布林带下轨突破 | 10 | 跌破下轨 | 标准 BOLL(20,2) |
| MACD 底背离 | 10 | 价格新低+MACD未新低 | 动量反转信号 |
| KDJ J值极值 | 8 | J < 0 | 超卖极值 |
| 资金费率 | 15 | < -0.1% | 币圈独有，空头拥挤=反弹概率高 |
| 距高点回撤 | 10 | > 30% | 币圈常见深度回调后反弹 |
| 底部放量(加分) | 5 | ≥ 2.0x | 恐慌盘涌出信号 |

## 设计原则

1. 职责单一：仅负责"找数据、存数据、给数据"
2. 缓存优先：先查本地，最小化网络 I/O
3. 向后兼容：get_klines() 行为不变，get_klines_cached() 透明加速
4. 防封控：RateLimiter 限流 + 指数退避
5. 与 A 股缓存对齐：相同的设计模式，统一的开发体验
