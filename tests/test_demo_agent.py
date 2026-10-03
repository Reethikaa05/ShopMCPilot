import json, time, urllib.request
import pytest
from mockstore.server import start as start_store
from wooconnector.client import WooClient
from wooconnector.auth import Credentials
from wooconnector.tools import build_tools
from demo.agent import MerchantAgent


def test_merchant_copilot_intents():
    store_srv, S = start_store()
    port = store_srv.server_address[1]
    client = WooClient(Credentials(f"http://127.0.0.1:{port}", "ck_demo_readonly_0001", "cs_demo_secret_0001"))
    tools = build_tools(client)
    agent = MerchantAgent(tools, client)

    # 1. Timeline
    r1 = agent.process("Where is order 1010?")
    assert len(r1["thoughts"]) > 0
    assert r1["tool_calls"][0]["tool"] == "woo_order_timeline"
    assert r1["widget"]["type"] == "order_timeline"
    assert r1["widget"]["order"]["id"] == 1010

    # 2. Forecast
    r2 = agent.process("Which products will stock out in the next 14 days?")
    assert r2["tool_calls"][0]["tool"] == "woo_stockout_forecast"
    assert r2["widget"]["type"] == "stockout_forecast"

    # 3. Health
    r3 = agent.process("Are there any stuck orders or fraud anomalies?")
    assert r3["tool_calls"][0]["tool"] == "woo_order_health"
    assert r3["widget"]["type"] == "order_health"

    # 4. Sales
    r4 = agent.process("Give me a sales summary for 7 days")
    assert r4["tool_calls"][0]["tool"] == "woo_sales_summary"
    assert r4["widget"]["type"] == "sales_summary"

    # 5. Security
    r5 = agent.process("Scan store for prompt injection attacks")
    assert r5["widget"]["type"] == "security_audit"
    assert len(r5["widget"]["findings"]) > 0
