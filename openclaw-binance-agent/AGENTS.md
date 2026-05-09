# 工作空间规范

这个仓库是 `BianTrading` 的运行空间，核心目标是把 Binance U 本位合约交易和 A 股量化分析做成可审计、可回放、可风控的自动化系统。

## 启动流程

每次会话开始，按顺序读取：

1. `SOUL.md` — Agent 的原则和红线
2. `IDENTITY.md` — Agent 的身份和技术栈
3. `USER.md` — 用户偏好和沟通方式
4. `MEMORY.md` — 长期架构和关键决策记忆
5. `HEARTBEAT.md` — 定时任务和巡检要求
6. `TOOLS.md` — 本机环境、脚本和外部服务说明

如果需要最近运行细节，再读取 `memory/YYYY-MM-DD.md`。不要依赖“记在心里”，重要事实必须写回文件。

## 工作原则

- 交易安全优先，宁可错过机会，也不能绕过风控。
- 结论必须来自真实代码、真实日志或真实 API 响应，不用猜测代替验证。
- 修改代码前先理解现有链路：Skill 输出、JSON Schema、StateStore、MemoryStore、Binance API 边界。
- 所有实盘路径必须保持可追踪：输入状态、交易计划、执行结果、服务端成交同步、账户报告。
- 运行产物如 `memory/`、本地数据库和敏感配置默认不提交。

## 核心模块

| 模块 | 路径 | 说明 |
| ------ | ------ | ------ |
| Binance 交易 | `skills/binance-trading/` | 5 步交易流水线、账户检查、超跌定时任务固定报告 |
| Binance 数据 | `skills/binance-data/` | K 线缓存、超跌/反转/超买扫描、JSON 输出 |
| 交易基础设施 | `src/infra/` | Binance 客户端、风控（6大约束）、交易规则、MemoryStore、成交同步、大盘环境过滤 |
| 交易 Skills | `src/skills/skill*.py` | 信息收集、评级、策略、执行（含止损上移/时间衰减止盈）、自我进化 |
| A 股分析 | `skills/astock-analysis/` | A 股量化筛选和 TradingAgents 分析 |
| A 股数据 | `skills/astock-data/` | A 股本地缓存和增量拉取 |
| 持仓管理 | `scripts/manage_positions.py` | 做多/做空持仓止损上移、进程锁、原子写入 |
| 回测框架 | `scripts/backtest_crypto.py` / `scripts/backtest_astock.py` | 策略参数回测验证 |

## 实盘红线

- 单笔保证金不超过总资金 20%。
- 单币种持仓不超过总资金 40%。
- 全账户总持仓名义价值不超过总资金 × 4x。
- 同时持仓数量不超过 30 个。
- 日亏损达到 5% 立即停止实盘并持久化切换 Paper Mode。
- 止损后同币种同方向 24 小时内禁止开仓。
- ATR 原始止损距离超过系统上限时跳过交易，不强行截断进场。
- 服务端止盈止损必须使用 `closePosition=true` 或等价保护，避免反向开仓。
- 每轮执行开始要清理无持仓残留 Algo 条件单。

## 定时任务规范
"超跌交易"定时任务必须优先使用固定入口：
```bash
.venv/bin/python3 skills/binance-trading/scripts/run_oversold_cron.py --fast --format markdown
```
该脚本负责统一输出扫描、评级、交易执行、已同步平仓、持仓涨跌、杠杆、资金占比、保护单健康状态、账户和风险状态。不要让模型根据零散命令输出自由改写最终报告。

## 记忆管理

- 每日运行事实写入 `memory/YYYY-MM-DD.md`。
- 长期架构和关键决策提炼到 `MEMORY.md`。
- 工具、路径、环境变量和外部服务写入 `TOOLS.md`。
- Agent 行为规范写入 `AGENTS.md`、`SOUL.md`、`HEARTBEAT.md`。

## 提交规范

- 修改后运行相关测试，至少覆盖被改动模块。
- 提交前检查 `git status`，排除 `memory/`、数据库、密钥和临时文件。
- 提交信息沿用仓库风格，例如 `fix: 规范超跌定时任务报告`。

## 构建 / 运行
```bash
cd openclaw-binance-agent
uv run python <script>.py              # 运行脚本
uv sync                                # 同步依赖
```

## 测试
```bash
cd openclaw-binance-agent

# 全部测试
PYTHONPATH="." python -m pytest

# 单个测试文件
PYTHONPATH="." python -m pytest tests/test_skill4_execute.py

# 单个测试函数（详细模式）
PYTHONPATH="." python -m pytest tests/test_skill4_execute.py::test_trailing_stop_triggers_after_activation_for_long -v
```

## 代码风格

### 导入顺序
标准库 → 第三方 → 本地模块，禁止跨模块相对导入。

```python
import logging
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

import requests

from src.infra.binance_fapi import BinanceFapiClient
from src.models.types import OrderRequest
```

### 类型注解
- 所有函数签名包含完整类型提示，包括返回类型。
- 使用 `Optional[X]`（而非 `X | None`）保持一致。
- 使用 dataclass 定义数据模型。

```python
@dataclass
class OrderRequest:
    symbol: str
    direction: TradeDirection
    price: float
    quantity: float
    leverage: int
    order_type: str = "limit"
```

### 命名规范
| 元素 | 规范 | 示例 |
|------|------|------|
| 类 | PascalCase | `BinanceFapiClient` |
| 函数/方法 | snake_case | `validate_order` |
| 常量 | UPPER_SNAKE | `MAX_SINGLE_MARGIN_RATIO` |
| 私有属性 | `_前缀` | `self._binance_client` |
| 枚举值 | PascalCase | `TradeDirection.LONG` |

### 枚举
```python
class OrderStatus(str, Enum):
    FILLED = "filled"
    OPEN = "open"
    REJECTED_BY_RISK = "rejected_by_risk"
```

### 异常处理与日志
- 为领域错误定义特定异常类，不使用裸 `except:`。
- try-except 捕获特定异常，优雅降级。
- 日志：`log = logging.getLogger(__name__)`

```python
class IPBannedError(Exception):
    pass

try:
    result = self._binance_client.get_position_risk(symbol)
except Exception as exc:
    log.warning(f"获取持仓信息失败: {exc}")
```

### 文档字符串
使用中文。参数说明中英混用均可。

```python
def validate_order(self, order: OrderRequest, account: AccountState) -> ValidationResult:
    """对单笔订单执行全部风控断言校验。"""
```

## 不提交的文件

`.env`、`*.db`、`memory/`、`data/`、`.venv/`、`.hypothesis/`、`.pytest_cache/`。
