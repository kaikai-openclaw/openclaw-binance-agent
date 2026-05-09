#!/usr/bin/env python3
"""查询账户状态"""
import sqlite3
import json

db_path = "data/test_state.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 获取最新的账户相关信息
cursor.execute("""
    SELECT skill_name, data, created_at
    FROM state_snapshots
    WHERE skill_name IN ('skill1_collect', 'account_info', 'position_info')
    ORDER BY created_at DESC
    LIMIT 20
""")

results = cursor.fetchall()
for skill_name, data, created_at in results:
    print(f"\n[{created_at}] {skill_name}")
    try:
        parsed = json.loads(data)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    except:
        print(data)
    print("-" * 60)

conn.close()
