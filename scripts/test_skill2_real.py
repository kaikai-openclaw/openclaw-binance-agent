"""
Skill2 真实集成测试：用 TradingAgents + MiniMax 跑完整 Skill2 链路。
测试 BTC-USD 单币种，验证 adapter → TradingAgentsModule → Skill2Analyze 全链路。
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
log = logging.getLogger(__name__)

from src.infra.state_store import StateStore
from src.integrations.trading_agents_adapter import create_trading_agents_analyzer
from src.skills.skill2_analyze import Skill2Analyze, TradingAgentsModule


def main():
    print("=" * 60)
    print("Skill2 真实集成测试 (TradingAgents + MiniMax)")
    print("=" * 60)

    # 1. 初始化
    store = StateStore(db_path="data/test_state.db")
    with open("config/schemas/skill2_input.json") as f:
        in_schema = json.load(f)
    with open("config/schemas/skill2_output.json") as f:
        out_schema = json.load(f)

    # 2. 创建真实 analyzer（会调用 MiniMax API）
    print("\n[init] 初始化 TradingAgents (MiniMax)...")
    t0 = time.time()
    analyzer = create_trading_agents_analyzer(
        max_debate_rounds=0,
    )
    print(f"   初始化耗时: {time.time() - t0:.1f}s")

    # 3. 组装 Skill2
    ta_module = TradingAgentsModule(analyzer=analyzer)
    skill2 = Skill2Analyze(
        state_store=store,
        input_schema=in_schema,
        output_schema=out_schema,
        trading_agents=ta_module,
        rating_threshold=7,
    )

    # 4. 伪造 Skill-1 输出（只测 BTC，减少 API 消耗）
    now = datetime.now(timezone.utc).isoformat()
    s1_out_id = store.save(
        "skill1_collect",
        {
            "pipeline_run_id": "skill2-real-test",
            "candidates": [
                {
                    "symbol": "BTCUSDT",
                    "heat_score": 9.0,
                    "source_url": "https://test",
                    "collected_at": now,
                },
            ],
        },
    )
    s2_in_id = store.save("trigger_skill2", {"input_state_id": s1_out_id})

    print(f"\n[data] Skill-1 输出 state_id: {s1_out_id}")
    print(f"[data] Skill-2 输入 state_id: {s2_in_id}")
    print("\n[run] 开始执行 Skill2 (可能需要 2-5 分钟)...")
    print("-" * 60)

    t1 = time.time()
    try:
        out_id = skill2.execute(s2_in_id)
        elapsed = time.time() - t1
        result = store.load(out_id)

        print("-" * 60)
        print(f"\n[ok] Skill2 执行成功! 耗时: {elapsed:.1f}s")
        print(f"[out] 输出 state_id: {out_id}")
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False)}")

        # 简要总结
        ratings = result.get("ratings", [])
        failed = result.get("failed_symbols", [])
        print(f"\n[summary] 通过评级: {len(ratings)} 个")
        for r in ratings:
            print(
                f"   {r['symbol']}: score={r['rating_score']}, "
                f"signal={r['signal']}, confidence={r['confidence']}"
            )
        if failed:
            print(f"[fail] 失败: {len(failed)} 个")
            for f_ in failed:
                print(f"   {f_['symbol']}: {f_['reason']}")

    except Exception as e:
        elapsed = time.time() - t1
        print(f"\n[error] Skill2 执行失败 ({elapsed:.1f}s): {e}")
        log.exception("详细错误")
        sys.exit(1)
    finally:
        ta_module.shutdown()
        store.close()


if __name__ == "__main__":
    main()
