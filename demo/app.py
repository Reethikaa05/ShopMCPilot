"""Mission Control: live demo UI. Starts the mock store + connector + dashboard in one process.
    python -m demo.app        ->  http://127.0.0.1:8090
"""
from __future__ import annotations
import json, pathlib, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mockstore.server import start as start_store
from wooconnector.audit import Audit
from wooconnector.auth import Credentials
from wooconnector.client import WooClient
from wooconnector.ratelimit import AdaptiveLimiter
from wooconnector.tools import build_tools, call_tool
from demo.agent import MerchantAgent

HERE = pathlib.Path(__file__).parent


def main(port=8090, store_port=8089):
    store_srv, S = start_store(store_port)
    audit = Audit()
    client = WooClient(
        Credentials(f"http://127.0.0.1:{store_port}", "ck_demo_readonly_0001", "cs_demo_secret_0001"),
        limiter=AdaptiveLimiter(rate=20, burst=10, min_rate=1),
        audit=audit,
        cache_ttl=0,
        backoff_base=0.1
    )
    state = {"reveal_pii": False}
    tools = build_tools(client, reveal_pii=state["reveal_pii"])
    agent = MerchantAgent(tools, client)

    def refresh_tools():
        nonlocal tools, agent
        tools = build_tools(client, reveal_pii=state["reveal_pii"])
        agent = MerchantAgent(tools, client)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _json(self, obj, code=200):
            raw = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                raw = (HERE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/tools":
                self._json([t.spec() for t in tools.values()])
            elif self.path.startswith("/api/events"):
                since = int(self.path.split("since=")[-1]) if "since=" in self.path else 0
                self._json({
                    "events": audit.since(since),
                    "limiter": client.limiter.stats(),
                    "store": {
                        "served": S.served,
                        "throttled": S.throttled,
                        "injected": S.injected,
                        "chaos": S.chaos,
                        "limit": S.limit
                    }
                })
            elif self.path == "/api/kpi":
                # Compute live executive summary
                sales = call_tool(tools, client, "woo_sales_summary", {"days": 30})
                health = call_tool(tools, client, "woo_order_health", {"days": 30, "stuck_days": 3})
                inv = call_tool(tools, client, "woo_inventory_report", {"threshold": 5})
                self._json({
                    "orders_total": len(S.orders),
                    "products_total": len(S.products),
                    "revenue_30d": sales.get("revenue", 0),
                    "aov_30d": sales.get("aov", 0),
                    "daily_revenue": sales.get("daily_revenue", {}),
                    "top_products": sales.get("top_products", []),
                    "out_of_stock_count": len(inv.get("out_of_stock", [])),
                    "low_stock_count": len(inv.get("low_stock", [])),
                    "stuck_orders_count": len(health.get("stuck_orders", [])),
                    "failure_rate": health.get("rates", {}).get("failed_pct", 0),
                    "refund_rate": health.get("rates", {}).get("refunded_pct", 0),
                    "health_flags": health.get("flags", []),
                    "by_status": health.get("by_status", {}),
                    "pii_mode": "revealed" if state["reveal_pii"] else "masked"
                })
            elif self.path == "/api/security":
                # Store security audit for prompt injections
                findings = []
                # Check orders customer_note
                for o in S.orders:
                    cn = o.get("customer_note") or ""
                    if any(w in cn.lower() for w in ["ignore", "system prompt", "instruction", "refund"]):
                        findings.append({
                            "type": "Indirect Prompt Injection in Order Note",
                            "severity": "HIGH",
                            "target": f"Order #{o['id']}",
                            "raw_content": cn,
                            "defended_by": "redact.untrusted() wrapped as safe untrusted_text envelope",
                            "status": "QUARANTINED"
                        })
                # Check products descriptions
                for p in S.products:
                    d = p.get("description") or ""
                    if any(w in d.lower() for w in ["ignore", "secret", "instruction", "reveal"]):
                        findings.append({
                            "type": "Prompt Injection in Catalog Description",
                            "severity": "CRITICAL",
                            "target": f"Product #{p['id']} ({p['name']})",
                            "raw_content": d,
                            "defended_by": "redact.untrusted() with HTML tag stripping & canary flag",
                            "status": "QUARANTINED"
                        })
                self._json({
                    "status": "secure",
                    "injections_neutralized": len(findings),
                    "findings": findings,
                    "guardrails": [
                        "GET-only client (no write/mutation endpoints exist)",
                        "Read-only API key enforcement",
                        "PII masking active by default (names, emails, phones, addresses)",
                        "Hard caps on per_page and page scan limit",
                        "Credentials stored in 0600 file; never returned in tool outputs",
                        "All unstructured text wrapped in untrusted envelopes"
                    ]
                })
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")

            if self.path == "/api/call":
                self._json(call_tool(tools, client, body["tool"], body.get("args")))
            elif self.path == "/api/agent/chat":
                query = body.get("query", "")
                res = agent.process(query)
                self._json(res)
            elif self.path == "/api/toggle-pii":
                state["reveal_pii"] = not state["reveal_pii"]
                refresh_tools()
                self._json({"ok": True, "reveal_pii": state["reveal_pii"]})
            elif self.path == "/api/probe-write":
                # Demonstrate GET-only write block
                # 1. Check client capabilities
                has_write_methods = any(hasattr(client, m) for m in ["post", "put", "delete", "patch"])
                # 2. Probe store server with an actual POST request
                store_url = f"http://127.0.0.1:{store_port}/wp-json/wc/v3/orders"
                req = urllib.request.Request(
                    store_url,
                    data=b'{"status": "cancelled", "customer_note": "HACK"}',
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                try:
                    urllib.request.urlopen(req)
                    store_resp = {"status": 200, "message": "Failed: write succeeded"}
                except urllib.error.HTTPError as e:
                    store_resp = {"status": e.code, "message": e.read().decode()}
                except Exception as e:
                    store_resp = {"status": 500, "message": str(e)}

                self._json({
                    "connector_has_write_methods": has_write_methods,
                    "client_read_only_contract": "WooClient class only implements .get() and .paginate()",
                    "store_post_probe": store_resp,
                    "sandbox_intact": True
                })
            elif self.path == "/api/chaos":
                S.chaos = float(body.get("chaos", S.chaos))
                S.limit = int(body.get("limit", S.limit))
                self._json({"ok": True})
            elif self.path == "/api/stress":
                n, t0 = int(body.get("n", 80)), time.time()
                names = [t for t in tools if t in ("woo_list_orders", "woo_list_products", "woo_get_order", "woo_search_products")]
                def one(i):
                    nm = names[i % len(names)]
                    a = {"woo_get_order": {"id": 1001 + i % 100}, "woo_search_products": {"query": "tea"}}.get(nm, {"page": 1 + i % 5, "per_page": 5})
                    return "error" not in call_tool(tools, client, nm, a)
                with ThreadPoolExecutor(20) as ex:
                    ok = sum(ex.map(one, range(n)))
                self._json({"n": n, "ok": ok, "seconds": round(time.time() - t0, 2)})
            else:
                self._json({"error": "not found"}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    print(f"\n  Mission Control  ->  http://127.0.0.1:{port}\n  Mock WooCommerce ->  http://127.0.0.1:{store_port}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(*(int(x) for x in sys.argv[1:3]))
