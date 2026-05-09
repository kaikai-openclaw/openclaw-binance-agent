# TradingAgents 接入配置指南

本文档说明如何将 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多智能体分析框架接入本系统的 Skill-2（深度分析与评级）。

---

## 1. TradingAgents 简介

TradingAgents 是一个基于 LLM 的多智能体金融交易分析框架，模拟真实交易公司的职能分工：

```
基本面分析师 ──┐
情绪分析师   ──┤──→ 研究员（多空辩论）──→ 交易员 ──→ 风控团队 ──→ 决策
新闻分析师   ──┤
技术分析师   ──┘
```

支持的 LLM 提供商：OpenAI (GPT)、Google (Gemini)、Anthropic (Claude)、xAI (Grok)、OpenRouter、Ollama（本地模型）。

---

## 2. 安装 TradingAgents

```bash
# 克隆 TradingAgents
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents

# 创建环境（推荐 Python 3.13）
conda create -n tradingagents python=3.13
conda activate tradingagents

# 安装
pip install -e .
```

---

## 3. 配置 API 密钥

TradingAgents 需要 LLM 提供商的 API 密钥，以及可选的数据源密钥。

### 3.1 LLM 提供商（选一个即可）

```bash
# OpenAI（推荐）
export OPENAI_API_KEY="sk-..."

# Google Gemini
export GOOGLE_API_KEY="..."

# Anthropic Claude
export ANTHROPIC_API_KEY="..."

# xAI Grok
export XAI_API_KEY="..."

# OpenRouter（聚合多家模型）
export OPENROUTER_API_KEY="..."
```

本地模型使用 Ollama，无需 API 密钥，设置 `llm_provider: "ollama"` 即可。

### 3.2 数据源（可选）

```bash
# Alpha Vantage（可选，默认使用 yfinance 免费数据）
export ALPHA_VANTAGE_API_KEY="..."
```

默认使用 yfinance 获取市场数据，无需额外 API 密钥。

### 3.3 使用 .env 文件

也可以在项目根目录创建 `.env` 文件：

```env
OPENAI_API_KEY=sk-...
ALPHA_VANTAGE_API_KEY=...
```

---

## 4. TradingAgents 配置参数

TradingAgents 的 `DEFAULT_CONFIG` 包含以下可调参数：

```python
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()

# ── LLM 设置 ──
config["llm_provider"] = "openai"        # openai / google / anthropic / xai / openrouter / ollama
config["deep_think_llm"] = "gpt-5.2"     # 复杂推理用的模型
config["quick_think_llm"] = "gpt-5-mini" # 快速任务用的模型
config["backend_url"] = "https://api.openai.com/v1"  # API 端点

# ── 提供商特定配置 ──
config["google_thinking_level"] = None      # "high" / "minimal"
config["openai_reasoning_effort"] = None    # "high" / "medium" / "low"
config["anthropic_effort"] = None           # "high" / "medium" / "low"

# ── 辩论与讨论 ──
config["max_debate_rounds"] = 1             # 多空研究员辩论轮数（越多越慢但越准）
config["max_risk_discuss_rounds"] = 1       # 风控讨论轮数

# ── 数据源 ──
config["data_vendors"] = {
    "core_stock_apis": "yfinance",          # yfinance（免费）或 alpha_vantage
    "technical_indicators": "yfinance",
    "fundamental_data": "yfinance",
    "news_data": "yfinance",
}
```

### 4.1 模型选择建议

| 场景 | 推荐配置 | 说明 |
|------|----------|------|
| 高精度分析 | deep_think=gpt-5.2, debate_rounds=2 | 更准确但更慢、更贵 |
| 快速筛选 | deep_think=gpt-5-mini, debate_rounds=1 | 速度快、成本低 |
| 本地运行 | provider=ollama, model=llama3 | 免费但精度较低 |
| 成本优化 | provider=openrouter | 可选择性价比最高的模型 |

---

## 5. 接入本系统

### 5.1 接入架构

```
Skill-1 输出候选币种
    │
    ▼
┌──────────────────────────────────────────────┐
│  Skill-2 (skill2_analyze.py)                 │
│                                              │
│  TradingAgentsModule(analyzer=回调函数)       │
│       │                                      │
│       ▼                                      │
│  ┌─────────────────────────────────────┐     │
│  │  TradingAgents (外部框架)            │     │
│  │                                     │     │
│  │  基本面分析 → 情绪分析 → 技术分析    │     │
│  │       ↓                             │     │
│  │  多空辩论 → 交易决策 → 风控评估      │     │
│  │       ↓                             │     │
│  │  返回: decision (buy/sell/hold)      │     │
│  └─────────────────────────────────────┘     │
│       │                                      │
│       ▼                                      │
│  转换为: {rating_score, signal, confidence}   │
└──────────────────────────────────────────────┘
    │
    ▼
Skill-3 读取评级结果
```

### 5.2 编写 analyzer 回调

在项目中创建 `src/integrations/trading_agents_adapter.py`：

