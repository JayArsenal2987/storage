#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time, math
import atexit
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple
from dotenv import load_dotenv
import numpy as np

# ========================= CONFIG =========================
load_dotenv('config/example.env')
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE = int(os.getenv("LEVERAGE", "50"))

SYMBOLS = {
    "ETHUSDT": 0.01,
    "BNBUSDT": 0.03,
    "XRPUSDT": 10.0,
    "SOLUSDT": 0.1,
    "ADAUSDT": 10.0,
    "DOGEUSDT": 40.0,
    "TRXUSDT": 20.0,
}

PRECISIONS = {
    "ETHUSDT": 3, "BNBUSDT": 2, "XRPUSDT": 1,
    "SOLUSDT": 3, "ADAUSDT": 0, "DOGEUSDT": 0, "TRXUSDT": 0
}

# SQUEEZE MOMENTUM PARAMETERS
SQUEEZE_TIMEFRAME = int(os.getenv("SQUEEZE_TIMEFRAME", "570"))
BB_LENGTH = int(os.getenv("BB_LENGTH", "570"))
KC_LENGTH = int(os.getenv("KC_LENGTH", "570"))
BB_MULT = float(os.getenv("BB_MULT", "0.8"))
KC_MULT = float(os.getenv("KC_MULT", "0.6"))
USE_TRUE_RANGE = os.getenv("USE_TRUE_RANGE", "True").lower() == "true"

# ADX PARAMETERS
ADX_PERIODS = int(os.getenv("ADX_PERIODS", "570"))
ADX_THRESHOLD = float(os.getenv("ADX_THRESHOLD", "25"))

KLINE_LIMIT = max(BB_LENGTH, KC_LENGTH, ADX_PERIODS) + 100

# Daily PNL limits
DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", "1000.0"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-100.0"))

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "current_signal": None,
        "last_signal_change": 0,
        "current_position": 0.0,
        "adx": None,
        "adx_ready": False,
        "ready": False,
        "last_exec_ts": 0.0,
        "last_target": None,
        "daily_pnl": 0.0,
        "daily_pnl_reset_time": time.time(),
        "daily_limit_reached": False,
        "entry_price": None,
        "squeeze_state": None,
        "prev_squeeze_state": None,
        "momentum": 0.0,
        "momentum_color": "gray",
        "prev_momentum": 0.0,
    }
    for symbol in SYMBOLS
}

api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE =========================
def save_klines():
    save_data = {sym: list(state[sym]["klines"]) for sym in SYMBOLS}
    with open('klines.json', 'w') as f:
        json.dump(save_data, f)
    logging.info("Saved klines to klines.json")

def load_klines():
    try:
        with open('klines.json', 'r') as f:
            load_data = json.load(f)
        for sym in SYMBOLS:
            state[sym]["klines"] = deque(load_data.get(sym, []), maxlen=KLINE_LIMIT)
        logging.info("Loaded klines from klines.json")
    except FileNotFoundError:
        logging.info("No klines.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load klines: {e}")

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    prec = PRECISIONS.get(symbol, 3)
    return round(size, prec)

def calculate_pnl_percent(symbol: str, current_price: float) -> float:
    st = state[symbol]
    if st["current_signal"] is None or st["entry_price"] is None:
        return 0.0
    
    entry = st["entry_price"]
    if st["current_signal"] == "LONG":
        pnl_percent = ((current_price - entry) / entry) * 100
    else:
        pnl_percent = ((entry - current_price) / entry) * 100
    
    return pnl_percent * LEVERAGE

