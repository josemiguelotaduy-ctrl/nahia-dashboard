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
START = os.environ.get("DASH_START", "2022-08-01")


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
                "cancelled": bool(o.get("cancelledAt")),
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
    # Load vat_by_country early so we can compute per-order IVA
    # (Shopify currentTotalTaxSet returns 0 for "taxes included" stores, so we compute it ourselves)
    _cfg_path = os.path.join(ROOT, "config.json")
    _cfg_early = json.load(open(_cfg_path)) if os.path.exists(_cfg_path) else {}
    vat_by_country = (_cfg_early.get("params") or {}).get("vat_by_country", {})
    for o in allo:
        if o["d"] < START:
            continue
        if o.get("cancelled"):
            continue
        rep = 1 if (o["u"] and o["u"] in seen) or o["idx"] >= 2 else 0
        if o["u"]:
            seen.add(o["u"])
        # Compute IVA using per-country rate (taxes are included in the gross price)
        cc = o.get("c") or "??"
        vat_rate = vat_by_country.get(cc, vat_by_country.get("??", 0))
        order_tax = round(o["g"] * vat_rate / (100 + vat_rate), 4) if vat_rate else 0.0
        day = days[o["d"]]
        day["orders"] += 1
        day["gross"] += o["g"]; day["tax"] += order_tax; day["shipping_charged"] += o["s"]
        day["discounts"] += o["disc"]; day["refunds"] += o["ref"]; day["cogs"] += o["k"]
        day["repeat_orders"] += rep
        key = "paypal" if o["pp"] else "shopify_payments"
        day[f"{key}_amount"] += o["g"]; day[f"{key}_orders"] += 1
        orders_out.append({"d": o["d"], "c": o["c"], "g": o["g"], "s": o["s"], "k": o["k"], "u": o["u"], "r": rep, "i": o["i"]})
    print(f"Shopify: {fetched} fetched, {len(orders_out)} in history, {sum(o['r'] for o in orders_out)} from returning customers")
    # Override daily sales/shipping/discounts/refunds with ShopifyQL so every date range matches Shopify Analytics to the cent
    sql_rows = 0
    for yr in range(2022, dt.date.today().year + 1):
        end = "today" if yr == dt.date.today().year else f"{yr}-12-31"
        q_sql = f'{{ shopifyqlQuery(query: "FROM sales SHOW orders, gross_sales, discounts, returns, net_sales, shipping_charges, taxes, total_sales, cost_of_goods_sold TIMESERIES day SINCE {yr}-01-01 UNTIL {end}") {{ tableData {{ rows }} }} }}'
        td = (gql(q_sql, {}).get("shopifyqlQuery") or {}).get("tableData") or {}
        for r in td.get("rows", []):
            d_str = r["day"]
            if d_str < START:
                continue
            tot_s = float(r["total_sales"])
            ord_c = float(r["orders"])
            ship_c = float(r["shipping_charges"])
            disc_c = abs(float(r["discounts"]))
            ret_c = abs(float(r["returns"]))
            cogs_c = abs(float(r.get("cost_of_goods_sold", 0) or 0))
            day = days[d_str]
            if tot_s or ord_c or ship_c or ret_c or d_str in days:
                day["orders"] = ord_c
                day["gross"] = tot_s
                day["shipping_charged"] = ship_c
                day["discounts"] = disc_c
                day["refunds"] = ret_c
                if cogs_c:  # Only override COGS if ShopifyQL returns a non-zero value
                    day["cogs"] = cogs_c
                sql_rows += 1
    print(f"ShopifyQL: {sql_rows} daily sales rows synced")
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
    start = max(dt.datetime.fromisoformat(START).replace(tzinfo=dt.timezone.utc),
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1090))
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
meta_campaigns_out = []
google_campaigns_out = []


