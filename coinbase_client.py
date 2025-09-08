import requests

COINBASE_URL = "https://api.exchange.coinbase.com/products/ETH-USD/ticker"

def fetch_eth_usd_price():
    """
    Fetch current ETH/USD price from Coinbase public API.
    No API key required.
    """
    try:
        r = requests.get(COINBASE_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["price"])
    except Exception as e:
        return None