def check_and_reset_daily_counters(symbol: str):
    st = state[symbol]
    current_time = time.time()
    
    last_reset = st.get("daily_pnl_reset_time", 0)
    current_day = int(current_time // 86400)
    last_reset_day = int(last_reset // 86400)
    
    if current_day > last_reset_day:
        st["daily_pnl"] = 0.0
        st["daily_limit_reached"] = False
        st["daily_pnl_reset_time"] = current_time
        logging.info(f"{symbol} Daily PNL counters reset (new day)")

async def safe_api_call(func, *args, **kwargs):
    global api_calls_count, api_calls_reset_time
    now = time.time()
    if now - api_calls_reset_time > 60:
        api_calls_count = 0
        api_calls_reset_time = now
    if api_calls_count >= 10:
        wait_time = 60 - (now - api_calls_reset_time)
        if wait_time > 0:
            logging.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            api_calls_count = 0
            api_calls_reset_time = time.time()
    for attempt in range(3):
        try:
            api_calls_count += 1
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            if "-1003" in str(e) or "Way too many requests" in str(e):
                wait_time = (2 ** attempt) * 60
                logging.warning(f"Rate limited, attempt {attempt+1}/3, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                raise e
    raise Exception("Max API retry attempts reached")

async def place_order(client: AsyncClient, symbol: str, side: str, quantity: float, action: str):
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            return True
        if "LONG" in action:
            position_side = "LONG"
        elif "SHORT" in action:
            position_side = "SHORT"
        else:
            logging.error(f"Unknown action type: {action}")
            return False
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side
        }
        result = await safe_api_call(client.futures_create_order, **params)
        logging.info(f"{symbol} {action} EXECUTED - {side} {quantity} - OrderID: {result.get('orderId')}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} FAILED: {e}")
        return False

# ========================= SQUEEZE MOMENTUM INDICATORS =========================
def calculate_sma(data: list, period: int) -> Optional[float]:
    if len(data) < period:
        return None
    return sum(data[-period:]) / period

def calculate_std_dev(data: list, period: int, sma: float) -> Optional[float]:
    if len(data) < period or sma is None:
        return None
    variance = sum((x - sma) ** 2 for x in data[-period:]) / period
    return math.sqrt(variance)

def calculate_true_range_value(high: float, low: float, prev_close: float) -> float:
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    return max(tr1, tr2, tr3)

def linear_regression(y_values: list) -> float:
    """
    Calculate linear regression value at the end point
    Matches Pine Script's linreg(source, length, offset=0)
    """
    n = len(y_values)
    if n == 0:
        return 0.0
    
    # X values are just indices: 0, 1, 2, ..., n-1
    x_values = list(range(n))
    
    # Calculate means
    x_mean = sum(x_values) / n
    y_mean = sum(y_values) / n
    
    # Calculate slope (beta)
    numerator = sum((x_values[i] - x_mean) * (y_values[i] - y_mean) for i in range(n))
    denominator = sum((x_values[i] - x_mean) ** 2 for i in range(n))
    
    if denominator == 0:
        return y_values[-1]  # Return last value if no trend
    
    slope = numerator / denominator
    
    # Calculate intercept (alpha)
    intercept = y_mean - slope * x_mean
    
    # Return value at the last point (x = n-1)
    return intercept + slope * (n - 1)

def calculate_squeeze_momentum(symbol: str) -> dict:
    klines = state[symbol]["klines"]
    
    if len(klines) < max(BB_LENGTH, KC_LENGTH):
        return {
            "squeeze_state": None,
            "momentum": 0.0,
            "momentum_color": "gray"
        }
    
    completed = list(klines)[:-1]
    if len(completed) < max(BB_LENGTH, KC_LENGTH):
        return {
            "squeeze_state": None,
            "momentum": 0.0,
            "momentum_color": "gray"
        }
    
    closes = [k["close"] for k in completed]
    highs = [k["high"] for k in completed]
    lows = [k["low"] for k in completed]
    
    # Calculate Bollinger Bands
    bb_basis = calculate_sma(closes, BB_LENGTH)
    if bb_basis is None:
        return {"squeeze_state": None, "momentum": 0.0, "momentum_color": "gray"}
    
    std_dev = calculate_std_dev(closes, BB_LENGTH, bb_basis)
    if std_dev is None:
        return {"squeeze_state": None, "momentum": 0.0, "momentum_color": "gray"}
    
    upper_bb = bb_basis + (BB_MULT * std_dev)
    lower_bb = bb_basis - (BB_MULT * std_dev)
    
    # Calculate Keltner Channels
    kc_basis = calculate_sma(closes, KC_LENGTH)
    if kc_basis is None:
        return {"squeeze_state": None, "momentum": 0.0, "momentum_color": "gray"}
    
    if USE_TRUE_RANGE:
        tr_values = []
        for i in range(1, len(completed)):
            tr = calculate_true_range_value(
                completed[i]["high"],
                completed[i]["low"],
                completed[i-1]["close"]
            )
            tr_values.append(tr)
        
        if len(tr_values) < KC_LENGTH:
            return {"squeeze_state": None, "momentum": 0.0, "momentum_color": "gray"}
        
        atr = calculate_sma(tr_values, KC_LENGTH)
        if atr is None:
            return {"squeeze_state": None, "momentum": 0.0, "momentum_color": "gray"}
        
        range_ma = atr
    else:
        ranges = [highs[i] - lows[i] for i in range(len(highs))]
        range_ma = calculate_sma(ranges, KC_LENGTH)
        if range_ma is None:
            return {"squeeze_state": None, "momentum": 0.0, "momentum_color": "gray"}
    
    upper_kc = kc_basis + (range_ma * KC_MULT)
    lower_kc = kc_basis - (range_ma * KC_MULT)
    
    # Determine squeeze state
    sqz_on = (lower_bb > lower_kc) and (upper_bb < upper_kc)
    sqz_off = (lower_bb < lower_kc) and (upper_bb > upper_kc)
    
    if sqz_on:
        squeeze_state = "on"
    elif sqz_off:
        squeeze_state = "off"
    else:
        squeeze_state = "none"
    
    # Calculate momentum using linear regression (matching Pine Script exactly)
    if len(completed) >= KC_LENGTH:
        # Calculate the series: source - avg(avg(highest, lowest), sma(close))
        differences = []
        for i in range(len(completed) - KC_LENGTH, len(completed)):
            # Get highest and lowest over KC_LENGTH period ending at i
            start_idx = max(0, i - KC_LENGTH + 1)
            period_highs = highs[start_idx:i+1]
            period_lows = lows[start_idx:i+1]
            period_closes = closes[start_idx:i+1]
            
            highest = max(period_highs) if period_highs else highs[i]
            lowest = min(period_lows) if period_lows else lows[i]
            midpoint = (highest + lowest) / 2
            
            close_sma = calculate_sma(period_closes, len(period_closes))
            if close_sma is None:
                close_sma = closes[i]
            
            avg_value = (midpoint + close_sma) / 2
            differences.append(closes[i] - avg_value)
        
        # Apply linear regression to the differences
        momentum = linear_regression(differences)
    else:
        momentum = 0.0
    
    # Determine momentum color based on Pine Script logic
    prev_momentum = state[symbol].get("prev_momentum", 0.0)
    
    if momentum > 0:
        # Positive momentum
        if momentum > prev_momentum:
            momentum_color = "lime"  # Increasing positive
        else:
            momentum_color = "green"  # Decreasing positive
    else:
        # Negative momentum
        if momentum < prev_momentum:
            momentum_color = "red"  # Decreasing negative (more negative)
        else:
            momentum_color = "maroon"  # Increasing negative (toward zero)
    
    return {
        "squeeze_state": squeeze_state,
        "momentum": momentum,
        "momentum_color": momentum_color,
        "upper_bb": upper_bb,
        "lower_bb": lower_bb,
        "upper_kc": upper_kc,
        "lower_kc": lower_kc,
        "bb_basis": bb_basis,
        "kc_basis": kc_basis
    }

# ========================= ADX CALCULATION =========================
def calculate_true_range(high1: float, low1: float, close0: float) -> float:
    tr1 = high1 - low1
    tr2 = abs(high1 - close0)
    tr3 = abs(low1 - close0)
    return max(tr1, tr2, tr3)

def calculate_directional_movement(high1: float, high0: float, low1: float, low0: float) -> tuple:
    up_move = high1 - high0
    down_move = low0 - low1
    plus_dm  = up_move if (up_move > down_move and up_move > 0) else 0
    minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
    return plus_dm, minus_dm

def calculate_adx(symbol: str) -> Optional[float]:
    klines = state[symbol]["klines"]
    if len(klines) < ADX_PERIODS + 1:
        return None
    completed = list(klines)[:-1]
    if len(completed) < ADX_PERIODS + 1:
        return None
    recent = completed[-(ADX_PERIODS + 1):]
    tr_values, plus_dm_values, minus_dm_values = [], [], []
    for i in range(1, len(recent)):
        cur = recent[i]
        prev = recent[i - 1]
        tr_values.append(calculate_true_range(cur["high"], cur["low"], prev["close"]))
        plus_dm, minus_dm = calculate_directional_movement(cur["high"], prev["high"], cur["low"], prev["low"])
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)
    alpha = 1.0 / ADX_PERIODS
    sm_tr = tr_values[0]
    for tr in tr_values[1:]:
        sm_tr = alpha * tr + (1 - alpha) * sm_tr
    sm_pdm = plus_dm_values[0]
    for pdm in plus_dm_values[1:]:
        sm_pdm = alpha * pdm + (1 - alpha) * sm_pdm
    sm_mdm = minus_dm_values[0]
    for mdm in minus_dm_values[1:]:
        sm_mdm = alpha * mdm + (1 - alpha) * sm_mdm
    if sm_tr == 0:
        return None
    plus_di  = (sm_pdm / sm_tr) * 100
    minus_di = (sm_mdm / sm_tr) * 100
    if (plus_di + minus_di) == 0:
        return None
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100
    prev_adx = state[symbol].get("adx")
    adx = dx if prev_adx is None else (alpha * dx + (1 - alpha) * prev_adx)
    state[symbol]["adx"] = adx
    state[symbol]["adx_ready"] = True
    return adx

# ========================= TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> dict:
    st = state[symbol]
    price = st["price"]
    current_signal = st["current_signal"]
    
    if price is None or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    check_and_reset_daily_counters(symbol)
    
    if st["daily_limit_reached"]:
        if current_signal is not None:
            logging.info(f"{symbol} Daily limit reached, forcing exit")
            st["current_signal"] = None
            return {"changed": True, "action": "LIMIT_EXIT", "signal": None}
        return {"changed": False, "action": "NONE", "signal": None}
    
    if current_signal is not None:
        current_pnl = calculate_pnl_percent(symbol, price)
        
        if st["daily_pnl"] >= DAILY_PROFIT_TARGET:
            st["daily_limit_reached"] = True
            logging.info(f"{symbol} DAILY PROFIT TARGET REACHED! Total PNL: +{st['daily_pnl']:.2f}%")
            st["current_signal"] = None
            return {"changed": True, "action": "PROFIT_TARGET_EXIT", "signal": None}
        
        if st["daily_pnl"] <= DAILY_LOSS_LIMIT:
            st["daily_limit_reached"] = True
            logging.info(f"{symbol} DAILY LOSS LIMIT HIT! Total PNL: {st['daily_pnl']:.2f}%")
            st["current_signal"] = None
            return {"changed": True, "action": "LOSS_LIMIT_EXIT", "signal": None}
    
    squeeze_result = calculate_squeeze_momentum(symbol)
    adx = calculate_adx(symbol)
    
    if squeeze_result["squeeze_state"] is None or adx is None:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    prev_squeeze = st["prev_squeeze_state"]
    st["squeeze_state"] = squeeze_result["squeeze_state"]
    st["momentum"] = squeeze_result["momentum"]
    st["momentum_color"] = squeeze_result["momentum_color"]
    
    adx_ok = (adx >= ADX_THRESHOLD)
    
    new_signal = current_signal
    action_type = "NONE"
    
    # ENTRY LOGIC - Only on squeeze release
    if current_signal is None:
        if prev_squeeze == "on" and st["squeeze_state"] == "off" and adx_ok:
            # LONG: Ascending momentum (lime = positive increasing, maroon = negative increasing toward zero)
            if squeeze_result["momentum_color"] in ["lime", "maroon"]:
                new_signal = "LONG"
                action_type = "ENTRY"
                logging.info(f"{symbol} ENTRY LONG (Squeeze released, Momentum ASCENDING: {squeeze_result['momentum']:.4f}, Color: {squeeze_result['momentum_color']}, ADX: {adx:.1f}>={ADX_THRESHOLD})")
            # SHORT: Descending momentum (green = positive decreasing, red = negative decreasing)
            elif squeeze_result["momentum_color"] in ["green", "red"]:
                new_signal = "SHORT"
                action_type = "ENTRY"
                logging.info(f"{symbol} ENTRY SHORT (Squeeze released, Momentum DESCENDING: {squeeze_result['momentum']:.4f}, Color: {squeeze_result['momentum_color']}, ADX: {adx:.1f}>={ADX_THRESHOLD})")
    
    # EXIT LOGIC - Immediate momentum reversal
    elif current_signal == "LONG":
        # Exit LONG when momentum becomes descending (green or red)
        if squeeze_result["momentum_color"] in ["green", "red"]:
            new_signal = None
            action_type = "EXIT"
            current_pnl = calculate_pnl_percent(symbol, price)
            logging.info(f"{symbol} EXIT LONG (Momentum now DESCENDING: {squeeze_result['momentum_color']}, PNL: {current_pnl:+.2f}%)")
    
    elif current_signal == "SHORT":
        # Exit SHORT when momentum becomes ascending (lime or maroon)
        if squeeze_result["momentum_color"] in ["lime", "maroon"]:
            new_signal = None
            action_type = "EXIT"
            current_pnl = calculate_pnl_percent(symbol, price)
            logging.info(f"{symbol} EXIT SHORT (Momentum now ASCENDING: {squeeze_result['momentum_color']}, PNL: {current_pnl:+.2f}%)")
    
    st["prev_momentum"] = squeeze_result["momentum"]
    st["prev_squeeze_state"] = st["squeeze_state"]
    
    if new_signal != current_signal:
        st["current_signal"] = new_signal
        st["last_signal_change"] = time.time()
        return {"changed": True, "action": action_type, "signal": new_signal}
    
    return {"changed": False, "action": "NONE", "signal": current_signal}

# ========================= LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    streams = [f"{s.lower()}@markPrice@1s" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logging.info("WebSocket connected")
                async for message in ws:
                    try:
                        data = json.loads(message).get("data", {})
                        symbol = data.get("s")
                        price_str = data.get("p")
                        event_time = data.get("E")
                        if symbol in SYMBOLS and price_str and event_time:
                            price = float(price_str)
                            state[symbol]["price"] = price
                            event_time /= 1000
                            minute = int(event_time // 60)
                            klines = state[symbol]["klines"]
                            if not klines or klines[-1]["minute"] != minute:
                                klines.append({"minute": minute, "high": price, "low": price, "close": price})
                            else:
                                klines[-1]["high"] = max(klines[-1]["high"], price)
                                klines[-1]["low"] = min(klines[-1]["low"], price)
                                klines[-1]["close"] = price
                            if len(klines) >= max(BB_LENGTH, KC_LENGTH, ADX_PERIODS) and not state[symbol]["ready"]:
                                squeeze_result = calculate_squeeze_momentum(symbol)
                                adx = calculate_adx(symbol)
                                if squeeze_result["squeeze_state"] is not None and adx is not None:
                                    state[symbol]["ready"] = True
                                    logging.info(f"{symbol} ready ({len(klines)} candles, ADX {adx:.1f}, Squeeze: {squeeze_result['squeeze_state']})")
                            else:
                                calculate_adx(symbol)
                                calculate_squeeze_momentum(symbol)
                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)

async def status_logger():
    while True:
        await asyncio.sleep(300)
        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"=== STATUS REPORT {current_time} ===")
        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                candle_count = len(st["klines"])
                remaining = max(0, max(BB_LENGTH, KC_LENGTH, ADX_PERIODS) - candle_count)
                logging.info(f"{symbol}: Not ready - {candle_count}/{max(BB_LENGTH, KC_LENGTH, ADX_PERIODS)} candles ({remaining} more needed)")
                continue
            
            price = st["price"]
            adx = st.get("adx")
            squeeze_state = st.get("squeeze_state")
            momentum = st.get("momentum", 0.0)
            momentum_color = st.get("momentum_color", "gray")
            
            if price:
                current_sig = st["current_signal"]
                display_sig = current_sig or "FLAT"
                
                logging.info(f"{symbol}: Price={price:.6f} | ADX={f'{adx:.1f}' if adx is not None else 'N/A'}")
                logging.info(f"  Squeeze State: {squeeze_state} | Momentum: {momentum:.4f} | Color: {momentum_color}")
                logging.info(f"  Signal: {display_sig}")
                
                daily_pnl_color = "+" if st["daily_pnl"] >= 0 else ""
                logging.info(f"  Daily PNL: {daily_pnl_color}{st['daily_pnl']:.2f}% (Target: +{DAILY_PROFIT_TARGET}% / Limit: {DAILY_LOSS_LIMIT}%)")
                
                if st["daily_limit_reached"]:
                    if st["daily_pnl"] >= DAILY_PROFIT_TARGET:
                        logging.info(f"  STATUS: PROFIT TARGET REACHED - Trading paused until next day")
                    else:
                        logging.info(f"  STATUS: LOSS LIMIT HIT - Trading paused until next day")
                else:
                    if current_sig is None:
                        logging.info(f"  FLAT - Waiting for squeeze release (ON->OFF) + momentum signal + ADX >={ADX_THRESHOLD}")
                        logging.info(f"    LONG = lime/maroon (ascending), SHORT = green/red (descending)")
                    elif current_sig == "LONG":
                        current_pnl = calculate_pnl_percent(symbol, price)
                        logging.info(f"  LONG - Current PNL: {current_pnl:+.2f}% | Will exit when momentum descends (green/red)")
                    elif current_sig == "SHORT":
                        current_pnl = calculate_pnl_percent(symbol, price)
                        logging.info(f"  SHORT - Current PNL: {current_pnl:+.2f}% | Will exit when momentum ascends (lime/maroon)")
        
        logging.info("=== END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    while True:
        await asyncio.sleep(0.1)
        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                continue
            signal_result = update_trading_signals(symbol)
            target_size = SYMBOLS[symbol]
            current_signal = st["current_signal"]
            if current_signal == "LONG":
                final_position = target_size
            elif current_signal == "SHORT":
                final_position = -target_size
            elif current_signal is None:
                final_position = 0.0
            else:
                final_position = st["current_position"]
            if signal_result["changed"]:
                current_pos = st["current_position"]
                if abs(final_position - current_pos) > 1e-12:
                    await execute_position_change(client, symbol, final_position, current_pos)

async def execute_position_change(client: AsyncClient, symbol: str, target: float, current: float):
    st = state[symbol]
    now = time.time()
    last_target = st.get("last_target", None)
    last_when = st.get("last_exec_ts", 0.0)
    if last_target is not None and abs(target - last_target) < 1e-12 and (now - last_when) < 2.0:
        logging.info(f"{symbol} dedup: skipping duplicate execution")
        return
    if abs(target - current) < 1e-12:
        return
    
    try:
        if current != 0.0 and target == 0.0 and st["entry_price"] is not None:
            current_price = st["price"]
            closed_pnl = calculate_pnl_percent(symbol, current_price)
            st["daily_pnl"] += closed_pnl
            pnl_sign = "+" if closed_pnl >= 0 else ""
            logging.info(f"{symbol} Position closed with {pnl_sign}{closed_pnl:.2f}% PNL. Daily total: {st['daily_pnl']:+.2f}%")
        
        if target == 0.0:
            if current > 0:
                ok = await place_order(client, symbol, "SELL", current, "LONG CLOSE")
                if not ok:
                    return
            elif current < 0:
                ok = await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
                if not ok:
                    return
        elif target > 0:
            if current < 0:
                ok = await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
                if not ok:
                    return
                ok = await place_order(client, symbol, "BUY", target, "LONG ENTRY")
                if not ok:
                    return
            else:
                if target > current:
                    ok = await place_order(client, symbol, "BUY", target - current, "LONG ENTRY")
                    if not ok:
                        return
                else:
                    ok = await place_order(client, symbol, "SELL", current - target, "LONG CLOSE")
                    if not ok:
                        return
        else:
            if current > 0:
                ok = await place_order(client, symbol, "SELL", current, "LONG CLOSE")
                if not ok:
                    return
                ok = await place_order(client, symbol, "SELL", abs(target), "SHORT ENTRY")
                if not ok:
                    return
            else:
                cur_abs = abs(current)
                tgt_abs = abs(target)
                if tgt_abs > cur_abs:
                    ok = await place_order(client, symbol, "SELL", tgt_abs - cur_abs, "SHORT ENTRY")
                    if not ok:
                        return
                else:
                    ok = await place_order(client, symbol, "BUY", cur_abs - tgt_abs, "SHORT CLOSE")
                    if not ok:
                        return
        
        if target != 0.0 and current == 0.0:
            st["entry_price"] = st["price"]
            logging.info(f"{symbol} Entry price set: {st['entry_price']:.6f}")
        elif target == 0.0:
            st["entry_price"] = None
        
        st["current_position"] = target
        st["last_target"] = target
        st["last_exec_ts"] = now
    except Exception as e:
        logging.error(f"{symbol} position change failed: {e}")

async def init_bot(client: AsyncClient):
    logging.info("Initializing bot...")
    load_klines()
    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        required_candles = max(BB_LENGTH, KC_LENGTH, ADX_PERIODS)
        squeeze_result = calculate_squeeze_momentum(symbol)
        adx_value = calculate_adx(symbol)
        
        if len(klines) >= required_candles and squeeze_result["squeeze_state"] is not None and adx_value is not None:
            state[symbol]["ready"] = True
            state[symbol]["prev_squeeze_state"] = squeeze_result["squeeze_state"]
            logging.info(f"{symbol} ready ({len(klines)} candles, ADX {adx_value:.1f}, Squeeze: {squeeze_result['squeeze_state']})")
        else:
            symbols_needing_data.append(symbol)
            logging.info(f"{symbol} needs data ({len(klines)}/{required_candles} candles)")
    
    if symbols_needing_data:
        logging.info(f"Fetching historical data for {len(symbols_needing_data)} symbols...")
        successful_fetches = 0
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")
                required_candles = max(BB_LENGTH, KC_LENGTH, ADX_PERIODS)
                klines_data = await safe_api_call(client.futures_mark_price_klines, symbol=symbol, interval="1m", limit=required_candles + 50)
                st = state[symbol]
                st["klines"].clear()
                for kline in klines_data:
                    minute = int(float(kline[0]) / 1000 // 60)
                    st["klines"].append({"minute": minute, "high": float(kline[2]), "low": float(kline[3]), "close": float(kline[4])})
                
                squeeze_result = calculate_squeeze_momentum(symbol)
                adx_val = calculate_adx(symbol)
                
                if squeeze_result["squeeze_state"] is not None and adx_val is not None:
                    st["ready"] = True
                    st["prev_squeeze_state"] = squeeze_result["squeeze_state"]
                    logging.info(f"{symbol} ready ({len(st['klines'])} candles, ADX {adx_val:.1f}, Squeeze: {squeeze_result['squeeze_state']})")
                    successful_fetches += 1
                else:
                    logging.warning(f"{symbol} insufficient data ({len(st['klines'])} candles)")
                
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)
            except Exception as e:
                logging.error(f"{symbol} fetch failed: {e}")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)
        logging.info(f"Fetch complete: {successful_fetches}/{len(symbols_needing_data)} successful")
    else:
        logging.info("All symbols ready from loaded data")
    
    logging.info("Synchronizing positions...")
    try:
        for symbol in SYMBOLS:
            try:
                position_info = await safe_api_call(client.futures_position_information, symbol=symbol)
                actual_position_size = 0.0
                for pos in position_info:
                    if pos['symbol'] == symbol:
                        actual_position_size = float(pos['positionAmt'])
                        break
                if actual_position_size > 0:
                    state[symbol]["current_signal"] = "LONG"
                    state[symbol]["current_position"] = actual_position_size
                    logging.info(f"{symbol} synced: LONG {actual_position_size}")
                elif actual_position_size < 0:
                    state[symbol]["current_signal"] = "SHORT"
                    state[symbol]["current_position"] = actual_position_size
                    logging.info(f"{symbol} synced: SHORT {actual_position_size}")
                else:
                    state[symbol]["current_signal"] = None
                    state[symbol]["current_position"] = 0.0
                    logging.info(f"{symbol} synced: FLAT")
            except Exception as e:
                logging.warning(f"{symbol} sync failed: {e}")
                state[symbol]["current_signal"] = None
                state[symbol]["current_position"] = 0.0
    except Exception as e:
        logging.error(f"Position sync failed: {e}")
    
    save_klines()
    await asyncio.sleep(2)
    logging.info("Initialization complete")

