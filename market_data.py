import requests
import time
from loguru import logger

class MarketData:
    def __init__(self):
        self._cache = {}
        self._cache_ttl = 60  # 1 minute

    def get_btc_data(self) -> dict:
        """
        Fetches BTC price and 24h change from Binance API.
        Returns a dict: {'price': 63850.50, 'change_pct': 1.2}
        """
        now = time.time()
        if 'btc' in self._cache:
            data, timestamp = self._cache['btc']
            if now - timestamp < self._cache_ttl:
                return data

        try:
            url = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            json_data = response.json()
            
            price = float(json_data['lastPrice'])
            change_pct = float(json_data['priceChangePercent'])
            
            result = {'price': price, 'change_pct': change_pct}
            self._cache['btc'] = (result, now)
            return result
        except Exception as e:
            logger.error(f"Failed to fetch market data from Binance: {e}")
            # Fallback data just in case
            if 'btc' in self._cache:
                return self._cache['btc'][0]
            return {'price': 0.0, 'change_pct': 0.0}

market_data = MarketData()

if __name__ == "__main__":
    print(market_data.get_btc_data())
