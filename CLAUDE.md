# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

这是一个 **OpenClaw** 平台工作区 —— 本地 AI Agent 运行时。核心子项目是 `openclaw-binance-agent/`，一个面向 Binance USDT 永续合约的量化交易 Agent，同时具备 A 股分析能力。

- 平台配置：`openclaw.json`（Agent、模型、频道、绑定）
- Agent 工作目录：`openclaw-binance-agent/`（Python >= 3.11，`uv` 包管理器）
- 提交格式：`type: 中文描述`（如 `fix: 规范超跌定时任务报告`、`feat: 新增 GLM-5.1 模型配置`）
- Docstring 使用中文
- 不提交：`.env`、`*.db`、`memory/`、`data/`、`.venv/`、`.hypothesis/`

## 构建与运行命令

所有命令在 `openclaw-binance-agent/` 下执行：

```bash
# 同步依赖
uv sync

# 运行脚本
uv run python <script>.py

# 运行全部测试
PYTHONPATH="." python -m pytest

# 运行单个测试文件
PYTHONPATH="." python -m pytest tests/test_skill4_execute.py

# 运行单个测试函数（详细输出）
PYTHONPATH="." python -m pytest tests/test_skill4_execute.py::test_function_name -v
```

## 核心架构：5 步交易流水线

每轮交易按顺序执行，Skill 间只传 `state_id`（UUID），不传原始数据，避免 LLM 上下文膨胀。

```
Skill-1 收集 → Skill-2 评级 → Skill-3 策略 → Skill-4 执行 → Skill-5 进化
  (扫描过滤)    (LLM评级)     (ATR止损止盈)   (Binance下单)   (记忆统计)
      ↓             ↓              ↓              ↓              ↓
 state_store.db state_store.db state_store.db state_store.db trading_state.db
```

**Skill 文件**（`src/skills/`）：
- 流水线 Skill：`skill1_collect.py` → `skill2_analyze.py` → `skill3_strategy.py` → `skill4_execute.py` → `skill5_evolve.py`
- 独立策略 Skill（跳过 Skill-2）：`crypto_oversold.py`、`crypto_overbought.py`、`crypto_reversal.py`、`crypto_wick.py`
- A 股 Skill：`astock_reversal.py`、`astock_trade_plan.py`

所有 Skill 继承 `BaseSkill`（`src/skills/base.py`），内置 JSON Schema（draft-07）校验和状态持久化。输入输出 Schema 在 `config/schemas/`。

## 基础设施层（`src/infra/`）

- `binance_fapi.py` — Binance 签名请求、订单、持仓、Algo 条件单、userTrades；重试时重新签名
- `binance_public.py` — 公开行情端点（ticker、K 线、资金费率、持仓量）
- `binance_kline_cache.py` — SQLite K 线缓存，WAL 模式，增量拉取
- `risk_controller.py` — 6 大硬编码风控约束，Paper Mode 持久化到 SQLite
- `state_store.py` — Skill 间 JSON 状态快照
- `memory_store.py` — 历史交易记录 + 反思日志，供 Skill-5 自我进化
- `trade_sync.py` — 从 Binance userTrades 同步已实现盈亏，幂等去重
- `exchange_rules.py` — LOT_SIZE / PRICE_FILTER / minNotional 规整
- `rate_limiter.py` — 令牌桶限流（1000 req/min）
- `fees.py` — Maker/Taker 费率建模
- `circuit_breaker.py` — BTC 极端行情熔断器

## 数据模型（`src/models/types.py`）

所有共享类型：`TradeDirection`、`Signal`、`OrderStatus`、`Candidate`、`Rating`、`OrderRequest`、`TradePlan` 等。使用 `Optional[X]`（不用 `X | None`）。数据模型使用 dataclass。

## 风控红线（硬编码，不可配置）

- 单笔保证金 ≤ 总资金 20%
- 单币种持仓 ≤ 总资金 40%
- 总持仓名义价值 ≤ 总资金 × 4x
- 同时持仓不超过 30 个
- 日亏损 ≥ 5% 自动切 Paper Mode（持久化，重启后仍生效）
- 止损后同币种同方向 24 小时冷却

## 定时任务入口

```bash
# 超跌交易（主入口，输出固定 Markdown 报告）
.venv/bin/python3 skills/binance-trading/scripts/run_oversold_cron.py --fast --format markdown

# 插针交易
.venv/bin/python3 skills/binance-trading/scripts/run_wick_cron.py --mode short --format markdown

# 持仓管理（止损上移，支持做多/做空）
.venv/bin/python3 scripts/manage_positions.py

# 账户检查
.venv/bin/python3 skills/binance-trading/scripts/check_account.py
```

## 关键设计决策

- Skill 间只传 `state_id`，每个 Skill 独立从 StateStore 加载输入
- 依赖注入：Binance 客户端、风控、存储全部通过构造函数注入，便于 mock
- 服务端保护单用 `closePosition=true`，防止止损/止盈触发后反向开仓
- 非阻塞执行：挂保护单后立即返回，适合定时任务调度
- 独立策略 Skill（插针、超买等）跳过 Skill-2 评级以保证时效性
- per-strategy 独立进化：每个 `strategy_tag` 独立调整 `rating_threshold` 和 `risk_ratio`

## Python 代码风格

导入顺序：stdlib → 第三方 → 本地。跨模块调用不用相对导入。

| 元素     | 规范         | 示例                       |
|----------|-------------|----------------------------|
| 类       | PascalCase  | `BinanceFapiClient`        |
| 函数     | snake_case  | `validate_order`           |
| 常量     | UPPER_SNAKE | `MAX_SINGLE_MARGIN_RATIO`  |
| 私有属性 | `_underscore` | `self._binance_client`   |
| 枚举值   | PascalCase  | `TradeDirection.LONG`      |

日志：`log = logging.getLogger(__name__)`。只用具体异常，禁止裸 `except:`。

## 平台格式化

- **Discord/WhatsApp：** 不用 markdown 表格，用列表
- **Discord 链接：** 用 `<>` 包裹抑制预览
- **WhatsApp：** 不用标题，用 **加粗** 或大写强调
