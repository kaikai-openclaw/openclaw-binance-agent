# 实现计划：OpenClaw Binance 交易 Agent

## 概述

基于 5 步流水线 Skill 架构，按照自底向上的依赖顺序实现：先构建数据模型和基础设施层（State_Store、Memory_Store、Rate_Limiter、Risk_Controller、Binance_Fapi_Client），再实现 Skill 基类和各 Skill 业务逻辑，最后完成 Pipeline 编排和启动脚本。所有代码使用 Python，测试使用 pytest + hypothesis。

## 任务

- [ ] 1. 搭建项目结构与核心数据模型
  - [x] 1.1 初始化项目骨架
    - 创建 `pyproject.toml`，声明依赖：jsonschema、hypothesis、pytest、requests、websocket-client
    - 创建目录结构：`src/`、`src/skills/`、`src/infra/`、`src/models/`、`config/`、`config/schemas/`、`tests/`、`data/`、`scripts/`
    - 创建各目录的 `__init__.py`
    - _需求: 9.1_

  - [x] 1.2 实现核心数据模型 `src/models/types.py`
    - 定义所有 dataclass：`Candidate`、`Rating`、`TradePlan`、`ExecutionResult`、`TradeRecord`、`StrategyStats`、`ReflectionLog`、`AccountState`、`OrderRequest`、`ValidationResult`
    - 定义所有枚举：`TradeDirection`、`Signal`、`OrderStatus`、`PipelineStatus`、`AlertLevel`
    - _需求: 3.3, 3.8, 5.2_

  - [x] 1.3 编写属性测试：盈亏比例计算正确性
    - **Property 19: 盈亏比例计算正确性**
    - 在 `tests/test_properties.py` 中实现 `test_pnl_ratio_calculation`
    - 验证做多/做空方向的盈亏比例公式
    - **验证需求: 5.2**

  - [x] 1.4 编写属性测试：数值参数边界校验
    - **Property 16: 数值参数边界校验**
    - 在 `tests/test_properties.py` 中实现 `test_numeric_boundary_validation`
    - 验证非正数价格和非正数头寸规模被拒绝
    - **验证需求: 3.8**

- [ ] 2. 实现 State_Store 状态存储
  - [x] 2.1 实现 `src/infra/state_store.py`
    - 实现 `StateStore` 类：`save()`、`load()`、`get_latest()` 方法
    - 创建 SQLite 表 `state_snapshots`（含 state_id、skill_name、data、created_at、status 字段）
    - 创建索引 `idx_skill_created`
    - 定义 `StateNotFoundError` 异常
    - _需求: 1.6, 2.5, 3.6, 4.13, 5.1, 6.1, 6.2, 6.5_

  - [x] 2.2 编写属性测试：State_Store 存取 round-trip
    - **Property 1: State_Store 存取 round-trip**
    - 在 `tests/test_properties.py` 中实现 `test_state_store_round_trip`
    - 验证任意 JSON 数据存取一致性，state_id 符合 UUID v4 格式
    - **验证需求: 1.6, 2.1, 2.5, 3.1, 3.6, 4.1, 4.13, 5.1, 5.3, 6.1, 6.2**

  - [x] 2.3 编写单元测试：State_Store 异常场景
    - 在 `tests/test_state_store.py` 中测试 `StateNotFoundError`、`get_latest` 空结果
    - _需求: 6.5_

- [x] 3. 检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请向用户确认。

- [ ] 4. 实现 Rate_Limiter 限流器
  - [x] 4.1 实现 `src/infra/rate_limiter.py`
    - 实现基于令牌桶算法的 `RateLimiter` 类
    - 实现 `acquire()`、`pause()`、`stop()`、`get_queue_size()` 方法
    - 正常速率 1000 次/分钟，降级速率 500 次/分钟，队列阈值 800
    - _需求: 4.8, 7.1, 7.2, 7.3_

  - [x] 4.2 编写属性测试：限流速率不变量
    - **Property 11: 限流速率不变量**
    - 在 `tests/test_properties.py` 中实现 `test_rate_limiter_invariant`
    - 验证任意 60 秒窗口内请求数不超过速率上限
    - **验证需求: 4.8, 7.1**

  - [x] 4.3 编写属性测试：限流自动降速
    - **Property 12: 限流自动降速**
    - 在 `tests/test_properties.py` 中实现 `test_rate_limiter_auto_degrade`
    - 验证队列超过 800 时自动降速至 500/min
    - **验证需求: 7.2**

  - [x] 4.4 编写单元测试：Rate_Limiter HTTP 429/418 处理
    - 在 `tests/test_rate_limiter.py` 中测试 `pause(30)` 和 `stop()` 行为
    - _需求: 7.3, 7.4_