def meta():
    # Meta only serves the last 37 months of insights
    since = max(START, (dt.date.today() - dt.timedelta(days=1090)).isoformat())
    rng = json.dumps({"since": since, "until": dt.date.today().isoformat()})
    qs = urllib.parse.urlencode({"fields": "spend,impressions,clicks,actions,action_values", "time_increment": 1, "time_range": rng,
                                 "limit": 500, "access_token": ENV["META_ACCESS_TOKEN"]})
    url = f"https://graph.facebook.com/v21.0/{ENV['META_AD_ACCOUNT_ID']}/insights?" + qs
    rows = []
    while url:
        d = http_json(url)
        rows += d["data"]
        url = d.get("paging", {}).get("next")
    for r in rows:
        day = days[r["date_start"]]
        day["meta_spend"] += float(r["spend"])
        day["meta_clicks"] += float(r["clicks"])
        day["meta_impr"] += float(r["impressions"])
        for ac in r.get("actions", []):
            at = ac["action_type"]
            if at == "omni_view_content":
                day["meta_vc"] += float(ac["value"])
            elif at == "omni_add_to_cart":
                day["meta_atc"] += float(ac["value"])
            elif at == "omni_initiated_checkout":
                day["meta_ic"] += float(ac["value"])
            elif at == "omni_purchase":
                day["meta_conv"] += float(ac["value"])
        for av in r.get("action_values", []):
            if av["action_type"] == "omni_purchase":
                day["meta_value"] += float(av["value"])
    # Also fetch campaign-level daily breakdown for scientific testing tab
    try:
        qs_c = urllib.parse.urlencode({"fields": "campaign_name,spend,impressions,clicks,actions,action_values",
                                       "level": "campaign", "time_increment": 1, "time_range": rng,
                                       "limit": 500, "access_token": ENV["META_ACCESS_TOKEN"]})
        url_c = f"https://graph.facebook.com/v21.0/{ENV['META_AD_ACCOUNT_ID']}/insights?" + qs_c
        while url_c:
            dc = http_json(url_c)
            for r in dc.get("data", []):
                act = {a["action_type"]: float(a["value"]) for a in r.get("actions", [])}
                av = {a["action_type"]: float(a["value"]) for a in r.get("action_values", [])}
                meta_campaigns_out.append({
                    "d": r["date_start"],
                    "n": r.get("campaign_name", ""),
                    "sp": round(float(r.get("spend", 0)), 2),
                    "cl": int(float(r.get("clicks", 0))),
                    "vc": int(act.get("omni_view_content", 0)),
                    "atc": int(act.get("omni_add_to_cart", 0)),
                    "ic": int(act.get("omni_initiated_checkout", 0)),
                    "cv": int(act.get("omni_purchase", 0)),
                    "val": round(av.get("omni_purchase", 0.0), 2),
                })
            url_c = dc.get("paging", {}).get("next")
    except Exception as e:
        print(f"Meta campaign-level warning: {e}", file=sys.stderr)
    print(f"Meta: {len(rows)} days, {len(meta_campaigns_out)} campaign-day rows")


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
        google_campaigns_out.append({
            "d": r["date"],
            "n": r.get("campaign", ""),
            "sp": round(float(r["cost"]), 2),
            "cl": int(float(r["clicks"])),
            "cv": round(float(r["conversions"]), 2),
            "val": round(float(r["conversions_value"]), 2),
        })
        n += 1
    print(f"Google: {n} rows")


def main():
    # Cache previous meta spend in case Meta API hits a temporary checkpoint
    prev_meta = {}
    dp = os.path.join(ROOT, "data.json")
    if os.path.exists(dp):
        try:
            prev_days = json.load(open(dp)).get("days", {})
            for d, row in prev_days.items():
                prev_meta[d] = {
                    "meta_spend": row.get("meta_spend", 0),
                    "meta_clicks": row.get("meta_clicks", 0),
                    "meta_impr": row.get("meta_impr", 0),
                    "meta_value": row.get("meta_value", 0),
                }
        except Exception:
            pass

    for fn in (shopify, paypal, meta, google):
        try:
            fn()
        except Exception as e:
            print(f"WARNING {fn.__name__} failed: {e}", file=sys.stderr)
            if fn == meta and prev_meta:
                print("Restoring cached Meta numbers from previous run")
                for d, m_dict in prev_meta.items():
                    for mk, mv in m_dict.items():
                        if mv:
                            days[d][mk] = mv
    cfg_path = os.path.join(ROOT, "config.json")
    cfg = json.load(open(cfg_path))
    payload = {
        "generated": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "start": START,
        "orders": orders_out,
        "days": {k: {m: round(v, 4) for m, v in d.items()} for k, d in sorted(days.items())},
        "meta_campaigns": meta_campaigns_out or (json.load(open(dp)).get("meta_campaigns", []) if os.path.exists(dp) else []),
        "google_campaigns": google_campaigns_out,
        "config": cfg,
    }
    json.dump(payload, open(os.path.join(ROOT, "data.json"), "w"), indent=1)
    html = open(os.path.join(ROOT, "template.html")).read().replace("__DATA__", json.dumps(payload))
    os.makedirs(os.path.join(ROOT, "public"), exist_ok=True)
    open(os.path.join(ROOT, "public", "index.html"), "w").write(html)
    open(os.path.join(ROOT, "index.html"), "w").write(html)
    print("OK -> dashboard/index.html & public/index.html")
    # Auto-push to GitHub if remote exists (generated files: our fresh build always wins)
    try:
        import subprocess
        sh = lambda *c: subprocess.run(c, cwd=ROOT, capture_output=True)
        sh("git", "fetch", "origin", "main")
        sh("git", "reset", "--soft", "origin/main")
        sh("git", "add", "-A")
        sh("git", "commit", "-m", f"Auto-update {dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}")
        p_res = sh("git", "push", "origin", "main")
        if p_res.returncode == 0:
            print("Synced and pushed to GitHub Pages successfully")
        else:
            print(f"Git push warning: {p_res.stderr.decode()[:200]}")
    except Exception as e:
        print(f"Git auto-sync skipped: {e}")


if __name__ == "__main__":
    main()