```python
"""
TradingAgents 适配器

将 TradingAgents 的 propagate() 输出转换为本系统 Skill-2 所需的格式：
{rating_score: int(1-10), signal: str, confidence: float(0-100)}
"""

import logging
from datetime import datetime
from typing import Any

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

log = logging.getLogger(__name__)


def create_trading_agents_analyzer(
    llm_provider: str = "openai",
    deep_think_llm: str = "gpt-5-mini",
    quick_think_llm: str = "gpt-5-mini",
    max_debate_rounds: int = 1,
) -> callable:
    """
    创建一个可注入 TradingAgentsModule 的 analyzer 回调函数。

    参数:
        llm_provider: LLM 提供商
        deep_think_llm: 复杂推理模型
        quick_think_llm: 快速任务模型
        max_debate_rounds: 辩论轮数

    返回:
        analyzer(symbol, market_data) -> dict
    """
    # 初始化 TradingAgents 配置
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = llm_provider
    config["deep_think_llm"] = deep_think_llm
    config["quick_think_llm"] = quick_think_llm
    config["max_debate_rounds"] = max_debate_rounds
    config["data_vendors"] = {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    }

    # 创建 TradingAgentsGraph 实例（复用同一实例避免重复初始化）
    ta = TradingAgentsGraph(debug=False, config=config)

    def analyzer(symbol: str, market_data: dict) -> dict[str, Any]:
        """
        调用 TradingAgents 分析单个币种。

        参数:
            symbol: 币种符号，如 "BTCUSDT"
            market_data: 市场数据（含 heat_score 等）

        返回:
            {
                "rating_score": int,    # 1-10
                "signal": str,          # "long" / "short" / "hold"
                "confidence": float,    # 0-100
            }
        """
        # TradingAgents 使用股票代码格式，需要转换
        # BTCUSDT → BTC（去掉 USDT 后缀）
        ticker = symbol.replace("USDT", "")

        # 使用当前日期作为分析日期
        analysis_date = datetime.now().strftime("%Y-%m-%d")

        try:
            _, decision = ta.propagate(ticker, analysis_date)

            # 解析 TradingAgents 的决策输出
            return _parse_decision(decision, symbol)

        except Exception as exc:
            log.error(f"TradingAgents 分析 {symbol} 失败: {exc}")
            raise

    return analyzer


def _parse_decision(decision: str, symbol: str) -> dict[str, Any]:
    """
    将 TradingAgents 的文本决策解析为结构化评级结果。

    TradingAgents 的 decision 通常是一段包含交易建议的文本，
    需要从中提取方向、评级和置信度。

    参数:
        decision: TradingAgents 返回的决策文本
        symbol: 币种符号

    返回:
        标准化的评级字典
    """
    decision_lower = decision.lower()

    # 解析交易信号
    if "strong buy" in decision_lower or "strongly recommend buying" in decision_lower:
        signal = "long"
        base_score = 9
        confidence = 85.0
    elif "buy" in decision_lower or "long" in decision_lower:
        signal = "long"
        base_score = 7
        confidence = 70.0
    elif "strong sell" in decision_lower or "strongly recommend selling" in decision_lower:
        signal = "short"
        base_score = 9
        confidence = 85.0
    elif "sell" in decision_lower or "short" in decision_lower:
        signal = "short"
        base_score = 7
        confidence = 70.0
    elif "hold" in decision_lower or "neutral" in decision_lower:
        signal = "hold"
        base_score = 5
        confidence = 50.0
    else:
        # 无法解析时默认观望
        signal = "hold"
        base_score = 4
        confidence = 30.0

    # 根据决策文本中的关键词微调评级
    if "high confidence" in decision_lower:
        confidence = min(95.0, confidence + 15)
        base_score = min(10, base_score + 1)
    elif "low confidence" in decision_lower:
        confidence = max(10.0, confidence - 15)
        base_score = max(1, base_score - 1)

    return {
        "rating_score": base_score,
        "signal": signal,
        "confidence": confidence,
    }
```

### 5.3 在 Pipeline 中使用

```python
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.skills.skill2_analyze import TradingAgentsModule, Skill2Analyze

# 创建 analyzer 回调
analyzer = create_trading_agents_analyzer(
    llm_provider="openai",
    deep_think_llm="gpt-5-mini",    # 成本较低
    quick_think_llm="gpt-5-mini",
    max_debate_rounds=1,             # 速度优先
)

# 注入到 TradingAgentsModule
trading_agents = TradingAgentsModule(analyzer=analyzer)

# 注入到 Skill-2
skill2 = Skill2Analyze(
    state_store=state_store,
    input_schema=skill2_input_schema,
    output_schema=skill2_output_schema,
    trading_agents=trading_agents,
    rating_threshold=6,
)
```

---

## 6. 配置参数对照表

本系统的策略参数与 TradingAgents 配置的对应关系：

