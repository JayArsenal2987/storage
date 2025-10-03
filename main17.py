#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time
import atexit
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple
from dotenv import load_dotenv

# ========================= CONFIG =========================
load_dotenv()
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

# ========================= SUPERTREND + FILTERS CONFIG =========================
# Adjustable ATR settings (now in hours)
SUPERTREND_ATR_PERIODS = int(os.getenv("SUPERTREND_ATR_PERIODS", "19"))    # Default 19 hours
SUPERTREND_MULTIPLIER = float(os.getenv("SUPERTREND_MULTIPLIER", "2.0"))   # Default 2.0

# Filter settings (now in hours)
ADX_PERIODS = int(os.getenv("ADX_PERIODS", "19"))                          # Default 19 hours
ADX_THRESHOLD = float(os.getenv("ADX_THRESHOLD", "25.0"))                  # Default 25
RSI_PERIODS = int(os.getenv("RSI_PERIODS", "19"))                          # Default 19 hours
RSI_SHORT_THRESHOLD = float(os.getenv("RSI_SHORT_THRESHOLD", "49.0"))      # SHORT when RSI < 49
RSI_LONG_THRESHOLD = float(os.getenv("RSI_LONG_THRESHOLD", "51.0"))        # LONG when RSI > 51

# Calculate required kline buffer (need extra for calculations)
KLINE_LIMIT = max(SUPERTREND_ATR_PERIODS, ADX_PERIODS, RSI_PERIODS) + 20

# Timeframe
TIMEFRAME = "1h"  # Use hourly candles

# Daily PNL limits
DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", "200.0"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-100.0"))

# Validate config
if SUPERTREND_ATR_PERIODS < 1:
    raise ValueError(f"SUPERTREND_ATR_PERIODS must be >= 1, got {SUPERTREND_ATR_PERIODS}")
if SUPERTREND_MULTIPLIER <= 0:
    raise ValueError(f"SUPERTREND_MULTIPLIER must be > 0, got {SUPERTREND_MULTIPLIER}")

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "current_signal": None,
        "last_signal_change": 0,
        "current_position": 0.0,
        "entry_price": None,
        "last_exec_ts": 0.0,
        "last_target": None,
        "daily_pnl": 0.0,
        "daily_pnl_reset_time": time.time(),
        "daily_limit_reached": False,
        # Supertrend specific
        "atr": None,
        "supertrend_upper": None,
        "supertrend_lower": None,
        "supertrend_trend": None,
        # Filter indicators
        "adx": None,
        "rsi": None,
        "ready": False,
    }
    for symbol in SYMBOLS
}

api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE =========================
def save_klines():
    save_data = {sym: list(state[sym]["klines"]) for sym in SYMBOLS}
    with open('klines_supertrend.json', 'w') as f:
        json.dump(save_data, f)
    logging.info("Saved klines to klines_supertrend.json")

def load_klines():
    try:
        with open('klines_supertrend.json', 'r') as f:
            load_data = json.load(f)
        for sym in SYMBOLS:
            state[sym]["klines"] = deque(load_data.get(sym, []), maxlen=KLINE_LIMIT)
        logging.info("Loaded klines from klines_supertrend.json")
    except FileNotFoundError:
        logging.info("No klines_supertrend.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load klines: {e}")

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    prec = PRECISIONS.get(symbol, 3)
    return round(size, prec)

def calculate_pnl_percent(symbol: str, current_price: float) -> float:
    """Calculate PNL percentage for current position"""
    st = state[symbol]
    if st["current_signal"] is None or st["entry_price"] is None:
        return 0.0
    
    entry = st["entry_price"]
    if st["current_signal"] == "LONG":
        pnl_percent = ((current_price - entry) / entry) * 100
    else:  # SHORT
        pnl_percent = ((entry - current_price) / entry) * 100
    
    return pnl_percent * LEVERAGE

def check_and_reset_daily_counters(symbol: str):
    """Reset daily PNL counters at midnight UTC"""
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

