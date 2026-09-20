import urllib.request, json, ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# Test Binance candle endpoint
coins = ["NEAR", "AVAX", "ARB", "ENA", "MANTA"]

print("Testing Binance candle endpoint from GitHub Actions:")
print("-" * 50)

for coin in coins:
    url = f"https://api.binance.com/api/v3/klines?symbol={coin}USDT&interval=1h&limit=24"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            data = json.loads(r.read().decode())
        if data and len(data) > 0:
            closes = [float(c[4]) for c in data]
            print(f"{coin}: {len(data)} candles ✅ | last close: {closes[-1]:.4f}")
        else:
            print(f"{coin}: empty response ❌")
    except Exception as e:
        print(f"{coin}: {e} ❌")

print("\nDone.")
