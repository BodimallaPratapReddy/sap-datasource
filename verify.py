from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.cerebras import Cerebras

load_dotenv()

agent = Agent(
    model=Cerebras(id="gpt-oss-120b",
                   api_key="csk-jve64wdd69hdwnmrx439828rd3yx8phewf4y6kcnfdpjwp9x"),
    markdown=True,
)

if __name__ == "__main__":
    agent.print_response("What is 237 multiplied by 19?")