# ========================= SUPERTREND + FILTER INDICATORS =========================
def calculate_true_range(high1: float, low1: float, close0: float) -> float:
    """Calculate True Range"""
    tr1 = high1 - low1
    tr2 = abs(high1 - close0)
    tr3 = abs(low1 - close0)
    return max(tr1, tr2, tr3)

def calculate_rsi(symbol: str) -> Optional[float]:
    """Calculate RSI using completed candles"""
    klines = state[symbol]["klines"]
    if len(klines) < RSI_PERIODS + 2:
        return None
    
    completed = list(klines)[:-1]  # Exclude current incomplete candle
    if len(completed) < RSI_PERIODS + 1:
        return None
    
    closes = [k["close"] for k in completed[-(RSI_PERIODS + 1):]]
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    avg_gain = sum(gains) / RSI_PERIODS
    avg_loss = sum(losses) / RSI_PERIODS
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    state[symbol]["rsi"] = rsi
    return rsi

def calculate_directional_movement(high1: float, high0: float, low1: float, low0: float) -> tuple:
    """Calculate directional movement for ADX"""
    up_move = high1 - high0
    down_move = low0 - low1
    plus_dm  = up_move if (up_move > down_move and up_move > 0) else 0
    minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
    return plus_dm, minus_dm

def calculate_adx(symbol: str) -> Optional[float]:
    """Calculate ADX using completed candles"""
    klines = state[symbol]["klines"]
    if len(klines) < ADX_PERIODS + 2:
        return None
    
    completed = list(klines)[:-1]  # Exclude current incomplete candle
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
    
    # Exponential smoothing
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
    return adx

def calculate_atr(symbol: str) -> Optional[float]:
    """Calculate ATR using completed candles"""
    klines = state[symbol]["klines"]
    if len(klines) < SUPERTREND_ATR_PERIODS + 1:
        return None
    
    completed = list(klines)[:-1]  # Exclude current incomplete candle
    if len(completed) < SUPERTREND_ATR_PERIODS + 1:
        return None
    
    recent = completed[-(SUPERTREND_ATR_PERIODS + 1):]
    tr_values = []
    
    for i in range(1, len(recent)):
        cur = recent[i]
        prev = recent[i - 1]
        tr = calculate_true_range(cur["high"], cur["low"], prev["close"])
        tr_values.append(tr)
    
    if len(tr_values) < SUPERTREND_ATR_PERIODS:
        return None
    
    # Simple Moving Average of True Range
    atr = sum(tr_values[-SUPERTREND_ATR_PERIODS:]) / SUPERTREND_ATR_PERIODS
    state[symbol]["atr"] = atr
    return atr

def calculate_supertrend(symbol: str) -> Tuple[Optional[float], Optional[float], Optional[int]]:
    """Calculate Supertrend bands and trend direction"""
    klines = state[symbol]["klines"]
    if len(klines) < SUPERTREND_ATR_PERIODS + 2:
        return None, None, None
    
    atr = calculate_atr(symbol)
    if atr is None:
        return None, None, None
    
    # Get current candle data
    if len(klines) < 2:
        return None, None, None
    
    current = klines[-1]
    previous = klines[-2]
    
    # Calculate HL2 (actually using OHLC4: (O+H+L+C)/4 as source)
    # For current candle, use close as open (since we don't track opens separately)
    hl2 = (current["close"] + current["high"] + current["low"] + current["close"]) / 4
    prev_hl2 = (previous["close"] + previous["high"] + previous["low"] + previous["close"]) / 4
    
    # Calculate basic upper and lower bands
    basic_upper = hl2 + (SUPERTREND_MULTIPLIER * atr)
    basic_lower = hl2 - (SUPERTREND_MULTIPLIER * atr)
    
    # Get previous values
    prev_upper = state[symbol].get("supertrend_upper")
    prev_lower = state[symbol].get("supertrend_lower")
    prev_trend = state[symbol].get("supertrend_trend")
    prev_close = previous["close"]
    current_close = current["close"]
    
    # Calculate final upper and lower bands
    if prev_upper is None or basic_upper < prev_upper or prev_close > prev_upper:
        final_upper = basic_upper
    else:
        final_upper = prev_upper
        
    if prev_lower is None or basic_lower > prev_lower or prev_close < prev_lower:
        final_lower = basic_lower
    else:
        final_lower = prev_lower
    
    # Determine trend
    if prev_trend is None:
        trend = 1  # Start with uptrend
    elif prev_trend == -1 and current_close > final_lower:
        trend = 1  # Change to uptrend
    elif prev_trend == 1 and current_close < final_upper:
        trend = -1  # Change to downtrend
    else:
        trend = prev_trend  # Continue current trend
    
    # Store values
    state[symbol]["supertrend_upper"] = final_upper
    state[symbol]["supertrend_lower"] = final_lower
    state[symbol]["supertrend_trend"] = trend
    
    return final_upper, final_lower, trend

