import urllib.request, json, ssl, time

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def fetch(url, delay=1.5):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        data = json.loads(r.read().decode())
    time.sleep(delay)
    return data

# Step 1: Get all Indodax IDR coins
print("Step 1: Fetching Indodax coins...")
summaries = fetch("https://indodax.com/api/summaries", delay=1)
indodax_coins = set()
for pair in summaries.get("tickers", {}).keys():
    if pair.endswith("_idr"):
        indodax_coins.add(pair[:-4].upper())
print(f"  Indodax coins: {len(indodax_coins)}")

# Step 2: Get CoinGecko coin list and build symbol -> ID mapping
print("Step 2: Fetching CoinGecko coin list...")
cg_list = fetch("https://api.coingecko.com/api/v3/coins/list", delay=2)
# Build mapping: symbol.upper() -> id (take first match)
cg_map = {}
for coin in cg_list:
    sym = coin["symbol"].upper()
    if sym not in cg_map:
        cg_map[sym] = coin["id"]
print(f"  CoinGecko coins: {len(cg_list)}")

# Step 3: Find overlap
overlap = indodax_coins & set(cg_map.keys())
only_indodax = indodax_coins - set(cg_map.keys())
print(f"  On both Indodax + CoinGecko: {len(overlap)}")
print(f"  Indodax-only (no CoinGecko): {len(only_indodax)}")
print(f"  Indodax-only coins: {', '.join(sorted(only_indodax))}")

# Step 4: Test candle fetch for 5 coins
print("\nStep 3: Testing OHLCV fetch for sample coins...")
test_coins = list(overlap)[:5]
for sym in test_coins:
    cg_id = cg_map[sym]
    url = (f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
           f"?vs_currency=usd&days=2")
    try:
        data = fetch(url, delay=2)
        if data and len(data) > 0:
            print(f"  {sym} ({cg_id}): {len(data)} candles ✅")
        else:
            print(f"  {sym}: empty ❌")
    except Exception as e:
        print(f"  {sym}: {str(e)[:50]} ❌")

print("\nDone.")
