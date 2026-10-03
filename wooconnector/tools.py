"""Tool registry: one source of truth for MCP server, CLI, tests, and the generated spec doc.

Design rules for agent-friendliness:
  * every tool is read-only and idempotent (annotated as such for MCP clients)
  * results are compact projections, never raw WooCommerce blobs (saves tokens, hides PII)
  * errors are *returned* as structured objects with a `hint`, so the agent can self-correct
  * hard caps on page sizes and scan sizes so one bad prompt can't hammer a store
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Callable
from . import insights, redact
from .client import WooClient, WooError, WooNotFound


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list
    handler: Callable

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": {"type": "object", "properties": self.properties, "required": self.required,
                                "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}}


INT = lambda d, lo=1, hi=None, default=None: {"type": "integer", "description": d, "minimum": lo, **({"maximum": hi} if hi else {}), **({"default": default} if default else {})}
STR = lambda d, **k: {"type": "string", "description": d, **k}
PAGE = {"page": INT("1-based page", 1, default=1), "per_page": INT("Items per page (max 50)", 1, 50, 20)}


def build_tools(client: WooClient, reveal_pii: bool = False) -> dict[str, Tool]:
    def listing(path, shaper, extra_keys):
        def h(a):
            params = {k: a.get(k) for k in extra_keys}
            params.update(page=a.get("page", 1), per_page=min(a.get("per_page", 20), 50))
            data, hd = client.get(path, params)
            hl = {k.lower(): v for k, v in hd.items()}
            total, pages = int(hl.get("x-wp-total", len(data))), int(hl.get("x-wp-totalpages", 1))
            page = params["page"]
            return {"items": [shaper(x) for x in data], "page": page, "total": total, "total_pages": pages,
                    "next_page": page + 1 if page < pages else None}
        return h

    def get_one(path, shaper):
        return lambda a: shaper(client.get(f"{path}/{a['id']}")[0])

    def search_orders(a):
        q = a["query"].strip().lstrip("#")
        if q.isdigit():  # order number lookup is by far the most common support query
            try:
                return {"items": [redact.order_detail(client.get(f"/orders/{q}")[0], reveal_pii)], "matched_by": "order_id"}
            except WooNotFound:
                pass
        data, _ = client.get("/orders", {"search": q, "per_page": min(a.get("limit", 10), 25)})
        return {"items": [redact.order_summary(o, reveal_pii) for o in data], "matched_by": "text_search"}

    def search_products(a):
        data, _ = client.get("/products", {"search": a["query"], "per_page": min(a.get("limit", 10), 25), "status": "publish"})
        if not data and a["query"].isdigit() is False and " " in a["query"]:  # fallback: try first token
            data, _ = client.get("/products", {"search": a["query"].split()[0], "per_page": 10, "status": "publish"})
        return {"items": [redact.product_summary(p) for p in data]}

    def timeline(a):
        o = client.get(f"/orders/{a['id']}")[0]
        notes, _ = client.get(f"/orders/{a['id']}/notes")
        ev = [{"at": o["date_created"], "event": f"Order placed ({o.get('payment_method_title')})"}]
        ev += [{"at": n["date_created"], "event": (redact.untrusted(n["note"], 200) or {}), "by": "customer" if n.get("customer_note") else "system/staff"} for n in notes]
        ev.append({"at": o["date_modified"], "event": f"Current status: {o['status']}"})
        return {"order": redact.order_summary(o, reveal_pii), "timeline": sorted(ev, key=lambda e: e["at"])}

    def status(a):
        c = client.creds.redacted()
        data, hd = client.get("/orders", {"per_page": 1}, use_cache=False)
        return {"connected": True, "store": c["base_url"], "key": c["consumer_key"], "scope": c["scope"],
                "pii_mode": "revealed" if reveal_pii else "masked", "rate_limiter": client.limiter.stats(), "client": client.stats}

    T = [
        Tool("woo_connection_status", "Verify credentials and show rate-limiter health. Call first if other tools fail.", {}, [], status),
        Tool("woo_list_products", "List products with filters. Returns compact rows; use woo_get_product for detail.",
             {**PAGE, "search": STR("Free-text search"), "stock_status": STR("instock | outofstock | onbackorder", enum=["instock", "outofstock", "onbackorder"]),
              "category": INT("Category ID"), "status": STR("publish | draft | private", enum=["publish", "draft", "private"])},
             [], listing("/products", redact.product_summary, ["search", "stock_status", "category", "status"])),
        Tool("woo_get_product", "Get one product by numeric ID.", {"id": INT("Product ID")}, ["id"], get_one("/products", redact.product_detail)),
        Tool("woo_search_products", "Fuzzy-ish product search by name/SKU (published only).", {"query": STR("Name or SKU"), "limit": INT("Max results", 1, 25, 10)}, ["query"], search_products),
        Tool("woo_list_orders", "List orders with filters. Customer PII is masked by default.",
             {**PAGE, "status": STR("pending|processing|on-hold|completed|cancelled|refunded|failed"),
              "after": STR("ISO8601 lower bound on created date, e.g. 2025-01-31T00:00:00"), "before": STR("ISO8601 upper bound"),
              "customer": INT("WooCommerce customer ID")},
             [], listing("/orders", lambda o: redact.order_summary(o, reveal_pii), ["status", "after", "before", "customer"])),
        Tool("woo_get_order", "Get one order (line items, payment, masked customer) by ID.", {"id": INT("Order ID")}, ["id"], get_one("/orders", lambda o: redact.order_detail(o, reveal_pii))),
        Tool("woo_search_orders", "Find orders by order number (#1234) or free text.", {"query": STR("Order number or text"), "limit": INT("Max results", 1, 25, 10)}, ["query"], search_orders),
        Tool("woo_order_timeline", "Chronological story of an order (placement, staff/system notes, status). Best tool for 'where is my order'.", {"id": INT("Order ID")}, ["id"], timeline),
        Tool("woo_inventory_report", "Out-of-stock and low-stock SKUs among stock-managed products.", {"threshold": INT("Low-stock threshold", 0, 1000, 5)}, [], lambda a: insights.inventory_report(client, a.get("threshold", 5))),
        Tool("woo_stockout_forecast", "Predict which SKUs sell out within the horizon, using recent sales velocity; includes reorder suggestions.",
             {"lookback_days": INT("Days of sales history", 7, 90, 30), "horizon_days": INT("Forecast horizon", 1, 60, 14)}, [], lambda a: insights.stockout_forecast(client, a.get("lookback_days", 30), a.get("horizon_days", 14))),
        Tool("woo_order_health", "Detect stuck orders, high failure/refund rates and statistical outliers; returns plain-language flags.",
             {"days": INT("Window in days", 1, 60, 14), "stuck_days": INT("Age that counts as stuck", 1, 30, 3)}, [], lambda a: insights.order_health(client, a.get("days", 14), a.get("stuck_days", 3))),
        Tool("woo_sales_summary", "Revenue, AOV, daily trend and top products for the last N days.", {"days": INT("Window in days", 1, 90, 7)}, [], lambda a: insights.sales_summary(client, a.get("days", 7))),
    ]
    return {t.name: t for t in T}


def call_tool(tools: dict, client: WooClient, name: str, args: dict | None) -> dict:
    audit = client.audit
    args = dict(args or {})
    t0 = time.perf_counter()
    tool = tools.get(name)
    if not tool:
        return {"error": {"type": "unknown_tool", "message": f"No tool '{name}'", "hint": f"Available: {', '.join(tools)}"}}
    unknown = set(args) - set(tool.properties)
    missing = [r for r in tool.required if r not in args]
    if unknown or missing:
        return {"error": {"type": "invalid_arguments", "message": f"unknown={sorted(unknown)} missing={missing}", "hint": "See inputSchema", "retryable": False}}
    for k, v in args.items():  # light type validation: catches LLM string-for-int mistakes with a fixable message
        want = tool.properties[k].get("type")
        if want == "integer":
            try:
                args[k] = int(v)
            except (TypeError, ValueError):
                return {"error": {"type": "invalid_arguments", "message": f"'{k}' must be an integer", "retryable": False}}
        if "enum" in tool.properties[k] and v not in tool.properties[k]["enum"]:
            return {"error": {"type": "invalid_arguments", "message": f"'{k}' must be one of {tool.properties[k]['enum']}", "retryable": False}}
    ok, out = True, None
    try:
        out = tool.handler(args)
    except WooError as e:
        ok, out = False, {"error": {"type": type(e).__name__, "message": str(e), "retryable": e.retryable, "hint": e.hint}}
    except Exception as e:  # never leak stack traces / secrets to the model
        ok, out = False, {"error": {"type": "internal_error", "message": "Unexpected connector error", "retryable": False}}
        audit.record("internal_error", tool=name, detail=repr(e)[:200])
    ms = round((time.perf_counter() - t0) * 1000, 1)
    audit.record("tool", tool=name, args=args, ok=ok, ms=ms)
    if ok:
        out = {**out, "_meta": {"latency_ms": ms, "rate_limiter": client.limiter.stats()}}
    return out

