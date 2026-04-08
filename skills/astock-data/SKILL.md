---
name: astock-data
description: A股历史数据基础设施服务。本地SQLite缓存优先、增量联网拉取、标准化JSON输出。为下游分析/回测/图表Skill提供稳定高效的数据源，避免重复联网。支持前复权/后复权/不复权，股票代码强校验（sh./sz./bj.前缀），数据清洗与校验。
user-invocable: true
metadata: {"openclaw":{"requires":{"bins":[".venv/bin/python3"]}}}
---

# A 股历史数据服务（Data Provider）

底层数据基础设施 Skill，专门负责 A 股历史行情数据的采集、缓存、清洗与分发。
为下游的因子分析、量化回测、图表渲染、研报生成等 Skill 提供标准化数据源。

## 核心能力

- 本地 SQLite 缓存优先，增量联网拉取（仅拉缺失段）
- 股票代码强校验：必须 `sh.`/`sz.`/`bj.` 前缀
- 支持前复权（qfq，默认）、后复权（hfq）、不复权（none）
- 数据清洗：空值填补、字段标准化、时间正序、close ∈ [low, high] 校验
- 标准化 JSON 输出：status_code + meta_info + data
- 接口防封控：内置延时重试 + 指数退避

## 用法

```bash
# 基本用法
.venv/bin/python3 {baseDir}/scripts/fetch_data.py sh.600519 2024-01-01 2024-06-30

# 后复权
.venv/bin/python3 {baseDir}/scripts/fetch_data.py sz.000001 2024-01-01 2024-12-31 --adjust hfq

# 不复权
.venv/bin/python3 {baseDir}/scripts/fetch_data.py sh.600519 2024-01-01 2024-06-30 --adjust none

# 输出原始 JSON（供程序调用）
.venv/bin/python3 {baseDir}/scripts/fetch_data.py sh.600519 2024-01-01 2024-06-30 --json
```

## 股票代码规范

| 前缀 | 交易所 | 示例 |
|------|--------|------|
| `sh.` | 上证 | `sh.600519`（贵州茅台） |
| `sz.` | 深证 | `sz.000001`（平安银行） |
| `bj.` | 北交所 | `bj.830799` |

不符合规范的代码（如 `AAPL`、`600519`）会被直接拒绝，返回 400。

## 输出格式

### 成功响应（200）

```json
{
  "status_code": 200,
  "message": "success",
  "meta_info": {
    "symbol": "sz.000001",
    "name": "平安银行",
    "frequency": "daily",
    "adjust": "qfq",
    "data_source": "local_cache",
    "row_count": 3
  },
  "data": [
    {"date": "2024-01-02", "open": 10.50, "high": 10.65, "low": 10.45, "close": 10.60, "volume": 1250000, "amount": 13250000.0},
    {"date": "2024-01-03", "open": 10.60, "high": 10.72, "low": 10.58, "close": 10.68, "volume": 1100000, "amount": 11710000.0}
  ]
}
```

### 错误响应（400/404/500）

```json
{
  "status_code": 400,
  "message": "Invalid symbol format. Expected prefix 'sh.', 'sz.', or 'bj.'. Received: 'AAPL'",
  "meta_info": {"symbol": "AAPL", "name": "", "frequency": "daily", "adjust": "qfq", "data_source": "none", "row_count": 0},
  "data": []
}
```

## 数据字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `date` | string | 日期 YYYY-MM-DD |
| `open` | float | 开盘价 |
| `high` | float | 最高价 |
| `low` | float | 最低价 |
| `close` | float | 收盘价 |
| `volume` | int | 成交量（手） |
| `amount` | float | 成交额（元） |

## data_source 字段说明

| 值 | 含义 |
|----|------|
| `local_cache` | 全部命中本地缓存，零网络请求 |
| `api` | 全部来自联网拉取（首次请求） |
| `mixed` | 部分缓存 + 部分联网（增量更新） |
| `none` | 无数据（错误响应） |

## 缓存策略

- 缓存存储在 `data/kline_cache.db`（SQLite）
- 按 (symbol, adjust, date) 三元组唯一索引
- 首次请求联网拉取后自动缓存
- 后续请求仅拉取缺失的日期段
- API 不可用时自动降级返回已有缓存数据

## 下游 Skill 编程调用

```python
from src.infra.akshare_client import AkshareClient
from src.infra.state_store import StateStore
from src.skills.skill_data_provider import SkillDataProvider

store = StateStore()
client = AkshareClient()
skill = SkillDataProvider(store, input_schema, output_schema, client)

result = skill.run({
    "symbol": "sh.600519",
    "start_date": "2024-01-01",
    "end_date": "2024-06-30",
    "adjust": "qfq",
})

if result["status_code"] == 200:
    for row in result["data"]:
        print(row["date"], row["close"])
```

## 设计原则

1. 职责单一：仅负责"找数据、存数据、给数据"，不参与任何业务逻辑分析
2. 缓存优先：先查本地，只拉缺失段，最小化网络 I/O
3. 数据准确：不篡改原始数值，复权数据与交易所一致
4. 防封控：API 调用间隔 ≥ 300ms，指数退避重试
5. 优雅降级：API 不可用时返回已有缓存 + 警告信息

## 注意事项

- 数据源为 akshare（东方财富/腾讯），无需 API Key
- 缓存数据持久化在本地 SQLite，重启不丢失
- 当前仅支持日线（daily），分钟线后续扩展
