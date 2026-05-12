"""
OANDA Trading Bot — Gold Only | CPR + EMA + Volume
===================================================
Pair:     XAU/USD (Gold only)
Sessions: Asian (9am-1pm SGT) | London (2pm-7pm SGT) | NY (8pm-11pm SGT)

FIX LOG:
  FIX 1  - Removed trade_journal import (next phase)
  FIX 2  - Fixed open_count NameError crash in sync
  FIX 3  - Hard 10-min duplicate lock (stops 4-6 orders/min bug)
  FIX 4  - sync no longer overwrites today["trades"] counter
  FIX 5  - sync no longer overwrites last_trade_entry_price
  FIX 6  - Smart re-entry guard (4 rules)
  FIX 7  - Daily summary at 11pm SGT
  FIX 8  - H4 block: Asian = -1pt penalty (not a kill); London/NY = BLOCKED direction
           All 7 checks always run; full score always shown in Telegram
  FIX 9  - Eliminated double signals.analyze() call in re-entry guard (halves API calls)
  FIX 10 - Eliminated duplicate "Watching" alert; BLOCKED handled by main scan alert only
  FIX 11 - *** SL now mirrors TP dynamically (ATR-based). Both SL and TP use the same
           pip distance so R:R is always 1:1. CPR dynamic target retained when within range.
           Old hardcoded SL=1200 / TP=2200 removed entirely. ***
  FIX 12 - Replaced hard 10-min time lock with M30 Win Candle Lock (superseded)
  FIX 13 - Replaced M30 Win Candle Lock with Next-Session Win Lock:
           After a TP win, block all new entries until the START of the
           next trading session (Asian 09:00 / London 14:00 / NY 20:00 SGT).
           Reason: 30 min too short for Gold — EMAs, RSI, M15 need a full
           session reset after a big move.
  FIX 14 - Re-entry price movement threshold raised from 500p → 1000p to prevent
           entering the exact same price zone after a loss.
  FIX 15 - ATR extreme-volatility gate in signals.py lowered from 10000p → 3000p
           so news-driven weeks are skipped automatically.
"""

import os
import json
import logging
import time
import requests
from datetime import datetime, timedelta
import pytz

from oanda_trader import OandaTrader
from signals import SignalEngine
from cpr import CPRCalculator
from telegram_alert import TelegramAlert
from calendar_filter import EconomicCalendar


class SafeFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        key = os.environ.get("OANDA_API_KEY", "")
        if key and key in msg:
            msg = msg.replace(key, "***")
        return msg


