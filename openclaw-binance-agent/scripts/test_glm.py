"""快速测试智谱 GLM API 连通性 + adapter 配置加载"""
import os
import sys
import json
import time
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "").strip()
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "").strip()
DEEP_THINK_LLM = os.environ.get("DEEP_THINK_LLM", "").strip()
QUICK_THINK_LLM = os.environ.get("QUICK_THINK_LLM", "").strip()
BACKEND_URL = os.environ.get("LLM_BACKEND_URL", "").strip()

print("=" * 50)
print("智谱 GLM 配置检查")
print("=" * 50)
print(f"LLM_PROVIDER:    {LLM_PROVIDER}")
print(f"DEEP_THINK_LLM:  {DEEP_THINK_LLM}")
print(f"QUICK_THINK_LLM: {QUICK_THINK_LLM}")
print(f"LLM_BACKEND_URL: {BACKEND_URL}")
print(f"ZHIPU_API_KEY:   {ZHIPU_API_KEY[:8]}...{ZHIPU_API_KEY[-4:]}" if ZHIPU_API_KEY else "ZHIPU_API_KEY: 未设置!")
print()

if not ZHIPU_API_KEY:
    print("[FAIL] ZHIPU_API_KEY 未设置，无法测试")
    sys.exit(1)

# 测试 1: 直接调用智谱 API
print("-" * 50)
print(f"[测试1] 直接调用 {DEEP_THINK_LLM} ...")
t0 = time.time()
try:
    resp = requests.post(
        f"{BACKEND_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {ZHIPU_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEP_THINK_LLM,
            "messages": [{"role": "user", "content": "用一句话介绍你自己"}],
        },
        timeout=30,
    )
    elapsed = time.time() - t0
    if resp.status_code == 200:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        model_used = data.get("model", "unknown")
        print(f"[OK] {elapsed:.1f}s | model={model_used}")
        print(f"     回复: {content[:200]}")
    else:
        print(f"[FAIL] HTTP {resp.status_code} | {elapsed:.1f}s")
        print(f"       {resp.text[:300]}")
except Exception as e:
    print(f"[FAIL] {e}")

# 测试 2: 通过 adapter 的配置加载
print()
print("-" * 50)
print("[测试2] adapter 配置加载验证 ...")
try:
    from src.integrations.trading_agents_adapter import (
        DEFAULT_LLM_PROVIDER, DEFAULT_DEEP_THINK_LLM,
        DEFAULT_QUICK_THINK_LLM, DEFAULT_BACKEND_URL, FAST_LLM_MODEL,
    )
    print(f"  DEFAULT_LLM_PROVIDER:  {DEFAULT_LLM_PROVIDER}")
    print(f"  DEFAULT_DEEP_THINK_LLM: {DEFAULT_DEEP_THINK_LLM}")
    print(f"  DEFAULT_QUICK_THINK_LLM: {DEFAULT_QUICK_THINK_LLM}")
    print(f"  DEFAULT_BACKEND_URL:   {DEFAULT_BACKEND_URL}")
    print(f"  FAST_LLM_MODEL:        {FAST_LLM_MODEL}")

    ok = (DEFAULT_LLM_PROVIDER == "zhipu"
          and "GLM" in DEFAULT_DEEP_THINK_LLM
          and "GLM" in DEFAULT_QUICK_THINK_LLM)
    print(f"\n  [{'OK' if ok else 'WARN'}] adapter 是否正确加载智谱配置: {ok}")
except Exception as e:
    print(f"  [FAIL] adapter 导入失败: {e}")

print()
print("=" * 50)
print("测试完成")
