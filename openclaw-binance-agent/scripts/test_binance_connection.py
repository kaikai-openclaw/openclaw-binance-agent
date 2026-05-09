#!/usr/bin/env python3
"""
Binance 合约账户连通性测试

测试项：
1. 网络连通（ping）
2. 服务器时间同步
3. API Key 认证（获取账户信息）
4. 合约交易权限（获取持仓）

用法：
    # 先在项目根目录创建 .env 文件：
    # BINANCE_API_KEY=your-api-key
    # BINANCE_API_SECRET=your-api-secret
    
    python scripts/test_binance_connection.py
"""

import hashlib
import hmac
import os
import sys
import time
from urllib.parse import urlencode

# 将项目根目录加入 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

try:
    import requests
except ImportError:
    print("❌ 缺少 requests 库，请运行: pip install requests")
    sys.exit(1)

BASE_URL = "https://fapi.binance.com"


def load_env():
    """从 .env 文件加载环境变量。"""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def sign_request(params: dict, secret: str) -> dict:
    """HMAC-SHA256 签名。"""
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    signature = hmac.new(
        secret.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    params["signature"] = signature
    return params


def test_ping():
    """测试 1：网络连通。"""
    print("\n[1/4] 测试网络连通 (ping)...")
    try:
        r = requests.get(f"{BASE_URL}/fapi/v1/ping", timeout=10)
        if r.status_code == 200 and r.json() == {}:
            print("  ✅ Binance fapi 网络连通正常")
            return True
        elif r.status_code == 451:
            print("  ❌ IP 被地区限制 (HTTP 451)")
            print("     请切换代理到香港/日本/新加坡等非限制地区")
            return False
        else:
            print(f"  ❌ 异常响应: HTTP {r.status_code}")
            print(f"     {r.text[:200]}")
            return False
    except requests.exceptions.ConnectionError:
        print("  ❌ 无法连接 Binance 服务器")
        print("     请检查网络连接和代理设置")
        return False
    except Exception as e:
        print(f"  ❌ 连接失败: {e}")
        return False


def test_server_time():
    """测试 2：服务器时间同步。"""
    print("\n[2/4] 测试服务器时间同步...")
    try:
        r = requests.get(f"{BASE_URL}/fapi/v1/time", timeout=10)
        server_time = r.json().get("serverTime", 0)
        local_time = int(time.time() * 1000)
        diff_ms = abs(server_time - local_time)
        diff_s = diff_ms / 1000

        if diff_s < 5:
            print(f"  ✅ 时间同步正常（偏差 {diff_ms}ms）")
            return True
        else:
            print(f"  ⚠️  时间偏差较大: {diff_s:.1f} 秒")
            print("     签名请求可能失败，请同步系统时间")
            return diff_s < 30  # 30 秒内还能用
    except Exception as e:
        print(f"  ❌ 获取服务器时间失败: {e}")
        return False


def test_account(api_key: str, api_secret: str):
    """测试 3：API Key 认证。"""
    print("\n[3/4] 测试 API Key 认证（获取账户信息）...")
    try:
        params = sign_request({}, api_secret)
        headers = {"X-MBX-APIKEY": api_key}
        r = requests.get(
            f"{BASE_URL}/fapi/v2/account",
            params=params,
            headers=headers,
            timeout=10,
        )

        if r.status_code == 200:
            data = r.json()
            balance = float(data.get("totalWalletBalance", 0))
            available = float(data.get("availableBalance", 0))
            pnl = float(data.get("totalUnrealizedProfit", 0))
            print(f"  ✅ API Key 认证成功")
            print(f"     总资金:     {balance:.2f} USDT")
            print(f"     可用保证金:  {available:.2f} USDT")
            print(f"     未实现盈亏:  {pnl:+.2f} USDT")
            return True
        elif r.status_code == 401:
            print("  ❌ API Key 无效或已过期")
            return False
        elif r.status_code == 403:
            print("  ❌ API Key 权限不足")
            msg = r.json().get("msg", "")
            print(f"     {msg}")
            return False
        else:
            print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ 请求失败: {e}")
        return False


def test_positions(api_key: str, api_secret: str):
    """测试 4：合约交易权限。"""
    print("\n[4/4] 测试合约交易权限（获取持仓）...")
    try:
        params = sign_request({}, api_secret)
        headers = {"X-MBX-APIKEY": api_key}
        r = requests.get(
            f"{BASE_URL}/fapi/v2/positionRisk",
            params=params,
            headers=headers,
            timeout=10,
        )

        if r.status_code == 200:
            positions = r.json()
            active = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
            print(f"  ✅ 合约交易权限正常")
            print(f"     监控币种数: {len(positions)}")
            if active:
                print(f"     当前持仓:   {len(active)} 笔")
                for p in active:
                    symbol = p.get("symbol", "")
                    amt = p.get("positionAmt", "0")
                    pnl = float(p.get("unRealizedProfit", 0))
                    print(f"       {symbol}: {amt} (盈亏 {pnl:+.2f} USDT)")
            else:
                print("     当前持仓:   无")
            return True
        else:
            print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ 请求失败: {e}")
        return False


def main():
    print("=" * 50)
    print("  Binance 合约账户连通性测试")
    print("=" * 50)

    load_env()

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    # 测试 1：网络连通
    if not test_ping():
        print("\n❌ 网络不通，后续测试跳过")
        sys.exit(1)

    # 测试 2：时间同步
    test_server_time()

    # 测试 3 & 4：需要 API Key
    if not api_key or not api_secret:
        print("\n⚠️  未配置 API Key，跳过认证测试")
        print("   请在 .env 文件中设置:")
        print("   BINANCE_API_KEY=your-key")
        print("   BINANCE_API_SECRET=your-secret")
        sys.exit(0)

    # 隐藏显示 key（只显示前 6 位）
    print(f"\n  API Key: {api_key[:6]}...{api_key[-4:]}")

    ok3 = test_account(api_key, api_secret)
    ok4 = False
    if ok3:
        ok4 = test_positions(api_key, api_secret)

    # 汇总
    print("\n" + "=" * 50)
    print("  测试结果汇总")
    print("=" * 50)
    results = [
        ("网络连通", True),
        ("时间同步", True),
        ("API 认证", ok3),
        ("合约权限", ok4),
    ]
    all_ok = True
    for name, ok in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}")
        if not ok:
            all_ok = False

    if all_ok:
        print("\n🎉 全部通过，可以开始使用交易 Agent")
    else:
        print("\n⚠️  部分测试未通过，请检查上方错误信息")


if __name__ == "__main__":
    main()
