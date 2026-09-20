import urllib.request, json, ssl, time

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# Test CoinGecko candle endpoint -- no API key needed
# CoinGecko uses coin IDs not symbols, so map our coins first
coin_ids = {
    "NEAR": "near",
    "AVAX": "avalanche-2",
    "ARB": "arbitrum",
    "ENA": "ethena",
    "MANTA": "manta-network"
}

print("Testing CoinGecko OHLCV endpoint from GitHub Actions:")
print("-" * 55)

for symbol, cg_id in coin_ids.items():
    # CoinGecko free tier: hourly OHLC for last 2 days
    url = (f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
           f"?vs_currency=usd&days=2")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = json.loads(r.read().decode())
        if data and len(data) > 0:
            print(f"{symbol}: {len(data)} candles ✅ | last close: {data[-1][4]:.4f}")
        else:
            print(f"{symbol}: empty response ❌")
    except Exception as e:
        print(f"{symbol}: {str(e)[:50]} ❌")
    time.sleep(1)

print("\nDone.")
