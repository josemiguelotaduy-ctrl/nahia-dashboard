#!/usr/bin/env python3
"""Nahia Home P&L dashboard builder.

Fetches Shopify, PayPal, Meta and Google Ads (Sheet CSV) data, aggregates it by day
and writes dashboard/index.html (self-contained) + dashboard/data.json.
Run daily:  python3 dashboard/build.py
"""
import csv, hashlib, io, json, os, sys, urllib.parse, urllib.request
import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Madrid")
START = os.environ.get("DASH_START", "2026-08-23")


def load_env():
    env = dict(os.environ)
    for path in (os.path.join(ROOT, "..", ".env"), os.path.join(ROOT, ".env")):
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env.setdefault(k, v.strip().strip('"'))
    return env


ENV = load_env()
days = defaultdict(lambda: defaultdict(float))
orders_out = []


def http_json(url, data=None, headers=None, auth=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    return json.load(urllib.request.urlopen(req, timeout=60))


def local_date(iso):
    d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
    return d.date().isoformat()


# ---------------------------------------------------------------- Shopify
def shopify():
    store = ENV["SHOPIFY_STORE"]
    tok = http_json(
        f"https://{store}/admin/oauth/access_token",
        urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": ENV["SHOPIFY_CLIENT_ID"],
            "client_secret": ENV["SHOPIFY_CLIENT_SECRET"]}).encode())["access_token"]

    def gql(q, v):
        d = http_json(f"https://{store}/admin/api/2025-10/graphql.json",
                      json.dumps({"query": q, "variables": v}).encode(),
                      {"X-Shopify-Access-Token": tok, "Content-Type": "application/json"})
        if "errors" in d:
            raise RuntimeError(d["errors"])
        return d["data"]

    q = """query($c:String,$f:String){orders(first:50,after:$c,query:$f){
      pageInfo{hasNextPage endCursor}
      nodes{id createdAt email customerJourneySummary{customerOrderIndex}
        shippingAddress{countryCodeV2}
        currentTotalPriceSet{shopMoney{amount}} currentTotalTaxSet{shopMoney{amount}}
        totalShippingPriceSet{shopMoney{amount}} totalDiscountsSet{shopMoney{amount}}
        totalRefundedSet{shopMoney{amount}} paymentGatewayNames
        lineItems(first:100){nodes{title quantity currentQuantity discountedTotalSet{shopMoney{amount}} variant{inventoryItem{unitCost{amount}}}}}}}}"""
    # Shopify only exposes the last 60 days without read_all_orders, so keep a local history
    hist_path = os.path.join(ROOT, "orders_history.json")
    hist = {o["id"]: o for o in json.load(open(hist_path))} if os.path.exists(hist_path) else {}
    cur, fetched = None, 0
    while True:
        d = gql(q, {"c": cur, "f": f"created_at:>={START}"})["orders"]
        for o in d["nodes"]:
            m = lambda k: float(o[k]["shopMoney"]["amount"])
            items, ocogs = [], 0.0
            for li in o["lineItems"]["nodes"]:
                uc = ((li["variant"] or {}).get("inventoryItem") or {}).get("unitCost")
                cost = li["currentQuantity"] * float(uc["amount"]) if uc else 0
                ocogs += cost
                rev = float(li["discountedTotalSet"]["shopMoney"]["amount"]) * li["currentQuantity"] / (li["quantity"] or 1)
                items.append([li["title"], li["currentQuantity"], round(rev, 2), round(cost, 2)])
            email = (o.get("email") or "").strip().lower()
            gw = ",".join(o["paymentGatewayNames"]).lower()
            hist[o["id"]] = {
                "id": o["id"], "created": o["createdAt"], "d": local_date(o["createdAt"]),
                "c": (o.get("shippingAddress") or {}).get("countryCodeV2") or "??",
                "g": round(m("currentTotalPriceSet"), 2), "tax": round(m("currentTotalTaxSet"), 2),
                "s": round(m("totalShippingPriceSet"), 2), "disc": round(m("totalDiscountsSet"), 2),
                "ref": round(m("totalRefundedSet"), 2), "k": round(ocogs, 2),
                "pp": "paypal" in gw,
                "u": hashlib.sha1(email.encode()).hexdigest()[:8] if email else None,
                "idx": (o.get("customerJourneySummary") or {}).get("customerOrderIndex") or 1,
                "i": items}
            fetched += 1
        if not d["pageInfo"]["hasNextPage"]:
            break
        cur = d["pageInfo"]["endCursor"]
    allo = sorted(hist.values(), key=lambda x: x["created"])
    json.dump(allo, open(hist_path, "w"))
    seen = set()
    for o in allo:
        if o["d"] < START:
            continue
        rep = 1 if (o["u"] and o["u"] in seen) or o["idx"] >= 2 else 0
        if o["u"]:
            seen.add(o["u"])
        day = days[o["d"]]
        day["orders"] += 1
        day["gross"] += o["g"]; day["tax"] += o["tax"]; day["shipping_charged"] += o["s"]
        day["discounts"] += o["disc"]; day["refunds"] += o["ref"]; day["cogs"] += o["k"]
        day["repeat_orders"] += rep
        key = "paypal" if o["pp"] else "shopify_payments"
        day[f"{key}_amount"] += o["g"]; day[f"{key}_orders"] += 1
        orders_out.append({"d": o["d"], "c": o["c"], "g": o["g"], "s": o["s"], "k": o["k"], "u": o["u"], "r": rep, "i": o["i"]})
    print(f"Shopify: {fetched} fetched, {len(orders_out)} in history, {sum(o['r'] for o in orders_out)} from returning customers")
    # Shopify Payments real fees (balance transactions, newest first)
    bq = """query($c:String){shopifyPaymentsAccount{balanceTransactions(first:100,after:$c,reverse:true){
      pageInfo{hasNextPage endCursor} nodes{type fee{amount} transactionDate}}}}"""
    cur, nf, done = None, 0, False
    while not done:
        d = gql(bq, {"c": cur})["shopifyPaymentsAccount"]["balanceTransactions"]
        for t in d["nodes"]:
            day = local_date(t["transactionDate"])
            if day < START:
                done = True
                break
            fee = float(t["fee"]["amount"])
            if fee:
                days[day]["sp_fees"] += fee
                nf += 1
        if not d["pageInfo"]["hasNextPage"]:
            break
        cur = d["pageInfo"]["endCursor"]
    print(f"Shopify Payments: {nf} fee lines")


