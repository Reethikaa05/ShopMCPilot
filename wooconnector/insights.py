"""Higher-level analytics built only from read primitives: what an ops agent actually needs."""
from __future__ import annotations
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


COUNTED = "completed,processing,on-hold"


def _iso(days_ago: float) -> str:
    return (_now() - timedelta(days=days_ago)).isoformat(timespec="seconds")


def inventory_report(client, threshold=5, max_items=1000):
    low, out, scanned = [], [], 0
    for p in client.paginate("/products", {"status": "publish"}, max_items=max_items):
        scanned += 1
        if not p.get("manage_stock"):
            continue
        q = p.get("stock_quantity") or 0
        row = {"id": p["id"], "name": p["name"], "sku": p.get("sku"), "stock": q}
        if q <= 0:
            out.append(row)
        elif q <= threshold:
            low.append(row)
    return {"scanned": scanned, "threshold": threshold, "out_of_stock": out, "low_stock": sorted(low, key=lambda r: r["stock"])}


def sales_velocity(client, lookback_days=30, max_orders=3000):
    units, revenue, n = defaultdict(float), defaultdict(float), 0
    names = {}
    for o in client.paginate("/orders", {"after": _iso(lookback_days), "status": COUNTED}, max_items=max_orders):
        n += 1
        for li in o.get("line_items", []):
            units[li["product_id"]] += li["quantity"]
            revenue[li["product_id"]] += float(li["total"])
            names[li["product_id"]] = li["name"]
    return {"orders": n, "units": units, "revenue": revenue, "names": names}


def stockout_forecast(client, lookback_days=30, horizon_days=14):
    """Days-of-cover = stock / units-per-day. Flags SKUs that will run out inside the horizon."""
    v = sales_velocity(client, lookback_days)
    rows = []
    for p in client.paginate("/products", {"status": "publish"}):
        if not p.get("manage_stock"):
            continue
        per_day = v["units"].get(p["id"], 0) / lookback_days
        stock = p.get("stock_quantity") or 0
        cover = round(stock / per_day, 1) if per_day > 0 else None
        if cover is not None and cover <= horizon_days or stock <= 0 and per_day > 0:
            risk = "critical" if stock <= 0 or cover <= horizon_days / 3 else "high"
            rows.append({"id": p["id"], "name": p["name"], "stock": stock, "units_per_day": round(per_day, 2),
                         "days_of_cover": cover, "risk": risk,
                         "suggested_reorder_units": max(int(per_day * horizon_days * 2 - stock), 0)})
    rows.sort(key=lambda r: (r["days_of_cover"] if r["days_of_cover"] is not None else 0))
    return {"lookback_days": lookback_days, "horizon_days": horizon_days, "orders_analysed": v["orders"], "at_risk": rows,
            "method": "linear run-rate; ignores seasonality and open purchase orders"}


def order_health(client, days=14, stuck_days=3, max_orders=2000):
    """Operational anomalies: stuck orders, failure/refund rates, statistical outliers (z-score > 3)."""
    orders = list(client.paginate("/orders", {"after": _iso(days)}, max_items=max_orders))
    now = _now()
    by_status = defaultdict(int)
    for o in orders:
        by_status[o["status"]] += 1
    stuck = []
    for o in orders:
        if o["status"] in ("processing", "on-hold", "pending"):
            age = (now - datetime.fromisoformat(o["date_created"])).total_seconds() / 86400
            if age >= stuck_days:
                stuck.append({"id": o["id"], "status": o["status"], "age_days": round(age, 1), "total": o["total"]})
    totals = [float(o["total"]) for o in orders if o["status"] not in ("failed", "cancelled")]
    outliers = []
    if len(totals) > 10:
        mu, sd = statistics.mean(totals), statistics.pstdev(totals) or 1
        outliers = [{"id": o["id"], "total": o["total"], "z": round((float(o["total"]) - mu) / sd, 1)}
                    for o in orders if o["status"] not in ("failed", "cancelled") and (float(o["total"]) - mu) / sd > 3]
    n = max(len(orders), 1)
    rates = {"failed_pct": round(100 * by_status["failed"] / n, 1), "refunded_pct": round(100 * by_status["refunded"] / n, 1),
             "cancelled_pct": round(100 * by_status["cancelled"] / n, 1)}
    flags = []
    if rates["failed_pct"] > 8: flags.append(f"Payment failure rate {rates['failed_pct']}% is high: check gateway health")
    if stuck: flags.append(f"{len(stuck)} orders unfulfilled for >= {stuck_days} days")
    if outliers: flags.append(f"{len(outliers)} unusually large orders: possible fraud or B2B")
    return {"window_days": days, "orders": len(orders), "by_status": dict(by_status), "rates": rates,
            "stuck_orders": sorted(stuck, key=lambda r: -r["age_days"])[:20], "outliers": outliers[:10], "flags": flags}


def sales_summary(client, days=7, max_orders=3000):
    daily, top, n, rev = defaultdict(float), defaultdict(lambda: [0, 0.0]), 0, 0.0
    for o in client.paginate("/orders", {"after": _iso(days), "status": COUNTED}, max_items=max_orders):
        t = float(o["total"]); n += 1; rev += t
        daily[o["date_created"][:10]] += t
        for li in o["line_items"]:
            top[li["name"]][0] += li["quantity"]; top[li["name"]][1] += float(li["total"])
    best = sorted(top.items(), key=lambda kv: -kv[1][1])[:5]
    return {"days": days, "orders": n, "revenue": round(rev, 2), "aov": round(rev / n, 2) if n else 0,
            "daily_revenue": {k: round(v, 2) for k, v in sorted(daily.items())},
            "top_products": [{"name": k, "units": u, "revenue": round(r, 2)} for k, (u, r) in best]}
