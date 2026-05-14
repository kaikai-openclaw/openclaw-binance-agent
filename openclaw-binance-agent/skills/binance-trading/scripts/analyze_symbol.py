#!/usr/bin/env python3
"""
指定币种深度分析（Skill-1 + Skill-2，OpenClaw skill 调用入口）

对指定币种执行量化筛选 + 深度分析评级。

用法:
    python3 analyze_symbol.py BTCUSDT
    python3 analyze_symbol.py SOLUSDT --fast
"""
import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from datetime import datetime, timezone
from src.infra.binance_public import BinancePublicClient
from src.infra.memory_store import MemoryStore
from src.infra.rate_limiter import RateLimiter
from src.infra.state_store import StateStore
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.skills.skill1_collect import calc_ema
from src.skills.skill1_collect import Skill1Collect
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")

MARKET_BREADTH_MIN_QUOTE_VOLUME = 20_000_000
MAJOR_BREADTH_BASES = {
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "LINK",
    "LTC",
    "BCH",
    "DOT",
    "NEAR",
    "AAVE",
    "UNI",
}


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tradable_usdt_perp_symbols(exchange_info: Dict[str, Any]) -> Set[str]:
    symbols = set()
    for item in exchange_info.get("symbols", []):
        if (
            item.get("status") == "TRADING"
            and item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
        ):
            symbols.add(item.get("symbol", ""))
    return symbols


def _closed_closes(klines: List[list]) -> List[float]:
    closes = []
    for row in klines:
        close = _safe_float(row[4] if len(row) > 4 else None)
        if close is not None and close > 0:
            closes.append(close)
    return closes


def _calculate_breadth(
    client: BinancePublicClient,
    tickers: List[Dict[str, Any]],
    tradable: Set[str],
) -> Dict[str, Any]:
    universe = []
    for item in tickers:
        symbol = item.get("symbol", "")
        if symbol not in tradable:
            continue
        quote_volume = _safe_float(item.get("quoteVolume"))
        change_pct = _safe_float(item.get("priceChangePercent"))
        if quote_volume is None or change_pct is None:
            continue
        if quote_volume < MARKET_BREADTH_MIN_QUOTE_VOLUME:
            continue
        universe.append((symbol, change_pct))

    up_24h = sum(1 for _, change_pct in universe if change_pct > 0)
    breadth_24h = round(up_24h / len(universe) * 100, 2) if universe else None

    up_4h = 0
    sample_4h = 0
    major_up_4h = 0
    major_sample_4h = 0
    for symbol, _ in universe:
        try:
            closes = _closed_closes(client.get_klines_cached(symbol, "4h", 3))
        except Exception:
            continue
        if len(closes) < 2:
            continue
        sample_4h += 1
        is_up = closes[-1] > closes[-2]
        if is_up:
            up_4h += 1
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if base in MAJOR_BREADTH_BASES:
            major_sample_4h += 1
            if is_up:
                major_up_4h += 1

    return {
        "breadth_pct_24h": breadth_24h,
        "breadth_pct_4h": round(up_4h / sample_4h * 100, 2) if sample_4h else None,
        "major_breadth_pct_4h": (
            round(major_up_4h / major_sample_4h * 100, 2)
            if major_sample_4h
            else None
        ),
        "breadth_sample_size": sample_4h,
        "major_breadth_sample_size": major_sample_4h,
    }


