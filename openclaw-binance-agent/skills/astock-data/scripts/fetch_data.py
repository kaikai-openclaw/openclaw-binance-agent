#!/usr/bin/env python3
"""
A股历史数据服务 CLI（Skill Data Provider）

用法:
    python3 fetch_data.py sh.600519 2024-01-01 2024-06-30
    python3 fetch_data.py sz.000001 2024-01-01 2024-12-31 --adjust hfq
    python3 fetch_data.py sh.600519 2024-01-01 2024-06-30 --json
"""
import argparse
import json
import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.infra.akshare_client import AkshareClient
from src.infra.state_store import StateStore
from src.skills.skill_data_provider import SkillDataProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)


def load_schema(name: str) -> dict:
    schema_dir = os.path.join(PROJECT_ROOT, "config", "schemas")
    with open(os.path.join(schema_dir, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="A股历史数据服务（Data Provider）")
    parser.add_argument("symbol", type=str,
                        help="股票代码（如 sh.600519, sz.000001, bj.830799）")
    parser.add_argument("start_date", type=str,
                        help="开始日期 YYYY-MM-DD")
    parser.add_argument("end_date", type=str,
                        help="结束日期 YYYY-MM-DD")
    parser.add_argument("--adjust", type=str, default="qfq",
                        choices=["qfq", "hfq", "none"],
                        help="复权方式（默认 qfq 前复权）")
    parser.add_argument("--json", action="store_true",
                        help="输出原始 JSON（供程序调用）")
    args = parser.parse_args()

    db_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(db_dir, exist_ok=True)
    store = StateStore(db_path=os.path.join(db_dir, "state_store.db"))
    client = AkshareClient()

    try:
        skill = SkillDataProvider(
            state_store=store,
            input_schema=load_schema("skill_data_input.json"),
            output_schema=load_schema("skill_data_output.json"),
            client=client,
            cache_db_path=os.path.join(db_dir, "kline_cache.db"),
        )

        input_data = {
            "symbol": args.symbol.lower(),
            "start_date": args.start_date,
            "end_date": args.end_date,
            "frequency": "daily",
            "adjust": args.adjust,
        }

        # 直接调用 run()（跳过 Schema 校验走 execute 也行）
        result = skill.run(input_data)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        # 人类可读输出
        status = result["status_code"]
        meta = result["meta_info"]
        data = result["data"]

        if status != 200:
            print(f"❌ [{status}] {result['message']}")
            sys.exit(1)

        print(f"📊 {meta['symbol']} {meta['name']}  "
              f"[{args.start_date} ~ {args.end_date}]  "
              f"复权={meta['adjust']}  来源={meta['data_source']}")
        print(f"   共 {meta['row_count']} 条记录")
        print("-" * 80)
        print(f"  {'日期':12s} {'开盘':>10s} {'最高':>10s} {'最低':>10s} "
              f"{'收盘':>10s} {'成交量(手)':>12s} {'成交额(元)':>14s}")
        print("-" * 80)

        # 数据量大时只显示头尾
        show_rows = data
        truncated = False
        if len(data) > 20:
            show_rows = data[:10] + data[-10:]
            truncated = True

        for i, row in enumerate(show_rows):
            if truncated and i == 10:
                print(f"  {'... 省略中间 ' + str(len(data) - 20) + ' 行 ...':^80s}")
            print(f"  {row['date']:12s} {row['open']:10.2f} {row['high']:10.2f} "
                  f"{row['low']:10.2f} {row['close']:10.2f} "
                  f"{row['volume']:12d} {row['amount']:14.2f}")

        print("-" * 80)
        skill.close()

    except Exception as e:
        print(f"❌ 数据获取失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
