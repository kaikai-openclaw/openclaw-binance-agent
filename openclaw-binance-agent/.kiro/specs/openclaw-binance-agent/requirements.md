# 需求文档

## 简介

本文档定义基于 OpenClaw 框架的加密货币自动化交易 Agent 系统需求。该系统采用 5 步流水线 Skill 架构（信息收集→深度分析→策略制定→自动执行→展示进化），集成 TradingAgents 开源分析子模块，对接 Binance U本位合约（fapi）接口，并内置硬编码风控机制与长期记忆自我进化能力，形成高胜率、高可用的自动化交易闭环。

## 术语表

- **Agent**: 基于 OpenClaw 框架运行的加密货币自动化交易智能体，负责协调全部 Skill 的执行
- **Skill**: OpenClaw 框架中的独立功能模块，具备标准化 JSON Schema 输入输出，支持串联执行与单独调用
- **Pipeline（流水线）**: 由 Skill-1 至 Skill-5 按顺序组成的单向数据流处理管道
- **TradingAgents_Module**: 引入的开源分析子模块（https://github.com/TauricResearch/TradingAgents），负责对候选币种进行专业评级
- **Binance_Fapi_Client**: 封装 Binance U本位合约 fapi 接口的客户端模块
- **Risk_Controller**: 硬编码风控拦截层，贯穿全流程对交易指令进行物理隔离校验
- **State_Store**: 基于状态 ID 的轻量化数据存储，各 Skill 间通过状态 ID 引用数据而非传递全量 JSON
- **Memory_Store**: Agent 长期记忆库，存储历史交易归因数据用于策略自我进化
- **Paper_Trading_Mode**: 模拟盘模式，在风控降级后继续收集市场数据以验证和优化策略
- **Rate_Limiter**: API 请求限流队列缓冲模块，控制请求频率不超过 Binance 限制

## 需求

### 需求 1：市场信息收集与候选币种筛选（Skill-1）

**用户故事：** 作为交易 Agent 的运营者，我希望系统能自动从真实市场渠道收集信息并筛选出主流货币和热点货币，以便为后续分析提供可靠的候选币种列表。

#### 验收标准

1. WHEN Pipeline 触发 Skill-1 执行时，THE Agent SHALL 调用 OpenClaw 框架的 websearch 技能检索当前加密货币市场热点信息
2. WHEN websearch 返回搜索结果后，THE Agent SHALL 调用 OpenClaw 框架的 xurl 技能抓取目标页面的结构化数据
3. THE Agent SHALL 对每条采集到的市场信息标注数据来源 URL 和采集时间戳
4. THE Agent SHALL 仅输出经过来源验证的真实市场数据，禁止生成任何未经外部数据源确认的信息（防幻觉约束）
5. WHEN 信息收集完成后，THE Agent SHALL 输出符合 JSON Schema（draft-07）规范的标准化候选币种列表，包含币种符号、市场热度评分和数据来源字段
6. THE Agent SHALL 将候选币种列表存入 State_Store 并生成唯一状态 ID，仅将该状态 ID 传递给下游 Skill
7. IF websearch 或 xurl 调用失败，THEN THE Agent SHALL 记录错误日志并在 60 秒后进行重试，最多重试 3 次
8. IF 重试 3 次后仍失败，THEN THE Agent SHALL 将 Skill-1 状态标记为失败并发出告警通知，阻止下游 Skill 执行

### 需求 2：深度分析与评级（Skill-2）

**用户故事：** 作为交易 Agent 的运营者，我希望系统能对候选币种进行专业的多维度分析和评级，以便识别出具有交易价值的目标币种。

#### 验收标准