- [ ] 5. 实现 Risk_Controller 风控拦截层
  - [x] 5.1 实现 `src/infra/risk_controller.py`
    - 实现 `RiskController` 类，硬编码四大风控常量
    - 实现 `validate_order()`：单笔保证金 ≤ 20%、单币持仓 ≤ 30%、止损冷却期检查
    - 实现 `check_daily_loss()`：日亏损 ≥ 5% 检测
    - 实现 `execute_degradation()`：取消挂单、停止实盘、告警、切换 Paper Mode
    - 实现 `is_paper_mode()`、`record_stop_loss()`
    - _需求: 4.2, 4.11, 4.12, 8.1, 8.2, 8.3, 8.4, 8.5, 8.8, 8.9_

  - [x] 5.2 编写属性测试：风控断言不变量
    - **Property 7: 风控断言不变量**
    - 在 `tests/test_properties.py` 中实现 `test_risk_controller_invariant`
    - 验证通过校验的订单必须满足所有硬编码约束
    - **验证需求: 3.4, 4.2, 8.1, 8.2, 8.8**

  - [x] 5.3 编写属性测试：止损冷却期
    - **Property 8: 止损冷却期**
    - 在 `tests/test_properties.py` 中实现 `test_stop_loss_cooldown`
    - 验证 24 小时内同方向订单被拒绝，超过 24 小时后放行
    - **验证需求: 8.3**

  - [x] 5.4 编写属性测试：日亏损降级触发
    - **Property 9: 日亏损降级触发**
    - 在 `tests/test_properties.py` 中实现 `test_daily_loss_degradation`
    - 验证亏损 ≥ 5% 时触发降级并进入 Paper Mode
    - **验证需求: 4.11, 4.12, 8.5**

  - [x] 5.5 编写单元测试：Risk_Controller 降级流程
    - 在 `tests/test_risk_controller.py` 中测试完整降级流程（取消挂单→停止实盘→告警→Paper Mode）
    - _需求: 8.5, 8.6, 8.7_

- [ ] 6. 实现 Binance_Fapi_Client 合约客户端
  - [x] 6.1 实现 `src/infra/binance_fapi.py`
    - 实现 `BinanceFapiClient` 类
    - 实现 `place_limit_order()`、`place_market_order()`、`get_positions()`、`get_account_info()`、`cancel_all_orders()`、`get_position_risk()`
    - 实现指数退避重试逻辑（退避序列 [1, 2, 4, 8, 16]，最多 5 次）
    - 集成 Rate_Limiter 进行请求限流
    - 处理 HTTP 429（暂停 30s）和 HTTP 418（紧急停止）
    - _需求: 4.3, 4.9, 4.10, 7.3, 7.4, 7.5, 7.6_

  - [x] 6.2 编写属性测试：指数退避序列正确性
    - **Property 13: 指数退避序列正确性**
    - 在 `tests/test_properties.py` 中实现 `test_exponential_backoff_sequence`
    - 验证第 N 次重试等待时间为 2^N 秒，序列为 [1, 2, 4, 8, 16]
    - **验证需求: 7.6**

- [ ] 7. 实现 Memory_Store 长期记忆库
  - [x] 7.1 实现 `src/infra/memory_store.py`
    - 实现 `MemoryStore` 类：`record_trade()`、`get_recent_trades()`、`compute_stats()`、`save_reflection()`、`get_latest_reflection()`
    - 创建 SQLite 表存储交易记录和反思日志
    - _需求: 5.3, 5.4, 5.5, 5.7_

  - [x] 7.2 编写属性测试：策略统计与调优触发
    - **Property 15: 策略统计与调优触发**
    - 在 `tests/test_properties.py` 中实现 `test_strategy_stats_and_tuning`
    - 验证胜率计算公式正确，胜率 < 40% 时生成调优建议
    - **验证需求: 5.4, 5.5, 5.6**

- [x] 8. 检查点 - 确保所有基础设施层测试通过
  - 确保所有测试通过，如有问题请向用户确认。

