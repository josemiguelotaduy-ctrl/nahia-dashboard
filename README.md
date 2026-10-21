# Nahia Home P&L dashboard
- `python3 build.py` pulls Shopify, PayPal, Meta and Google Ads (Sheet) and writes `public/index.html`.
- `config.json`: IVA, UPS % and expense percentages.
- `orders_history.json`: local order history (Shopify only exposes 60 days). Do not delete.
- Publish: private GitHub repo + Cloudflare Pages (build output directory `public`, no build command) + Cloudflare Access.
- Daily refresh: `.github/workflows/update.yml` (needs repo secrets, see .env keys).
