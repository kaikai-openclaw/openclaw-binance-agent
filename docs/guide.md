# OpenClaw Binance 交易 Agent — 配置与使用指南

## 1. 系统简介

本系统是一个基于 OpenClaw 框架的加密货币自动化交易 Agent，采用 5 步流水线（Pipeline）架构：

```
信息收集 → 深度分析 → 策略制定 → 自动执行 → 展示进化
(Skill-1)  (Skill-2)  (Skill-3)  (Skill-4)  (Skill-5)
```

核心特性：
- 状态 ID 指针模式：Skill 间仅传递 UUID，全量数据存于 SQLite，避免上下文膨胀
- 硬编码风控：四条不可绕过的风控规则，保护资金安全
- 优雅降级：日亏损 ≥ 5% 自动切换 Paper Mode，继续收集数据
- 自我进化：基于历史交易自动调优评级阈值和风险比例
- 防幻觉约束：Skill-1 仅输出经外部来源验证的真实数据

---

## 2. 环境准备

### 2.1 系统要求

- Python >= 3.10
- macOS / Linux
- Binance 合约账户（实盘模式需要）

### 2.2 安装步骤

```bash
# 克隆项目
git clone <repo-url>
cd openclaw-binance-agent

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装运行时依赖
pip install jsonschema[format] requests websocket-client pyyaml

# 安装开发/测试依赖
pip install pytest hypothesis hypothesis-jsonschema
```

### 2.3 验证安装

```bash
python -m pytest tests/ -v
# 预期输出：304 passed
```

---

## 3. 项目结构

```
openclaw-binance-agent/
├── config/
│   └── schemas/                  # JSON Schema 定义（10 个文件）
│       ├── skill1_input.json     # Skill-1 输入：触发时间 + 搜索关键词
│       ├── skill1_output.json    # Skill-1 输出：候选币种列表
│       ├── skill2_input.json     # Skill-2 输入：上游 state_id
│       ├── skill2_output.json    # Skill-2 输出：评级结果
│       ├── skill3_input.json     # Skill-3 输入：上游 state_id
│       ├── skill3_output.json    # Skill-3 输出：交易计划
│       ├── skill4_input.json     # Skill-4 输入：上游 state_id
│       ├── skill4_output.json    # Skill-4 输出：执行结果
│       ├── skill5_input.json     # Skill-5 输入：上游 state_id（可选）
│       └── skill5_output.json    # Skill-5 输出：账户摘要 + 进化数据
├── src/
│   ├── agent.py                  # Pipeline 编排器
│   ├── models/types.py           # 数据模型 + 计算函数
│   ├── skills/                   # 5 个 Skill 实现
│   │   ├── base.py               # Skill 基类
│   │   ├── skill1_collect.py     # 信息收集与候选筛选
│   │   ├── skill2_analyze.py     # 深度分析与评级
│   │   ├── skill3_strategy.py    # 交易策略制定
│   │   ├── skill4_execute.py     # 自动交易执行
│   │   └── skill5_evolve.py      # 展示与自我进化
│   └── infra/                    # 基础设施层
│       ├── state_store.py        # 状态存储（SQLite）
│       ├── memory_store.py       # 长期记忆库（SQLite）
│       ├── risk_controller.py    # 风控拦截层
│       ├── rate_limiter.py       # 令牌桶限流器
│       └── binance_fapi.py       # Binance 合约客户端
├── tests/                        # 304 个测试
├── data/                         # SQLite 数据库（运行时生成）
└── scripts/                      # 启动脚本
```

---

## 4. 配置文件

创建 `config/default.yaml`：

