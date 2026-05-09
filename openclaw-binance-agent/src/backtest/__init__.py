"""
最小可用回测框架（P0-3）

- `metrics`：Sharpe、MaxDD、Calmar、胜率、盈利因子、净胜率
- `engine`：bar-by-bar 回放，支持入场信号生成器 + 固定 SL/TP/最大持仓期
- 集成 `src.infra.fees`：真实扣除手续费和滑点

仅依赖标准库 + numpy/pandas（已在项目 deps 里）。
"""

from src.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    EntrySignal,
    Trade,
)
from src.backtest.metrics import (
    calc_max_drawdown,
    calc_profit_factor,
    calc_sharpe_ratio,
    summarize_trades,
)

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "EntrySignal",
    "Trade",
    "calc_max_drawdown",
    "calc_profit_factor",
    "calc_sharpe_ratio",
    "summarize_trades",
]
