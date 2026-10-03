"""End-to-end proof over real MCP stdio transport: spawns `python -m wooconnector serve`, lists tools, calls some."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mockstore.server import start


async def main():
    srv, S = start()
    env = {**os.environ, "WOO_BASE_URL": f"http://127.0.0.1:{srv.server_address[1]}", "WOO_CONSUMER_KEY": "ck_demo_readonly_0001",
           "WOO_CONSUMER_SECRET": "cs_demo_secret_0001", "PYTHONPATH": os.getcwd()}
    params = StdioServerParameters(command=sys.executable, args=["-m", "wooconnector", "serve"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"[MCP] {len(tools)} tools:", ", ".join(t.name for t in tools))
            for name, args in [("woo_search_orders", {"query": "#1010"}), ("woo_stockout_forecast", {"horizon_days": 10}), ("woo_get_order", {"id": 424242})]:
                res = await s.call_tool(name, args)
                print(f"[MCP] {name} ->", res.content[0].text[:160], "...")
    print("[MCP] OK")

asyncio.run(main())
