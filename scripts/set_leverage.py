#!/usr/bin/env python3
"""调整杠杆 - 直接调用 Binance fapi API"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

import requests
import hashlib
import hmac
from urllib.parse import urlencode

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BREVUSDT"
LEVERAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 10
BASE_URL = "https://fapi.binance.com"

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

ts = int(time.time() * 1000)
params = {"symbol": SYMBOL, "leverage": LEVERAGE, "timestamp": ts}
query_string = urlencode(params)
signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
params["signature"] = signature

# GET 方式
resp = requests.post(
    f"{BASE_URL}/fapi/v1/leverage?{urlencode(params)}",
    headers={"X-MBX-APIKEY": api_key},
    timeout=10
)
result = resp.json()

if resp.status_code == 200:
    print(f"✅ {SYMBOL} 杠杆已调整为 {LEVERAGE}x")
    print(f"   回报: {result}")
else:
    print(f"❌ 失败 [{resp.status_code}]: {result}")
