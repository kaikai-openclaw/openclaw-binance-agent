# BianTrading — Binance 量化交易 Agent

> 给 Kiro 的快速上下文文档。读完本文件即可理解项目全貌，无需再逐一翻阅其他文档。

## 项目定位

**BianTrading** 是运行在 OpenClaw 框架上的量化交易 Agent，核心目标是 Binance U 本位合约（USDT 永续）自动交易，同时保留 A 股量化分析能力。系统以"风控优先、可审计、可回放"为设计原则，所有交易路径必须可追踪。

- 工作目录：`/Users/zengkai/.openclaw/openclaw-binance-agent`
- Python：`>=3.11`，虚拟环境：`.venv/`
- 运行命令前缀：`.venv/bin/python3` 或 `uv run python`

---

## 目录结构

```
openclaw-binance-agent/
├── src/
│   ├── infra/              # 基础设施层（Binance 客户端、风控、缓存、同步）
│   ├── skills/             # 5 步交易流水线 Skill
│   └── models/             # 数据类型定义
├── skills/
│   ├── binance-trading/    # 交易流水线脚本入口
│   ├── binance-data/       # K 线缓存与扫描脚本
│   ├── astock-analysis/    # A 股分析
│   └── astock-data/        # A 股数据服务
├── config/schemas/         # JSON Schema（Skill 输入输出校验）
├── data/                   # SQLite 数据库（不提交）
├── memory/                 # 运行记忆（不提交）
├── tests/                  # 单元测试
├── AGENTS.md               # 工作空间规范（启动必读）
├── SOUL.md                 # Agent 性格与红线
├── SKILL.md                # Skill 清单与执行安全
├── TOOLS.md                # 脚本路径与环境变量
├── MEMORY.md               # 长期架构决策记忆
├── HEARTBEAT.md            # 定时任务与巡检规范
├── IDENTITY.md             # Agent 身份与技术栈
└── USER.md                 # 用户偏好
```

---

## 核心架构：5 步交易流水线

每轮交易按顺序执行，Skill 间通过 `state_id`（UUID）传递状态，不传递原始数据。

```
Skill-1 收集候选  →  Skill-2 评级  →  Skill-3 策略  →  Skill-4 执行  →  Skill-5 进化
  (scan + score)     (TradingAgents)   (ATR SL/TP)     (Binance fapi)   (MemoryStore)
       ↓                   ↓                ↓                ↓                ↓
  state_store.db      state_store.db   state_store.db   state_store.db  trading_state.db
```

| Skill | 文件 | 职责 |
|-------|------|------|
| Skill-1 | `src/skills/skill1_collect.py` | 全市场扫描，4 步过滤（成交额→量比→技术指标→相关性去重），输出候选列表 |
| Skill-2 | `src/skills/skill2_analyze.py` | 调用 TradingAgents 多智能体评级，输出 rating_score(1-10) / signal / confidence |
| Skill-3 | `src/skills/skill3_strategy.py` | ATR 动态止损止盈，固定风险模型计算仓位，输出交易计划 |
| Skill-4 | `src/skills/skill4_execute.py` | 风控校验 → 限价下单 → 服务端保护单 → 持仓监控 |
| Skill-5 | `src/skills/skill5_evolve.py` | 账户报告、成交同步、历史统计、参数自我调整 |

---

## 基础设施层（`src/infra/`）

| 模块 | 职责 |
|------|------|
| `binance_fapi.py` | Binance 签名请求、订单、持仓、Algo 条件单、userTrades；重试时重新签名 |
| `binance_public.py` | 公开行情端点（ticker、K 线、资金费率、持仓量） |
| `binance_kline_cache.py` | SQLite K 线缓存，WAL 模式，增量拉取 |
| `risk_controller.py` | 硬编码 6 大风控约束，Paper Mode 持久化到 SQLite |
| `market_regime.py` | A 股大盘环境过滤，牛/熊/横盘三态，进程级单例，TTL 内存缓存 |
| `memory_store.py` | 历史交易记录 + 反思日志，供 Skill-5 自我进化 |
| `state_store.py` | Skill 间 JSON 状态快照（瞬态） |
| `trade_sync.py` | 从 Binance `userTrades` 同步已实现盈亏，幂等去重 |
| `exchange_rules.py` | LOT_SIZE / PRICE_FILTER / minNotional 规整 |
| `rate_limiter.py` | 令牌桶限流（1000 req/min） |
| `fees.py` | Maker/Taker 费率建模，净盈亏计算 |

---

## 风控红线（硬编码，不可配置）

| 约束 | 值 |
|------|----|
| 单笔保证金上限 | ≤ 总资金 20% |
| 单币种累计持仓 | ≤ 总资金 40% |
| 日亏损触发 Paper Mode | ≥ 5% |
| 止损冷却期 | 同币种同方向 24 小时 |
| 最大同时持仓数 | 12 个 |
| 总持仓名义价值 | ≤ 总资金 × 4x |

Paper Mode 持久化到 `data/trading_state.db`，进程重启后仍生效。

---

## 交易策略

