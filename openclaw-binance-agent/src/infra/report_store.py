"""
TradingAgents 分析报告持久化模块

将 TradingAgents 多智能体分析的完整 final_state 保存为结构化 markdown 文件。
保存路径：data/reports/{market}/{symbol}/{date}/complete_report.md

供下游 Skill 和 OpenClaw Agent 查询历史分析报告。
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_REPORTS_DIR = "data/reports"


def save_analysis_report(
    final_state: Dict[str, Any],
    symbol: str,
    market: str = "crypto",
    reports_dir: str = DEFAULT_REPORTS_DIR,
) -> Optional[str]:
    """将 TradingAgents 的 final_state 保存为 markdown 报告。

    Args:
        final_state: TradingAgents propagate() 返回的完整状态
        symbol: 交易对/股票代码（如 BTCUSDT / 600519）
        market: 市场类型（crypto / astock）
        reports_dir: 报告根目录

    Returns:
        保存的报告文件路径，失败返回 None
    """
    if not final_state:
        return None

    date_str = datetime.now().strftime("%Y-%m-%d")
    time_str = datetime.now().strftime("%H%M%S")
    save_dir = Path(reports_dir) / market / symbol / date_str
    save_dir.mkdir(parents=True, exist_ok=True)

    sections = []

    # 1. 分析师团队报告
    analyst_parts = []
    for key, label in [
        ("market_report", "市场分析师"),
        ("sentiment_report", "情绪分析师"),
        ("news_report", "新闻分析师"),
        ("fundamentals_report", "基本面分析师"),
    ]:
        content = final_state.get(key, "")
        if content:
            analyst_parts.append(f"### {label}\n\n{content}")
    if analyst_parts:
        sections.append("## 一、分析师团队报告\n\n" + "\n\n".join(analyst_parts))

    # 2. 研究团队辩论
    debate = final_state.get("investment_debate_state", {})
    if debate:
        research_parts = []
        for key, label in [
            ("bull_history", "多头研究员"),
            ("bear_history", "空头研究员"),
            ("judge_decision", "研究主管决策"),
        ]:
            content = debate.get(key, "")
            if content:
                research_parts.append(f"### {label}\n\n{content}")
        if research_parts:
            sections.append("## 二、研究团队辩论\n\n" + "\n\n".join(research_parts))

    # 3. 交易计划
    trader_plan = final_state.get("trader_investment_plan", "")
    if trader_plan:
        sections.append(f"## 三、交易计划\n\n{trader_plan}")

    # 4. 风控团队
    risk = final_state.get("risk_debate_state", {})
    if risk:
        risk_parts = []
        for key, label in [
            ("aggressive_history", "激进派"),
            ("conservative_history", "保守派"),
            ("neutral_history", "中立派"),
            ("judge_decision", "风控主管决策"),
        ]:
            content = risk.get(key, "")
            if content:
                risk_parts.append(f"### {label}\n\n{content}")
        if risk_parts:
            sections.append("## 四、风控团队评估\n\n" + "\n\n".join(risk_parts))

    # 5. 最终决策
    final_decision = final_state.get("final_trade_decision", "")
    if final_decision:
        sections.append(f"## 五、最终交易决策\n\n{final_decision}")

    if not sections:
        log.warning("[ReportStore] %s 无有效分析内容，跳过保存", symbol)
        return None

    # 组装完整报告
    header = (
        f"# 交易分析报告: {symbol}\n\n"
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"市场: {market}\n\n"
        f"---\n\n"
    )
    report_content = header + "\n\n".join(sections)

    # 保存文件（带时间戳避免覆盖）
    filename = f"report_{time_str}.md"
    filepath = save_dir / filename
    filepath.write_text(report_content, encoding="utf-8")

    # 同时保存一份 complete_report.md（最新的覆盖）
    (save_dir / "complete_report.md").write_text(report_content, encoding="utf-8")

    # 保存原始 final_state 为 JSON（供程序读取）
    state_path = save_dir / f"state_{time_str}.json"
    try:
        state_path.write_text(
            json.dumps(final_state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("[ReportStore] 保存 state JSON 失败: %s", e)

    log.info("[ReportStore] 报告已保存: %s", filepath)
    return str(filepath)


def list_reports(
    market: str = "",
    symbol: str = "",
    reports_dir: str = DEFAULT_REPORTS_DIR,
) -> List[Dict[str, str]]:
    """列出历史分析报告。

    Args:
        market: 筛选市场（crypto/astock），空字符串=全部
        symbol: 筛选交易对/股票代码，空字符串=全部
        reports_dir: 报告根目录

    Returns:
        [{"market": "crypto", "symbol": "BTCUSDT", "date": "2026-04-09",
          "path": "data/reports/crypto/BTCUSDT/2026-04-09/complete_report.md"}, ...]
    """
    base = Path(reports_dir)
    if not base.exists():
        return []

    results = []
    markets = [market] if market else [d.name for d in base.iterdir() if d.is_dir()]

    for m in markets:
        market_dir = base / m
        if not market_dir.exists():
            continue
        symbols = [symbol] if symbol else [d.name for d in market_dir.iterdir() if d.is_dir()]
        for s in symbols:
            sym_dir = market_dir / s
            if not sym_dir.exists():
                continue
            for date_dir in sorted(sym_dir.iterdir(), reverse=True):
                if not date_dir.is_dir():
                    continue
                report = date_dir / "complete_report.md"
                if report.exists():
                    results.append({
                        "market": m,
                        "symbol": s,
                        "date": date_dir.name,
                        "path": str(report),
                    })
    return results


def read_report(
    symbol: str,
    date: str = "",
    market: str = "",
    reports_dir: str = DEFAULT_REPORTS_DIR,
) -> Optional[str]:
    """读取指定报告的内容。

    Args:
        symbol: 交易对/股票代码
        date: 日期（YYYY-MM-DD），空字符串=最新
        market: 市场类型，空字符串=自动检测

    Returns:
        报告 markdown 内容，未找到返回 None
    """
    base = Path(reports_dir)
    markets = [market] if market else [d.name for d in base.iterdir() if d.is_dir()]

    for m in markets:
        sym_dir = base / m / symbol
        if not sym_dir.exists():
            continue

        if date:
            report = sym_dir / date / "complete_report.md"
            if report.exists():
                return report.read_text(encoding="utf-8")
        else:
            # 取最新日期
            dates = sorted(
                [d for d in sym_dir.iterdir() if d.is_dir()],
                key=lambda d: d.name,
                reverse=True,
            )
            for d in dates:
                report = d / "complete_report.md"
                if report.exists():
                    return report.read_text(encoding="utf-8")
    return None
