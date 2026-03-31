import requests
import json
import os

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("GOOGLE_API_KEY", "")
if not API_KEY:
    raise RuntimeError("GOOGLE_API_KEY 未设置，请在 .env 文件中配置")

def get_hot_coins(top=3):
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10)
    data = [d for d in r.json() if d["symbol"].endswith("USDT")]
    sorted_coins = sorted(data, key=lambda x: float(x["quoteVolume"]), reverse=True)[:top]
    return sorted_coins

def analyze(coin):
    prompt = f"""
    分析加密货币 {coin['symbol']} 的近期市场活跃度：
    - 当前价格: {coin['lastPrice']}
    - 24h涨跌幅: {coin['priceChangePercent']}%
    - 24h最高/最低: {coin['highPrice']} / {coin['lowPrice']}
    - 24h成交额(USDT): {coin['quoteVolume']}
    
    给出简短的市场状态分析（不超过50个字），并给出评级分数（1-10分）和交易方向（LONG/SHORT/HOLD）。
    返回严格的JSON格式: {{"rating": <int>, "direction": "<str>", "analysis": "<str>"}}
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=payload)
        resp = r.json()
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        return {"rating": 5, "direction": "HOLD", "analysis": "分析失败: " + str(e)}

def main():
    coins = get_hot_coins(3)
    results = []
    for c in coins:
        print(f"Analyzing {c['symbol']}...")
        ans = analyze(c)
        ans["symbol"] = c["symbol"]
        ans["price"] = c["lastPrice"]
        ans["change"] = c["priceChangePercent"]
        results.append(ans)
    print("---RESULT---")
    print(json.dumps(results, ensure_ascii=False))

if __name__ == "__main__":
    main()
