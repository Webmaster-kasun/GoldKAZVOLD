"""
OANDA Trade Executor
- Retry + backoff on rate limit (429)
- 0.5s delay between all API calls to avoid rate limiting
- Balance cached from login
"""

import os
import time
import requests
import logging

log = logging.getLogger(__name__)

CALL_DELAY = 0.5  # seconds between API calls — prevents rate limiting


class OandaTrader:
    def __init__(self, demo=True):
        self.api_key     = os.environ.get("OANDA_API_KEY", "")
        self.account_id  = os.environ.get("OANDA_ACCOUNT_ID", "")
        self.demo        = demo
        self.base_url    = "https://api-fxpractice.oanda.com" if demo else "https://api-trade.oanda.com"
        self.headers     = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json"
        }
        self.last_balance = 0.0
        log.info(f"OANDA | Mode: {'DEMO/PRACTICE' if demo else 'LIVE'}")
        log.info(f"OANDA | URL:  {self.base_url}")
        log.info(f"Account: {self.account_id}")
        if self.api_key:
            log.info(f"API Key: {self.api_key[:8]}****")
        else:
            log.error("API Key is EMPTY — check OANDA_API_KEY environment variable!")

    def _get(self, url, params=None, retries=3):
        """GET with delay + retry on rate limit."""
        time.sleep(CALL_DELAY)
        for attempt in range(retries):
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code == 429:
                    wait = 15 * (attempt + 1)
                    log.warning(f"Rate limited (429) — waiting {wait}s before retry {attempt+1}/{retries}")
                    time.sleep(wait)
                    continue
                return r
            except requests.exceptions.Timeout:
                log.warning(f"Timeout attempt {attempt+1}/{retries}")
                time.sleep(5)
            except Exception as e:
                log.warning(f"Request error attempt {attempt+1}: {e}")
                time.sleep(3)
        return None

    def login(self):
        if not self.api_key:
            log.error("Login failed: OANDA_API_KEY is empty")
            return False
        if not self.account_id:
            log.error("Login failed: OANDA_ACCOUNT_ID is empty")
            return False
        try:
            url = f"{self.base_url}/v3/accounts/{self.account_id}"
            r   = self._get(url)
            if r is None:
                log.error("Login failed: no response after retries")
                return False
            log.info(f"Login status: {r.status_code}")
            log.info(f"Login response: {r.text[:200]}")
            if r.status_code == 200:
                self.last_balance = float(r.json()["account"]["balance"])
                log.info(f"Login success! Balance: ${self.last_balance:.2f}")
                return True
            elif r.status_code == 401:
                log.error("401 Unauthorized — API key wrong or expired")
            elif r.status_code == 403:
                log.error("403 Forbidden — Account ID may be wrong")
            else:
                log.error(f"Login failed: {r.status_code} {r.text}")
            return False
        except Exception as e:
            log.error(f"Login error: {e}")
            return False

    def get_balance(self):
        """Returns cached balance — avoids extra API call."""
        return self.last_balance

    def get_price(self, instrument):
        try:
            r = self._get(
                f"{self.base_url}/v3/accounts/{self.account_id}/pricing",
                params={"instruments": instrument}
            )
            if r and r.status_code == 200:
                price = r.json()["prices"][0]
                bid   = float(price["bids"][0]["price"])
                ask   = float(price["asks"][0]["price"])
                return (bid + ask) / 2, bid, ask
        except Exception as e:
            log.error(f"get_price error: {e}")
        return None, None, None

    def get_position(self, instrument):
        try:
            r = self._get(f"{self.base_url}/v3/accounts/{self.account_id}/positions/{instrument}")
            if r and r.status_code == 200:
                pos         = r.json()["position"]
                long_units  = int(float(pos["long"]["units"]))
                short_units = int(float(pos["short"]["units"]))
                if long_units != 0 or short_units != 0:
                    return pos
            return None
        except Exception as e:
            log.error(f"get_position error: {e}")
            return None

    def check_pnl(self, position):
        try:
            long_pnl  = float(position["long"].get("unrealizedPL", 0))
            short_pnl = float(position["short"].get("unrealizedPL", 0))
            return long_pnl + short_pnl
        except:
            return 0

    def place_order(self, instrument, direction, size, stop_distance, limit_distance):
        try:
            units       = size if direction == "BUY" else -size
            price, bid, ask = self.get_price(instrument)
            if price is None:
                return {"success": False, "error": "Cannot get price"}

            pip       = 0.01 if instrument == "XAU_USD" else (0.01 if "JPY" in instrument else 0.0001)
            precision = 2    if instrument == "XAU_USD" else (3    if "JPY" in instrument else 5)
            entry     = ask if direction == "BUY" else bid

            if direction == "BUY":
                sl_price = round(entry - stop_distance  * pip, precision)
                tp_price = round(entry + limit_distance * pip, precision)
            else:
                sl_price = round(entry + stop_distance  * pip, precision)
                tp_price = round(entry - limit_distance * pip, precision)

            log.info(f"Placing {direction} {instrument} | units={units} | entry={entry} | SL={sl_price} | TP={tp_price}")

            time.sleep(CALL_DELAY)
            r = requests.post(
                f"{self.base_url}/v3/accounts/{self.account_id}/orders",
                headers=self.headers,
                json={"order": {
                    "type":        "MARKET",
                    "instrument":  instrument,
                    "units":       str(units),
                    "timeInForce": "FOK",
                    "stopLossOnFill":    {"price": str(sl_price), "timeInForce": "GTC"},
                    "takeProfitOnFill":  {"price": str(tp_price), "timeInForce": "GTC"}
                }},
                timeout=15
            )
            data = r.json()
            log.info(f"Order response: {r.status_code} {str(data)[:200]}")

            if r.status_code in [200, 201]:
                if "orderFillTransaction" in data:
                    return {"success": True, "trade_id": data["orderFillTransaction"].get("id", "N/A")}
                elif "orderCancelTransaction" in data:
                    return {"success": False, "error": "Order cancelled: " + data["orderCancelTransaction"].get("reason", "Unknown")}
                return {"success": True}
            return {"success": False, "error": data.get("errorMessage", str(data))}

        except Exception as e:
            log.error(f"place_order error: {e}")
            return {"success": False, "error": str(e)}

    def close_position(self, instrument):
        try:
            time.sleep(CALL_DELAY)
            r = requests.put(
                f"{self.base_url}/v3/accounts/{self.account_id}/positions/{instrument}/close",
                headers=self.headers,
                json={"longUnits": "ALL", "shortUnits": "ALL"},
                timeout=15
            )
            return {"success": r.status_code == 200}
        except Exception as e:
            log.error(f"close_position error: {e}")
            return {"success": False}