1. WHEN Skill-2 接收到 Skill-1 输出的状态 ID 时，THE Agent SHALL 从 State_Store 中读取对应的候选币种列表
2. THE Agent SHALL 调用 TradingAgents_Module 对每个候选币种执行独立的深度分析
3. WHEN TradingAgents_Module 返回分析结果后，THE Agent SHALL 输出符合 JSON Schema（draft-07）规范的结构化评级结果，每条记录仅包含币种符号、1-10 评级分、多空或观望信号、置信度百分比四个核心字段
4. THE Agent SHALL 过滤掉评级分低于 6 分的币种，仅保留具有交易价值的目标币种
5. THE Agent SHALL 将评级结果存入 State_Store 并生成唯一状态 ID，仅将该状态 ID 传递给下游 Skill
6. IF TradingAgents_Module 调用超时超过 30 秒，THEN THE Agent SHALL 终止该币种的分析并在评级结果中标记为"分析超时"
7. IF TradingAgents_Module 对某币种返回错误，THEN THE Agent SHALL 跳过该币种并记录错误日志，继续处理剩余币种

### 需求 3：交易策略制定（Skill-3）

**用户故事：** 作为交易 Agent 的运营者，我希望系统能基于评级结果自动生成精确的量化交易计划，以便为自动执行提供明确的入场、止损、止盈参数。

#### 验收标准

1. WHEN Skill-3 接收到 Skill-2 输出的状态 ID 时，THE Agent SHALL 从 State_Store 中读取对应的评级结果
2. THE Agent SHALL 基于固定风险模型为每个目标币种计算头寸规模百分比，公式为：头寸规模 = (账户风险比例 × 账户总资金) / (入场价格 - 止损价格)
3. THE Agent SHALL 为每个目标币种输出包含以下字段的交易计划：币种符号、交易方向（做多或做空）、入场价格区间上限、入场价格区间下限、头寸规模百分比、止损价格、止盈价格、持仓时间上限（小时）
4. THE Agent SHALL 对每个交易计划执行风控预校验：单笔保证金不超过总资金的 20%，单币累计持仓不超过总资金的 30%
5. IF 交易计划的头寸规模超过风控预校验阈值，THEN THE Agent SHALL 自动缩减头寸规模至合规范围内并记录调整日志
6. THE Agent SHALL 将交易计划存入 State_Store 并生成唯一状态 ID，仅将该状态 ID 传递给下游 Skill
7. IF 当前无目标币种通过评级筛选，THEN THE Agent SHALL 输出空交易计划并将 Pipeline 状态标记为"本轮无交易机会"
8. THE Agent SHALL 对所有数值参数执行强类型校验和极值边界检查，拒绝非正数的价格和非正数的头寸规模

### 需求 4：自动交易执行与风控（Skill-4）

**用户故事：** 作为交易 Agent 的运营者，我希望系统能自动在 Binance U本位合约市场执行交易计划并实施严格的实时风控，以便在捕获交易机会的同时保护资金安全。

#### 验收标准

1. WHEN Skill-4 接收到 Skill-3 输出的状态 ID 时，THE Agent SHALL 从 State_Store 中读取对应的交易计划
2. THE Binance_Fapi_Client SHALL 在下单前对每笔交易执行以下硬编码风控断言：单笔保证金不超过总资金的 20%、单币累计持仓不超过总资金的 30%、止损触发后的同币种同方向订单被拒绝（禁逆势补仓）
3. WHEN 风控断言全部通过后，THE Binance_Fapi_Client SHALL 通过 Binance fapi 接口提交限价订单
4. WHILE 存在未平仓持仓时，THE Agent SHALL 每 30 秒轮询一次 Binance fapi 接口获取持仓状态和未实现盈亏
5. WHEN 持仓的未实现亏损触及止损价格时，THE Binance_Fapi_Client SHALL 立即提交市价平仓订单
6. WHEN 持仓的未实现盈利触及止盈价格时，THE Binance_Fapi_Client SHALL 立即提交市价平仓订单
7. WHEN 持仓时间超过交易计划中定义的持仓时间上限时，THE Binance_Fapi_Client SHALL 立即提交市价平仓订单
8. THE Rate_Limiter SHALL 将所有 Binance fapi 请求纳入队列缓冲，确保请求频率不超过每分钟 1000 次
9. IF Binance fapi 请求因网络超时或断线失败，THEN THE Binance_Fapi_Client SHALL 采用指数退避策略重试，初始间隔 1 秒，最大间隔 60 秒，最多重试 5 次
10. IF 重试 5 次后仍失败，THEN THE Agent SHALL 将该订单标记为"执行失败"并发出告警通知
11. WHEN 当日累计已实现亏损达到总资金的 5% 时，THE Risk_Controller SHALL 立即停止所有实盘下单操作并发出告警通知
12. WHEN Risk_Controller 触发日亏损 5% 阈值后，THE Agent SHALL 自动切换至 Paper_Trading_Mode，继续执行 Pipeline 流程但所有订单仅在模拟环境中记录，不提交至 Binance fapi
13. THE Agent SHALL 将每笔订单的执行结果（订单 ID、成交价格、成交数量、手续费、时间戳）存入 State_Store 并生成唯一状态 ID