# ----------------------------------------------------------------- PayPal
def paypal():
    cid, sec = ENV["PAYPAL_CLIENT_ID"], ENV["PAYPAL_CLIENT_SECRET"]
    tok = http_json("https://api-m.paypal.com/v1/oauth2/token", b"grant_type=client_credentials", auth=f"{cid}:{sec}")["access_token"]
    start = dt.datetime.fromisoformat(START).replace(tzinfo=dt.timezone.utc)
    end, n = dt.datetime.now(dt.timezone.utc), 0
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S-0000")
    while start < end:
        stop = min(start + dt.timedelta(days=30), end)
        page = 1
        while True:
            qs = urllib.parse.urlencode({"start_date": fmt(start), "end_date": fmt(stop),
                                         "fields": "transaction_info", "page_size": 500, "page": page})
            d = http_json("https://api-m.paypal.com/v1/reporting/transactions?" + qs, headers={"Authorization": "Bearer " + tok})
            for t in d.get("transaction_details", []):
                i = t["transaction_info"]
                fee = i.get("fee_amount")
                if fee:
                    days[local_date(i["transaction_initiation_date"])]["paypal_fees"] += abs(float(fee["value"]))
                    n += 1
            if page >= d.get("total_pages", 1):
                break
            page += 1
        start = stop
    print(f"PayPal: {n} fee lines")


# ------------------------------------------------------------------- Meta
def meta():
    rng = json.dumps({"since": START, "until": dt.date.today().isoformat()})
    qs = urllib.parse.urlencode({"fields": "spend,impressions,clicks,action_values", "time_increment": 1, "time_range": rng,
                                 "limit": 500, "access_token": ENV["META_ACCESS_TOKEN"]})
    d = http_json(f"https://graph.facebook.com/v21.0/{ENV['META_AD_ACCOUNT_ID']}/insights?" + qs)
    for r in d["data"]:
        day = days[r["date_start"]]
        day["meta_spend"] += float(r["spend"])
        day["meta_clicks"] += float(r["clicks"])
        day["meta_impr"] += float(r["impressions"])
        for av in r.get("action_values", []):
            if av["action_type"] == "omni_purchase":
                day["meta_value"] += float(av["value"])
    print(f"Meta: {len(d['data'])} days")


# ----------------------------------------------------------------- Google
def google():
    raw = urllib.request.urlopen(ENV["GOOGLE_ADS_SHEET_CSV"], timeout=60).read().decode()
    n = 0
    for r in csv.DictReader(io.StringIO(raw)):
        if r["date"] < START:
            continue
        day = days[r["date"]]
        day["google_spend"] += float(r["cost"])
        day["google_clicks"] += float(r["clicks"])
        day["google_conv"] += float(r["conversions"])
        day["google_value"] += float(r["conversions_value"])
        day["google_impr"] += float(r["impressions"])
        n += 1
    print(f"Google: {n} rows")


def main():
    for fn in (shopify, paypal, meta, google):
        try:
            fn()
        except Exception as e:  # keep dashboard alive if one source fails
            print(f"WARNING {fn.__name__} failed: {e}", file=sys.stderr)
    cfg_path = os.path.join(ROOT, "config.json")
    cfg = json.load(open(cfg_path))
    payload = {
        "generated": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "start": START,
        "orders": orders_out,
        "days": {k: {m: round(v, 4) for m, v in d.items()} for k, d in sorted(days.items())},
        "config": cfg,
    }
    json.dump(payload, open(os.path.join(ROOT, "data.json"), "w"), indent=1)
    html = open(os.path.join(ROOT, "template.html")).read().replace("__DATA__", json.dumps(payload))
    os.makedirs(os.path.join(ROOT, "public"), exist_ok=True)
    open(os.path.join(ROOT, "public", "index.html"), "w").write(html)
    print("OK -> dashboard/public/index.html")


if __name__ == "__main__":
    main()
