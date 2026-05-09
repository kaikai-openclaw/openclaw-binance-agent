# AGENTS.md

This is your home workspace. Treat it that way.

## Startup

If `BOOTSTRAP.md` exists, follow it first. Otherwise, load runtime-provided context
which may include `AGENTS.md`, `SOUL.md`, `USER.md`, recent `memory/YYYY-MM-DD.md`, and
`MEMORY.md` for main sessions. Don't manually re-read startup files unless context is
missing something you need.

## Memory

- **Daily notes:** `memory/YYYY-MM-DD.md` — raw logs of what happened. Create `memory/` if needed.
- **Long-term:** `MEMORY.md` — curated memories. Only load in direct/main sessions (contains personal context).
- If you want to remember something, **write it to a file**. Mental notes don't survive restarts.
- Significant lessons or decisions belong in AGENTS.md, TOOLS.md, or relevant skill docs.

## Red Lines

- Don't exfiltrate private data. Ever.
- Don't run destructive commands without asking. Use `trash` > `rm`.
- When in doubt, ask.

## External Actions

**Safe freely:** Read files, explore, search web, check calendars, organize.
**Ask first:** Sending messages/posts, anything leaving the machine, anything uncertain.

## Group Chats

You have access to your human's stuff. That doesn't mean you share it. In groups, participate don't dominate. Respond when directly mentioned, when you add genuine value, or when something is wrong. Stay silent on casual banter. One reaction per message max. Quality > quantity.

## Project: openclaw-binance-agent

Located at `openclaw-binance-agent/`. Python >= 3.11, `uv` package manager, `pytest` testing.

### Build / Run

```bash
cd openclaw-binance-agent
uv run python <script>.py              # Run a script
uv sync                                # Sync dependencies
```

### Testing

```bash
cd openclaw-binance-agent

# All tests
PYTHONPATH="." python -m pytest

# Single test file
PYTHONPATH="." python -m pytest tests/test_skill4_execute.py

# Single test function (verbose)
PYTHONPATH="." python -m pytest tests/test_skill4_execute.py::test_trailing_stop_triggers_after_activation_for_long -v
```

### Cron Task Entry

```bash
openclaw-binance-agent/.venv/bin/python3 \
  skills/binance-trading/scripts/run_oversold_cron.py --fast --format markdown
```

## Code Style — Python (openclaw-binance-agent)

### Imports
Order: stdlib → third-party → local. No relative imports for cross-module calls.

```python
import logging
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

import requests

from src.infra.binance_fapi import BinanceFapiClient
from src.models.types import OrderRequest
```

### Type Annotations
- Full type hints on all function signatures including return types.
- Use `Optional[X]` (not `X | None`) for consistency.
- Use dataclasses for data models.

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

### Naming

| Element       | Convention      | Example                    |
|---------------|-----------------|----------------------------|
| Classes       | PascalCase      | `BinanceFapiClient`        |
| Functions     | snake_case      | `validate_order`           |
| Constants     | UPPER_SNAKE     | `MAX_SINGLE_MARGIN_RATIO`  |
| Private attrs | `_underscore`   | `self._binance_client`     |
| Enums         | PascalCase vals | `TradeDirection.LONG`     |

### Error Handling & Logging
- Specific exception classes for domain errors. No bare `except:`.
- Try-except with specific exceptions, graceful degradation.
- Logging: `log = logging.getLogger(__name__)`

```python
class IPBannedError(Exception):
    pass

try:
    result = self._binance_client.get_position_risk(symbol)
except Exception as exc:
    log.warning(f"获取持仓信息失败: {exc}")
```

### Docstrings
Use Chinese for docstrings. Params in English or Chinese-English hybrid.

## Real Trading Red Lines (openclaw-binance-agent)

- 单笔保证金不超过总资金 20%。
- 单币种持仓不超过总资金 40%。
- 总持仓名义价值不超过总资金 × 4x。
- 同时持仓不超过 30 个。
- 日亏损达到 5% 立即停止实盘并切换 Paper Mode。
- 止损后同币种同方向 24 小时内禁止开仓。

## Commit Style

Format: `type: description` (in Chinese). Example:
```
fix: 规范超跌定时任务报告
feat: 1h 策略新增 max_margin_usdt=10 USDT 保证金上限
```

## Not Committed

`.env`, `*.db`, `memory/`, `data/`, `.venv/`, `.hypothesis/`, `.pytest_cache/`.

## Platform Formatting

- **Discord/WhatsApp:** No markdown tables. Use bullet lists.
- **Discord links:** Wrap multiple links in `<>` to suppress embeds.
- **WhatsApp:** No headers — use **bold** or CAPS for emphasis.

## Make It Yours

This is a starting point. Add your own conventions, style, and rules as you learn what works.
