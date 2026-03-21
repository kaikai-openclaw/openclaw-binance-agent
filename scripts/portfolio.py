#!/usr/bin/env python3
"""
Binance Portfolio Monitor
Shows spot & futures balances, positions, and P&L.
"""

import sys
import json
import os
import hmac
import hashlib
import time
import argparse
from datetime import datetime

try:
    import requests
except ImportError:
    print("requests not installed. Run: pip install requests")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────

TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

if TESTNET:
    SPOT_URL = "https://testnet.binance.vision/api"
    FUTURES_URL = "https://testnet.binance.vision/api"
else:
    SPOT_URL = "https://api.binance.com/api"
    FUTURES_URL = "https://fapi.binance.com/fapi"

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

HEADERS = {"X-MBX-APIKEY": API_KEY} if API_KEY else {}


# ── Helpers ─────────────────────────────────────────────────────────────────

def sign(params, secret):
    qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def spot_account():
    if not API_KEY:
        return {"error": "BINANCE_API_KEY not set"}
    ts = int(time.time() * 1000)
    params = {"timestamp": ts, "recvWindow": 5000}
    params["signature"] = sign(params, API_SECRET)
    r = requests.get(f"{SPOT_URL}/v3/account", params=params, headers=HEADERS)
    if r.status_code != 200:
        return {"error": r.text}
    data = r.json()
    return {
        "balances": [
            {"asset": b["asset"], "free": float(b["free"]), "locked": float(b["locked"])}
            for b in data.get("balances", [])
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        ]
    }


def futures_account():
    if not API_KEY:
        return {"error": "BINANCE_API_KEY not set"}
    ts = int(time.time() * 1000)
    params = {"timestamp": ts, "recvWindow": 5000}
    params["signature"] = sign(params, API_SECRET)
    r = requests.get(f"{FUTURES_URL}/v2/account", params=params, headers=HEADERS)
    if r.status_code != 200:
        # try v1 fallback
        r = requests.get(f"{FUTURES_URL}/v1/account", params=params, headers=HEADERS)
        if r.status_code != 200:
            return {"error": r.text}
    data = r.json()
    assets = data.get("assets", [])
    positions = data.get("positions", [])
    return {"assets": assets, "positions": positions, "total_balance": data.get("totalMarginBalance", 0)}


def get_futures_positions():
    if not API_KEY:
        return []
    ts = int(time.time() * 1000)
    params = {"timestamp": ts, "recvWindow": 5000}
    params["signature"] = sign(params, API_SECRET)
    r = requests.get(f"{FUTURES_URL}/v1/positionRisk", params=params, headers=HEADERS)
    if r.status_code != 200:
        return []
    positions = r.json()
    return [
        {
            "symbol": p["symbol"],
            "size": float(p.get("positionAmt", 0)),
            "entry_price": float(p.get("entryPrice", 0)),
            "mark_price": float(p.get("markPrice", 0)),
            "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
            "leverage": int(p.get("leverage", 1)),
            "isolol": p.get("isolatedMargin", ""),
        }
        for p in positions
        if float(p.get("positionAmt", 0)) != 0
    ]


def portfolio_summary():
    result = {"spot": None, "futures": None, "timestamp": datetime.now().isoformat()}

    spot = spot_account()
    if "error" not in spot:
        result["spot"] = spot

    futures = futures_account()
    if "error" not in futures:
        result["futures"] = futures

    positions = get_futures_positions()
    result["futures_positions"] = positions

    return result


def main():
    parser = argparse.ArgumentParser(description="Binance Portfolio Monitor")
    parser.add_argument("--format", default="json", choices=["json", "md"])
    parser.add_argument("--show-zero", action="store_true", help="Show zero-balance assets")
    args = parser.parse_args()

    result = portfolio_summary()

    if args.format == "md":
        lines = ["## Binance Portfolio\n"]

        if result.get("spot"):
            lines.append("### Spot Account\n")
            lines.append("| Asset | Available | Locked |")
            lines.append("|-------|-----------|--------|")
            for b in result["spot"].get("balances", []):
                lines.append(f"| {b['asset']} | {b['free']:.6f} | {b['locked']:.6f} |")
            lines.append("")

        if result.get("futures_positions"):
            lines.append("### Futures Positions\n")
            lines.append("| Symbol | Side | Size | Entry | Mark | P&L |")
            lines.append("|--------|------|------|-------|------|-----|")
            for p in result["futures_positions"]:
                side = "LONG" if p["size"] > 0 else "SHORT"
                lines.append(
                    f"| {p['symbol']} | {side} | {abs(p['size']):.4f} "
                    f"| ${p['entry_price']:.4f} | ${p['mark_price']:.4f} "
                    f"| {p['unrealized_pnl']:+.2f} |"
                )
            lines.append("")

        if result.get("futures") and result["futures"].get("assets"):
            lines.append("### Futures Assets\n")
            lines.append("| Asset | Margin Balance |")
            lines.append("|-------|---------------|")
            for a in result["futures"]["assets"]:
                if float(a.get("marginBalance", 0)) > 0:
                    lines.append(f"| {a['asset']} | ${float(a['marginBalance']):.2f} |")
            lines.append("")

        if not lines[-1]:
            lines.pop()
        print("\n".join(lines) if lines else "No account data. Set BINANCE_API_KEY / BINANCE_API_SECRET.")
    else:
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