- [ ] 9. 创建 JSON Schema 定义文件
  - [x] 9.1 创建所有 Skill 的 JSON Schema 文件
    - 在 `config/schemas/` 下创建 10 个 Schema 文件：`skill1_input.json` 至 `skill5_output.json`
    - 按照设计文档中定义的 Schema 结构编写
    - _需求: 9.1_

  - [x] 9.2 编写属性测试：Schema 校验通过（合法数据）
    - **Property 2: Schema 校验通过——合法数据**
    - 在 `tests/test_properties.py` 中实现 `test_schema_valid_data_passes`
    - 验证符合 Schema 的合法数据通过校验
    - **验证需求: 1.5, 2.3, 3.3, 9.2, 9.3**

  - [x] 9.3 编写属性测试：Schema 校验拒绝（非法数据）
    - **Property 3: Schema 校验拒绝——非法数据**
    - 在 `tests/test_properties.py` 中实现 `test_schema_invalid_data_rejected`
    - 验证不符合 Schema 的数据被拒绝
    - **验证需求: 9.4, 9.5**

  - [x] 9.4 编写单元测试：Schema 校验边界场景
    - 在 `tests/test_schema_validation.py` 中测试缺少必填字段、类型错误、值越界等场景
    - _需求: 9.4, 9.5_

- [ ] 10. 实现 Skill 基类
  - [x] 10.1 实现 `src/skills/base.py`
    - 实现 `BaseSkill` 类：`execute()` 标准流程（加载输入→Schema 校验→执行业务逻辑→Schema 校验输出→存储状态）
    - 集成 State_Store 进行状态读写
    - 集成 jsonschema 进行输入/输出校验
    - 实现执行前后日志记录（含 state_id、执行耗时、成功/失败状态）
    - 定义 `SchemaValidationError` 异常
    - _需求: 6.6, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [x] 10.2 编写属性测试：执行日志完整性
    - **Property 18: 执行日志完整性**
    - 在 `tests/test_properties.py` 中实现 `test_execution_log_completeness`
    - 验证每次 Skill 执行前后各记录一条日志，包含必要字段
    - **验证需求: 6.6**

- [ ] 11. 实现 Skill-1 信息收集与候选筛选
  - [x] 11.1 实现 `src/skills/skill1_collect.py`
    - 继承 `BaseSkill`，实现 `run()` 方法
    - 调用 OpenClaw websearch 技能检索市场热点
    - 调用 OpenClaw xurl 技能抓取结构化数据
    - 为每条数据标注 `source_url` 和 `collected_at`
    - 实现防幻觉约束：仅输出经来源验证的真实数据
    - 实现重试逻辑：失败后 60 秒重试，最多 3 次
    - _需求: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x] 11.2 编写属性测试：数据来源标注完整性
    - **Property 4: 数据来源标注完整性**
    - 在 `tests/test_properties.py` 中实现 `test_data_source_annotation`
    - 验证每条候选币种记录包含合法 URI 和 ISO 8601 时间戳
    - **验证需求: 1.3**

- [ ] 12. 实现 Skill-2 深度分析与评级
  - [x] 12.1 实现 TradingAgents_Module 封装 `src/skills/skill2_analyze.py` 中的 `TradingAgentsModule`
    - 实现 `analyze()` 方法，30 秒超时控制
    - _需求: 2.2, 2.6, 2.7_

  - [x] 12.2 实现 `src/skills/skill2_analyze.py` 的 Skill-2 主逻辑
    - 继承 `BaseSkill`，实现 `run()` 方法
    - 从 State_Store 读取候选币种列表
    - 对每个候选币种调用 TradingAgentsModule 分析
    - 过滤评级分低于阈值（默认 6 分）的币种
    - 输出结构化评级结果
    - _需求: 2.1, 2.3, 2.4, 2.5_

  - [x] 12.3 编写属性测试：评级过滤阈值不变量
    - **Property 5: 评级过滤阈值不变量**
    - 在 `tests/test_properties.py` 中实现 `test_rating_filter_threshold`
    - 验证输出列表中所有评级分 ≥ 阈值，被过滤数量正确
    - **验证需求: 2.4**

- [ ] 13. 实现 Skill-3 交易策略制定
  - [x] 13.1 实现 `src/skills/skill3_strategy.py`
    - 继承 `BaseSkill`，实现 `run()` 方法
    - 实现 `calculate_position_size()` 固定风险模型头寸规模计算
    - 为每个目标币种生成交易计划（方向、入场区间、头寸规模、止损、止盈、持仓上限）
    - 执行风控预校验并自动裁剪超限头寸
    - 处理空评级列表场景（标记"本轮无交易机会"）
    - _需求: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [x] 13.2 编写属性测试：头寸规模计算正确性
    - **Property 6: 头寸规模计算正确性**
    - 在 `tests/test_properties.py` 中实现 `test_position_size_calculation`
    - 验证计算公式正确且头寸价值不超过 20%
    - **验证需求: 3.2, 3.5**