### 需求 5：状态展示与自我进化（Skill-5）

**用户故事：** 作为交易 Agent 的运营者，我希望系统能定时汇总展示账户状态，并基于历史交易数据自动优化策略参数，以便持续提升交易胜率。

#### 验收标准

1. WHEN Skill-5 被定时触发或 Pipeline 完成一轮执行后，THE Agent SHALL 从 State_Store 中读取当前账户余额、未平仓持仓列表和当日已实现盈亏
2. THE Agent SHALL 输出格式化的 Markdown 表格展示以下信息：账户总资金、可用保证金、未实现盈亏、当日已实现盈亏、各持仓币种的方向和数量和入场价格和当前价格和盈亏比例
3. WHEN 某笔持仓被平仓后，THE Agent SHALL 提取该笔交易的核心数据（币种、方向、入场价格、平仓价格、盈亏金额、持仓时长、评级分、策略参数）存入 Memory_Store
4. THE Agent SHALL 基于 Memory_Store 中最近 50 笔交易记录计算策略胜率和平均盈亏比
5. WHEN 策略胜率低于 40% 时，THE Agent SHALL 生成策略调优建议并记录至 Memory_Store 的反思日志中
6. THE Agent SHALL 基于反思日志中的调优建议自动调整 Skill-2 的评级过滤阈值和 Skill-3 的风险比例参数
7. IF Memory_Store 中的交易记录不足 10 笔，THEN THE Agent SHALL 跳过自我进化计算并使用默认策略参数

### 需求 6：全局状态管理与上下文传递

**用户故事：** 作为交易 Agent 的运营者，我希望各 Skill 之间通过轻量化状态 ID 传递数据，以便避免上下文窗口膨胀并支持故障恢复。

#### 验收标准

1. THE State_Store SHALL 为每次 Skill 输出生成全局唯一的状态 ID（UUID v4 格式）
2. THE State_Store SHALL 持久化存储每个状态 ID 对应的完整 JSON 数据快照
3. WHEN 任意 Skill 需要读取上游数据时，THE Skill SHALL 仅通过状态 ID 从 State_Store 中检索，禁止在 Skill 间直接传递全量 JSON 数据
4. THE Pipeline SHALL 按照 Skill-1 → Skill-2 → Skill-3 → Skill-4 → Skill-5 的严格顺序执行，前置 Skill 未输出完成时禁止触发后置 Skill
5. IF Pipeline 在某个 Skill 执行过程中崩溃，THEN THE Agent SHALL 从最后一次成功的状态 ID 快照恢复执行进程
6. THE Agent SHALL 在每个 Skill 执行前后记录带时间戳的执行日志，包含状态 ID、执行耗时和成功或失败状态

### 需求 7：API 限流与网络容错

**用户故事：** 作为交易 Agent 的运营者，我希望系统具备完善的 API 限流和网络容错能力，以便在高频调用和网络不稳定场景下保持系统可用性。

