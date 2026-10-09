import asyncio

from mcp import Client, StdioServerParameters


async def main():
    server_params = StdioServerParameters(
        command="mcp",
        args=["run", "src/server.py"]
    )

    async with Client(server_params) as client:

        # -------------------------------------------------
        # 1. List MCP tools
        # -------------------------------------------------
        tools = await client.list_tools()

        print("\nAvailable MCP tools:")
        for tool in tools.tools:
            print(f"- {tool.name}")

        # -------------------------------------------------
        # 2. List MCP resources
        # -------------------------------------------------
        resources = await client.list_resources()

        print("\nAvailable MCP resources:")
        for resource in resources.resources:
            print(f"- {resource.uri}")

        # -------------------------------------------------
        # 3. Read the pandal resource
        # -------------------------------------------------
        resource_result = await client.read_resource(
            "puja://2026/pandals"
        )

        print("\nPandal resource:")

        for content in resource_result.contents:
            if hasattr(content, "text"):
                print(content.text[:1000])

        # -------------------------------------------------
        # 4. Test restaurant availability
        # -------------------------------------------------
        result = await client.call_tool(
            "get_restaurant_availability",
            {
                "restaurant_id": "R020",
                "datetime_text": "2026-09-22 22:30"
            }
        )

        print("\nRestaurant availability:")

        if result.is_error:
            print(result.content)
        else:
            print(result.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())