- [ ] 14. 实现 Skill-4 自动交易执行
  - [x] 14.1 实现 `src/skills/skill4_execute.py`
    - 继承 `BaseSkill`，实现 `run()` 方法
    - 从 State_Store 读取交易计划
    - 对每笔交易调用 Risk_Controller 校验
    - 通过 Binance_Fapi_Client 提交限价订单
    - 实现 30 秒轮询持仓监控（止损/止盈/超时平仓）
    - 实现日亏损检查与 Paper Mode 降级
    - 记录执行结果至 State_Store
    - _需求: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.11, 4.12, 4.13_

  - [x] 14.2 编写属性测试：平仓条件触发
    - **Property 10: 平仓条件触发**
    - 在 `tests/test_properties.py` 中实现 `test_close_position_trigger`
    - 验证止损/止盈/超时三种平仓条件正确触发
    - **验证需求: 4.5, 4.6, 4.7**

  - [x] 14.3 编写属性测试：Paper Mode 行为一致性
    - **Property 14: Paper Mode 行为一致性**
    - 在 `tests/test_properties.py` 中实现 `test_paper_mode_behavior`
    - 验证 Paper Mode 下所有订单状态为 paper_trade
    - **验证需求: 8.6, 8.7**

- [ ] 15. 实现 Skill-5 展示与自我进化
  - [x] 15.1 实现 `src/skills/skill5_evolve.py`
    - 继承 `BaseSkill`，实现 `run()` 方法
    - 从 State_Store 读取账户状态和持仓信息
    - 输出格式化 Markdown 表格展示
    - 提取平仓交易数据存入 Memory_Store
    - 计算策略胜率和平均盈亏比
    - 实现 `compute_evolution_adjustment()` 策略调优逻辑
    - 基于反思日志调整 Skill-2 评级阈值和 Skill-3 风险比例
    - 处理交易记录不足 10 笔的场景
    - _需求: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [x] 16. 检查点 - 确保所有 Skill 测试通过
  - 确保所有测试通过，如有问题请向用户确认。

- [ ] 17. 实现 Pipeline 编排与启动脚本
  - [x] 17.1 实现 `src/agent.py` Pipeline 编排
    - 实现 Pipeline 主循环：Skill-1 → Skill-2 → Skill-3 → Skill-4 → Skill-5 严格顺序执行
    - 通过 state_id 串联各 Skill
    - 处理 Skill-3 输出"无交易机会"时跳过 Skill-4 直接进入 Skill-5
    - 实现 Pipeline 崩溃恢复：从最后成功 state_id 恢复
    - _需求: 6.3, 6.4, 6.5_

  - [x] 17.2 编写属性测试：Pipeline 执行顺序不变量
    - **Property 17: Pipeline 执行顺序不变量**
    - 在 `tests/test_properties.py` 中实现 `test_pipeline_execution_order`
    - 验证 Skill 执行时间戳严格递增
    - **验证需求: 6.4**

  - [x] 17.3 实现启动脚本
    - 创建 `scripts/run_pipeline.py`：启动正常 Pipeline
    - 创建 `scripts/run_paper_mode.py`：启动模拟盘模式
    - 创建 `config/default.yaml`：默认配置文件（风控阈值、API 参数、评级阈值等）
    - _需求: 8.6_

- [ ] 18. 集成测试与最终验证
  - [x] 18.1 编写集成测试
    - 在 `tests/test_skills.py` 中编写 Skill 链路集成测试
    - 使用 mock 模拟 Binance API 和 TradingAgents 调用
    - 测试完整 Pipeline 流程（含正常路径和降级路径）
    - _需求: 6.4, 9.6_

  - [x] 18.2 补充网络恢复同步逻辑
    - 在 `src/infra/binance_fapi.py` 中实现网络恢复后自动重新同步账户持仓和未完成订单
    - _需求: 7.7_

- [x] 19. 最终检查点 - 确保所有测试通过
  - 确保所有测试通过，如有问题请向用户确认。

## 备注

- 标记 `*` 的子任务为可选测试任务，可跳过以加速 MVP 开发
- 每个任务引用了对应的需求编号以确保可追溯性
- 属性测试验证设计文档中定义的 19 个正确性属性
- 检查点任务用于阶段性验证，确保增量开发的正确性
- 所有属性测试使用 hypothesis 库，每个属性至少运行 100 次迭代
