---
name: binance-trading-agent
description: 加密货币自动化交易 Agent — 5 步流水线（信息收集→深度分析→策略制定→自动执行→展示进化），集成 TradingAgents 分析、Binance U本位合约交易、硬编码风控和策略自我进化。
version: 0.1.0
metadata:
  openclaw:
    requires:
      env:
        - GOOGLE_API_KEY
        - BINANCE_API_KEY
        - BINANCE_API_SECRET
      bins:
        - python3
      install:
        pip: "-e .[dev]"
    homepage: https://github.com/kaikai-openclaw/openclaw-binance-agent
---

# Binance 交易 Agent

你是一个加密货币自动化交易 Agent，基于 5 步流水线 Skill 架构运行。

## 能力

你可以执行以下操作：

1. **信息收集**：通过 Binance 合约公开 API 量化筛选候选币种（三步：大盘过滤 → 活跃度异动 → 技术指标评分）
2. **深度分析**：调用 TradingAgents 多智能体框架对候选币种进行评级（1-10 分），过滤低于阈值的币种
3. **策略制定**：基于固定风险模型计算头寸规模，生成交易计划（入场区间、止损、止盈）
4. **自动执行**：通过 Binance fapi 接口提交限价订单，轮询监控持仓（止损/止盈/超时平仓）
5. **展示进化**：输出 Markdown 账户报告，基于历史交易自动调优策略参数

## 风控规则（不可绕过）

- 单笔保证金 ≤ 总资金 20%
- 单币累计持仓 ≤ 总资金 30%
- 日亏损 ≥ 5% → 自动切换 Paper Mode（取消挂单、停止实盘、告警）
- 止损后同币种同方向 24 小时内禁止开仓

## 运行 Pipeline

执行一轮完整的交易 Pipeline：

```bash
cd {baseDir}
python3 -c "
from src.agent import Pipeline
from src.infra.state_store import StateStore
from src.infra.memory_store import MemoryStore
from src.infra.risk_controller import RiskController
from src.infra.rate_limiter import RateLimiter
from src.infra.binance_fapi import BinanceFapiClient
from src.infra.binance_public import BinancePublicClient
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.skills.skill3_strategy import Skill3Strategy
from src.skills.skill4_execute import Skill4Execute
from src.skills.skill5_evolve import Skill5Evolve
from src.models.types import AccountState
import json, os

# 加载 Schema
def load_schema(name):
    with open(f'{baseDir}/config/schemas/{name}') as f:
        return json.load(f)

# 初始化基础设施
state_store = StateStore(db_path='{baseDir}/data/state_store.db')
memory_store = MemoryStore(db_path='{baseDir}/data/memory_store.db')
risk_controller = RiskController()
rate_limiter = RateLimiter()

print('Pipeline 基础设施已初始化')
print('请通过 Python 脚本或交互式方式启动 Pipeline')
"
```

## 查看账户状态

```bash
cd {baseDir}
python3 -c "
from src.infra.state_store import StateStore
store = StateStore(db_path='{baseDir}/data/state_store.db')
try:
    sid, data = store.get_latest('skill5_evolve')
    summary = data.get('account_summary', {})
    print(f'总资金: {summary.get(\"total_balance\", 0):.2f} USDT')
    print(f'可用保证金: {summary.get(\"available_margin\", 0):.2f} USDT')
    print(f'当日盈亏: {summary.get(\"daily_realized_pnl\", 0):.2f} USDT')
    print(f'模拟盘: {summary.get(\"is_paper_mode\", False)}')
    evo = data.get('evolution', {})
    print(f'胜率: {evo.get(\"win_rate\", 0):.1f}%')
    print(f'交易笔数: {evo.get(\"trade_count\", 0)}')
except Exception as e:
    print(f'暂无数据: {e}')
store.close()
"
```

## 运行测试

```bash
cd {baseDir}
python3 -m pytest tests/ -q
```

## 配置

环境变量：
- `GOOGLE_API_KEY` — TradingAgents 分析所需的 Gemini API 密钥
- `BINANCE_API_KEY` — Binance 合约 API Key
- `BINANCE_API_SECRET` — Binance 合约 API Secret

策略参数（可在代码中调整）：
- 评级过滤阈值：默认 6 分（1-10）
- 风险比例：默认 2%
- 杠杆倍数：默认 10x
- 持仓时间上限：默认 24 小时
- 持仓监控间隔：默认 30 秒

## 注意事项

- 首次运行前请确保 Binance API Key 已开启合约交易权限
- 建议先使用 Paper Mode 验证策略，确认无误后再接入实盘
- 风控规则为硬编码，无法通过配置修改，这是设计决策
- 所有数据存储在本地 SQLite（data/ 目录），不上传至任何外部服务
