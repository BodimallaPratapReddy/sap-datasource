
from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.cerebras import Cerebras

load_dotenv()

agent = Agent(
    model=Cerebras(id="gpt-oss-120b"),
    markdown=True,
)

if __name__ == "__main__":
    agent.print_response(
        "What is 237 multiplied by 19?",
        markdown=True,
    )