```yaml
# ============================================================
# API 连接配置
# ============================================================
api:
  # Binance U本位合约 API 地址
  binance_fapi_base_url: "https://fapi.binance.com"
  # API 密钥（从环境变量读取更安全，此处仅示例）
  api_key: ""
  api_secret: ""
  # 单次请求超时（秒）
  request_timeout: 10
  # 最大重试次数
  max_retries: 5
  # 指数退避序列（秒）
  backoff_sequence: [1, 2, 4, 8, 16]

# ============================================================
# 限流配置
# ============================================================
rate_limiter:
  # 正常速率（次/分钟）— Binance 限制为 1200，留 200 余量
  normal_rate: 1000
  # 降级速率（次/分钟）— 队列拥堵时自动降速
  degraded_rate: 500
  # 队列阈值 — 待发送请求超过此值时触发降速
  queue_threshold: 800

# ============================================================
# 策略参数（可调整）
# ============================================================
strategy:
  # 评级过滤阈值（1-10）— 低于此分数的币种不参与交易
  # 自我进化模块可能将此值调高至最多 8
  rating_threshold: 6

  # 账户风险比例 — 每笔交易承担的最大风险占总资金的比例
  # 自我进化模块可能将此值调低至最低 0.005
  risk_ratio: 0.02

  # 默认杠杆倍数
  default_leverage: 10

  # 持仓时间上限（小时）— 超时自动市价平仓
  max_hold_hours: 24.0

# ============================================================
# 持仓监控
# ============================================================
monitoring:
  # 轮询间隔（秒）— 每隔多久检查一次持仓状态
  poll_interval: 30

# ============================================================
# 数据存储
# ============================================================
storage:
  # State_Store 数据库路径 — 存储各 Skill 的输入输出快照
  state_store_db: "data/state_store.db"
  # Memory_Store 数据库路径 — 存储历史交易记录和反思日志
  memory_store_db: "data/memory_store.db"

# ============================================================
# 搜索关键词（Skill-1 使用）
# ============================================================
search:
  keywords:
    - "crypto market hot"
    - "cryptocurrency trending"
    - "binance futures top gainers"

# ============================================================
# 日志配置
# ============================================================
logging:
  level: "INFO"    # DEBUG / INFO / WARNING / ERROR / CRITICAL
  format: "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
```

### 4.1 环境变量（推荐）

生产环境建议通过环境变量传递 API 密钥：

```bash
export BINANCE_API_KEY="your_api_key_here"
export BINANCE_API_SECRET="your_api_secret_here"
```

---

## 5. 风控体系详解

### 5.1 四条硬编码规则

这些规则在 `RiskController` 中硬编码，无法通过配置文件修改：

| # | 规则 | 阈值 | 触发行为 |
|---|------|------|----------|
| 1 | 单笔保证金上限 | 总资金 × 20% | 拒绝订单 |
| 2 | 单币持仓上限 | 总资金 × 30% | 拒绝订单 |
| 3 | 日亏损降级 | 总资金 × 5% | 执行降级流程 |
| 4 | 止损冷却期 | 24 小时 | 拒绝同币种同方向开仓 |

### 5.2 降级流程

当日亏损达到 5% 时，系统按以下顺序执行降级：

```
日亏损 ≥ 5%
    │
    ├─ 1. 取消所有未成交挂单
    ├─ 2. 停止所有实盘下单
    ├─ 3. 发出 CRITICAL 告警
    └─ 4. 切换至 Paper Trading Mode
```

Paper Mode 下：
- Pipeline 继续正常执行（收集数据、分析、制定策略）
- 所有订单仅在本地模拟记录，不提交至 Binance
- 订单状态标记为 `paper_trade`
- 展示界面明确标注"模拟盘"

### 5.3 止损冷却期

某币种触发止损平仓后，24 小时内禁止该币种同方向开仓（禁逆势补仓）：

```
BTCUSDT 做多止损触发
    │
    ├─ 24h 内：BTCUSDT 做多 → 拒绝
    ├─ 24h 内：BTCUSDT 做空 → 允许（不同方向）
    ├─ 24h 内：ETHUSDT 做多 → 允许（不同币种）
    └─ 24h 后：BTCUSDT 做多 → 允许（冷却期已过）
```

---

## 6. Pipeline 流程详解

### 6.1 Skill-1：Binance 量化数据采集与候选筛选

- 调用 Binance 合约公开 API（无需 API Key）
- Step 0: 获取可交易交易对（exchangeInfo）
- Step 1: 大盘过滤（ticker/24hr 成交额、振幅、涨幅区间）
- Step 2: 活跃度异动（K线量比，短期/长期成交量对比）
- Step 3: 技术指标多因子评分（RSI + EMA 多头排列 + MACD）
- 按综合评分排序，输出 top N 候选
- 输出：候选币种列表（symbol + 量化指标 + signal_score）

### 6.2 Skill-2：深度分析与评级

- 从 State_Store 读取候选币种
- 对每个币种调用 TradingAgents 分析（30 秒超时）
- 过滤评级分 < 阈值（默认 6 分）的币种
- 超时或错误的币种跳过，不阻塞其他币种
- 输出：评级结果（rating_score + signal + confidence）

### 6.3 Skill-3：交易策略制定

- 从 State_Store 读取评级结果
- 使用固定风险模型计算头寸规模：
  ```
  头寸规模 = (风险比例 × 总资金) / |入场价 - 止损价|
  ```
