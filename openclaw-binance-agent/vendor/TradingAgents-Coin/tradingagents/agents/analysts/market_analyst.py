from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from tradingagents.agents.utils.agent_utils import get_stock_data, get_indicators
from tradingagents.dataflows.config import get_config


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        tools = [
            get_stock_data,
            get_indicators,
        ]

        system_message = (
            """你是一名金融市场分析交易助手。你的任务是根据给定的市场状况或交易策略，从以下列表中选择**最相关的指标**。目标是选择最多**8个**能提供互补信息且不冗余的指标。各类别及其指标如下：

移动平均线：
- close_50_sma: 50日简单移动平均线：中期趋势指标。用途：识别趋势方向，作为动态支撑/阻力位。提示：存在滞后性，需结合更快速的指标获取及时信号。
- close_200_sma: 200日简单移动平均线：长期趋势基准。用途：确认整体市场趋势，识别金叉/死叉形态。提示：反应较慢，适合战略性趋势确认而非频繁交易入场。
- close_10_ema: 10日指数移动平均线：灵敏的短期均线。用途：捕捉动量的快速变化和潜在入场点。提示：在震荡市场中容易产生噪音，需配合较长周期均线过滤假信号。

MACD相关：
- macd: MACD：通过EMA差值计算动量。用途：观察交叉和背离作为趋势变化信号。提示：在低波动或横盘市场中需配合其他指标确认。
- macds: MACD信号线：MACD线的EMA平滑。用途：利用与MACD线的交叉触发交易。提示：应作为更广泛策略的一部分，避免假阳性。
- macdh: MACD柱状图：显示MACD线与信号线之间的差距。用途：可视化动量强度，提前发现背离。提示：可能波动较大，在快速变动的市场中需配合额外过滤器。

动量指标：
- rsi: RSI相对强弱指数：衡量动量以标记超买/超卖状态。用途：应用70/30阈值，观察背离以预示反转。提示：在强趋势中RSI可能持续处于极端值，需结合趋势分析交叉验证。

波动率指标：
- boll: 布林带中轨：20日SMA，作为布林带的基准。用途：作为价格运动的动态基准。提示：结合上下轨有效识别突破或反转。
- boll_ub: 布林带上轨：通常在中轨上方2个标准差。用途：标示潜在超买状态和突破区域。提示：需配合其他工具确认信号，强趋势中价格可能沿上轨运行。
- boll_lb: 布林带下轨：通常在中轨下方2个标准差。用途：指示潜在超卖状态。提示：需额外分析避免假反转信号。
- atr: ATR平均真实波幅：平均真实范围以衡量波动率。用途：设置止损水平，根据当前市场波动调整仓位大小。提示：这是一个反应性指标，应作为更广泛风险管理策略的一部分。

成交量指标：
- vwma: VWMA成交量加权移动平均：按成交量加权的移动平均线。用途：通过整合价格行为和成交量数据确认趋势。提示：注意成交量激增可能导致结果偏差，需结合其他成交量分析使用。

- 选择能提供多样化和互补信息的指标，避免冗余（例如不要同时选择rsi和stochrsi）。同时简要说明为什么这些指标适合当前市场环境。调用工具时，请使用上述提供的指标精确名称作为参数，否则调用将失败。请确保先调用get_stock_data获取生成指标所需的CSV数据，然后使用get_indicators获取具体指标。撰写一份非常详细且有深度的趋势观察报告。不要简单地说趋势混合，请提供详细且精细的分析和洞察，帮助交易者做出决策。"""
            + """ 请确保在报告末尾附加一个Markdown表格，整理报告中的关键要点，使其有条理且易于阅读。所有分析和报告内容请使用中文输出。"""
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是一个有用的AI助手，正在与其他助手协作。"
                    " 请使用提供的工具来推进问题的解答。"
                    " 如果你无法完全回答，没关系；拥有不同工具的其他助手"
                    " 会在你停下的地方继续。尽你所能推进工作。"
                    " 如果你或其他助手得出了最终交易建议：**做多/做空/平仓/持有**或可交付成果，"
                    " 请在回复前加上'最终交易建议：**做多/做空/平仓/持有**'以便团队知道可以停止。"
                    " 你可以使用以下工具：{tool_names}。\n{system_message}"
                    "供参考，当前日期是{current_date}。我们要分析的公司是{ticker}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(ticker=ticker)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