# ========================= TRADING LOGIC WITH FILTERS =========================
def update_trading_signals(symbol: str) -> dict:
    """Supertrend trading logic with RSI and ADX filters"""
    st = state[symbol]
    price = st["price"]
    current_signal = st["current_signal"]
    
    if price is None or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    check_and_reset_daily_counters(symbol)
    
    # Check daily limits
    if st["daily_limit_reached"]:
        if current_signal is not None:
            logging.info(f"{symbol} Daily limit reached, forcing exit")
            st["current_signal"] = None
            return {"changed": True, "action": "LIMIT_EXIT", "signal": None}
        return {"changed": False, "action": "NONE", "signal": None}
    
    # Check daily PNL limits for existing positions
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
    
    # Calculate all indicators
    upper, lower, trend = calculate_supertrend(symbol)
    adx = calculate_adx(symbol)
    rsi = calculate_rsi(symbol)
    prev_trend = st.get("supertrend_trend")
    
    if upper is None or lower is None or trend is None or adx is None or rsi is None:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    # Filter conditions
    adx_ok = (adx >= ADX_THRESHOLD)
    rsi_short_ok = (rsi < RSI_SHORT_THRESHOLD)
    rsi_long_ok = (rsi > RSI_LONG_THRESHOLD)
    
    new_signal = current_signal
    action_type = "NONE"
    
    # Check for trend changes with filters (main signals)
    if prev_trend is not None and trend != prev_trend:
        if trend == 1 and prev_trend == -1:
            # Trend changed from down to up - BUY signal (with filters)
            if adx_ok and rsi_long_ok:
                new_signal = "LONG"
                action_type = "TREND_CHANGE"
                logging.info(f"{symbol} SUPERTREND BUY SIGNAL (trend: DOWN→UP, price {price:.6f} > {lower:.6f}, ADX {adx:.1f}>={ADX_THRESHOLD}, RSI {rsi:.1f}>{RSI_LONG_THRESHOLD})")
            else:
                logging.info(f"{symbol} SUPERTREND trend changed to UP but filters failed (ADX {adx:.1f}, RSI {rsi:.1f}) - staying FLAT")
                
        elif trend == -1 and prev_trend == 1:
            # Trend changed from up to down - SELL signal (with filters)
            if adx_ok and rsi_short_ok:
                new_signal = "SHORT"
                action_type = "TREND_CHANGE"
                logging.info(f"{symbol} SUPERTREND SELL SIGNAL (trend: UP→DOWN, price {price:.6f} < {upper:.6f}, ADX {adx:.1f}>={ADX_THRESHOLD}, RSI {rsi:.1f}<{RSI_SHORT_THRESHOLD})")
            else:
                logging.info(f"{symbol} SUPERTREND trend changed to DOWN but filters failed (ADX {adx:.1f}, RSI {rsi:.1f}) - staying FLAT")
    
    # For no position, align with current trend (with filters)
    elif current_signal is None:
        if trend == 1 and adx_ok and rsi_long_ok:
            new_signal = "LONG"
            action_type = "ENTRY"
            logging.info(f"{symbol} SUPERTREND LONG ENTRY (uptrend + filters confirmed, price {price:.6f} > {lower:.6f}, ADX {adx:.1f}, RSI {rsi:.1f})")
        elif trend == -1 and adx_ok and rsi_short_ok:
            new_signal = "SHORT"
            action_type = "ENTRY"
            logging.info(f"{symbol} SUPERTREND SHORT ENTRY (downtrend + filters confirmed, price {price:.6f} < {upper:.6f}, ADX {adx:.1f}, RSI {rsi:.1f})")
    
    # Exit logic - exit when ALL conditions are met
    elif current_signal == "LONG":
        # Exit LONG when: trend DOWN + ADX low + RSI low
        if trend == -1 and adx < ADX_THRESHOLD and rsi <= RSI_LONG_THRESHOLD:
            new_signal = None
            action_type = "EXIT"
            logging.info(f"{symbol} SUPERTREND EXIT LONG (ALL conditions met: trend→DOWN, ADX {adx:.1f}<{ADX_THRESHOLD}, RSI {rsi:.1f}≤{RSI_LONG_THRESHOLD})")
        # Also exit on trend change alone if it's a strong reversal
        elif trend == -1:
            # Check if this is just a trend change without filter confirmation
            logging.info(f"{symbol} SUPERTREND trend changed to DOWN but filters not aligned (ADX {adx:.1f}, RSI {rsi:.1f}) - holding LONG position")
            
    elif current_signal == "SHORT":
        # Exit SHORT when: trend UP + ADX low + RSI high  
        if trend == 1 and adx < ADX_THRESHOLD and rsi >= RSI_SHORT_THRESHOLD:
            new_signal = None
            action_type = "EXIT"
            logging.info(f"{symbol} SUPERTREND EXIT SHORT (ALL conditions met: trend→UP, ADX {adx:.1f}<{ADX_THRESHOLD}, RSI {rsi:.1f}≥{RSI_SHORT_THRESHOLD})")
        # Also log when trend changes but filters not aligned
        elif trend == 1:
            logging.info(f"{symbol} SUPERTREND trend changed to UP but filters not aligned (ADX {adx:.1f}, RSI {rsi:.1f}) - holding SHORT position")
    
    # Handle signal changes
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
                logging.info("WebSocket connected (using hourly timeframe)")
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
                            
                            # Use hourly candles instead of minute candles
                            hour = int(event_time // 3600)  # Convert to hours
                            klines = state[symbol]["klines"]
                            
                            # Update hourly kline data
                            if not klines or klines[-1]["hour"] != hour:
                                klines.append({"hour": hour, "high": price, "low": price, "close": price, "open": price})
                            else:
                                klines[-1]["high"] = max(klines[-1]["high"], price)
                                klines[-1]["low"] = min(klines[-1]["low"], price)
                                klines[-1]["close"] = price
                            
                            # Check if ready (need all indicators)
                            required_candles = max(SUPERTREND_ATR_PERIODS, ADX_PERIODS, RSI_PERIODS) + 1
                            if len(klines) >= required_candles and not state[symbol]["ready"]:
                                atr = calculate_atr(symbol)
                                adx = calculate_adx(symbol)
                                rsi = calculate_rsi(symbol)
                                if atr is not None and adx is not None and rsi is not None:
                                    calculate_supertrend(symbol)
                                    state[symbol]["ready"] = True
                                    logging.info(f"{symbol} ready ({len(klines)} hourly candles, ATR {atr:.6f}, ADX {adx:.1f}, RSI {rsi:.1f})")
                            else:
                                # Always calculate indicators for ready symbols
                                if state[symbol]["ready"]:
                                    calculate_atr(symbol)
                                    calculate_adx(symbol)
                                    calculate_rsi(symbol)
                                    calculate_supertrend(symbol)
                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)
            await asyncio.sleep(5)