def _build_btc_market_context(
    client: BinancePublicClient,
    expected_direction: str = "",
) -> Dict[str, Any]:
    exchange_info = client.get_exchange_info()
    tradable = _tradable_usdt_perp_symbols(exchange_info)
    tickers = client.get_tickers_24hr()
    breadth = _calculate_breadth(client, tickers, tradable)

    klines_4h = client.get_klines_cached("BTCUSDT", "4h", 80)
    closes_4h = _closed_closes(klines_4h)
    if len(closes_4h) < 21:
        return {
            "status": "unknown",
            "reason": "BTC 4h K线不足",
            "symbol": "BTCUSDT",
            **breadth,
        }

    ema5_4h = calc_ema(closes_4h, 5)[-1]
    ema20_4h = calc_ema(closes_4h, 20)[-1]
    last_close_4h = closes_4h[-1]

    realtime_price = None
    for item in tickers:
        if item.get("symbol") == "BTCUSDT":
            realtime_price = _safe_float(item.get("lastPrice"))
            break
    realtime_vs_ema20 = (
        round((realtime_price / ema20_4h - 1) * 100, 4)
        if realtime_price and ema20_4h > 0
        else None
    )
    realtime_recovery = bool(
        realtime_price
        and (
            realtime_price >= ema20_4h * 0.997
            or realtime_price >= last_close_4h * 1.003
        )
    )

    btc_1h_recovery = False
    btc_1h_no_new_low = False
    ema5_1h = None
    ema20_1h = None
    try:
        klines_1h = client.get_klines_cached("BTCUSDT", "1h", 21)
        closes_1h = _closed_closes(klines_1h)
        lows_1h = [
            low
            for low in (
                _safe_float(row[3] if len(row) > 3 else None)
                for row in klines_1h
            )
            if low is not None
        ]
        if len(closes_1h) >= 20:
            ema5_1h = calc_ema(closes_1h[-20:], 5)[-1]
            ema20_1h = calc_ema(closes_1h[-20:], 20)[-1]
        if len(lows_1h) >= 4:
            btc_1h_no_new_low = min(lows_1h[-2:]) >= min(lows_1h[-4:-2])
        btc_1h_recovery = bool(
            (ema5_1h is not None and ema20_1h is not None and ema5_1h >= ema20_1h)
            or btc_1h_no_new_low
        )
    except Exception:
        pass

    recent_return_pct = round((last_close_4h / closes_4h[-7] - 1) * 100, 4)
    hard_weak = last_close_4h < ema20_4h and ema5_4h < ema20_4h * 0.995
    btc_4h_bullish = last_close_4h >= ema20_4h and ema5_4h >= ema20_4h
    downgraded = hard_weak and realtime_recovery and btc_1h_recovery
    if expected_direction == "long":
        status = "cautious" if downgraded else ("blocked" if hard_weak else "enabled")
        reason = (
            "BTC实时价修复且1h趋势止跌"
            if downgraded
            else ("BTC 4h 短期趋势偏弱" if hard_weak else "market_regime_ok")
        )
    elif expected_direction == "short":
        if btc_4h_bullish:
            status = "cautious"
            reason = "BTC 4h偏强，与做空方向冲突"
        elif btc_1h_recovery or realtime_recovery:
            status = "cautious"
            reason = "BTC 4h偏弱但1h/实时修复，追空风险上升"
        elif hard_weak:
            status = "enabled"
            reason = "BTC 4h偏弱，方向上支持做空"
        else:
            status = "context_only"
            reason = "BTC 4h方向不明确，做空仅作谨慎参考"
    else:
        status = "context_only"
        reason = "BTC趋势上下文，仅供方向复核"

    return {
        "status": status,
        "breadth_status": status,
        "reason": reason,
        "symbol": "BTCUSDT",
        "btc_last_close": round(last_close_4h, 4),
        "recent_return_pct": recent_return_pct,
        "btc_ema5": round(ema5_4h, 4),
        "btc_ema20": round(ema20_4h, 4),
        "btc_realtime_price": round(realtime_price, 4) if realtime_price else None,
        "btc_realtime_vs_ema20_pct": realtime_vs_ema20,
        "btc_realtime_recovery": realtime_recovery,
        "btc_1h_recovery": btc_1h_recovery,
        "btc_1h_ema5": round(ema5_1h, 4) if ema5_1h is not None else None,
        "btc_1h_ema20": round(ema20_1h, 4) if ema20_1h is not None else None,
        "btc_1h_no_new_low": btc_1h_no_new_low,
        "btc_regime_downgraded_from_blocked": downgraded,
        "score_adjustment": 15 if downgraded else 0,
        **breadth,
    }


