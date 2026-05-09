#!/usr/bin/env python3
"""监控持仓和条件单状态，检测止盈止损触发"""
import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.infra.binance_fapi import BinanceFapiClient
from src.infra.rate_limiter import RateLimiter

# 加载 .env
if os.path.exists('.env'):
    with open('.env') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

def main():
    client = BinanceFapiClient(
        api_key=os.getenv('BINANCE_API_KEY'),
        api_secret=os.getenv('BINANCE_API_SECRET'),
        rate_limiter=RateLimiter()
    )
    
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "triggered_orders": [],
        "positions": [],
        "account": None
    }
    
    try:
        # 获取账户信息
        account = client.get_account_info()
        results["account"] = {
            "total_balance": account.total_balance,
            "available_balance": account.available_balance,
            "unrealized_pnl": account.total_unrealized_pnl
        }
        
        # 获取持仓
        positions = client.get_positions()
        for pos in positions:
            results["positions"].append({
                "symbol": pos.symbol,
                "position_amt": pos.position_amt,
                "entry_price": pos.entry_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "leverage": pos.leverage
            })
        
        # 获取条件单状态
        algo_orders = client.get_open_algo_orders()
        
        # 获取之前的订单状态（从文件读取）
        state_file = "/tmp/algo_orders_state.json"
        prev_state = {}
        if os.path.exists(state_file):
            with open(state_file) as f:
                prev_state = json.load(f)
        
        # 检查是否有新触发的订单
        current_state = {}
        for order in algo_orders:
            algo_id = str(order.get("algoId", ""))
            status = order.get("algoStatus", "")
            current_state[algo_id] = status
            
            # 检查状态变化
            prev_status = prev_state.get(algo_id, "")
            if prev_status and prev_status != status:
                # 状态发生变化
                symbol = order.get("symbol", "")
                side = order.get("side", "")
                trigger_price = order.get("triggerPrice", "")
                
                if status == "TRIGGERED":
                    order_type = "止盈" if side == "SELL" else "止损"
                    results["triggered_orders"].append({
                        "symbol": symbol,
                        "type": order_type,
                        "trigger_price": trigger_price,
                        "side": side
                    })
        
        # 保存当前状态
        with open(state_file, 'w') as f:
            json.dump(current_state, f)
        
        # 输出结果
        print(json.dumps(results, indent=2, ensure_ascii=False))
        
    except Exception as e:
        print(f"监控出错: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
