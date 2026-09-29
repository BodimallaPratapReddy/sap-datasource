
from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.openrouter import OpenRouter

load_dotenv()

agent = Agent(
    model=OpenRouter(id="openai/gpt-5-mini"),
    markdown=True,
)

if __name__ == "__main__":
    agent.print_response(
        "What is 237 multiplied by 19?",
        markdown=True,
    )