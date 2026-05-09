"""诊断超买筛选：显示评分分布和顶部确认情况，快速定位 scored=0 的原因。"""
import logging
import re
import sys

logging.disable(logging.CRITICAL)

from src.infra.binance_public import BinancePublicClient
from src.skills.crypto_overbought import (
    calc_overbought_score,
    _calc_drawdown_from_high,
    _check_kdj_dead_cross,
    H1_RSI_THRESHOLD, H1_BIAS_THRESHOLD, H1_CONSECUTIVE_UP,
    H1_RALLY_PCT, H1_RALLY_LOOKBACK, H1_RISE_LOOKBACK,
    H1_W_RSI, H1_W_FUNDING, H1_W_BIAS, H1_W_VOL_DIV,
    H1_W_BOLL, H1_W_RALLY, H1_W_KDJ, H1_W_MACD_DIV,
    H1_W_SHADOW, H1_W_SQUEEZE_RISK,
    DEFAULT_MIN_QUOTE_VOLUME, DEFAULT_MIN_OVERBOUGHT_SCORE,
)

MODE = sys.argv[1] if len(sys.argv) > 1 else "1h"
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 50  # 取前 N 个币种

client = BinancePublicClient()
print("获取行情数据...", flush=True)
tickers = client.get_tickers_24hr()

try:
    info = client.get_exchange_info()
    tradable = {
        s["symbol"] for s in info.get("symbols", [])
        if s.get("status") == "TRADING"
        and s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
    }
except Exception:
    tradable = set()

exclude_bases = {"USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP"}
pool = []
for t in tickers:
    sym = t.get("symbol", "")
    if sym not in tradable:
        continue
    base = sym.replace("USDT", "")
    if base in exclude_bases or not re.match(r"^[A-Z0-9]{2,15}$", base):
        continue
    qv = float(t.get("quoteVolume", 0))
    if qv < DEFAULT_MIN_QUOTE_VOLUME:
        continue
    t["quoteVolume"] = qv
    pool.append(t)

print(f"基础过滤后: {len(pool)} 个，取前 {SAMPLE} 个扫描...", flush=True)

weights = {
    "rsi": H1_W_RSI, "funding": H1_W_FUNDING,
    "bias": H1_W_BIAS, "vol_div": H1_W_VOL_DIV,
    "boll": H1_W_BOLL, "rally": H1_W_RALLY,
    "kdj": H1_W_KDJ, "macd_div": H1_W_MACD_DIV,
    "shadow": H1_W_SHADOW, "squeeze_risk": H1_W_SQUEEZE_RISK,
}

results = []
for item in pool[:SAMPLE]:
    sym = item["symbol"]
    try:
        klines = client.get_klines(sym, MODE, 100)
        if not klines or len(klines) < 60:
            continue
        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]
        opens  = [float(k[1]) for k in klines]
        vols   = [float(k[5]) for k in klines]

        r = calc_overbought_score(
            closes, highs, lows, opens, vols,
            None, None, item["quoteVolume"],
            H1_RSI_THRESHOLD, H1_BIAS_THRESHOLD, H1_CONSECUTIVE_UP,
            H1_RALLY_PCT, H1_RALLY_LOOKBACK, H1_RISE_LOOKBACK, weights,
        )
        score = r["overbought_score"]
        dd = _calc_drawdown_from_high(closes, H1_RALLY_LOOKBACK, highs) or 0.0
        has_dd = -12.0 <= dd <= -2.0
        macd_div = bool(r.get("macd_divergence"))
        rsi_div  = bool(r.get("rsi_divergence"))
        vol_div  = bool(r.get("volume_divergence"))
        kdj_dead = bool(
            r.get("kdj_j") and r["kdj_j"] > 80
            and _check_kdj_dead_cross(closes, highs, lows, 70.0)
        )
        confirm = macd_div or rsi_div or vol_div or kdj_dead
        results.append((score, sym, dd, has_dd, macd_div, rsi_div, vol_div, kdj_dead, confirm, r["signal_details"]))
    except Exception as e:
        pass

results.sort(reverse=True)

# ── 统计 ──
score_ge40 = sum(1 for r in results if r[0] >= DEFAULT_MIN_OVERBOUGHT_SCORE)
score_ge20 = sum(1 for r in results if r[0] >= 20)
score_ge10 = sum(1 for r in results if r[0] >= 10)
passed_all = sum(1 for r in results if r[0] >= DEFAULT_MIN_OVERBOUGHT_SCORE and r[3] and r[8])

print()
print(f"═══ 评分分布（前 {SAMPLE} 个）═══")
print(f"  评分 ≥ 40（进入顶部确认）: {score_ge40}")
print(f"  评分 ≥ 20                : {score_ge20}")
print(f"  评分 ≥ 10                : {score_ge10}")
print(f"  最终通过（分+回撤+确认）  : {passed_all}")
print()

# ── 明细（评分前20）──
print(f"{'符号':<16} {'分':>4} {'回撤%':>7} {'回撤OK':>6} {'MACD':>5} {'RSI':>5} {'量背':>5} {'KDJ':>5} {'确认':>5}  状态")
print("─" * 80)
for score, sym, dd, dd_ok, macd, rsi, vol, kdj, confirm, sig in results[:20]:
    if score >= DEFAULT_MIN_OVERBOUGHT_SCORE and dd_ok and confirm:
        status = "✅ 通过"
    elif score < DEFAULT_MIN_OVERBOUGHT_SCORE:
        status = f"分不足({score})"
    elif not dd_ok:
        status = f"回撤不符({dd:.1f}%)"
    else:
        status = "无确认信号"
    m = "T" if macd else "."
    rs = "T" if rsi else "."
    v = "T" if vol else "."
    k = "T" if kdj else "."
    print(f"{sym:<16} {score:>4} {dd:>7.1f} {str(dd_ok):>6} {m:>5} {rs:>5} {v:>5} {k:>5} {str(confirm):>5}  {status}")
    if score >= 15:
        print(f"  └ {sig[:90]}")