- 头寸规模超过 20% 自动裁剪
- 执行风控预校验，不通过则逐步裁剪（每次 -10%，最多 10 次）
- 空评级列表 → 标记 `no_opportunity`，跳过 Skill-4
- 输出：交易计划（方向 + 入场区间 + 止损止盈 + 头寸规模）

### 6.4 Skill-4：自动交易执行

- 从 State_Store 读取交易计划
- 检查日亏损，必要时执行降级
- 对每笔交易：风控校验 → 提交限价订单 → 轮询监控持仓
- 平仓条件：止损（做多: 当前价 ≤ 止损价）、止盈、超时
- Paper Mode 下不调用真实 API
- 输出：执行结果（订单状态 + 成交信息 + Paper Mode 标记）

### 6.5 Skill-5：展示与自我进化

- 读取账户状态，生成 Markdown 表格报告
- 提取已平仓交易存入 Memory_Store
- 基于最近 50 笔交易计算胜率和平均盈亏比
- 胜率 < 40% 时生成调优建议（提高评级阈值 + 降低风险比例）
- 交易记录 < 10 笔时跳过进化，使用默认参数
- 输出：账户摘要 + 持仓明细 + 进化数据

### 6.6 崩溃恢复

Pipeline 支持从崩溃点恢复：

```bash
python scripts/run_pipeline.py --resume
```

恢复逻辑：从 State_Store 中查找最后一个成功执行的 Skill，从其下一个 Skill 继续执行。

---

## 7. 自我进化机制

### 7.1 触发条件

| 条件 | 行为 |
|------|------|
| 交易记录 < 10 笔 | 跳过进化，使用默认参数 |
| 胜率 ≥ 40% | 维持默认参数（阈值=6，风险比例=2%） |
| 胜率 < 40% | 生成调优建议 |

### 7.2 调优公式

```
新评级阈值 = min(8, 6 + int((40 - 胜率) / 10))
新风险比例 = max(0.005, 0.02 × (胜率 / 40))
```

示例：
- 胜率 30% → 阈值 7，风险比例 1.5%
- 胜率 20% → 阈值 8，风险比例 1.0%
- 胜率 10% → 阈值 8，风险比例 0.5%

### 7.3 参数范围

| 参数 | 默认值 | 最小值 | 最大值 |
|------|--------|--------|--------|
| 评级过滤阈值 | 6 | 6 | 8 |
| 风险比例 | 0.02 (2%) | 0.005 (0.5%) | 0.02 (2%) |

---

## 8. 网络容错

### 8.1 重试策略

```
请求失败
    │
    ├─ 第 1 次重试：等待 1 秒
    ├─ 第 2 次重试：等待 2 秒
    ├─ 第 3 次重试：等待 4 秒
    ├─ 第 4 次重试：等待 8 秒
    ├─ 第 5 次重试：等待 16 秒
    └─ 全部失败：抛出 MaxRetryExceededError + 告警
```

### 8.2 HTTP 状态码处理

| 状态码 | 含义 | 处理方式 |
|--------|------|----------|
| 429 | 请求过多 | 暂停 30 秒后恢复，继续重试 |
| 418 | IP 被封禁 | 立即停止所有请求，发出紧急告警 |
| 其他 4xx/5xx | 服务端错误 | 指数退避重试 |

### 8.3 网络恢复同步

网络断线恢复后，系统自动调用 `sync_after_reconnect()` 重新同步：
1. 账户信息（余额、保证金）
2. 所有未平仓持仓
3. 所有未完成订单

---

## 9. 数据存储

### 9.1 State_Store（状态存储）

SQLite 表 `state_snapshots`：

| 字段 | 类型 | 说明 |
|------|------|------|
| state_id | TEXT PK | UUID v4 |
| skill_name | TEXT | Skill 名称 |
| data | TEXT | JSON 序列化的完整数据快照 |
| created_at | TEXT | ISO 8601 时间戳 |
| status | TEXT | success / failed |

### 9.2 Memory_Store（长期记忆）

SQLite 表 `trade_records`：

| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | TEXT | 币种交易对 |
| direction | TEXT | long / short |
| entry_price | REAL | 入场价格 |
| exit_price | REAL | 平仓价格 |
| pnl_amount | REAL | 盈亏金额 |
| hold_duration_hours | REAL | 持仓时长 |
| rating_score | INTEGER | 评级分 |
| position_size_pct | REAL | 头寸规模百分比 |
| closed_at | TEXT | 平仓时间 |

