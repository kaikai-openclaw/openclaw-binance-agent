import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import GLOBAL_RATE_LIMITER

def test_singleton():
    client1 = BinanceFapiClient(api_key="1", api_secret="1")
    client2 = BinanceFapiClient(api_key="2", api_secret="2")

    # 验证多个客户端实例共享全局限流器
    assert client1.rate_limiter is GLOBAL_RATE_LIMITER, "Client 1 does not use global rate limiter"
    assert client2.rate_limiter is GLOBAL_RATE_LIMITER, "Client 2 does not use global rate limiter"
    assert client1.rate_limiter is client2.rate_limiter, "Clients do not share the same rate limiter"

    # 验证初始的 token 为 MAX_BURST
    assert GLOBAL_RATE_LIMITER._tokens == 30.0, f"Expected 30 tokens, got {GLOBAL_RATE_LIMITER._tokens}"
    
    print("Test passed: Rate limiter is a global singleton and initialized with correct burst limit.")

if __name__ == "__main__":
    test_singleton()
