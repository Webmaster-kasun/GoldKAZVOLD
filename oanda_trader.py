"""
OANDA Trade Executor
- Stop Loss + Take Profit set automatically on every order
- Caches balance from login to avoid redundant API calls
- Retry with backoff on 429 rate limit responses
"""

import os
import time
import requests
import logging

log = logging.getLogger(__name__)


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
        self.last_balance = 0.0   # cached from login — avoids extra API call
        log.info(f"OANDA | Mode: {'DEMO/PRACTICE' if demo else 'LIVE'}")
        log.info(f"OANDA | URL:  {self.base_url}")
        log.info(f"Account: {self.account_id}")
        if self.api_key:
            log.info(f"API Key: {self.api_key[:8]}****")
        else:
            log.error("API Key is EMPTY — check OANDA_API_KEY environment variable!")

    def _get(self, url, params=None, retries=3):
        """GET with retry + backoff on rate limit (429) or transient errors."""
        for attempt in range(retries):
            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code == 429:
                    wait = 10 * (attempt + 1)
                    log.warning(f"Rate limited (429) — waiting {wait}s before retry {attempt+1}/{retries}")
                    time.sleep(wait)
                    continue
                return r
            except requests.exceptions.Timeout:
                log.warning(f"Timeout on attempt {attempt+1}/{retries} — {url}")
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
            log.info(f"Login response: {r.text[:300]}")
            if r.status_code == 200:
                self.last_balance = float(r.json()["account"]["balance"])
                log.info(f"Login success! Balance: ${self.last_balance:.2f}")
                return True
            elif r.status_code == 401:
                log.error("Login failed: 401 Unauthorized — API key wrong or expired")
                log.error("Check: Practice key needs demo_mode=true | Live key needs demo_mode=false")
            elif r.status_code == 403:
                log.error("Login failed: 403 Forbidden — Account ID may be wrong")
            else:
                log.error(f"Login failed: {r.status_code} {r.text}")
            return False
        except Exception as e:
            log.error(f"Login error: {e}")
            return False

    def get_balance(self):
        try:
            r = self._get(f"{self.base_url}/v3/accounts/{self.account_id}")
            if r and r.status_code == 200:
                self.last_balance = float(r.json()["account"]["balance"])
                log.info(f"Balance: ${self.last_balance:.2f}")
                return self.last_balance
        except Exception as e:
            log.error(f"get_balance error: {e}")
        return self.last_balance  # return cached value on failure

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
                mid   = (bid + ask) / 2
                return mid, bid, ask
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

    def place_order(self, instrument, direction, size, stop_distance, limit_distance, currency="USD"):
        try:
            units = size if direction == "BUY" else -size

            price, bid, ask = self.get_price(instrument)
            if price is None:
                return {"success": False, "error": "Cannot get price"}

            if instrument == "XAU_USD":
                pip       = 0.01
                precision = 2
            elif "JPY" in instrument:
                pip       = 0.01
                precision = 3
            else:
                pip       = 0.0001
                precision = 5

            entry = ask if direction == "BUY" else bid

            if direction == "BUY":
                sl_price = round(entry - (stop_distance  * pip), precision)
                tp_price = round(entry + (limit_distance * pip), precision)
            else:
                sl_price = round(entry + (stop_distance  * pip), precision)
                tp_price = round(entry - (limit_distance * pip), precision)

            log.info(f"Placing {direction} {instrument} | units={units} | entry={entry} | SL={sl_price} | TP={tp_price}")

            payload = {
                "order": {
                    "type":        "MARKET",
                    "instrument":  instrument,
                    "units":       str(units),
                    "timeInForce": "FOK",
                    "stopLossOnFill": {
                        "price":       str(sl_price),
                        "timeInForce": "GTC"
                    },
                    "takeProfitOnFill": {
                        "price":       str(tp_price),
                        "timeInForce": "GTC"
                    }
                }
            }

            r    = requests.post(
                f"{self.base_url}/v3/accounts/{self.account_id}/orders",
                headers=self.headers,
                json=payload,
                timeout=15
            )
            data = r.json()
            log.info(f"Order response: {r.status_code} {str(data)[:300]}")

            if r.status_code in [200, 201]:
                if "orderFillTransaction" in data:
                    trade_id = data["orderFillTransaction"].get("id", "N/A")
                    log.info(f"Trade placed! ID: {trade_id}")
                    return {"success": True, "trade_id": trade_id}
                elif "orderCancelTransaction" in data:
                    reason = data["orderCancelTransaction"].get("reason", "Unknown")
                    return {"success": False, "error": f"Order cancelled: {reason}"}
                return {"success": True}
            else:
                error = data.get("errorMessage", str(data))
                return {"success": False, "error": error}

        except Exception as e:
            log.error(f"place_order error: {e}")
            return {"success": False, "error": str(e)}

    def close_position(self, instrument):
        try:
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