def main():
    parser = argparse.ArgumentParser(description="币种深度分析")
    parser.add_argument("symbol", type=str, help="币种符号（如 BTCUSDT 或 BTC）")
    parser.add_argument("--fast", action="store_true", help="快速 LLM 分析模式")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    db_dir = os.path.join(PROJECT_ROOT, "data")
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    memory_store = MemoryStore(db_path=os.path.join(db_dir, "trading_state.db"))
    client = BinancePublicClient(rate_limiter=RateLimiter())

    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")

    def load_schema(name):
        with open(os.path.join(schema_dir, name)) as f:
            return json.load(f)

    try:
        # ── Skill-1: 指定币种模式 ──
        print(f"📡 Step 1: 收集 {symbol} 市场数据...")
        skill1 = Skill1Collect(
            state_store=store,
            input_schema=load_schema("skill1_input.json"),
            output_schema=load_schema("skill1_output.json"),
            client=client,
        )
        trigger_data = {
            "trigger_time": datetime.now(timezone.utc).isoformat(),
            "target_symbols": [symbol.replace("USDT", "")],
        }
        trigger_id = store.save("analyze_trigger", trigger_data)
        s1_id = skill1.execute(trigger_id)
        s1_data = store.load(s1_id)
        candidates = s1_data.get("candidates", [])

        if not candidates:
            print(f"⚠️  {symbol} 未通过技术指标筛选（信号评分或 ADX 不足）")
            return

        c = candidates[0]
        print(f"   评分: {c['signal_score']}/100 | 方向: {c.get('signal_direction','?')} | "
              f"RSI: {c.get('rsi', 'N/A')} | ADX: {c.get('adx', 'N/A')}")

        if args.fast:
            print("   补充 BTC 4h/1h 与市场广度上下文...")
            market_regime = _build_btc_market_context(
                client,
                expected_direction=c.get("signal_direction", ""),
            )
            s1_data["market_regime"] = market_regime
            for candidate in candidates:
                candidate["market_regime_status"] = market_regime.get("status")
                candidate["market_score_adjustment"] = market_regime.get(
                    "score_adjustment"
                )
            s1_id = store.save("skill1_collect_enriched", s1_data)
            print(
                "   BTC: "
                f"状态={market_regime.get('status')} | "
                f"close={market_regime.get('btc_last_close')} | "
                f"EMA5/20={market_regime.get('btc_ema5')}/"
                f"{market_regime.get('btc_ema20')} | "
                f"1h修复={market_regime.get('btc_1h_recovery')} | "
                f"4h广度={market_regime.get('breadth_pct_4h')}% | "
                f"24h广度={market_regime.get('breadth_pct_24h')}%"
            )

        # ── Skill-2: 深度分析 ──
        mode_str = "快速模式" if args.fast else "完整模式"
        print(f"\n🔬 Step 2: 深度分析（{mode_str}）...")

        rating_threshold, _ = memory_store.get_evolved_params()
        analyzer_fn = create_trading_agents_analyzer(fast_mode=args.fast)
        ta_module = TradingAgentsModule(analyzer=analyzer_fn)

        skill2 = Skill2Analyze(
            state_store=store,
            input_schema=load_schema("skill2_input.json"),
            output_schema=load_schema("skill2_output.json"),
            trading_agents=ta_module,
            rating_threshold=rating_threshold,
        )
        s2_input_id = store.save("skill2_input", {"input_state_id": s1_id})
        s2_id = skill2.execute(s2_input_id)
        s2_data = store.load(s2_id)

        ratings = s2_data.get("ratings", [])
        failed = s2_data.get("failed_symbols", [])

        if ratings:
            for r in ratings:
                print(f"\n✅ {r['symbol']} 通过评级")
                print(f"   评分: {r['rating_score']}/10 | 信号: {r['signal']} | 置信度: {r['confidence']:.0f}%")
                if r.get("comment"):
                    # 截断过长的评论
                    comment = r["comment"][:300]
                    print(f"   点评: {comment}")
        else:
            print(f"\n⚠️  {symbol} 未通过评级（阈值 {rating_threshold} 分）")

        if failed:
            for f_item in failed:
                print(f"   ❌ {f_item['symbol']}: {f_item['reason']}")

    except Exception as e:
        print(f"❌ 分析失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()
        memory_store.close()
        ta_ref = locals().get("ta_module")
        if ta_ref:
            ta_ref.shutdown()


if __name__ == "__main__":
    main()
