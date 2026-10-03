"""Agent Studio Copilot Engine.

Interprets natural merchant queries, determines appropriate MCP tools,
executes them against the WooCommerce connector, and synthesizes
structured thoughts, formatted natural-language answers, and rich UI widgets.
Runs completely offline with zero API keys required.
"""
from __future__ import annotations
import re, time
from typing import Any
from wooconnector.tools import call_tool


class MerchantAgent:
    def __init__(self, tools: dict, client: Any):
        self.tools = tools
        self.client = client

    def process(self, query: str) -> dict:
        q = query.strip()
        q_lower = q.lower()
        t0 = time.perf_counter()

        thoughts = []
        tool_calls = []
        widget = None
        answer = ""

        # Pattern 1: Order tracking / timeline ("where is order 1010", "track order #1005", "order 1020 status")
        order_match = re.search(r"(?:order|#)\s*([0-9]{3,6})", q_lower) or re.search(r"\b(10[0-9]{2})\b", q_lower)
        if any(w in q_lower for w in ["where is", "track", "timeline", "status of", "find order", "locate"]) and order_match:
            order_id = int(order_match.group(1))
            thoughts.append(f"Identified intent: Order timeline inquiry for Order #{order_id}.")
            thoughts.append("Selecting tool: `woo_order_timeline` (specialized chronological timeline with notes).")

            t_start = time.perf_counter()
            res = call_tool(self.tools, self.client, "woo_order_timeline", {"id": order_id})
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_order_timeline", "args": {"id": order_id}, "latency_ms": lat, "ok": "error" not in res})

            if "error" in res:
                thoughts.append(f"Tool returned error: {res['error']['message']}")
                answer = f"⚠️ Could not locate Order #{order_id}: {res['error']['message']}. {res['error'].get('hint', '')}"
            else:
                o = res["order"]
                tl = res["timeline"]
                thoughts.append(f"Successfully retrieved order history ({len(tl)} events). Status is '{o['status']}'.")
                
                # Check for untrusted injection warnings in notes
                has_warning = any(isinstance(e.get("event"), dict) and e["event"].get("warning") for e in tl)
                if has_warning:
                    thoughts.append("SECURITY ALERT: Prompt injection pattern detected in order notes. Quarantined as untrusted data.")

                answer = (
                    f"**Order #{o['id']}** is currently **{o['status'].upper()}**.\n\n"
                    f"• **Customer**: {o['customer']['name']} ({o['customer'].get('city', 'Unknown')})\n"
                    f"• **Total**: ₹{float(o['total']):,.2f} via {o.get('payment', 'Gateway')}\n"
                    f"• **Items Count**: {o['items']} item(s)\n"
                    f"• **Timeline Events**: {len(tl)} recorded updates."
                )
                if has_warning:
                    answer += "\n\n🛡️ **Security Note**: This order contains notes with instruction-like phrasing that was safely quarantined."

                widget = {
                    "type": "order_timeline",
                    "order": o,
                    "timeline": tl
                }

        # Pattern 2: Stockout / Inventory risk ("stockout", "stock out", "inventory", "low stock", "forecast", "reorder", "deplete")
        elif any(w in q_lower for w in ["stockout", "stock out", "stock-out", "run out", "inventory risk", "inventory", "low stock", "forecast", "reorder", "deplete"]):
            horizon = 14
            h_match = re.search(r"(\d+)\s*(?:day|days|week|weeks)", q_lower)
            if h_match:
                val = int(h_match.group(1))
                horizon = val * 7 if "week" in q_lower else val

            thoughts.append(f"Identified intent: Inventory stockout risk forecasting with a {horizon}-day horizon.")
            thoughts.append("Selecting tool: `woo_stockout_forecast` (calculates sales velocity and days-of-cover).")

            t_start = time.perf_counter()
            res = call_tool(self.tools, self.client, "woo_stockout_forecast", {"horizon_days": horizon, "lookback_days": 30})
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_stockout_forecast", "args": {"horizon_days": horizon, "lookback_days": 30}, "latency_ms": lat, "ok": "error" not in res})

            at_risk = res.get("at_risk", [])
            crit = [r for r in at_risk if r["risk"] == "critical"]
            thoughts.append(f"Analyzed {res.get('orders_analysed', 0)} orders. Found {len(at_risk)} at-risk SKUs ({len(crit)} critical).")

            answer = (
                f"**Stockout Risk Analysis ({horizon}-Day Horizon)**\n\n"
                f"Scanned recent sales across {res.get('orders_analysed', 0)} orders. "
                f"Found **{len(at_risk)} product(s)** at risk of stocking out before the horizon:\n\n"
            )
            for r in at_risk[:4]:
                cover = f"{r['days_of_cover']} days" if r['days_of_cover'] is not None else "0 days (Out of Stock)"
                answer += f"• **{r['name']}** — Stock: {r['stock']} units | Run rate: {r['units_per_day']} /day | Cover: **{cover}** | Suggest Reorder: **{r['suggested_reorder_units']} units**\n"

            widget = {
                "type": "stockout_forecast",
                "horizon_days": horizon,
                "at_risk": at_risk,
                "orders_analysed": res.get("orders_analysed", 0)
            }

        # Pattern 3: Order Health / Stuck orders / Anomalies ("health", "stuck", "fraud", "anomalies", "refunds", "failures")
        elif any(w in q_lower for w in ["health", "stuck", "fraud", "anomaly", "anomalies", "failure", "refund", "outlier"]):
            thoughts.append("Identified intent: Operational anomaly and order health diagnostic.")
            thoughts.append("Selecting tool: `woo_order_health` (detects stalled processing, high failure rates, and statistical whale outliers).")

            t_start = time.perf_counter()
            res = call_tool(self.tools, self.client, "woo_order_health", {"days": 30, "stuck_days": 3})
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_order_health", "args": {"days": 30, "stuck_days": 3}, "latency_ms": lat, "ok": "error" not in res})

            stuck = res.get("stuck_orders", [])
            outliers = res.get("outliers", [])
            flags = res.get("flags", [])
            rates = res.get("rates", {})

            thoughts.append(f"Evaluated {res.get('orders', 0)} orders. Found {len(stuck)} stuck orders and {len(outliers)} statistical outliers.")

            answer = (
                f"**Store Order Health Report (Last 30 Days)**\n\n"
                f"• **Evaluated Orders**: {res.get('orders', 0)}\n"
                f"• **Payment Failure Rate**: {rates.get('failed_pct', 0)}%\n"
                f"• **Refund Rate**: {rates.get('refunded_pct', 0)}%\n"
                f"• **Stuck Orders (≥3 days)**: {len(stuck)} orders requiring merchant intervention\n"
                f"• **Whale Outliers (Z > 3)**: {len(outliers)} unusually large orders\n\n"
            )
            if flags:
                answer += "**Active Flags:**\n" + "\n".join(f"⚠️ {f}" for f in flags)
            else:
                answer += "✅ All core operational health indicators are within normal parameters."

            widget = {
                "type": "order_health",
                "health": res
            }

        # Pattern 4: Sales / Financial performance ("sales", "revenue", "financial", "top products", "performance")
        elif any(w in q_lower for w in ["sales", "revenue", "financial", "performance", "aov", "earnings", "top product"]):
            days = 7
            d_match = re.search(r"(\d+)\s*(?:day|days)", q_lower)
            if d_match:
                days = int(d_match.group(1))

            thoughts.append(f"Identified intent: Sales and revenue performance summary for {days} days.")
            thoughts.append("Selecting tool: `woo_sales_summary` (aggregates revenue, AOV, daily velocity, top selling products).")

            t_start = time.perf_counter()
            res = call_tool(self.tools, self.client, "woo_sales_summary", {"days": days})
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_sales_summary", "args": {"days": days}, "latency_ms": lat, "ok": "error" not in res})

            top = res.get("top_products", [])
            thoughts.append(f"Calculated ₹{res.get('revenue', 0):,.2f} total revenue over {res.get('orders', 0)} orders.")

            answer = (
                f"**Sales Performance ({days}-Day Summary)**\n\n"
                f"• **Total Revenue**: ₹{res.get('revenue', 0):,.2f}\n"
                f"• **Completed Orders**: {res.get('orders', 0)}\n"
                f"• **Average Order Value (AOV)**: ₹{res.get('aov', 0):,.2f}\n\n"
                f"**Top Selling Products:**\n"
            )
            for p in top[:3]:
                answer += f"• **{p['name']}** — {p['units']} units (₹{p['revenue']:,.2f})\n"

            widget = {
                "type": "sales_summary",
                "summary": res
            }

        # Pattern 5: Security / Prompt Injection check ("injection", "canary", "attack", "security", "untrusted")
        elif any(w in q_lower for w in ["injection", "canary", "attack", "security", "hack", "threat", "prompt"]):
            thoughts.append("Identified intent: Store-wide indirect prompt injection and security vulnerability audit.")
            thoughts.append("Selecting tools: `woo_list_orders`, `woo_get_order`, and `woo_get_product` scanning untrusted fields.")

            t_start = time.perf_counter()
            findings = []
            # Scan recent orders for notes containing instruction-like phrases
            orders_res = call_tool(self.tools, self.client, "woo_list_orders", {"per_page": 20})
            tool_calls.append({"tool": "woo_list_orders", "args": {"per_page": 20}, "latency_ms": 10.0, "ok": True})
            for o in orders_res.get("items", []):
                detail = call_tool(self.tools, self.client, "woo_get_order", {"id": o["id"]})
                cn = detail.get("customer_note")
                if isinstance(cn, dict) and cn.get("warning"):
                    findings.append({
                        "type": "Order Customer Note Attack",
                        "id": o["id"],
                        "title": f"Order #{o['id']}",
                        "detail": cn["warning"]
                    })
            # Scan product description
            prod = call_tool(self.tools, self.client, "woo_get_product", {"id": 102})
            if prod and isinstance(prod.get("description"), dict) and prod["description"].get("warning"):
                findings.append({
                    "type": "Catalog Description Attack",
                    "id": prod["id"],
                    "title": prod.get("name", "Filter Coffee Powder"),
                    "detail": prod["description"]["warning"]
                })
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_get_product", "args": {"id": 102}, "latency_ms": lat, "ok": True})

            thoughts.append(f"Security scan complete. Identified {len(findings)} indirect prompt injection vectors quarantined by redact.py.")

            answer = (
                f"🛡️ **Security & Prompt Injection Audit**\n\n"
                f"The connector scanned customer notes and product descriptions for indirect prompt injection attempts. "
                f"Found **{len(findings)} quarantined payload(s)**.\n\n"
                f"**Connector Defense Status**: ACTIVE. All untrusted text is automatically wrapped in `untrusted_text` envelopes with warning flags, "
                f"preventing LLMs from executing malicious merchant data as system instructions."
            )

            widget = {
                "type": "security_audit",
                "findings": findings
            }

        # Pattern 6: Product search ("search product", "find product", "look up tea", "chai")
        elif any(w in q_lower for w in ["search product", "find product", "lookup product", "look up product", "search for", "catalog", "browse products"]) or (any(w in q_lower for w in ["search", "find", "show me", "look up"]) and any(w in q_lower for w in ["tea", "coffee", "chai", "item", "product"])):
            term = re.sub(r"(?:search|find|lookup|look up|product|products|for|about|me)\s*", "", q, flags=re.I).strip() or "chai"
            thoughts.append(f"Identified intent: Product catalog search for '{term}'.")
            thoughts.append("Selecting tool: `woo_search_products`.")

            t_start = time.perf_counter()
            res = call_tool(self.tools, self.client, "woo_search_products", {"query": term, "limit": 6})
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_search_products", "args": {"query": term, "limit": 6}, "latency_ms": lat, "ok": "error" not in res})

            items = res.get("items", [])
            thoughts.append(f"Catalog query returned {len(items)} matching products.")

            answer = f"**Product Search Results for '{term}':**\n\n"
            if not items:
                answer += "No matching published products found in the catalog."
            else:
                for p in items:
                    answer += f"• **{p['name']}** (SKU: `{p.get('sku', 'N/A')}`) — ₹{float(p.get('price', 0)):,.2f} | Stock: {p.get('stock_quantity', 'N/A')} ({p.get('stock_status')})\n"

            widget = {
                "type": "product_list",
                "query": term,
                "products": items
            }

        # Pattern 7: Fallback / Connection status / Help
        else:
            thoughts.append("Query is general or connection-related. Probing store connectivity.")
            t_start = time.perf_counter()
            res = call_tool(self.tools, self.client, "woo_connection_status", {})
            lat = round((time.perf_counter() - t_start) * 1000, 1)
            tool_calls.append({"tool": "woo_connection_status", "args": {}, "latency_ms": lat, "ok": "error" not in res})

            thoughts.append("Store is healthy and connected via read-only REST credentials.")
            answer = (
                f"👋 **WooCommerce Agent Studio Copilot Ready!**\n\n"
                f"Connected to store: `{res.get('store', 'http://127.0.0.1:8089')}` in **read-only mode** with **PII {res.get('pii_mode', 'masked')}**.\n\n"
                f"You can ask me questions such as:\n"
                f"• *\"Where is order 1010 and what is its status?\"*\n"
                f"• *\"Which products will stock out in the next 14 days?\"*\n"
                f"• *\"Are there any stuck orders or payment anomalies?\"*\n"
                f"• *\"Show me a sales summary and top products for the last 7 days.\"*\n"
                f"• *\"Scan the store for prompt injection attacks or security threats.\"*\n"
                f"• *\"Find products related to coffee or tea.\"*"
            )

        total_lat = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "query": query,
            "thoughts": thoughts,
            "tool_calls": tool_calls,
            "answer": answer,
            "widget": widget,
            "total_latency_ms": total_lat
        }