async def status_logger():
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"=== SUPERTREND STATUS REPORT {current_time} ===")
        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                candle_count = len(st["klines"])
                required = max(SUPERTREND_ATR_PERIODS, ADX_PERIODS, RSI_PERIODS) + 1
                remaining = max(0, required - candle_count)
                logging.info(f"{symbol}: Not ready - {candle_count}/{required} hourly candles ({remaining} more needed)")
                continue
            
            price = st["price"]
            if price is None:
                continue
                
            current_sig = st["current_signal"]
            display_sig = current_sig or "FLAT"
            atr = st.get("atr")
            adx = st.get("adx")
            rsi = st.get("rsi")
            upper = st.get("supertrend_upper")
            lower = st.get("supertrend_lower")
            trend = st.get("supertrend_trend")
            
            logging.info(f"{symbol}: Price={price:.6f} | Signal: {display_sig}")
            
            daily_pnl_color = "+" if st["daily_pnl"] >= 0 else ""
            logging.info(f"  Daily PNL: {daily_pnl_color}{st['daily_pnl']:.2f}% (Target: +{DAILY_PROFIT_TARGET}% / Limit: {DAILY_LOSS_LIMIT}%)")
            
            if st["daily_limit_reached"]:
                if st["daily_pnl"] >= DAILY_PROFIT_TARGET:
                    logging.info(f"  STATUS: PROFIT TARGET REACHED - Trading paused until next day")
                else:
                    logging.info(f"  STATUS: LOSS LIMIT HIT - Trading paused until next day")
                continue
            
            if atr and adx is not None and rsi is not None and upper and lower and trend is not None:
                trend_text = "UP" if trend == 1 else "DOWN"
                active_band = lower if trend == 1 else upper
                
                # Filter status
                adx_status = "✓" if adx >= ADX_THRESHOLD else "✗"
                rsi_long_status = "✓" if rsi > RSI_LONG_THRESHOLD else "✗"
                rsi_short_status = "✓" if rsi < RSI_SHORT_THRESHOLD else "✗"
                
                logging.info(f"  Supertrend: ATR={atr:.6f} | Trend: {trend_text} | Multiplier: {SUPERTREND_MULTIPLIER}")
                logging.info(f"  Upper Band: {upper:.6f} | Lower Band: {lower:.6f}")
                logging.info(f"  Active Band: {active_band:.6f} (current trailing stop)")
                logging.info(f"  Filters: ADX={adx:.1f}{adx_status}(≥{ADX_THRESHOLD}) | RSI={rsi:.1f} [LONG{rsi_long_status}(>{RSI_LONG_THRESHOLD}) | SHORT{rsi_short_status}(<{RSI_SHORT_THRESHOLD})]")
                
                if current_sig is None:
                    if trend == 1:
                        filter_status = "✓" if adx >= ADX_THRESHOLD and rsi > RSI_LONG_THRESHOLD else "✗"
                        logging.info(f"  FLAT - Uptrend active, filters {filter_status} for LONG entry")
                    else:
                        filter_status = "✓" if adx >= ADX_THRESHOLD and rsi < RSI_SHORT_THRESHOLD else "✗"
                        logging.info(f"  FLAT - Downtrend active, filters {filter_status} for SHORT entry")
                elif current_sig == "LONG":
                    current_pnl = calculate_pnl_percent(symbol, price)
                    # Check exit conditions
                    will_exit = trend == -1 and adx < ADX_THRESHOLD and rsi <= RSI_LONG_THRESHOLD
                    exit_status = "✓" if will_exit else "✗"
                    logging.info(f"  LONG - Current PNL: {current_pnl:+.2f}% | Protected by lower band {lower:.6f}")
                    logging.info(f"    Exit conditions {exit_status}: trend DOWN({trend == -1}) + ADX<{ADX_THRESHOLD}({adx < ADX_THRESHOLD}) + RSI≤{RSI_LONG_THRESHOLD}({rsi <= RSI_LONG_THRESHOLD})")
                elif current_sig == "SHORT":
                    current_pnl = calculate_pnl_percent(symbol, price)
                    # Check exit conditions  
                    will_exit = trend == 1 and adx < ADX_THRESHOLD and rsi >= RSI_SHORT_THRESHOLD
                    exit_status = "✓" if will_exit else "✗"
                    logging.info(f"  SHORT - Current PNL: {current_pnl:+.2f}% | Protected by upper band {upper:.6f}")
                    logging.info(f"    Exit conditions {exit_status}: trend UP({trend == 1}) + ADX<{ADX_THRESHOLD}({adx < ADX_THRESHOLD}) + RSI≥{RSI_SHORT_THRESHOLD}({rsi >= RSI_SHORT_THRESHOLD})") = calculate_pnl_percent(symbol, price)
                    logging.info(f"  LONG - Current PNL: {current_pnl:+.2f}% | Protected by lower band {lower:.6f}")
                    logging.info(f"    Will exit if trend changes to DOWN (price < {upper:.6f})")
                elif current_sig == "SHORT":
                    current_pnl = calculate_pnl_percent(symbol, price)
                    logging.info(f"  SHORT - Current PNL: {current_pnl:+.2f}% | Protected by upper band {upper:.6f}")
                    logging.info(f"    Will exit if trend changes to UP (price > {lower:.6f})")
        
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
            
            # Calculate target position
            if current_signal == "LONG":
                final_position = target_size
            elif current_signal == "SHORT":
                final_position = -target_size
            elif current_signal is None:
                final_position = 0.0
            else:
                final_position = st["current_position"]
            
            # Execute if signal changed
            if signal_result["changed"]:
                current_pos = st["current_position"]
                if abs(final_position - current_pos) > 1e-12:
                    await execute_position_change(client, symbol, final_position, current_pos)