SQLite 表 `reflection_logs`：

| 字段 | 类型 | 说明 |
|------|------|------|
| win_rate | REAL | 胜率 |
| avg_pnl_ratio | REAL | 平均盈亏比 |
| suggested_rating_threshold | INTEGER | 建议评级阈值 |
| suggested_risk_ratio | REAL | 建议风险比例 |
| reasoning | TEXT | 调优推理过程 |

---

## 10. 扩展与接入

系统通过依赖注入设计，所有外部服务均可替换：

| 组件 | 注入点 | 说明 |
|------|--------|------|
| 信息源 | Skill1Collect(client) | 注入 BinancePublicClient 实例 |
| 分析引擎 | TradingAgentsModule(analyzer) | 替换为 TradingAgents 实际调用 |
| 交易客户端 | Skill4Execute(binance_client) | 替换为真实 BinanceFapiClient |
| 账户状态 | account_state_provider 回调 | 替换为实时 API 查询 |

### 10.1 接入真实 Binance API

```python
from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter

client = BinanceFapiClient(
    api_key="your_key",
    api_secret="your_secret",
    rate_limiter=RateLimiter(),
)
```

### 10.2 接入 TradingAgents

```python
from src.skills.skill2_analyze import TradingAgentsModule

def real_analyzer(symbol: str, market_data: dict) -> dict:
    # 调用 TradingAgents 开源项目
    # https://github.com/TauricResearch/TradingAgents
    result = trading_agents_api.analyze(symbol, market_data)
    return {
        "rating_score": result.score,
        "signal": result.signal,
        "confidence": result.confidence,
    }

module = TradingAgentsModule(analyzer=real_analyzer)
```

---

## 11. 测试说明

### 11.1 测试分类

| 类型 | 文件 | 数量 | 说明 |
|------|------|------|------|
| 属性测试 | test_properties.py | ~90 | 19 个正确性属性，hypothesis 随机生成 |
| 单元测试 | test_*.py | ~170 | 各模块独立测试 |
| Schema 测试 | test_schema_validation.py | ~49 | 缺字段/类型错误/值越界/格式错误 |
| 集成测试 | test_skills.py | 3 | 完整 Pipeline 链路 |

### 11.2 运行命令

```bash
# 全部测试
python -m pytest tests/ -v

# 仅属性测试（含 hypothesis）
python -m pytest tests/test_properties.py -v

# 仅某个模块
python -m pytest tests/test_risk_controller.py -v

# 仅集成测试
python -m pytest tests/test_skills.py -v

# 查看测试覆盖率（需安装 pytest-cov）
python -m pytest tests/ --cov=src --cov-report=term-missing
```

### 11.3 属性测试列表

| # | 属性 | 验证内容 |
|---|------|----------|
| 1 | State_Store round-trip | 存取数据一致性 + UUID v4 格式 |
| 2 | Schema 合法数据通过 | 符合 Schema 的数据通过校验 |
| 3 | Schema 非法数据拒绝 | 缺字段/额外字段/类型错误被拒绝 |
| 4 | 数据来源标注 | source_url 为合法 URI，collected_at 为 ISO 8601 |
| 5 | 评级过滤阈值 | 输出中所有 rating_score ≥ 阈值 |
| 6 | 头寸规模计算 | 公式正确且不超过 20% |
| 7 | 风控断言不变量 | 通过校验的订单满足所有约束 |
| 8 | 止损冷却期 | 24h 内拒绝，24h 后放行 |
| 9 | 日亏损降级 | ≥ 5% 触发降级进入 Paper Mode |
| 10 | 平仓条件触发 | 止损/止盈/超时正确触发 |
| 11 | 限流速率不变量 | 速率 ≤ 1000/min |
| 12 | 限流自动降速 | 队列 > 800 时降至 500/min |
| 13 | 指数退避序列 | 第 N 次等待 2^N 秒 |
| 14 | Paper Mode 一致性 | 所有订单状态为 paper_trade |
| 15 | 策略统计与调优 | 胜率公式正确，< 40% 触发调优 |
| 16 | 数值边界校验 | 非正数价格/头寸被拒绝 |
| 17 | Pipeline 执行顺序 | Skill 时间戳严格递增 |
| 18 | 执行日志完整性 | 前后各一条日志，含必要字段 |
| 19 | 盈亏比例计算 | 做多/做空公式正确，互为相反数 |