#### 验收标准

1. THE Rate_Limiter SHALL 维护一个请求队列，确保所有 Binance fapi 请求的发送频率不超过每分钟 1000 次
2. WHEN 请求队列中的待发送请求数量超过 800 时，THE Rate_Limiter SHALL 自动降低请求发送速率至每分钟 500 次
3. IF Binance fapi 返回 HTTP 429（请求过多）响应，THEN THE Rate_Limiter SHALL 暂停所有请求发送 30 秒后恢复
4. IF Binance fapi 返回 HTTP 418（IP 被封禁）响应，THEN THE Agent SHALL 立即停止所有 API 调用并发出紧急告警
5. THE Binance_Fapi_Client SHALL 对每次 API 请求设置 10 秒超时限制
6. IF API 请求超时，THEN THE Binance_Fapi_Client SHALL 采用指数退避策略重试，退避序列为 1 秒、2 秒、4 秒、8 秒、16 秒
7. THE Agent SHALL 在网络恢复后自动重新同步账户持仓状态和未完成订单状态

### 需求 8：硬编码风控体系

**用户故事：** 作为交易 Agent 的运营者，我希望系统内置不可绕过的硬编码风控规则，以便在任何情况下都能保护资金安全。

#### 验收标准

1. THE Risk_Controller SHALL 在每笔订单提交前执行以下断言校验：单笔保证金占总资金比例不超过 20%
2. THE Risk_Controller SHALL 在每笔订单提交前执行以下断言校验：单币种累计持仓占总资金比例不超过 30%
3. WHEN 某币种触发止损平仓后，THE Risk_Controller SHALL 在 24 小时内拒绝该币种同方向的新开仓订单（禁逆势补仓）
4. THE Risk_Controller SHALL 每 30 秒计算一次当日累计已实现亏损占总资金的比例
5. WHEN 当日累计已实现亏损达到总资金的 5% 时，THE Risk_Controller SHALL 执行以下降级流程：立即取消所有未成交的挂单、停止所有实盘下单操作、发出告警通知、将系统切换至 Paper_Trading_Mode
6. WHILE 系统处于 Paper_Trading_Mode 时，THE Agent SHALL 继续执行完整的 Pipeline 流程，但所有订单仅在本地模拟环境中记录和追踪
7. WHILE 系统处于 Paper_Trading_Mode 时，THE Agent SHALL 在每次展示中明确标注当前为模拟盘状态
8. IF 任何风控断言校验失败，THEN THE Risk_Controller SHALL 拒绝该笔订单并记录包含拒绝原因的详细日志
9. THE Risk_Controller SHALL 作为独立拦截层运行，任何交易指令在到达 Binance_Fapi_Client 之前都必须通过 Risk_Controller 的校验

### 需求 9：Skill 输出 JSON Schema 规范

**用户故事：** 作为交易 Agent 的开发者，我希望每个 Skill 的输入输出都遵循严格的 JSON Schema 规范，以便实现模块间的标准化对接和独立测试。

#### 验收标准

1. THE Agent SHALL 为每个 Skill 定义符合 JSON Schema draft-07 规范的输入 Schema 和输出 Schema
2. THE Agent SHALL 在每个 Skill 执行前使用输入 Schema 校验输入数据的合法性
3. THE Agent SHALL 在每个 Skill 执行后使用输出 Schema 校验输出数据的合法性
4. IF 输入数据未通过 Schema 校验，THEN THE Agent SHALL 拒绝执行该 Skill 并返回包含校验错误详情的错误响应
5. IF 输出数据未通过 Schema 校验，THEN THE Agent SHALL 将该 Skill 执行标记为失败并记录 Schema 校验错误日志
6. THE Agent SHALL 支持对任意单个 Skill 进行独立调用测试，输入符合 Schema 的测试数据即可获得符合 Schema 的输出结果
