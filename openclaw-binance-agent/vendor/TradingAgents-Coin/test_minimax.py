"""Quick smoke test for MiniMax provider integration."""

from dotenv import load_dotenv
load_dotenv()

from tradingagents.llm_clients import create_llm_client

client = create_llm_client(
    provider="minimax",
    model="MiniMax-M2.5-highspeed",
)

llm = client.get_llm()
response = llm.invoke("你好，请用一句话介绍你自己。")
print(f"Model: MiniMax-M2.5-highspeed")
print(f"Response: {response.content}")
