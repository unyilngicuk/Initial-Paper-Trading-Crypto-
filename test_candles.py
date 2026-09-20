import urllib.request, json, time

INDODAX_BASE = "https://indodax.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

coins = ["br", "avax", "near", "syn"]

for coin in coins:
    now_ts = int(time.time())
    from_ts = now_ts - 25 * 3600
    url = (f"{INDODAX_BASE}/tradingview/history"
           f"?symbol={coin.upper()}_IDR&resolution=60"
           f"&from={from_ts}&to={now_ts}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode().strip()
        if raw and raw != "OK":
            data = json.loads(raw)
            closes = data.get("c", [])
            print(f"{coin.upper()}: {len(closes)} candles ✅")
        else:
            print(f"{coin.upper()}: returned '{raw}' ❌")
    except Exception as e:
        print(f"{coin.upper()}: {e} ❌")
    time.sleep(1)
