import json, os, stat, threading, time
import pytest
from wooconnector import auth, redact
from wooconnector.client import WooClient, WooAuthError, WooNotFound, WooRateLimited, ReadOnlyViolation, WooError
from wooconnector.ratelimit import AdaptiveLimiter
from wooconnector.tools import build_tools, call_tool
from .conftest import CK, CS


# ---------- authentication ----------
def test_api_key_auth_ok_and_bad(store, client):
    assert call_tool(build_tools(client), client, "woo_connection_status", {})["connected"] is True
    bad = WooClient(auth.Credentials(store.url, CK, "wrong"), backoff_base=0.01)
    out = call_tool(build_tools(bad), bad, "woo_get_order", {"id": 1001})
    assert out["error"]["type"] == "WooAuthError" and "hint" in out["error"]


def test_oauth_consent_flow_issues_read_scoped_keys(store):
    creds = auth.run_authorize_flow(store.url, port=0, auto_open=True, timeout=10)
    assert creds.scope == "read" and creds.consumer_key.startswith("ck_")
    c = WooClient(creds)
    assert call_tool(build_tools(c), c, "woo_list_orders", {"per_page": 1})["total"] > 100


def test_oauth_rejects_state_mismatch(store, monkeypatch):
    monkeypatch.setattr(auth.secrets, "token_urlsafe", lambda n: "expected")
    orig = auth.build_authorize_url
    monkeypatch.setattr(auth, "build_authorize_url", lambda b, c, r, s, scope="read": orig(b, c, r, "attacker-state", scope))
    with pytest.raises(auth.AuthError, match="CSRF"):
        auth.run_authorize_flow(store.url, port=0, auto_open=True, timeout=10)


def test_credentials_file_is_0600_and_redacted(tmp_path):
    p = auth.save(auth.Credentials("https://x.shop", CK, CS), tmp_path / "c.json")
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert CS not in json.dumps(auth.load(p).redacted())


def test_refuses_plain_http_remote():
    with pytest.raises(WooError, match="plain-http"):
        WooClient(auth.Credentials("http://shop.example.com", CK, CS))


# ---------- list / get / search primitives ----------
def test_list_pagination_and_filters(client):
    T = build_tools(client)
    p1 = call_tool(T, client, "woo_list_orders", {"per_page": 10, "status": "completed"})
    assert len(p1["items"]) == 10 and p1["next_page"] == 2
    p2 = call_tool(T, client, "woo_list_orders", {"per_page": 10, "status": "completed", "page": 2})
    assert not {o["id"] for o in p1["items"]} & {o["id"] for o in p2["items"]}
    assert all(o["status"] == "completed" for o in p1["items"] + p2["items"])


def test_per_page_is_capped(client):
    assert len(call_tool(build_tools(client), client, "woo_list_products", {"per_page": 50})["items"]) <= 50


def test_get_and_not_found(client):
    T = build_tools(client)
    assert call_tool(T, client, "woo_get_product", {"id": 100})["name"].startswith("Masala")
    err = call_tool(T, client, "woo_get_order", {"id": 99999})["error"]
    assert err["type"] == "WooNotFound" and not err["retryable"]


def test_search_by_order_number_and_text(client):
    T = build_tools(client)
    assert call_tool(T, client, "woo_search_orders", {"query": "#1010"})["items"][0]["id"] == 1010
    assert call_tool(T, client, "woo_search_products", {"query": "chai"})["items"]


def test_timeline_is_chronological(client):
    tl = call_tool(build_tools(client), client, "woo_order_timeline", {"id": 1020})["timeline"]
    assert [e["at"] for e in tl] == sorted(e["at"] for e in tl) and len(tl) >= 3


def test_paginate_walks_all_pages(client, store):
    assert len(list(client.paginate("/orders", per_page=25, max_items=10_000))) == len(store.orders)
    assert len(list(client.paginate("/orders", per_page=25, max_items=30))) == 30


# ---------- rate limiting & resilience ----------
def test_429_is_retried_with_retry_after(store):
    store.limit, store.window = 3, 2.0
    c = WooClient(auth.Credentials(store.url, CK, CS), limiter=AdaptiveLimiter(rate=100, burst=100), cache_ttl=0, backoff_base=0.01)
    t0 = time.time()
    for _ in range(6):
        c.get("/products", {"per_page": 1})
    assert c.stats["retries"] >= 1 and c.limiter.throttle_events >= 1 and time.time() - t0 >= 1