| 本系统参数 | 位置 | TradingAgents 对应 | 说明 |
|-----------|------|-------------------|------|
| rating_threshold | config/default.yaml → strategy | 无直接对应 | 本系统独有的过滤阈值 |
| risk_ratio | config/default.yaml → strategy | 无直接对应 | 本系统的头寸规模计算参数 |
| ANALYSIS_TIMEOUT | skill2_analyze.py | 无直接对应 | 本系统对单次分析的超时控制（30s） |
| — | — | llm_provider | TradingAgents 的 LLM 提供商 |
| — | — | deep_think_llm | TradingAgents 的推理模型 |
| — | — | max_debate_rounds | TradingAgents 的辩论轮数 |
| — | — | data_vendors | TradingAgents 的数据源 |

---

## 7. 成本估算

TradingAgents 每次分析一个币种的 LLM 调用成本取决于模型和辩论轮数：

| 配置 | 每次分析约耗时 | 每次分析约成本 | 适用场景 |
|------|--------------|--------------|----------|
| gpt-5-mini, 1 轮辩论 | 15-30 秒 | ~$0.01-0.03 | 日常筛选 |
| gpt-5.2, 1 轮辩论 | 30-60 秒 | ~$0.05-0.15 | 重点分析 |
| gpt-5.2, 2 轮辩论 | 60-120 秒 | ~$0.10-0.30 | 高精度决策 |
| ollama (本地), 1 轮 | 30-120 秒 | 免费 | 开发测试 |

假设每轮 Pipeline 分析 5 个候选币种：
- 经济模式（gpt-5-mini）：约 $0.05-0.15 / 轮
- 标准模式（gpt-5.2）：约 $0.25-0.75 / 轮

---

## 8. 注意事项

### 8.1 加密货币支持

TradingAgents 原生设计面向美股市场，数据源（yfinance / Alpha Vantage）主要提供股票数据。对于加密货币：

- yfinance 支持部分加密货币（如 BTC-USD、ETH-USD），但格式与 Binance 不同
- 可能需要在 adapter 中做额外的数据转换
- 技术指标和基本面数据的可用性可能受限

建议在 adapter 中补充加密货币专用的数据获取逻辑。

### 8.2 超时控制

本系统对 TradingAgents 的单次分析设置了 30 秒超时（`ANALYSIS_TIMEOUT`）。如果使用较慢的模型或多轮辩论，可能需要调大：

```python
# 在 src/skills/skill2_analyze.py 中修改
ANALYSIS_TIMEOUT = 120  # 2 分钟，适合 gpt-5.2 + 2 轮辩论
```

### 8.3 错误处理

TradingAgents 分析某个币种失败时，Skill-2 会跳过该币种并继续处理剩余币种，不会阻塞整个 Pipeline。失败原因记录在日志中。

### 8.4 并发限制

当前实现是逐个币种串行分析。如果候选币种较多且需要加速，可以考虑在 Skill-2 中引入并发分析（需注意 LLM API 的并发限制）。

---

## 9. 完整接入示例

```python
"""完整的 TradingAgents 接入示例"""

import json
import os
from dotenv import load_dotenv

load_dotenv()

from src.infra.state_store import StateStore
from src.infra.memory_store import MemoryStore
from src.infra.risk_controller import RiskController
from src.infra.rate_limiter import RateLimiter
from src.infra.binance_fapi import BinanceFapiClient
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule
from src.skills.skill3_strategy import Skill3Strategy
from src.skills.skill4_execute import Skill4Execute
from src.skills.skill5_evolve import Skill5Evolve
from src.models.types import AccountState


def load_schema(name):
    with open(f"config/schemas/{name}") as f:
        return json.load(f)


# 初始化基础设施
state_store = StateStore(db_path="data/state_store.db")
memory_store = MemoryStore(db_path="data/memory_store.db")
risk_controller = RiskController()
rate_limiter = RateLimiter()
binance_client = BinanceFapiClient(
    api_key=os.getenv("BINANCE_API_KEY", ""),
    api_secret=os.getenv("BINANCE_API_SECRET", ""),
    rate_limiter=rate_limiter,
)

# 创建 TradingAgents analyzer
analyzer = create_trading_agents_analyzer(
    llm_provider="openai",
    deep_think_llm="gpt-5-mini",
    quick_think_llm="gpt-5-mini",
    max_debate_rounds=1,
)
trading_agents = TradingAgentsModule(analyzer=analyzer)

# 账户状态提供者
def get_account():
    info = binance_client.get_account_info()
    return AccountState(
        total_balance=info.total_balance,
        available_margin=info.available_balance,
        daily_realized_pnl=0.0,
        positions=[],
    )

# 构建 Skill-2（接入 TradingAgents）
skill2 = Skill2Analyze(
    state_store=state_store,
    input_schema=load_schema("skill2_input.json"),
    output_schema=load_schema("skill2_output.json"),
    trading_agents=trading_agents,
    rating_threshold=6,
)

print("TradingAgents 已成功接入 Skill-2")
```