async def execute_position_change(client: AsyncClient, symbol: str, target: float, current: float):
    st = state[symbol]
    now = time.time()
    last_target = st.get("last_target", None)
    last_when = st.get("last_exec_ts", 0.0)
    
    # Prevent duplicate executions
    if last_target is not None and abs(target - last_target) < 1e-12 and (now - last_when) < 2.0:
        logging.info(f"{symbol} dedup: skipping duplicate execution")
        return
    if abs(target - current) < 1e-12:
        return
    
    try:
        # Calculate PNL when closing position
        if current != 0.0 and target == 0.0 and st["entry_price"] is not None:
            current_price = st["price"]
            closed_pnl = calculate_pnl_percent(symbol, current_price)
            st["daily_pnl"] += closed_pnl
            pnl_sign = "+" if closed_pnl >= 0 else ""
            logging.info(f"{symbol} Position closed with {pnl_sign}{closed_pnl:.2f}% PNL. Daily total: {st['daily_pnl']:+.2f}%")
        
        # Execute order logic
        if target == 0.0:
            # Close all positions
            if current > 0:
                ok = await place_order(client, symbol, "SELL", current, "LONG CLOSE")
                if not ok:
                    return
            elif current < 0:
                ok = await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
                if not ok:
                    return
        elif target > 0:
            # Go long
            if current < 0:
                # Close short first, then open long
                ok = await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
                if not ok:
                    return
                ok = await place_order(client, symbol, "BUY", target, "LONG ENTRY")
                if not ok:
                    return
            else:
                # Adjust long position
                if target > current:
                    ok = await place_order(client, symbol, "BUY", target - current, "LONG ENTRY")
                    if not ok:
                        return
                else:
                    ok = await place_order(client, symbol, "SELL", current - target, "LONG CLOSE")
                    if not ok:
                        return
        else:
            # Go short (target < 0)
            if current > 0:
                # Close long first, then open short
                ok = await place_order(client, symbol, "SELL", current, "LONG CLOSE")
                if not ok:
                    return
                ok = await place_order(client, symbol, "SELL", abs(target), "SHORT ENTRY")
                if not ok:
                    return
            else:
                # Adjust short position
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
        
        # Update entry price
        if target != 0.0 and current == 0.0:
            st["entry_price"] = st["price"]
            logging.info(f"{symbol} Entry price set: {st['entry_price']:.6f}")
        elif target == 0.0:
            st["entry_price"] = None
        
        # Update state
        st["current_position"] = target
        st["last_target"] = target
        st["last_exec_ts"] = now
    except Exception as e:
        logging.error(f"{symbol} position change failed: {e}")