def test_limiter_is_aimd():
    now = [0.0]
    lim = AdaptiveLimiter(rate=8, burst=8, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    lim.on_throttle(2.0)
    assert lim.rate == 4 and lim.acquire() >= 2.0
    for _ in range(100):
        lim.on_success()
    assert lim.rate == 8


def test_gives_up_with_actionable_error(store):
    store.limit = 0
    c = WooClient(auth.Credentials(store.url, CK, CS), max_retries=2, backoff_base=0.01, cache_ttl=0, limiter=AdaptiveLimiter(rate=100, burst=100))
    out = call_tool(build_tools(c), c, "woo_get_product", {"id": 100})
    assert out["error"]["type"] == "WooRateLimited" and out["error"]["retryable"] and out["error"]["hint"]


def test_chaos_503s_are_survived(store):
    store.chaos = 0.4
    c = WooClient(auth.Credentials(store.url, CK, CS), max_retries=8, backoff_base=0.01, cache_ttl=0, limiter=AdaptiveLimiter(rate=300, burst=300, min_rate=50))
    for i in range(15):
        c.get("/products/100")
    assert c.stats["retries"] > 0 and store.injected > 0


def test_concurrent_agents_share_one_limiter(store):
    store.limit, store.window = 15, 1.0
    c = WooClient(auth.Credentials(store.url, CK, CS), limiter=AdaptiveLimiter(rate=10, burst=5), cache_ttl=0, backoff_base=0.01)
    errs = []
    def work():
        try:
            for _ in range(5): c.get("/products", {"per_page": 1})
        except Exception as e: errs.append(e)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs and c.stats["requests"] >= 40


def test_cache_avoids_duplicate_calls(store):
    c = WooClient(auth.Credentials(store.url, CK, CS), cache_ttl=30)
    c.get("/products/100"); c.get("/products/100")
    assert c.stats["cache_hits"] == 1 and c.stats["requests"] == 1


# ---------- safety ----------
def test_read_only_enforced():
    c = WooClient(auth.Credentials("http://127.0.0.1:1", CK, CS))
    with pytest.raises(ReadOnlyViolation):
        c.request("DELETE", "/orders/1")


def test_pii_masked_by_default_and_revealable(client):
    raw = json.dumps(call_tool(build_tools(client), client, "woo_get_order", {"id": 1010}))
    assert "@example.com" not in raw and "+9198" not in raw and "Demo Street" not in raw
    revealed = json.dumps(call_tool(build_tools(client, reveal_pii=True), client, "woo_get_order", {"id": 1010}))
    assert "@example.com" in revealed


def test_prompt_injection_is_flagged(client):
    T = build_tools(client)
    note_order = next(o for o in client.get("/orders", {"per_page": 20})[0] if "ignore all previous" in o["customer_note"])
    out = call_tool(T, client, "woo_get_order", {"id": note_order["id"]})
    assert "warning" in out["customer_note"] and "untrusted_text" in out["customer_note"]
    prod = call_tool(T, client, "woo_get_product", {"id": 102})
    assert "warning" in prod["description"]


def test_secrets_never_in_tool_output(client):
    T = build_tools(client)
    blob = "".join(json.dumps(call_tool(T, client, n, a)) for n, a in [("woo_connection_status", {}), ("woo_list_orders", {}), ("woo_inventory_report", {})])
    assert CS not in blob and CK not in blob


def test_bad_arguments_are_self_correctable(client):
    T = build_tools(client)
    assert call_tool(T, client, "woo_get_order", {})["error"]["type"] == "invalid_arguments"
    assert call_tool(T, client, "woo_get_order", {"id": "abc"})["error"]["type"] == "invalid_arguments"
    assert call_tool(T, client, "woo_list_products", {"stock_status": "banana"})["error"]["type"] == "invalid_arguments"
    assert call_tool(T, client, "woo_delete_order", {"id": 1})["error"]["type"] == "unknown_tool"
    assert call_tool(T, client, "woo_get_order", {"id": "1010"})["id"] == 1010  # LLM sent string for int: coerced


def test_redact_helpers():
    assert redact.mask_email("john.doe@gmail.com") == "j***@g***.com"
    assert redact.mask_phone("+919812345678") == "******78"


# ---------- analytics ----------
def test_forecast_flags_known_low_stock(client):
    ids = {r["id"] for r in call_tool(build_tools(client), client, "woo_stockout_forecast", {})["at_risk"]}
    assert 111 in ids  # Sandalwood Soap: stock 0 with sales history


def test_order_health_finds_stuck_orders(client):
    out = call_tool(build_tools(client), client, "woo_order_health", {"days": 30, "stuck_days": 3})
    assert out["stuck_orders"] and out["flags"]


# ---------- MCP spec ----------
def test_every_tool_has_valid_readonly_spec(client):
    for t in build_tools(client).values():
        s = t.spec()
        assert s["inputSchema"]["type"] == "object" and s["annotations"]["readOnlyHint"] is True
        assert set(s["inputSchema"]["required"]) <= set(s["inputSchema"]["properties"])