async def main():
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing Binance API credentials in .env")
    
    # FOR TESTNET (safe testing with fake money):
    # client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
    # logging.info("Connected to BINANCE TESTNET - Using fake money for testing")
    
    # FOR LIVE TRADING (REAL MONEY - COMMENT OUT TESTNET AND UNCOMMENT BELOW):
    client = await AsyncClient.create(API_KEY, API_SECRET)
    logging.info("Connected to BINANCE LIVE - Using REAL money")
    
    atexit.register(save_klines)
    try:
        await init_bot(client)
        price_task = asyncio.create_task(price_feed_loop(client))
        trade_task = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())
        logging.info("Bot started")
        await asyncio.gather(price_task, trade_task, status_task)
    finally:
        await client.close_connection()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    print("=" * 80)
    print("SQUEEZE MOMENTUM + ADX TRADING BOT (LINEAR REGRESSION CORRECTED)")
    print("=" * 80)
    print("STRATEGY: Squeeze Momentum with Proper Linear Regression")
    print("")
    print("SQUEEZE MOMENTUM PARAMETERS:")
    print(f"  BB Length: {BB_LENGTH}")
    print(f"  KC Length: {KC_LENGTH}")
    print(f"  BB MultFactor: {BB_MULT}")
    print(f"  KC MultFactor: {KC_MULT}")
    print(f"  Use True Range: {USE_TRUE_RANGE}")
    print("")
    print("ADX PARAMETERS:")
    print(f"  ADX Period: {ADX_PERIODS} minutes")
    print(f"  ADX Threshold: {ADX_THRESHOLD}")
    print("")
    print("ENTRY RULES:")
    print("  LONG Entry:")
    print("    - Squeeze transitions from ON to OFF (breakout)")
    print("    - Momentum ASCENDING (lime or maroon colors)")
    print("    - ADX >= threshold")
    print("")
    print("  SHORT Entry:")
    print("    - Squeeze transitions from ON to OFF (breakout)")
    print("    - Momentum DESCENDING (green or red colors)")
    print("    - ADX >= threshold")
    print("")
    print("EXIT RULES:")
    print("  LONG Exit: Momentum becomes DESCENDING (green or red)")
    print("  SHORT Exit: Momentum becomes ASCENDING (lime or maroon)")
    print("")
    print("DAILY PNL LIMITS (Per Symbol):")
    print(f"  Profit Target: +{DAILY_PROFIT_TARGET}%")
    print(f"  Loss Limit: {DAILY_LOSS_LIMIT}%")
    print("")
    print("MOMENTUM COLORS (Pine Script):")
    print("  - Lime: Positive momentum increasing")
    print("  - Green: Positive momentum decreasing")
    print("  - Red: Negative momentum decreasing (more negative)")
    print("  - Maroon: Negative momentum increasing (toward zero)")
    print("")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print(f"Leverage: {LEVERAGE}x")
    print("=" * 80)
    print("NOTE: Currently set to TESTNET mode")
    print("=" * 80)
    asyncio.run(main())
