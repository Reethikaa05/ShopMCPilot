"""MCP server (stdio). Register in Agent Studio / Claude Desktop / any MCP client as a command:
    python -m wooconnector serve
"""
from __future__ import annotations
import asyncio, json, os
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from . import auth
from .client import WooClient
from .tools import build_tools, call_tool


def make_server(client: WooClient, reveal_pii: bool = False) -> Server:
    tools = build_tools(client, reveal_pii)
    server = Server("woocommerce-agent-connector")

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [types.Tool(name=s["name"], description=s["description"], inputSchema=s["inputSchema"],
                           annotations=types.ToolAnnotations(**s["annotations"])) for s in (t.spec() for t in tools.values())]

    @server.call_tool()
    async def _call(name: str, arguments: dict) -> list[types.TextContent]:
        # blocking HTTP work runs in a thread so the MCP loop stays responsive
        out = await asyncio.to_thread(call_tool, tools, client, name, arguments)
        return [types.TextContent(type="text", text=json.dumps(out, default=str))]

    return server


async def serve():
    creds = auth.load()
    client = WooClient(creds, allow_insecure=os.environ.get("WOO_ALLOW_INSECURE") == "1")
    server = make_server(client, reveal_pii=os.environ.get("WOO_REVEAL_PII") == "1")
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())