async def init_bot(client: AsyncClient):
    logging.info("Initializing Supertrend bot...")
    load_klines()
    symbols_needing_data = []
    
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        required_periods = max(SUPERTREND_ATR_PERIODS, ADX_PERIODS, RSI_PERIODS)
        atr = calculate_atr(symbol)
        adx = calculate_adx(symbol)
        rsi = calculate_rsi(symbol)
        
        if len(klines) >= required_periods + 1 and atr is not None and adx is not None and rsi is not None:
            calculate_supertrend(symbol)
            state[symbol]["ready"] = True
            logging.info(f"{symbol} ready ({len(klines)} hourly candles, ATR {atr:.6f}, ADX {adx:.1f}, RSI {rsi:.1f})")
        else:
            symbols_needing_data.append(symbol)
            logging.info(f"{symbol} needs data ({len(klines)}/{required_periods + 1} hourly candles)")
    
    if symbols_needing_data:
        logging.info(f"Fetching historical data for {len(symbols_needing_data)} symbols...")
        successful_fetches = 0
        required_periods = max(SUPERTREND_ATR_PERIODS, ADX_PERIODS, RSI_PERIODS)
        
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")
                # Fetch hourly klines instead of minute klines
                klines_data = await safe_api_call(client.futures_klines, symbol=symbol, interval=TIMEFRAME, limit=required_periods + 20)
                st = state[symbol]
                st["klines"].clear()
                
                for kline in klines_data:
                    hour = int(float(kline[0]) / 1000 // 3600)  # Convert to hours
                    st["klines"].append({
                        "hour": hour, 
                        "open": float(kline[1]),
                        "high": float(kline[2]), 
                        "low": float(kline[3]), 
                        "close": float(kline[4])
                    })
                
                atr = calculate_atr(symbol)
                adx = calculate_adx(symbol)
                rsi = calculate_rsi(symbol)
                
                if atr is not None and adx is not None and rsi is not None:
                    calculate_supertrend(symbol)
                    st["ready"] = True
                    logging.info(f"{symbol} ready ({len(st['klines'])} hourly candles, ATR {atr:.6f}, ADX {adx:.1f}, RSI {rsi:.1f})")
                    successful_fetches += 1
                else:
                    logging.warning(f"{symbol} insufficient data ({len(st['klines'])} hourly candles)")
                
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)  # Rate limiting
            except Exception as e:
                logging.error(f"{symbol} fetch failed: {e}")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)
        logging.info(f"Fetch complete: {successful_fetches}/{len(symbols_needing_data)} successful")
    else:
        logging.info("All symbols ready from loaded data")
    
    # Synchronize positions
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
    client = await AsyncClient.create(API_KEY, API_SECRET)
    atexit.register(save_klines)
    try:
        await init_bot(client)
        price_task = asyncio.create_task(price_feed_loop(client))
        trade_task = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())
        logging.info("Supertrend bot started")
        await asyncio.gather(price_task, trade_task, status_task)
    finally:
        await client.close_connection()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    print("=" * 80)
    print("SUPERTREND + FILTERS TRADING BOT - Anti-Ping-Pong Version")
    print("=" * 80)
    print("SUPERTREND + ADX + RSI STRATEGY:")
    print("Trend-Following with volatility and momentum filters to reduce false signals")
    print("")
    print("CURRENT CONFIGURATION:")
    print(f"  Timeframe: {TIMEFRAME} (hourly candles)")
    print(f"  ATR Period: {SUPERTREND_ATR_PERIODS} hours")
    print(f"  ATR Multiplier: {SUPERTREND_MULTIPLIER}")
    print(f"  ADX Period: {ADX_PERIODS} hours")
    print(f"  ADX Threshold: {ADX_THRESHOLD} (volatility filter)")
    print(f"  RSI Period: {RSI_PERIODS} hours")
    print(f"  RSI Thresholds: SHORT<{RSI_SHORT_THRESHOLD} | LONG>{RSI_LONG_THRESHOLD}")
    print(f"  Source: OHLC4 (Open + High + Low + Close) / 4")
    print("")
    print("HOURLY TIMEFRAME BENEFITS:")
    print("  - Much larger ATR values for better band separation")
    print("  - Reduced noise compared to minute-based signals")
    print("  - More reliable trend detection over longer periods")
    print("  - Less frequent but higher-quality signals")
    print("  - Better suited for swing trading approach")
    print("")
    print("ENTRY SIGNALS (All conditions required):")
    print("  LONG Entry:")
    print("    - Supertrend changes from DOWN to UP")
    print(f"    - ADX >= {ADX_THRESHOLD} (confirms volatility)")
    print(f"    - RSI > {RSI_LONG_THRESHOLD} (confirms upward momentum)")
    print("")
    print("  SHORT Entry:")
    print("    - Supertrend changes from UP to DOWN")
    print(f"    - ADX >= {ADX_THRESHOLD} (confirms volatility)")
    print(f"    - RSI < {RSI_SHORT_THRESHOLD} (confirms downward momentum)")
    print("")
    print("EXIT SIGNALS (All conditions required):")
    print("  LONG Exit (ALL must be true):")
    print("    - Supertrend changes to DOWN")
    print(f"    - ADX falls below {ADX_THRESHOLD} (volatility decreases)")
    print(f"    - RSI falls to {RSI_LONG_THRESHOLD} or below (momentum weakens)")
    print("")
    print("  SHORT Exit (ALL must be true):")
    print("    - Supertrend changes to UP") 
    print(f"    - ADX falls below {ADX_THRESHOLD} (volatility decreases)")
    print(f"    - RSI rises to {RSI_SHORT_THRESHOLD} or above (momentum weakens)")
    print("")
    print("POSITION HOLDING:")
    print("  - If Supertrend changes but filters don't align → HOLD position")
    print("  - Only exits when ALL three conditions confirm the reversal")
    print("  - Prevents premature exits during temporary pullbacks")
    print("")
    print("ANTI-PING-PONG BENEFITS:")
    print("  - ADX filter prevents trading in sideways/low volatility markets")
    print("  - RSI filter confirms momentum direction before entry")
    print("  - Reduces false breakouts and whipsaw trades")
    print("  - Only exits on trend change (no filter interference)")
    print("")
    print("DAILY PNL LIMITS (Per Symbol):")
    print(f"  Profit Target: +{DAILY_PROFIT_TARGET}% → Stop trading for the day")
    print(f"  Loss Limit: {DAILY_LOSS_LIMIT}% → Stop trading for the day")
    print("  Resets: Midnight UTC every day")
    print("")
    print("ADJUSTABLE SETTINGS:")
    print("  Add to your .env file to customize:")
    print("    SUPERTREND_ATR_PERIODS=19      # ATR calculation period in hours")
    print("    SUPERTREND_MULTIPLIER=2.0      # ATR multiplier for bands")
    print("    ADX_PERIODS=19                 # ADX calculation period in hours")
    print("    ADX_THRESHOLD=25.0             # Minimum ADX for entry")
    print("    RSI_PERIODS=19                 # RSI calculation period in hours")
    print("    RSI_LONG_THRESHOLD=51.0        # RSI threshold for LONG")
    print("    RSI_SHORT_THRESHOLD=49.0       # RSI threshold for SHORT")
    print("    DAILY_PROFIT_TARGET=200.0      # Daily profit target %")
    print("    DAILY_LOSS_LIMIT=-100.0        # Daily loss limit %")
    print("")
    print("PERIOD GUIDE (in hours):")
    print("  - Shorter periods (12-24h): More responsive to recent changes")
    print("  - Medium periods (24-48h): Balanced approach (recommended)")
    print("  - Longer periods (48h+): Smoother, longer-term trends")
    print("  - Default 19h: Captures full trading day plus some overlap")
    print("")
    print("FILTER GUIDE:")
    print("  ADX Threshold:")
    print("    - Lower (15-20): More signals, some in sideways markets")
    print("    - Medium (25-30): Balanced (recommended)")
    print("    - Higher (35+): Only very strong trends")
    print("")
    print("  RSI Thresholds:")
    print("    - Closer to 50: More signals, earlier entries")
    print("    - Further from 50: Fewer signals, stronger confirmations")
    print("    - Current gap (49-51): Small buffer around neutral")
    print("")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print(f"Leverage: {LEVERAGE}x")
    print("=" * 80)
    asyncio.run(main())