| 策略 | 模式 | K 线周期 | 核心信号 | 典型持仓 |
|------|------|----------|----------|----------|
| 短期超跌反转 | 做多 | 4h | RSI<20 + 量比异动 + 资金费率 | 1~12h |
| 长期超跌均值回归 | 做多 | 1d | BIAS<-15% + MACD 底背离 + 距高点回撤>30% | 2~4 周 |
| 超买做空 | 做空 | 4h/1h | RSI>80 + 顶部确认（MACD/RSI 顶背离、KDJ 死叉、量价背离）+ 回撤 2%~15% | 1~12h |
| 右侧趋势反转 | 做多/空 | 4h/1h | MA 拐头 + MACD 反转 | 4~24h |
| 插针猎手 | 做多/空 | 15m/1h | 影线比率 + 成交量异动 + 价格回归度 | 1~12h |

止损止盈优先级：`wick_tip_price` > ATR 动态（默认 1.5x/3.0x）> 固定百分比（3%/6%）。
ATR 原始止损距离超过 `max_stop_pct`（7%）时跳过交易，不强行截断。
持仓期间自动执行止损上移（Break-even + 阶梯锁利，3步）和时间衰减止盈（2步）。

---

## 定时任务（`cron/jobs.json`）

| 任务 | 调度 | 超时 |
|------|------|------|
| 短期超跌交易（4h） | 每 2 小时（偶数点后 10 分） | 25 min |
| 长期超跌交易（1d） | 每 6 小时 | 25 min |
| 超买做空（4h） | 每 2 小时（奇数点） | 25 min |
| 趋势反转（4h/1h） | 每 2 小时 / 每小时 :30 | 25 min |
| 持仓管理（trailing stop） | 每小时 :50 | 2 min |
| K 线缓存刷新 | 每 6 小时 | 30 min |
| 记忆提炼（Memory Dreaming） | 每日 03:00 | — |

---

## 高频脚本

```bash
# 超跌交易定时任务（主入口，输出固定 Markdown 报告）
.venv/bin/python3 skills/binance-trading/scripts/run_oversold_cron.py --fast --format markdown

# 插针交易
.venv/bin/python3 skills/binance-trading/scripts/run_wick_cron.py --mode short --format markdown

# 账户检查
.venv/bin/python3 skills/binance-trading/scripts/check_account.py

# 完整流水线（可指定 --paper 模拟盘）
.venv/bin/python3 skills/binance-trading/scripts/run_pipeline.py --fast
.venv/bin/python3 skills/binance-trading/scripts/run_pipeline.py --paper --fast --symbols BTC,SOL

# 持仓管理（止损上移，支持做多/做空）
.venv/bin/python3 scripts/manage_positions.py

# 数据扫描（纯 JSON 输出，供自动化消费）
.venv/bin/python3 skills/binance-data/scripts/scan_oversold.py --mode short --json
.venv/bin/python3 skills/binance-data/scripts/scan_reversal.py --mode short
.venv/bin/python3 skills/binance-data/scripts/scan_overbought.py --mode short

# 回测（策略参数验证）
.venv/bin/python3 scripts/backtest_crypto.py
.venv/bin/python3 scripts/backtest_astock.py
```

---

## 数据资产

| 数据库 | 路径 | 内容 |
|--------|------|------|
| Binance K 线缓存 | `data/binance_kline_cache.db` | 538 个 USDT 永续合约，552k 行 4h K 线，84 MB |
| StateStore | `data/state_store.db` | Skill 输入输出快照（瞬态） |
| MemoryStore / 风控状态 | `data/trading_state.db` | 历史交易、反思日志、Paper Mode、止损冷却 |
| A 股 K 线缓存 | `data/kline_cache.db` | 5271 只股票，248 万行，324 MB |
| 分析报告 | `data/reports/` | TradingAgents Markdown 报告 |

---

## 必需环境变量

```
BINANCE_API_KEY
BINANCE_API_SECRET
LLM_PROVIDER
FAST_LLM_MODEL
```

配置文件：项目 `.env` 或 `~/.openclaw/.env`。密钥不提交。

---

## 测试

```bash
# 核心测试
PYTHONPATH="." python -m pytest tests/test_run_oversold_cron.py tests/test_skill2_analyze.py tests/test_skill4_execute.py

# 基础设施测试
PYTHONPATH="." python -m pytest tests/test_binance_fapi.py tests/test_trade_sync.py
```

---

## 关键设计决策

- **Skill 间只传 state_id**：避免 LLM 上下文膨胀，每个 Skill 独立可测。
- **依赖注入**：Binance 客户端、风控、存储全部通过构造函数注入，便于 mock。
- **服务端保护单用 `closePosition=true`**：防止止损/止盈触发后反向开仓。
- **非阻塞执行模式**：入场确认后立即挂保护单返回，不长轮询，适合定时任务。
- **Paper Mode 持久化**：日亏损 ≥5% 自动切换，重启不丢失状态。
- **per-strategy 独立进化**：每个 `strategy_tag` 独立调整 `rating_threshold` 和 `risk_ratio`。

---

## 会话启动顺序

每次新会话按顺序读取：`SOUL.md` → `IDENTITY.md` → `USER.md` → `MEMORY.md` → `HEARTBEAT.md` → `TOOLS.md`。
需要最近运行细节时再读 `memory/YYYY-MM-DD.md`。
