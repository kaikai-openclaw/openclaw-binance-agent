#!/usr/bin/env python3
"""分析 RAYSOLUSDT"""
import sys
import os
import json
import logging

logging.basicConfig(level=logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.integrations.trading_agents_adapter import create_trading_agents_analyzer

analyzer = create_trading_agents_analyzer(max_debate_rounds=0)
result = analyzer('RAYSOLUSDT', {'symbol': 'RAYSOLUSDT', 'heat_score': 9.0})

with open('/tmp/raysol_result.json', 'w') as f:
    json.dump(result, f, indent=2)

print("Done!")