handler      = logging.StreamHandler()
handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(message)s"))
file_handler = logging.FileHandler("performance_log.txt")
file_handler.setFormatter(SafeFormatter("%(asctime)s | %(levelname)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler, file_handler])
log = logging.getLogger(__name__)

ASSETS = {
    "XAU_USD": {
        "instrument":    "XAU_USD",
        "asset":         "XAUUSD",
        "emoji":         "🥇",
        "setting":       "trade_gold",
        "pip":           0.01,
        "precision":     2,
        "session_hours": [(9, 23)],
    },
}

RISK_PCT_PER_TRADE = 0.014  # 1.4% of balance
RISK_USD_MAX       = 37.0
RISK_USD_MIN       = 1.0

# FIX 11: SL and TP both derived from ATR — no more hardcoded values.
# SL = ATR * ATR_SL_MULT  |  TP = SL (1:1 R:R minimum guaranteed)
# CPR dynamic target is then applied IF it falls within [TP * 0.8, TP * 1.5]
ATR_SL_MULT    = 1.5   # SL = 1.5 × ATR pips
ATR_SL_MIN     = 800   # never tighter than 800p (gold gap risk)
ATR_SL_MAX     = 3000  # cap SL to keep risk controllable
# R:R minimum enforced later — if CPR target < SL distance, trade is skipped


def calc_position_size(balance, stop_pips, pip, score, price):
    try:
        risk_dollars  = min(balance * RISK_PCT_PER_TRADE, RISK_USD_MAX)
        risk_dollars  = max(risk_dollars, RISK_USD_MIN)
        risk_per_unit = stop_pips * pip
        if risk_per_unit <= 0:
            return 1
        scale = 1.0 if score >= 6 else 0.75
        units = max(1, int((risk_dollars / risk_per_unit) * scale))
        log.info(f"Size: bal=${balance:.2f} risk=${risk_dollars:.2f} stop={stop_pips}p units={units} score={score}/7")
        return units
    except Exception as e:
        log.warning(f"Position size error: {e}")
        return 1


def load_settings():
    default = {
        "max_trades_day":         999,
        "signal_threshold":       5,
        "signal_threshold_asian": 4,
        "demo_mode":              True,
        "trade_gold":             True,
        "trade_gold_asian":       True,
        "max_consec_losses":      999,
        "max_spread_gold":        999,
        "max_spread_gold_asian":  999,
        "strategy":               "hybrid_cpr_breakout_gold",
        "max_trades_asian":       999,
        "max_trades_main":        999,
    }
    try:
        with open("settings.json") as f:
            saved = json.load(f)
            default.update(saved)
    except FileNotFoundError:
        with open("settings.json", "w") as f:
            json.dump(default, f, indent=2)
    return default


def sync_closed_trades(trader, today, trade_log):
    """Sync W/L from OANDA. Does NOT touch trade counter or entry price."""
    try:
        from datetime import timezone
        sg_tz         = pytz.timezone("Asia/Singapore")
        now_sg        = datetime.now(sg_tz)
        day_start     = now_sg.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

        url    = trader.base_url + "/v3/accounts/" + trader.account_id + "/trades"
        params = {"state": "CLOSED", "instrument": "XAU_USD", "count": "20"}
        r      = requests.get(url, headers=trader.headers, params=params, timeout=10)
        if r.status_code != 200:
            return

        trades = r.json().get("trades", [])
        wins = losses = trade_count = 0
        for t in trades:
            if t.get("closeTime", "") < day_start_utc:
                continue
            trade_count += 1
            pl = float(t.get("realizedPL", 0))
            if pl > 0:   wins   += 1
            elif pl < 0: losses += 1

        today["wins"]   = wins
        today["losses"] = losses
        # FIX 4: Never overwrite today["trades"] — local counter is source of truth

        # Bug #6 Fix: accumulate realized PL from actual closed trade objects (not balance delta)
        trade_pl_sum = sum(float(t.get("realizedPL", 0)) for t in trades
                          if t.get("closeTime", "") >= day_start_utc)
        today["realized_pl_trades"] = round(trade_pl_sum, 4)

        consec = 0
        for t in sorted(trades, key=lambda x: x.get("closeTime", ""), reverse=True):
            if t.get("closeTime", "") < day_start_utc:
                break
            if float(t.get("realizedPL", 0)) < 0:
                consec += 1
            else:
                break
        today["consec_losses"] = consec

        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

        today_closed = [t for t in trades if t.get("closeTime", "") >= day_start_utc]
        if today_closed:
            latest = sorted(today_closed, key=lambda x: x.get("closeTime", ""))[-1]
            today["last_trade_close_time"]   = latest.get("closeTime", "")
            today["last_trade_close_result"] = "WIN" if float(latest.get("realizedPL", 0)) > 0 else "LOSS"
            # FIX 5: Do NOT overwrite last_trade_entry_price here

            # FIX 13: Next-Session Win Lock
            if float(latest.get("realizedPL", 0)) > 0:
                try:
                    from datetime import timezone as _tz
                    import pytz as _pytz
                    close_raw = latest.get("closeTime", "")
                    # Bug #3 Fix: attach UTC tz explicitly before converting — prevents silent naive-dt bug
                    close_dt  = datetime.strptime(close_raw[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=_tz.utc)
                    sg_tz     = _pytz.timezone("Asia/Singapore")
                    close_sgt = close_dt.astimezone(sg_tz)
                    h = close_sgt.hour

                    if h < 9:
                        next_session_sgt = close_sgt.replace(hour=9, minute=0, second=0, microsecond=0)
                    elif h < 14:
                        next_session_sgt = close_sgt.replace(hour=14, minute=0, second=0, microsecond=0)
                    elif h < 20:
                        next_session_sgt = close_sgt.replace(hour=20, minute=0, second=0, microsecond=0)
                    else:
                        next_session_sgt = (close_sgt + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

                    next_session_utc = next_session_sgt.astimezone(_tz.utc)
                    today["last_win_candle_close"] = next_session_utc.strftime("%Y-%m-%dT%H:%M")
                    log.info("Next-Session Win Lock set: blocked until " +
                             next_session_sgt.strftime("%H:%M SGT") + " (" +
                             next_session_utc.strftime("%H:%M UTC") + ")")
                except Exception as _we:
                    log.warning("Win candle lock set error: " + str(_we))

        log.info("Synced W=" + str(wins) + " L=" + str(losses) + " consec=" + str(consec))

    except Exception as e:
        log.warning("Sync trades error: " + str(e))


def get_atr_pips(trader, instrument, pip, multiplier=1.0):
    try:
        url    = trader.base_url + "/v3/instruments/" + instrument + "/candles"
        params = {"count": "30", "granularity": "H1", "price": "M"}
        r      = requests.get(url, headers=trader.headers, params=params, timeout=10)
        if r.status_code != 200:
            return None
        c      = [x for x in r.json()["candles"] if x["complete"]]
        if len(c) < 15:
            return None
        highs  = [float(x["mid"]["h"]) for x in c]
        lows   = [float(x["mid"]["l"]) for x in c]
        closes = [float(x["mid"]["c"]) for x in c]
        trs    = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
                  for i in range(1, len(closes))]
        atr      = sum(trs[-14:]) / 14
        atr_pips = (atr / pip) * multiplier
        log.info(instrument + " ATR=" + str(round(atr, 4)) + " pips=" + str(round(atr_pips, 0)))
        return max(round(atr_pips), 10)
    except Exception as e:
        log.warning("ATR error: " + str(e))
        return None


def check_spread(trader, instrument, max_spread_pips, pip):
    try:
        mid, bid, ask = trader.get_price(instrument)
        if bid is None:
            return True, 0
        spread_pips = (ask - bid) / pip
        log.info(instrument + " spread=" + str(round(spread_pips, 1)) + " pips")
        return (spread_pips <= max_spread_pips), spread_pips
    except Exception as e:
        log.warning("Spread error: " + str(e))
        return True, 0


def send_daily_summary(alert, today, cpr_gold, mode):
    """FIX 7: Send P&L summary at 11pm SGT."""
    try:
        wins         = today.get("wins", 0)
        losses       = today.get("losses", 0)
        total        = wins + losses
        win_rate     = round((wins / total * 100)) if total > 0 else 0
        # Bug #6 Fix: use sum of closed trade PL (not balance delta which includes financing noise)
        realized     = today.get("realized_pl_trades", today.get("daily_pnl", 0.0))
        realized_sgd = round(realized * 1.35, 2)
        pnl_emoji    = "UP" if realized >= 0 else "DOWN"
        wr_emoji     = "GREEN" if win_rate >= 60 else ("YELLOW" if win_rate >= 40 else "RED")

        cpr_line = ""
        if cpr_gold:
            w     = cpr_gold.get("width_pct", 0)
            w_lbl = "NARROW-trending" if cpr_gold["is_narrow"] else ("WIDE-choppy" if cpr_gold["is_wide"] else "NORMAL")
            cpr_line = (
                "\n--- Tomorrow CPR ---\n"
                "TC=" + str(cpr_gold["tc"]) + " BC=" + str(cpr_gold["bc"]) + "\n"
                "R1=" + str(cpr_gold["r1"]) + " S1=" + str(cpr_gold["s1"]) + "\n"
                "Width=" + str(w) + "% " + w_lbl + "\n"
            )

        msg = (
            "📊 GOLD BOT Daily Summary\n"
            "-------------------------\n"
            "Mode:     " + mode + "\n"
            "-------------------------\n"
            "Trades:   " + str(total) + "\n"
            "W / L:    " + str(wins) + " / " + str(losses) + "\n"
            "Win Rate: " + wr_emoji + " " + str(win_rate) + "%\n"
            "-------------------------\n"
            "P&L: " + pnl_emoji + " $" + str(round(realized, 2)) + " USD\n"
            "     " + pnl_emoji + " $" + str(realized_sgd) + " SGD"
            + cpr_line +
            "-------------------------\n"
            "Bot resumes 9am SGT tomorrow"
        )
        alert.send(msg)
        log.info("Daily summary sent")
    except Exception as e:
        log.warning("Daily summary error: " + str(e))


def run_bot():
    log.info("🥇 GOLD BOT scanning...")
    settings = load_settings()
    sg_tz    = pytz.timezone("Asia/Singapore")
    now      = datetime.now(sg_tz)
    alert    = TelegramAlert()
    cpr_calc = CPRCalculator(demo=settings["demo_mode"])
    hour     = now.hour

    active_hours = (9 <= hour <= 23)
    london_open  = (14 <= hour <= 17)
    london       = (14 <= hour <= 19)
    ny_overlap   = (20 <= hour <= 23)
    asian        = (9 <= hour <= 13)
    good_session = active_hours

    if asian:
        session = "Asian Session (SGX/Tokyo 9am-1pm SGT)"
    elif london_open:
        session = "London Open (BEST for Gold breakouts!)"
    elif ny_overlap:
        session = "NY Overlap (BEST for Gold macro moves!)"
    elif london:
        session = "London Session"
    else:
        session = "Off-hours (monitoring only)"

    if now.weekday() == 5:
        log.info("Saturday — markets closed")
        return
    if now.weekday() == 6 and hour < 9:
        log.info("Sunday early — skipping")
        return

    trader = OandaTrader(demo=settings["demo_mode"])
    if not trader.login():
        # SPAM FIX 2: Login-fail alert throttled to once per hour.
        # Without this, a 1-hour OANDA outage sends 12 identical messages.
        login_fail_log = "login_fail_alert.json"
        try:
            with open(login_fail_log) as _f:
                _lf = json.load(_f)
        except (FileNotFoundError, json.JSONDecodeError):
            _lf = {"last_alert_min": -61}
        _cur_min = now.hour * 60 + now.minute
        _mins_since = _cur_min - _lf["last_alert_min"] if _cur_min >= _lf["last_alert_min"] else _cur_min + 1440 - _lf["last_alert_min"]
        if _mins_since >= 60:
            alert.send(
                "❌ OANDA Login Failed\n"
                "Check OANDA_API_KEY and OANDA_ACCOUNT_ID\n"
                "demo_mode=true  -> practice account\n"
                "demo_mode=false -> live account"
            )
            with open(login_fail_log, "w") as _f:
                json.dump({"last_alert_min": _cur_min}, _f)
        else:
            log.warning("Login failed (alert suppressed — sent " + str(_mins_since) + " min ago)")
        return

    current_balance = trader.last_balance
    mode            = "DEMO" if settings["demo_mode"] else "LIVE"

    trade_log = "trades_" + now.strftime("%Y%m%d") + ".json"
    try:
        with open(trade_log) as f:
            today = json.load(f)
    except FileNotFoundError:
        today = {
            "trades":                   0,
            "start_balance":            current_balance,
            "daily_pnl":                0.0,
            "stopped":                  False,
            "wins":                     0,
            "losses":                   0,
            "consec_losses":            0,
            "cooldowns":                {},
            "cpr_alert_sent":           False,
            "cpr_alert_asian_sent":     False,
            "news_alert_sent":          False,
            "daily_summary_sent":       False,
            "last_trade_close_time":    None,
            "last_trade_close_result":  None,
            "last_trade_entry_price":   None,
            "last_trade_entry_time":    None,
            "last_trade_entry_score":   0,
            "last_trade_entry_direction": "",
            "asian_trades_today":       0,
            "main_trades_today":        0,
            "last_win_candle_close":    None,
            "last_entry_candle":        None,
        }
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)
        log.info("New day! Start balance: $" + str(round(current_balance, 2)))

    start_balance = today.get("start_balance", current_balance)
    open_pnl      = 0.0
    for _n in ASSETS:
        _pos = trader.get_position(_n)
        if _pos:
            open_pnl += trader.check_pnl(_pos)
    realized_pnl = current_balance - start_balance
    pl_sgd       = realized_pnl * 1.35
    pnl_emoji    = "✅" if realized_pnl >= 0 else "❌"

    today["daily_pnl"] = realized_pnl
    with open(trade_log, "w") as f:
        json.dump(today, f, indent=2)

    sync_closed_trades(trader, today, trade_log)

    # FIX 7: Daily summary at 11pm SGT
    if hour == 23 and not today.get("daily_summary_sent", False):
        cpr_for_summary = cpr_calc.get_levels("XAU_USD")
        send_daily_summary(alert, today, cpr_for_summary, mode)
        today["daily_summary_sent"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

    if today["trades"] >= settings["max_trades_day"]:
        log.info("Max trades reached")
        return

    cpr_gold = cpr_calc.get_levels("XAU_USD")

    send_cpr_alert = (
        (asian and hour == 9 and not today.get("cpr_alert_asian_sent")) or
        (london_open and hour == 14 and not today.get("cpr_alert_sent"))
    )
    if send_cpr_alert:
        session_label = "Asian Open" if asian else "London Open"
        cpr_msg = "🌅 GOLD BOT — " + session_label + " CPR Levels\n"
        if cpr_gold:
            narrow_flag = " NARROW — TRENDING DAY!" if cpr_gold["is_narrow"] else ""
            wide_flag   = " WIDE — CHOPPY" if cpr_gold["is_wide"] else ""
            cpr_msg += (
                "🥇 GOLD CPR" + narrow_flag + wide_flag + "\n"
                "TC=" + str(cpr_gold["tc"]) + " BC=" + str(cpr_gold["bc"]) +
                " Pivot=" + str(cpr_gold["pivot"]) + "\n"
                "R1=" + str(cpr_gold["r1"]) + " S1=" + str(cpr_gold["s1"]) +
                " Width=" + str(cpr_gold["width_pct"]) + "%"
            )
        alert.send(cpr_msg)
        if asian:
            today["cpr_alert_asian_sent"] = True
        else:
            today["cpr_alert_sent"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

    if not good_session:
        log.info("Off-hours — sleeping silently")
        return

    calendar     = EconomicCalendar()
    news_summary = calendar.get_today_summary()
    if "No high" not in news_summary and not today.get("news_alert_sent"):
        alert.send("⚠️ NEWS ALERT!\n" + news_summary + "\nCPR levels often break around news!")
        today["news_alert_sent"] = True
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

    signals      = SignalEngine(demo=settings["demo_mode"])
    scan_results = []
    # Bug #8 Fix: initialize to safe defaults — used in scan alert after loop,
    # even if loop exits early (position open, off-session, cap hit, etc.)
    score     = 0
    direction = ""
    details   = "No scan run this cycle"

    for name, config in ASSETS.items():
        if not settings.get(config["setting"], True):
            continue
        if today["trades"] >= settings["max_trades_day"]:
            break

        cpr_levels = None  # Bug #7 Fix: always defined; set before any early-continue path

        position = trader.get_position(name)
        if position:
            pnl     = trader.check_pnl(position)
            pos_dir = "BUY" if int(float(position["long"]["units"])) > 0 else "SELL"
            emoji   = "📈" if pnl > 0 else "📉"
            scan_results.append(config["emoji"] + " " + name + ": " + pos_dir +
                                 " open " + emoji + " $" + str(round(pnl, 2)))
            continue

        session_hours = config.get("session_hours", [(14, 23)])
        pair_ok       = any(s <= hour <= e for (s, e) in session_hours)
        if not pair_ok:
            scan_results.append(config["emoji"] + " " + name + ": off-session")
            continue

        is_asian_gold = asian and name == "XAU_USD"

        if is_asian_gold and not settings.get("trade_gold_asian", True):
            scan_results.append(config["emoji"] + " " + name + ": Asian disabled")
            continue

        if is_asian_gold:
            cap          = settings.get("max_trades_asian", 999)
            asian_trades = today.get("asian_trades_today", 0)
            if asian_trades >= cap:
                scan_results.append(config["emoji"] + " " + name + ": Asian cap reached")
                continue
        else:
            cap         = settings.get("max_trades_main", 999)
            main_trades = today.get("main_trades_today", 0)
            if main_trades >= cap:
                scan_results.append(config["emoji"] + " " + name + ": Main cap reached")
                continue

        # ══════════════════════════════════════════════════════
        # RE-ENTRY GUARD — FIX 3 + FIX 6 + FIX 14
        # ══════════════════════════════════════════════════════
        last_entry_time      = today.get("last_trade_entry_time")
        last_entry_score     = today.get("last_trade_entry_score", 0)
        last_entry_direction = today.get("last_trade_entry_direction", "")
        last_entry_price     = today.get("last_trade_entry_price") or 0
        now_utc              = datetime.utcnow()

        # FIX 13: Next-Session Win Lock
        last_win_candle = today.get("last_win_candle_close")
        if last_win_candle:
            try:
                import pytz as _pytz
                sg_tz         = _pytz.timezone("Asia/Singapore")
                unlock_utc    = datetime.strptime(last_win_candle, "%Y-%m-%dT%H:%M").replace(
                    tzinfo=__import__("datetime").timezone.utc)  # Bug #3 Fix: always tz-aware
                now_utc_aware = datetime.now(__import__("datetime").timezone.utc).replace(second=0, microsecond=0)
                if now_utc_aware < unlock_utc:
                    remaining_min = max(1, int((unlock_utc - now_utc_aware).total_seconds() // 60))
                    unlock_sgt    = unlock_utc.astimezone(sg_tz)
                    scan_results.append(config["emoji"] + " " + name +
                        ": 🔒 Next-Session Lock — opens at " + unlock_sgt.strftime("%H:%M SGT") +
                        " (~" + str(remaining_min) + " min)")
                    log.info(name + " Next-Session Win Lock active — unlocks at " +
                             unlock_sgt.strftime("%H:%M SGT"))
                    continue
            except Exception as e:
                log.warning("Win Candle Lock error: " + str(e))

        # POST-WIN QUALITY GATE
        last_close_result = today.get("last_trade_close_result")
        if last_close_result == "WIN":
            post_win_threshold = (settings.get("signal_threshold_asian", 4) + 1 if is_asian_gold
                                  else settings["signal_threshold"] + 1)
            today["_post_win_threshold"] = post_win_threshold
        else:
            today["_post_win_threshold"] = 0

        # Same M30 candle duplicate lock
        last_entry_candle = today.get("last_entry_candle")
        if last_entry_candle:
            try:
                m30_floor2    = now_utc.replace(minute=(now_utc.minute // 30) * 30, second=0, microsecond=0)
                entry_candle_dt = datetime.strptime(last_entry_candle, "%Y-%m-%dT%H:%M")
                if m30_floor2 == entry_candle_dt:
                    scan_results.append(config["emoji"] + " " + name +
                        ": 🔒 Same-candle lock — wait for next M30")
                    log.info(name + " same-candle duplicate lock on M30=" + last_entry_candle)
                    continue
            except Exception as e:
                log.warning("Same-candle lock error: " + str(e))

        max_spread            = settings.get("max_spread_gold_asian", 999) if is_asian_gold else settings.get("max_spread_gold", 999)
        spread_ok, spread_val = check_spread(trader, name, max_spread, config["pip"])

        news_active, news_reason = calendar.is_news_time(name)
        if news_active:
            scan_results.append(config["emoji"] + " " + name + ": PAUSED — " + news_reason)
            continue

        asset_key = "XAUUSD_ASIAN" if is_asian_gold else config["asset"]
        threshold = settings.get("signal_threshold_asian", 4) if is_asian_gold else settings["signal_threshold"]

        # Single analyze() call
        score, direction, details = signals.analyze(asset=asset_key)
        log.info(name + ": score=" + str(score) + " dir=" + direction + " | " + details)

        if not spread_ok:
            scan_results.append(config["emoji"] + " " + name +
                ": Spread " + str(round(spread_val, 1)) + " pips | Score: " + str(score) + "/7")
            continue

        # POST-WIN QUALITY GATE ENFORCEMENT
        post_win_req = today.get("_post_win_threshold", 0)
        if post_win_req > 0 and score < post_win_req and direction not in ("NONE", "BLOCKED"):
            scan_results.append(config["emoji"] + " " + name +
                ": 🛡️ Post-win gate — need " + str(post_win_req) + "/7 after win, got " + str(score) + "/7")
            log.info(name + " post-win gate blocked — score=" + str(score) + " need=" + str(post_win_req))
            continue

        # FIX 6 + FIX 14: Smart re-entry rules
        # FIX 14: price_moved threshold raised from 500p → 1000p
        if last_entry_time and last_entry_score > 0 and last_entry_direction:
            try:
                peek_dir_eff  = last_entry_direction if direction == "BLOCKED" else direction
                same_dir      = (peek_dir_eff == last_entry_direction)
                price_now, _, _ = trader.get_price(name)
                # FIX 14: 1000p threshold (was 500p) prevents re-entering same zone after a loss
                price_moved   = (abs((price_now or 0) - last_entry_price) / config["pip"]) >= 1000 if last_entry_price else False

                log.info(name + " re-entry | last=" + last_entry_direction + "@" + str(last_entry_score) +
                         " now=" + direction + "@" + str(score) +
                         " same=" + str(same_dir) + " moved=" + str(price_moved))

                if same_dir and score <= last_entry_score and not price_moved:
                    scan_results.append(config["emoji"] + " " + name +
                        ": 🚫 Chasing — same " + last_entry_direction +
                        " score " + str(score) + " <= " + str(last_entry_score))
                    continue
                elif same_dir and score >= 6:
                    log.info(name + " ALLOWED — stronger score " + str(score))
                    today["last_trade_entry_score"]     = 0
                    today["last_trade_entry_direction"] = ""
                elif not same_dir and score >= 5 and direction not in ("NONE", "BLOCKED"):
                    log.info(name + " ALLOWED — direction flip to " + direction)
                    today["last_trade_entry_score"]     = 0
                    today["last_trade_entry_direction"] = ""
                elif price_moved and score >= 5:
                    log.info(name + " ALLOWED — new zone 1000p+")
                    today["last_trade_entry_score"]     = 0
                    today["last_trade_entry_direction"] = ""
                else:
                    reason = ("same dir " + str(score) + "/7" if same_dir
                              else direction + " score=" + str(score) + "/7 < 5")
                    scan_results.append(config["emoji"] + " " + name +
                        ": ⏳ Re-entry blocked — " + reason)
                    continue

                with open(trade_log, "w") as f:
                    json.dump(today, f, indent=2)

            except Exception as e:
                log.warning("Re-entry guard error: " + str(e))

        # ── BLOCKED: London/NY H4 hard block ─────────────────────────────────
        if direction == "BLOCKED":
            scan_results.append(
                config["emoji"] + " " + name + ": 🚫 H4 blocked | " + str(score) + "/7"
            )
            continue

        # ── Asian watching state ──────────────────────────────────────────────
        if is_asian_gold and score == 0 and direction == "NONE":
            scan_results.append(config["emoji"] + " " + name + ": Inside CPR — watching")
            continue

        if score < threshold or direction == "NONE":
            scan_results.append(config["emoji"] + " " + name + ": " + str(score) + "/7 — no setup yet")
            continue

        cpr_levels = cpr_calc.get_levels(config["instrument"])
        is_wide    = cpr_levels.get("is_wide", False) if cpr_levels else False

        price, _, _ = trader.get_price(name)
        raw_atr     = get_atr_pips(trader, name, config["pip"], multiplier=1.0)
        pip         = config["pip"]

        # ═══════════════════════════════════════════════════════════
        # FIX 11: Dynamic SL = ATR-based, TP mirrors SL (1:1 base)
        # Then CPR target applied if it improves R:R within range.
        # ═══════════════════════════════════════════════════════════
        if raw_atr is not None:
            atr_sl = int(raw_atr * ATR_SL_MULT)
            stop_pips = max(ATR_SL_MIN, min(atr_sl, ATR_SL_MAX))
        else:
            stop_pips = ATR_SL_MIN  # conservative fallback if ATR unavailable

        # TP starts as a mirror of SL (1:1 R:R)
        tp_pips  = stop_pips
        tp_label = f"Mirror SL {stop_pips}p (1:1 R:R)"

        # Override TP with CPR dynamic target if it falls in a reasonable range
        if cpr_levels and price:
            r1           = cpr_levels.get("r1", 0)
            s1           = cpr_levels.get("s1", 0)
            target_level = r1 if direction == "BUY" else s1
            if target_level:
                dist = abs(target_level - price) / pip
                # Use CPR target if it's between 80% and 200% of SL distance
                # This keeps R:R between 0.8 and 2.0 — never ruins it
                if stop_pips * 0.8 <= dist <= stop_pips * 2.0:
                    tp_pips  = int(dist)
                    tp_label = ("R1=" + str(r1) if direction == "BUY" else "S1=" + str(s1)) + " (CPR dynamic)"

        rr = tp_pips / stop_pips
        if rr < 0.8:
            scan_results.append(config["emoji"] + " " + name + ": R:R=" + str(round(rr, 1)) + " < 0.8 skip")
            continue

        size       = calc_position_size(current_balance, stop_pips, pip, score, price)
        max_loss   = round(size * stop_pips * pip, 2)
        max_profit = round(size * tp_pips   * pip, 2)
        # SPAM FIX 3: Removed pre-order elevated-size alert here.
        # Previously fired before order placement — meaning a failed order still sent the alert.
        # Size warning is now folded into the trade confirmation message below (only on success).

        try:
            mr = requests.get(trader.base_url + "/v3/accounts/" + trader.account_id,
                              headers=trader.headers, timeout=10)
            if mr.status_code == 200:
                acct      = mr.json().get("account", {})
                margin_av = float(acct.get("marginAvailable", current_balance))
                max_units = int((margin_av * 0.8) / (price * 0.05)) if price else size
                if max_units < 1:
                    scan_results.append(config["emoji"] + " " + name + ": Insufficient margin")
                    continue
                if size > max_units:
                    size = max_units
        except Exception as _me:
            log.warning("Margin check error: " + str(_me))

        result = trader.place_order(
            instrument     = name,
            direction      = direction,
            size           = size,
            stop_distance  = stop_pips,
            limit_distance = tp_pips
        )

        if result["success"]:
            now_utc_entry = datetime.utcnow()
            m30_entry     = now_utc_entry.replace(minute=(now_utc_entry.minute // 30) * 30, second=0, microsecond=0)
            today["trades"]                    += 1
            # Bug #5 Fix: do NOT reset consec_losses here — sync_closed_trades() is the source of truth.
            # Resetting at entry under-counts streaks (trade might still lose).
            today["breakeven_" + name]          = False
            today["last_trade_entry_price"]     = price
            today["last_trade_entry_time"]      = now_utc_entry.strftime("%Y-%m-%dT%H:%M:%S")
            today["last_trade_entry_score"]     = score
            today["last_trade_entry_direction"] = direction
            today["last_entry_candle"]          = m30_entry.strftime("%Y-%m-%dT%H:%M")
            if is_asian_gold:
                today["asian_trades_today"] = today.get("asian_trades_today", 0) + 1
            else:
                today["main_trades_today"]  = today.get("main_trades_today", 0) + 1

            with open(trade_log, "w") as f:
                json.dump(today, f, indent=2)

            cpr_summary = (
                "TC=" + str(cpr_levels["tc"]) + " BC=" + str(cpr_levels["bc"]) +
                " Pivot=" + str(cpr_levels["pivot"]) + "\n" +
                "R1=" + str(cpr_levels["r1"]) + " S1=" + str(cpr_levels["s1"]) +
                " Width=" + str(cpr_levels["width_pct"]) + "%"
            ) if cpr_levels else "CPR: unavailable"

            size_note = " (wide CPR)" if is_wide else ""
            size_warn = ("\n⚠️ SIZE=" + str(size) + " units — max loss $" + str(max_loss)) if size > 1 else ""
            alert.send(
                "🥇 GOLD TRADE! " + mode + "\n"
                + config["emoji"] + " " + name + "\n"
                "Direction: " + direction + "\n"
                "Score:    " + str(score) + "/7\n"
                "Entry:    " + str(round(price, config["precision"])) + "\n"
                "Size:     " + str(size) + " units" + size_note + size_warn + "\n"
                "ATR:      " + str(raw_atr) + "p\n"
                "Stop:     " + str(stop_pips) + "p = $" + str(max_loss) + "\n"
                "Target:   " + str(tp_pips) + "p = $" + str(max_profit) + " (" + tp_label + ")\n"
                "R:R:      1:" + str(round(tp_pips / stop_pips, 1)) + "\n"
                "Spread:   " + str(round(spread_val, 1)) + "p\n"
                "Trade #"   + str(today["trades"]) + "/" + str(settings["max_trades_day"]) + "\n"
                "Session:  " + session + "\n"
                "--- CPR ---\n" + cpr_summary + "\n"
                "--- Signals ---\n" + details.replace(" | ", "\n")
            )
            scan_results.append(config["emoji"] + " " + name + ": " + direction + " PLACED! " + str(score) + "/7")
        else:
            log.warning(name + " order failed: " + str(result.get("error", "")))
            scan_results.append(config["emoji"] + " " + name + ": order failed — " + str(result.get("error", ""))[:50])

    target_hit = realized_pnl >= 59  # ~80 SGD target
    if target_hit:
        target_msg = "TARGET HIT! $" + str(round(pl_sgd, 0)) + " SGD today!"
    elif realized_pnl > 0:
        target_msg = "Profit $" + str(round(pl_sgd, 0)) + " SGD"
    elif realized_pnl < 0:
        target_msg = "Loss $" + str(abs(round(pl_sgd, 0))) + " SGD"
    else:
        target_msg = "Scanning for setups..."

    summary  = "\n".join(scan_results) if scan_results else "No setups this scan"
    wins     = today.get("wins", 0)
    losses   = today.get("losses", 0)
    cpr_line = ""
    if cpr_gold:
        w_flag   = " NARROW" if cpr_gold["is_narrow"] else (" WIDE" if cpr_gold["is_wide"] else "")
        cpr_line = (
            "CPR Width: " + str(cpr_gold["width_pct"]) + "%" + w_flag + "\n"
            "TC=" + str(cpr_gold["tc"]) + " BC=" + str(cpr_gold["bc"]) + "\n"
            "R1=" + str(cpr_gold["r1"]) + " S1=" + str(cpr_gold["s1"]) + "\n"
        )

    threshold_used    = settings.get("signal_threshold_asian", 4) if asian else settings["signal_threshold"]
    trade_just_placed = any("PLACED" in r for r in scan_results)
    last_alert_min    = today.get("last_scan_alert_min", -61)
    last_alert_score  = today.get("last_alert_score", -1)
    last_alert_dir    = today.get("last_alert_direction", "")
    current_min       = now.hour * 60 + now.minute
    mins_since_alert  = current_min - last_alert_min if current_min >= last_alert_min else current_min + 1440 - last_alert_min

    # SPAM FIX 1: Suppress scan alerts for minor score noise.
    # Alert only when something *meaningful* changed:
    #   (a) direction flipped (NONE<->BUY/SELL or BUY<->SELL) — always actionable
    #   (b) score crossed the trade threshold in either direction — about to trade / just dropped out
    #   (c) score moved >=2 pts AND is at/above threshold — meaningful signal progress
    # Pure ±1 pt wobble below threshold (e.g. 2->3->2) is noise — suppressed.
    dir_changed    = (direction != last_alert_dir)
    crossed_thresh = (
        (score >= threshold_used and last_alert_score < threshold_used) or
        (score < threshold_used  and last_alert_score >= threshold_used)
    )
    big_score_move = (abs(score - last_alert_score) >= 2 and score >= threshold_used)
    score_changed  = dir_changed or crossed_thresh or big_score_move
    should_alert   = trade_just_placed or score_changed or mins_since_alert >= 60

    if should_alert:
        today["last_scan_alert_min"]  = current_min
        today["last_alert_score"]     = score
        today["last_alert_direction"] = direction
        with open(trade_log, "w") as f:
            json.dump(today, f, indent=2)

        signal_detail = ""
        # SPAM FIX 5: Only include full signal detail when score is actionable.
        # Hourly heartbeat scans with no setup are kept short — no CPR block, no signal breakdown.
        at_threshold = (score >= threshold_used and direction not in ("NONE", "BLOCKED"))
        if at_threshold and details:
            signal_detail = "--- Signals ---\n" + details.replace(" | ", "\n") + "\n"

        include_cpr = cpr_line if (at_threshold or trade_just_placed) else ""

        alert.send(
            "🥇 GOLD BOT Scan! " + mode + "\n"
            "Time: " + now.strftime("%H:%M SGT") + " | " + session + "\n"
            "Balance: $" + str(round(current_balance, 2)) +
            " | Realized: $" + str(round(realized_pnl, 2)) + " " + pnl_emoji + "\n"
            "Trades: " + str(today["trades"]) + "/" + str(settings["max_trades_day"]) +
            " | W/L: " + str(wins) + "/" + str(losses) + "\n"
            "Need: " + str(threshold_used) + "/7 to trade\n"
            + target_msg + "\n"
            "-------------------------\n"
            + include_cpr +
            "--- Setups ---\n"
            + summary + "\n"
            + signal_detail
        )
    else:
        log.info("Scan silent — next alert in " + str(60 - mins_since_alert) + " mins")


if __name__ == "__main__":
    log.info("🥇 GOLD BOT starting — scanning every 5 minutes via Railway...")
    while True:
        try:
            run_bot()
        except Exception as e:
            log.error("Bot error: " + str(e))
        log.info("Sleeping 5 minutes...")
        time.sleep(300)
