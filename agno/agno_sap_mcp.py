import asyncio
from os import environ

from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.openai.like import OpenAILike
from agno.tools.mcp import MCPTools


load_dotenv()


async def main():
    mcp_tools = MCPTools(
        url=environ["SAP_MCP_URL"],
        transport="streamable-http",
        headers={
            "Authorization": f"Bearer {environ['SAP_MCP_TOKEN']}"
        },
        timeout_seconds=120,
    )

    await mcp_tools.connect()

    try:
        agent = Agent(
            model=OpenAILike(
                id=environ["MANTLE_MODEL_ID"],
                api_key=environ["MANTLE_API_KEY"],
                base_url=environ["MANTLE_BASE_URL"],
            ),
            tools=[mcp_tools],
            markdown=True,
            instructions="""
You are a helpful SAP ABAP assistant.

You have access to the SAP system D05-DEV through MCP.

When answering questions about the SAP system:
1. Use MCP tools rather than guessing.
2. Prefer read-only tools.
3. Search SAP repository objects when necessary.
4. Read ABAP source when necessary.
5. Do not modify SAP objects.
6. Clearly distinguish information retrieved from SAP from general knowledge.
""",
        )

        await agent.aprint_response(
            "Explain the logic of this Function Module BWFID_GET_FIGL_ITEM and Tables used to extract data and list which tables i need to replicate to Fabric if i have extract the data of this function module ",
            stream=True,
        )

    finally:
        await mcp_tools.close()


if __name__ == "__main__":
    asyncio.run(main())