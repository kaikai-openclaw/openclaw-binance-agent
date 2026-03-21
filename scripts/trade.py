#!/usr/bin/env python3
"""
Binance Trade Executor
Places spot or futures orders on Binance.
⚠️ SAFETY: By default runs in DRY-RUN mode. Set DRY_RUN=false for live trading.
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
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

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


def place_spot_order(symbol, side, order_type, quantity, price=None):
    if DRY_RUN:
        return {"orderId": "DRY_RUN", "status": "dry_run", "side": side, "symbol": symbol,
                "type": order_type, "origQty": quantity, "price": price, "note": "Dry run — no real order"}

    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol, "side": side, "type": order_type,
        "quantity": quantity, "timestamp": ts, "recvWindow": 5000
    }
    if price:
        params["price"] = price
        params["timeInForce"] = "GTC"
    if order_type == "MARKET" and not price:
        # Market order needs quote order quantity
        pass
    params["signature"] = sign(params, API_SECRET)
    r = requests.post(f"{SPOT_URL}/v3/order", params=params, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def place_futures_order(symbol, side, position_side, order_type, quantity, price=None, leverage=10):
    if DRY_RUN:
        return {"orderId": "DRY_RUN", "status": "dry_run", "side": side, "symbol": symbol,
                "type": order_type, "origQty": quantity, "price": price,
                "positionSide": position_side, "leverage": leverage,
                "note": "Dry run — no real order"}

    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol, "side": side, "positionSide": position_side,
        "type": order_type, "quantity": quantity, "leverage": leverage,
        "timestamp": ts, "recvWindow": 5000
    }
    if price:
        params["price"] = price
        params["timeInForce"] = "GTC"
    params["signature"] = sign(params, API_SECRET)
    r = requests.post(f"{FUTURES_URL}/v1/order", params=params, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def set_leverage(symbol, leverage):
    if DRY_RUN:
        return {"leverage": leverage, "symbol": symbol, "status": "dry_run"}
    ts = int(time.time() * 1000)
    params = {
        "symbol": symbol, "leverage": leverage,
        "timestamp": ts, "recvWindow": 5000
    }
    params["signature"] = sign(params, API_SECRET)
    r = requests.post(f"{FUTURES_URL}/v1/leverage", params=params, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser(description="Binance Trade Executor")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair, e.g. BTCUSDT")
    parser.add_argument("--side", default="BUY", choices=["BUY", "SELL"])
    parser.add_argument("--type", default="MARKET", choices=["MARKET", "LIMIT"])
    parser.add_argument("--quantity", required=True, help="Order quantity")
    parser.add_argument("--price", type=float, help="Limit price (for LIMIT orders)")
    parser.add_argument("--market", default="spot", choices=["spot", "futures"])
    parser.add_argument("--leverage", type=int, default=10, help="Leverage for futures (default: 10x)")
    parser.add_argument("--position-side", default="LONG", choices=["LONG", "SHORT"],
                        help="Position side for futures")
    args = parser.parse_args()

    if DRY_RUN:
        print("⚠️  DRY-RUN MODE — no real orders will be placed")
        print(f"   To disable: set DRY_RUN=false\n")

    try:
        if args.market == "futures":
            if args.side == "BUY":
                position_side = "LONG"
            else:
                position_side = "SHORT"
            leverage = args.leverage
            set_leverage(args.symbol.upper(), leverage)
            result = place_futures_order(
                args.symbol.upper(), args.side, position_side,
                args.type.upper(), args.quantity, args.price, leverage
            )
        else:
            result = place_spot_order(
                args.symbol.upper(), args.side, args.type.upper(),
                args.quantity, args.price
            )
        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
