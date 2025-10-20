#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time
import atexit
from binance import AsyncClient
from collections import deque
from typing import Optional
from dotenv import load_dotenv
import numpy as np

# ========================= CONFIG =========================
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE = int(os.getenv("LEVERAGE", "50"))
USE_LIVE_CANDLE = True

# TAMA (Triple-Layer Adaptive Moving Average) PARAMETERS
USE_TAMA = True  # Toggle TAMA system

# Layer 1: Kalman Filter Parameters
KALMAN_Q = 0.001  # Process noise covariance (lower = smoother)
KALMAN_R = 0.01   # Measurement noise covariance (lower = more responsive)

# Layer 2: JMA Parameters
JMA_LENGTH_HIGH = 50
JMA_LENGTH_LOW = 50
JMA_LENGTH_CLOSE = 7
JMA_PHASE = 0
JMA_POWER = 3

# Layer 3: Efficiency Ratio Parameters
ER_PERIODS = 50
ER_THRESHOLD = 0.0
ALPHA_WEIGHT = 1.0  # ER influence multiplier

# Trailing Stop Configuration
TRAILING_STOP_PERCENT = 0.5

# Timeframe configuration
BASE_TIMEFRAME = "5m"

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
elif BASE_TIMEFRAME == "5m":
    BASE_MINUTES = 5
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

# Trading symbols and sizes
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
    "ETHUSDT": 3, "BNBUSDT": 2, "XRPUSDT": 1, "SOLUSDT": 3, 
    "ADAUSDT": 0, "DOGEUSDT": 0, "TRXUSDT": 0
}

MA_PERIODS = max(JMA_LENGTH_HIGH, JMA_LENGTH_LOW, JMA_LENGTH_CLOSE)
KLINE_LIMIT = max(100, MA_PERIODS + 100, ER_PERIODS + 100)

ENTRY_STRATEGY = "SYMMETRIC"

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        # Kalman Filter state (Layer 1)
        "kalman_x_high": None,
        "kalman_p_high": 1.0,
        "kalman_x_low": None,
        "kalman_p_low": 1.0,
        "kalman_x_close": None,
        "kalman_p_close": 1.0,
        "kalman_high": None,
        "kalman_low": None,
        "kalman_close": None,
        # JMA values (Layer 2)
        "jma_high": None,
        "jma_low": None,
        "jma_close": None,
        # TAMA final values (Layer 3 - adaptive)
        "tama_high": None,
        "tama_low": None,
        "tama_close": None,
        # Previous TAMA values
        "prev_tama_high": None,
        "prev_tama_low": None,
        "prev_tama_close": None,
        # Efficiency Ratio
        "efficiency_ratio": None,
        "er_ready": False,
        "ready": False,
        # LONG position tracking
        "long_position": 0.0,
        "long_trailing_stop_price": None,
        "long_peak_price": None,
        "last_long_exec_ts": 0.0,
        # SHORT position tracking
        "short_position": 0.0,
        "short_trailing_stop_price": None,
        "short_lowest_price": None,
        "last_short_exec_ts": 0.0,
        "stop_warning_logged": False,
    }
    for symbol in SYMBOLS
}

api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE FUNCTIONS =========================
def save_klines():
    save_data = {sym: list(state[sym]["klines"]) for sym in SYMBOLS}
    with open('klines.json', 'w') as f:
        json.dump(save_data, f)
    logging.info("📥 Saved klines to klines.json")

def load_klines():
    try:
        with open('klines.json', 'r') as f:
            load_data = json.load(f)
        for sym in SYMBOLS:
            state[sym]["klines"] = deque(load_data.get(sym, []), maxlen=KLINE_LIMIT)
        logging.info("📤 Loaded klines from klines.json")
    except FileNotFoundError:
        logging.info("No klines.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load klines: {e} - starting fresh")
    # Rebuild Kalman states from loaded klines
    for symbol in SYMBOLS:
        for kline in state[symbol]["klines"]:
            kalman_filter(symbol, "high", kline["high"])
            kalman_filter(symbol, "low", kline["low"])
            kalman_filter(symbol, "close", kline["close"])

def save_positions():
    position_data = {}
    for sym in SYMBOLS:
        position_data[sym] = {
            "long_position": state[sym]["long_position"],
            "long_trailing_stop_price": state[sym]["long_trailing_stop_price"],
            "long_peak_price": state[sym]["long_peak_price"],
            "short_position": state[sym]["short_position"],
            "short_trailing_stop_price": state[sym]["short_trailing_stop_price"],
            "short_lowest_price": state[sym]["short_lowest_price"],
        }
    with open('positions.json', 'w') as f:
        json.dump(position_data, f, indent=2)

def load_positions():
    try:
        with open('positions.json', 'r') as f:
            position_data = json.load(f)
        logging.info("💾 Loading positions from positions.json...")
        for sym in SYMBOLS:
            if sym not in position_data:
                logging.info(f"   [{sym}]: No saved data - starting fresh")
                continue
            loaded_long = position_data[sym].get("long_position", 0.0)
            loaded_short = position_data[sym].get("short_position", 0.0)
            state[sym]["long_position"] = loaded_long
            state[sym]["long_trailing_stop_price"] = position_data[sym].get("long_trailing_stop_price")
            state[sym]["long_peak_price"] = position_data[sym].get("long_peak_price")
            state[sym]["short_position"] = loaded_short
            state[sym]["short_trailing_stop_price"] = position_data[sym].get("short_trailing_stop_price")
            state[sym]["short_lowest_price"] = position_data[sym].get("short_lowest_price")
            if loaded_long > 0:
                if state[sym]["long_trailing_stop_price"] is None or state[sym]["long_peak_price"] is None:
                    logging.warning(f"⚠️ [{sym}] STALE LONG DATA: position={loaded_long} but stops missing")
                else:
                    logging.info(f"✅ [{sym}] LONG loaded: pos={loaded_long}, peak={state[sym]['long_peak_price']:.6f}")
            if loaded_short > 0:
                if state[sym]["short_trailing_stop_price"] is None or state[sym]["short_lowest_price"] is None:
                    logging.warning(f"⚠️ [{sym}] STALE SHORT DATA: position={loaded_short} but stops missing")
                else:
                    logging.info(f"✅ [{sym}] SHORT loaded: pos={loaded_short}, low={state[sym]['short_lowest_price']:.6f}")
            if loaded_long == 0 and loaded_short == 0:
                logging.info(f"   [{sym}]: FLAT (no positions)")
        logging.info("💾 Position loading complete")
    except FileNotFoundError:
        logging.info("💾 No positions.json found - all symbols starting fresh")
    except Exception as e:
        logging.error(f"❌ Failed to load positions: {e} - all symbols starting fresh")

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    prec = PRECISIONS.get(symbol, 3)
    return round(size, prec)

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
        logging.info(f"🚀 {symbol} {action} EXECUTED - {side} {quantity} - OrderID: {result.get('orderId')}")
        return True
    except Exception as e:
        logging.error(f"❌ {symbol} {action} FAILED: {e}")
        return False

# ========================= TRAILING STOP FUNCTIONS =========================
def initialize_trailing_stop(symbol: str, side: str, entry_price: float):
    st = state[symbol]
    if side == "LONG":
        st["long_peak_price"] = entry_price
        st["long_trailing_stop_price"] = entry_price * (1 - TRAILING_STOP_PERCENT / 100)
        logging.info(f"🎯 {symbol} LONG Trailing Stop initialized: Peak={entry_price:.6f}, Stop={st['long_trailing_stop_price']:.6f}")
    elif side == "SHORT":
        st["short_lowest_price"] = entry_price
        st["short_trailing_stop_price"] = entry_price * (1 + TRAILING_STOP_PERCENT / 100)
        logging.info(f"🎯 {symbol} SHORT Trailing Stop initialized: Lowest={entry_price:.6f}, Stop={st['short_trailing_stop_price']:.6f}")
    st["stop_warning_logged"] = False
    save_positions()

def update_trailing_stop(symbol: str, current_price: float) -> dict:
    st = state[symbol]
    result = {"long_hit": False, "short_hit": False}
    if st["long_position"] > 0:
        if st["long_peak_price"] is None or st["long_trailing_stop_price"] is None:
            if not st["stop_warning_logged"]:
                logging.warning(f"⚠️ {symbol} LONG position exists but stop is None - will auto-fix")
                st["stop_warning_logged"] = True
            return result
        if current_price > st["long_peak_price"]:
            st["long_peak_price"] = current_price
            new_stop = current_price * (1 - TRAILING_STOP_PERCENT / 100)
            if new_stop > st["long_trailing_stop_price"]:
                st["long_trailing_stop_price"] = new_stop
                save_positions()
        if current_price <= st["long_trailing_stop_price"]:
            loss_percent = ((st["long_peak_price"] - current_price) / st["long_peak_price"]) * 100
            position_loss = loss_percent * LEVERAGE
            logging.info(f"🛑 {symbol} LONG Trailing Stop HIT! Price={current_price:.6f} <= Stop={st['long_trailing_stop_price']:.6f}")
            logging.info(f"   Price fell {loss_percent:.2f}% from peak ${st['long_peak_price']:.6f} (~{position_loss:.1f}% position loss)")
            result["long_hit"] = True
    if st["short_position"] > 0:
        if st["short_lowest_price"] is None or st["short_trailing_stop_price"] is None:
            if not st["stop_warning_logged"]:
                logging.warning(f"⚠️ {symbol} SHORT position exists but stop is None - will auto-fix")
                st["stop_warning_logged"] = True
            return result
        if current_price < st["short_lowest_price"]:
            st["short_lowest_price"] = current_price
            new_stop = current_price * (1 + TRAILING_STOP_PERCENT / 100)
            if new_stop < st["short_trailing_stop_price"]:
                st["short_trailing_stop_price"] = new_stop
                save_positions()
        if current_price >= st["short_trailing_stop_price"]:
            loss_percent = ((current_price - st["short_lowest_price"]) / st["short_lowest_price"]) * 100
            position_loss = loss_percent * LEVERAGE
            logging.info(f"🛑 {symbol} SHORT Trailing Stop HIT! Price={current_price:.6f} >= Stop={st['short_trailing_stop_price']:.6f}")
            logging.info(f"   Price rose {loss_percent:.2f}% from lowest ${st['short_lowest_price']:.6f} (~{position_loss:.1f}% position loss)")
            result["short_hit"] = True
    return result

def reset_trailing_stop(symbol: str, side: str):
    st = state[symbol]
    if side == "LONG":
        st["long_trailing_stop_price"] = None
        st["long_peak_price"] = None
    elif side == "SHORT":
        st["short_trailing_stop_price"] = None
        st["short_lowest_price"] = None
    save_positions()

# ========================= TAMA INDICATOR CALCULATIONS =========================

def kalman_filter(symbol: str, field: str, measurement: float) -> Optional[float]:
    """
    LAYER 1: Kalman Filter
    Removes noise from raw price data and provides predictive smoothing
    Returns: Kalman-filtered value
    """
    st = state[symbol]
    
    if field == "high":
        x_key = "kalman_x_high"
        p_key = "kalman_p_high"
    elif field == "low":
        x_key = "kalman_x_low"
        p_key = "kalman_p_low"
    elif field == "close":
        x_key = "kalman_x_close"
        p_key = "kalman_p_close"
    else:
        raise ValueError(f"Invalid field: {field}")
    
    # Initialize Kalman state on first call
    if st[x_key] is None:
        st[x_key] = measurement
        st[p_key] = 1.0
        return measurement
    
    # Prediction step
    x_pred = st[x_key]
    p_pred = st[p_key] + KALMAN_Q
    
    # Update step
    kalman_gain = p_pred / (p_pred + KALMAN_R)
    x_updated = x_pred + kalman_gain * (measurement - x_pred)
    p_updated = (1 - kalman_gain) * p_pred
    
    # Store updated state
    st[x_key] = x_updated
    st[p_key] = p_updated
    
    return x_updated

def apply_kalman_to_klines(symbol: str):
    """
    Apply Kalman filter to high, low, close of latest kline
    Stores results in state for JMA consumption
    """
    klines = state[symbol]["klines"]
    if len(klines) == 0:
        return
    
    latest = klines[-1]
    
    # Apply Kalman filter to each field
    state[symbol]["kalman_high"] = kalman_filter(symbol, "high", latest["high"])
    state[symbol]["kalman_low"] = kalman_filter(symbol, "low", latest["low"])
    state[symbol]["kalman_close"] = kalman_filter(symbol, "close", latest["close"])

def calculate_jma_from_kalman(symbol: str, field: str, length: int, phase: int = 50, power: int = 2) -> Optional[float]:
    """
    LAYER 2: Jurik Moving Average
    Feeds Kalman-filtered data into JMA for low-lag smoothing
    Returns: JMA value from Kalman-filtered data
    """
    klines = state[symbol]["klines"]
    if len(klines) < length + 1:
        return None
    
    if USE_LIVE_CANDLE:
        completed = list(klines)
    else:
        completed = list(klines)[:-1]
    
    if len(completed) < length:
        return None
    
    # Simulate Kalman filter sequentially on historical data for the field
    values = []
    temp_x = None
    temp_p = 1.0
    for k in completed:
        measurement = k[field]
        if temp_x is None:
            temp_x = measurement
            temp_p = 1.0
            kalman_val = measurement
        else:
            x_pred = temp_x
            p_pred = temp_p + KALMAN_Q
            kalman_gain = p_pred / (p_pred + KALMAN_R)
            kalman_val = x_pred + kalman_gain * (measurement - x_pred)
            temp_p = (1 - kalman_gain) * p_pred
            temp_x = kalman_val
        values.append(kalman_val)
    
    if None in values:
        return None
    
    # JMA calculation on Kalman-filtered data
    phaseRatio = 0.5 if phase < -100 else (2.5 if phase > 100 else phase / 100 + 1.5)
    beta = 0.45 * (length - 1) / (0.45 * (length - 1) + 2)
    alpha = beta ** power
    
    e0 = 0.0
    e1 = 0.0
    e2 = 0.0
    jma = 0.0
    
    for src in values:
        e0 = (1 - alpha) * src + alpha * e0
        e1 = (src - e0) * (1 - beta) + beta * e1
        e2 = (e0 + phaseRatio * e1 - jma) * ((1 - alpha) ** 2) + (alpha ** 2) * e2
        jma = e2 + jma
    
    return jma

def calculate_efficiency_ratio(symbol: str) -> Optional[float]:
    """
    LAYER 3 INPUT: Efficiency Ratio (Kaufman)
    Measures trend strength for adaptive weighting
    Returns: ER value (0.0 = choppy, 1.0 = strong trend)
    """
    klines = list(state[symbol]["klines"])
    if USE_LIVE_CANDLE:
        completed = klines
    else:
        completed = klines[:-1]
    if len(completed) < ER_PERIODS + 1:
        return None
    closes = [k["close"] for k in completed[-(ER_PERIODS + 1):]]
    net_change = abs(closes[-1] - closes[0])
    sum_changes = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
    if sum_changes == 0:
        return None
    er = net_change / sum_changes
    state[symbol]["efficiency_ratio"] = er
    state[symbol]["er_ready"] = True
    return er

def calculate_tama(symbol: str, jma_value: Optional[float], kalman_value: float, er: Optional[float]) -> Optional[float]:
    """
    LAYER 3: Triple-Layer Adaptive Moving Average (TAMA)
    Final adaptive layer that adjusts JMA responsiveness based on ER
    
    Formula: TAMA = JMA + (α × ER × (Kalman_Price - JMA))
    
    Flow:
    1. Raw Price → Kalman Filter (noise reduction)
    2. Kalman Output → JMA (low-lag smoothing)
    3. JMA + ER → TAMA (adaptive responsiveness)
    
    When ER is HIGH (trending): TAMA pulls closer to price (fast)
    When ER is LOW (choppy): TAMA stays closer to JMA (smooth)
    """
    if jma_value is None:
        return None
    
    if not USE_TAMA or er is None:
        return jma_value
    
    # Adaptive adjustment based on efficiency ratio
    adjustment = ALPHA_WEIGHT * er * (kalman_value - jma_value)
    tama = jma_value + adjustment
    
    return tama

# ========================= TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> dict:
    """
    TAMA RIBBON Strategy
    Uses Triple-Layer Adaptive Moving Average for entries
    TRUE HEDGE MODE - can hold LONG and SHORT simultaneously
    """
    st = state[symbol]
    price = st["price"]
    result = {"long_entry": False, "short_entry": False}
    
    if price is None or not st["ready"]:
        return result
    
    # Apply Kalman filter to current kline
    apply_kalman_to_klines(symbol)
    
    # Calculate JMA from Kalman-filtered data (Layer 1 → Layer 2)
    jma_high = calculate_jma_from_kalman(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
    jma_low = calculate_jma_from_kalman(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
    jma_close = calculate_jma_from_kalman(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
    
    # Calculate Efficiency Ratio for adaptive weighting
    er = calculate_efficiency_ratio(symbol)
    
    if jma_high is None or jma_low is None or jma_close is None or er is None:
        return result
    
    # Apply TAMA formula (Layer 2 → Layer 3)
    tama_high = calculate_tama(symbol, jma_high, st["kalman_high"], er)
    tama_low = calculate_tama(symbol, jma_low, st["kalman_low"], er)
    tama_close = calculate_tama(symbol, jma_close, st["kalman_close"], er)
    
    if tama_high is None or tama_low is None or tama_close is None:
        return result
    
    # Store current TAMA values
    st["tama_high"] = tama_high
    st["tama_low"] = tama_low
    st["tama_close"] = tama_close
    
    prev_tama_high = st["prev_tama_high"]
    prev_tama_low = st["prev_tama_low"]
    prev_tama_close = st["prev_tama_close"]
    
    # Initialize previous values
    if prev_tama_high is None or prev_tama_low is None or prev_tama_close is None:
        st["prev_tama_high"] = tama_high
        st["prev_tama_low"] = tama_low
        st["prev_tama_close"] = tama_close
        return result
    
    # ER filter
    er_allows_trading = (er >= ER_THRESHOLD)
    if not er_allows_trading:
        st["prev_tama_high"] = tama_high
        st["prev_tama_low"] = tama_low
        st["prev_tama_close"] = tama_close
        return result
    
    # CROSSOVER STRATEGY
    if ENTRY_STRATEGY == "CROSSOVER":
        cross_above_high = (tama_close > tama_high) and (prev_tama_close <= prev_tama_high)
        cross_below_low = (tama_close < tama_low) and (prev_tama_close >= prev_tama_low)
        aligned_long = (price > tama_close) and (tama_close > tama_high)
        aligned_short = (price < tama_close) and (tama_close < tama_low)
        
        if st["long_position"] == 0:
            if cross_above_high or aligned_long:
                result["long_entry"] = True
                adjustment_pct = ((tama_close - jma_close) / jma_close) * 100 if jma_close != 0 else 0
                logging.info(f"🟢 {symbol} ENTRY LONG (TAMA CROSSOVER)")
                logging.info(f"   Price={price:.6f}, TAMA_Close={tama_close:.6f}, TAMA_High={tama_high:.6f}")
                logging.info(f"   ER={er:.3f}, α={ALPHA_WEIGHT}, Adjustment={adjustment_pct:+.2f}%")
        
        if st["short_position"] == 0:
            if cross_below_low or aligned_short:
                result["short_entry"] = True
                adjustment_pct = ((tama_close - jma_close) / jma_close) * 100 if jma_close != 0 else 0
                logging.info(f"🟢 {symbol} ENTRY SHORT (TAMA CROSSOVER)")
                logging.info(f"   Price={price:.6f}, TAMA_Close={tama_close:.6f}, TAMA_Low={tama_low:.6f}")
                logging.info(f"   ER={er:.3f}, α={ALPHA_WEIGHT}, Adjustment={adjustment_pct:+.2f}%")
    
    # SYMMETRIC STRATEGY
    elif ENTRY_STRATEGY == "SYMMETRIC":
        price_above_ribbon = (price > tama_high) and (price > tama_low)
        price_below_ribbon = (price < tama_high) and (price < tama_low)
        
        if st["long_position"] == 0:
            if price_above_ribbon:
                result["long_entry"] = True
                logging.info(f"🟢 {symbol} ENTRY LONG (TAMA SYMMETRIC)")
                logging.info(f"   Price={price:.6f} > TAMA_High={tama_high:.6f} & TAMA_Low={tama_low:.6f}")
                logging.info(f"   ER={er:.3f}, α={ALPHA_WEIGHT}")
        
        if st["short_position"] == 0:
            if price_below_ribbon:
                result["short_entry"] = True
                logging.info(f"🟢 {symbol} ENTRY SHORT (TAMA SYMMETRIC)")
                logging.info(f"   Price={price:.6f} < TAMA_High={tama_high:.6f} & TAMA_Low={tama_low:.6f}")
                logging.info(f"   ER={er:.3f}, α={ALPHA_WEIGHT}")
    
    # Update previous values
    st["prev_tama_high"] = tama_high
    st["prev_tama_low"] = tama_low
    st["prev_tama_close"] = tama_close
    
    return result

# ========================= MAIN LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    streams = [f"{s.lower()}@kline_{BASE_TIMEFRAME.lower()}" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logging.info("📡 WebSocket connected")
                async for message in ws:
                    try:
                        data = json.loads(message).get("data", {})
                        if "k" in data:
                            k = data["k"]
                            symbol = k["s"]
                            if symbol in SYMBOLS:
                                state[symbol]["price"] = float(k["c"])
                                kline_data = {
                                    "open_time": int(k["t"] / 1000),
                                    "open": float(k["o"]),
                                    "high": float(k["h"]),
                                    "low": float(k["l"]),
                                    "close": float(k["c"])
                                }
                                klines = state[symbol]["klines"]
                                if klines and klines[-1]["open_time"] == kline_data["open_time"]:
                                    klines[-1] = kline_data
                                else:
                                    klines.append(kline_data)
                                if len(state[symbol]["klines"]) >= MA_PERIODS and not state[symbol]["ready"]:
                                    apply_kalman_to_klines(symbol)
                                    jma_high = calculate_jma_from_kalman(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
                                    jma_low = calculate_jma_from_kalman(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
                                    jma_close = calculate_jma_from_kalman(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                                    er = calculate_efficiency_ratio(symbol)
                                    if (jma_high is not None) and (jma_low is not None) and (jma_close is not None) and (er is not None):
                                        state[symbol]["ready"] = True
                                        candle_mode = " [LIVE]" if USE_LIVE_CANDLE else " [COMPLETED]"
                                        tama_status = " [TAMA: Kalman+JMA+ER]" if USE_TAMA else ""
                                        logging.info(f"✅ {symbol} ready - TAMA initialized{candle_mode}{tama_status}")
                                else:
                                    calculate_efficiency_ratio(symbol)
                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)

async def status_logger():
    """2-minute status report"""
    while True:
        await asyncio.sleep(120)
        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"📊 === STATUS REPORT {current_time} ===")
        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                candle_count = len(st["klines"])
                price = st["price"]
                price_str = f"Price={price:.6f} | " if price else ""
                logging.info(f"{symbol}: {price_str}Not ready - {candle_count} {BASE_TIMEFRAME} candles")
                continue
            price = st["price"]
            er = st.get("efficiency_ratio")
            if price and er is not None:
                apply_kalman_to_klines(symbol)
                jma_high = calculate_jma_from_kalman(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
                jma_low = calculate_jma_from_kalman(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
                jma_close = calculate_jma_from_kalman(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                
                if jma_high and jma_low and jma_close:
                    tama_high = calculate_tama(symbol, jma_high, st["kalman_high"], er)
                    tama_low = calculate_tama(symbol, jma_low, st["kalman_low"], er)
                    tama_close = calculate_tama(symbol, jma_close, st["kalman_close"], er)
                    
                    ribbon_width = tama_high - tama_low if tama_high and tama_low else 0
                    ribbon_pct = (ribbon_width / price) * 100 if ribbon_width else 0
                    er_status = " ✅ TREND" if er >= ER_THRESHOLD else " ⚠️ CHOPPY"
                    logging.info(f"{symbol}: Price={price:.6f}")
                    
                    if USE_TAMA and tama_high and tama_low and tama_close:
                        logging.info(f"  Layer 1 (Kalman): High={st['kalman_high']:.6f} | Low={st['kalman_low']:.6f} | Close={st['kalman_close']:.6f}")
                        logging.info(f"  Layer 2 (JMA): High={jma_high:.6f} | Low={jma_low:.6f} | Close={jma_close:.6f}")
                        logging.info(f"  Layer 3 (TAMA): High={tama_high:.6f} | Low={tama_low:.6f} | Close={tama_close:.6f}")
                        logging.info(f"  Ribbon Width={ribbon_width:.6f} ({ribbon_pct:.3f}%) | ER={er:.3f}{er_status} | α={ALPHA_WEIGHT}")
                    
                    long_status = f"LONG: {st['long_position']}" if st['long_position'] > 0 else "LONG: None"
                    short_status = f"SHORT: {st['short_position']}" if st['short_position'] > 0 else "SHORT: None"
                    logging.info(f"  {long_status} | {short_status}")
                    
                    if st["long_position"] > 0:
                        if st["long_trailing_stop_price"] and st["long_peak_price"]:
                            distance = ((price - st["long_trailing_stop_price"]) / price) * 100
                            logging.info(f"  LONG Stop: ${st['long_trailing_stop_price']:.6f} (Peak: ${st['long_peak_price']:.6f}, Dist: {distance:.2f}%)")
                        else:
                            logging.warning(f"  ⚠️ LONG Stop: MISSING!")
                    
                    if st["short_position"] > 0:
                        if st["short_trailing_stop_price"] and st["short_lowest_price"]:
                            distance = ((st["short_trailing_stop_price"] - price) / price) * 100
                            logging.info(f"  SHORT Stop: ${st['short_trailing_stop_price']:.6f} (Low: ${st['short_lowest_price']:.6f}, Dist: {distance:.2f}%)")
                        else:
                            logging.warning(f"  ⚠️ SHORT Stop: MISSING!")
        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic - TRUE HEDGE MODE with TAMA"""
    while True:
        await asyncio.sleep(0.1)
        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                continue
            price = st["price"]
            if price is None:
                continue
            
            # Safety check - reinitialize missing stops
            if st["long_position"] > 0 and (st["long_trailing_stop_price"] is None or st["long_peak_price"] is None):
                logging.warning(f"🔧 [{symbol}] SAFETY: Re-initializing missing LONG stop")
                initialize_trailing_stop(symbol, "LONG", price)
            if st["short_position"] > 0 and (st["short_trailing_stop_price"] is None or st["short_lowest_price"] is None):
                logging.warning(f"🔧 [{symbol}] SAFETY: Re-initializing missing SHORT stop")
                initialize_trailing_stop(symbol, "SHORT", price)
            
            # Update trailing stops
            stop_result = update_trailing_stop(symbol, price)
            
            # Handle LONG stop hit
            if stop_result["long_hit"] and st["long_position"] > 0:
                success = await execute_close_position(client, symbol, "LONG", st["long_position"])
                if success:
                    st["long_position"] = 0.0
                    reset_trailing_stop(symbol, "LONG")
                    save_positions()
                    logging.info(f"✅ [{symbol}] LONG position closed")
                else:
                    logging.error(f"❌ [{symbol}] LONG close failed")
            
            # Handle SHORT stop hit
            if stop_result["short_hit"] and st["short_position"] > 0:
                success = await execute_close_position(client, symbol, "SHORT", st["short_position"])
                if success:
                    st["short_position"] = 0.0
                    reset_trailing_stop(symbol, "SHORT")
                    save_positions()
                    logging.info(f"✅ [{symbol}] SHORT position closed")
                else:
                    logging.error(f"❌ [{symbol}] SHORT close failed")
            
            # Check entry signals
            signals = update_trading_signals(symbol)
            
            # Handle LONG entry
            if signals["long_entry"] and st["long_position"] == 0:
                target_size = SYMBOLS[symbol]
                success = await execute_open_position(client, symbol, "LONG", target_size)
                if success:
                    st["long_position"] = target_size
                    initialize_trailing_stop(symbol, "LONG", price)
                    save_positions()
                    logging.info(f"✅ [{symbol}] LONG position opened")
            
            # Handle SHORT entry
            if signals["short_entry"] and st["short_position"] == 0:
                target_size = SYMBOLS[symbol]
                success = await execute_open_position(client, symbol, "SHORT", target_size)
                if success:
                    st["short_position"] = target_size
                    initialize_trailing_stop(symbol, "SHORT", price)
                    save_positions()
                    logging.info(f"✅ [{symbol}] SHORT position opened")

async def execute_open_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Open new position with 2-second duplicate protection"""
    st = state[symbol]
    now = time.time()
    if side == "LONG":
        if (now - st["last_long_exec_ts"]) < 2.0:
            logging.info(f"🛡️ {symbol} LONG dedup: skipping duplicate entry")
            return False
        st["last_long_exec_ts"] = now
    else:
        if (now - st["last_short_exec_ts"]) < 2.0:
            logging.info(f"🛡️ {symbol} SHORT dedup: skipping duplicate entry")
            return False
        st["last_short_exec_ts"] = now
    order_side = "BUY" if side == "LONG" else "SELL"
    success = await place_order(client, symbol, order_side, size, f"{side} ENTRY")
    return success

async def execute_close_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Close existing position"""
    order_side = "SELL" if side == "LONG" else "BUY"
    success = await place_order(client, symbol, order_side, size, f"{side} CLOSE")
    if not success:
        logging.error(f"❌ {symbol} {side} CLOSE FAILED - position remains open!")
    return success

async def recover_positions_from_exchange(client: AsyncClient):
    """Recover actual positions from Binance"""
    logging.info("🔍 Checking exchange for existing positions...")
    try:
        account_info = await safe_api_call(client.futures_account)
        positions = account_info.get('positions', [])
        recovered_symbols = {sym: {"long": False, "short": False} for sym in SYMBOLS}
        for position in positions:
            symbol = position['symbol']
            if symbol not in SYMBOLS:
                continue
            position_amt = float(position['positionAmt'])
            position_side = position['positionSide']
            if abs(position_amt) > 0.0001:
                entry_price = float(position['entryPrice'])
                mark_price = float(position['markPrice'])
                unrealized_pnl = float(position['unrealizedProfit'])
                if position_side == "LONG" and position_amt > 0:
                    logging.info(f"♻️ [{symbol}] RECOVERED LONG: Amt={position_amt}, Entry={entry_price:.6f}, PNL={unrealized_pnl:.2f}")
                    state[symbol]["long_position"] = position_amt
                    recovered_symbols[symbol]["long"] = True
                    init_price = mark_price if mark_price > 0 else (state[symbol]["price"] if state[symbol]["price"] else entry_price)
                    if init_price:
                        initialize_trailing_stop(symbol, "LONG", init_price)
                        logging.info(f"✅ [{symbol}] LONG stop initialized at {init_price:.6f}")
                elif position_side == "SHORT" and position_amt < 0:
                    logging.info(f"♻️ [{symbol}] RECOVERED SHORT: Amt={position_amt}, Entry={entry_price:.6f}, PNL={unrealized_pnl:.2f}")
                    state[symbol]["short_position"] = abs(position_amt)
                    recovered_symbols[symbol]["short"] = True
                    init_price = mark_price if mark_price > 0 else (state[symbol]["price"] if state[symbol]["price"] else entry_price)
                    if init_price:
                        initialize_trailing_stop(symbol, "SHORT", init_price)
                        logging.info(f"✅ [{symbol}] SHORT stop initialized at {init_price:.6f}")
        recovered_count = sum(1 for sym_data in recovered_symbols.values() if sym_data["long"] or sym_data["short"])
        if recovered_count > 0:
            logging.info(f"✅ Recovery complete: {recovered_count} symbols with active positions")
            save_positions()
        else:
            logging.info("✅ No active positions found on exchange")
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")

async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 STRATEGY: TAMA (Triple-Layer Adaptive Moving Average)")
    logging.info(f"📊   Layer 1: Kalman Filter (Q={KALMAN_Q}, R={KALMAN_R})")
    logging.info(f"📊   Layer 2: JMA (high={JMA_LENGTH_HIGH}, low={JMA_LENGTH_LOW}, close={JMA_LENGTH_CLOSE})")
    logging.info(f"📊   Layer 3: Efficiency Ratio Adaptation (ER periods={ER_PERIODS}, α={ALPHA_WEIGHT})")
    logging.info(f"📊 MODE: TRUE HEDGE MODE (LONG + SHORT simultaneously)")
    logging.info(f"📊 SYMBOLS: {len(SYMBOLS)} symbols tracked independently")
    logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
    logging.info(f"📊 Entry Strategy: {ENTRY_STRATEGY}")
    logging.info(f"📊 Trailing Stop: {TRAILING_STOP_PERCENT}% (~{TRAILING_STOP_PERCENT * LEVERAGE:.1f}% pos risk @ {LEVERAGE}x)")
    
    load_klines()
    load_positions()
    
    logging.info("📋 Initial state per symbol:")
    for sym in SYMBOLS:
        st = state[sym]
        long_str = f"LONG={st['long_position']}" if st['long_position'] > 0 else "LONG=None"
        short_str = f"SHORT={st['short_position']}" if st['short_position'] > 0 else "SHORT=None"
        logging.info(f"   [{sym}]: {long_str}, {short_str}")
    
    await recover_positions_from_exchange(client)
    
    logging.info("📋 Final state after exchange verification:")
    for sym in SYMBOLS:
        st = state[sym]
        status_parts = []
        if st['long_position'] > 0:
            stop_status = "✅" if (st['long_trailing_stop_price'] and st['long_peak_price']) else "❌"
            status_parts.append(f"LONG={st['long_position']} {stop_status}")
        if st['short_position'] > 0:
            stop_status = "✅" if (st['short_trailing_stop_price'] and st['short_lowest_price']) else "❌"
            status_parts.append(f"SHORT={st['short_position']} {stop_status}")
        if not status_parts:
            status_parts.append("FLAT")
        logging.info(f"   [{sym}]: {', '.join(status_parts)}")
    
    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        if len(klines) >= MA_PERIODS:
            jma_high = calculate_jma_from_kalman(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
            jma_low = calculate_jma_from_kalman(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
            jma_close = calculate_jma_from_kalman(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
            er = calculate_efficiency_ratio(symbol)
            if (jma_high is not None) and (jma_low is not None) and (jma_close is not None) and (er is not None):
                state[symbol]["ready"] = True
                logging.info(f"✅ {symbol} ready from loaded data")
            else:
                symbols_needing_data.append(symbol)
        else:
            symbols_needing_data.append(symbol)
    
    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")
                needed_candles = max(MA_PERIODS + 100, 100)
                klines_data = await safe_api_call(
                    client.futures_mark_price_klines,
                    symbol=symbol,
                    interval=BASE_TIMEFRAME,
                    limit=min(needed_candles, 1500)
                )
                st = state[symbol]
                st["klines"].clear()
                for kline in klines_data:
                    open_time = int(float(kline[0]) / 1000)
                    high = float(kline[2])
                    low = float(kline[3])
                    close = float(kline[4])
                    st["klines"].append({
                        "open_time": open_time,
                        "open": float(kline[1]),
                        "high": high,
                        "low": low,
                        "close": close
                    })
                    # Build Kalman states sequentially
                    kalman_filter(symbol, "high", high)
                    kalman_filter(symbol, "low", low)
                    kalman_filter(symbol, "close", close)
                apply_kalman_to_klines(symbol)
                jma_high = calculate_jma_from_kalman(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
                jma_low = calculate_jma_from_kalman(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
                jma_close = calculate_jma_from_kalman(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                er = calculate_efficiency_ratio(symbol)
                if (jma_high is not None) and (jma_low is not None) and (jma_close is not None) and (er is not None):
                    st["ready"] = True
                    logging.info(f"✅ {symbol} ready from API")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(15)
            except Exception as e:
                logging.error(f"❌ {symbol} fetch failed: {e}")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(15)
    else:
        logging.info("🎯 All symbols ready!")
    
    save_klines()
    await asyncio.sleep(2)
    logging.info("🚀 Initialization complete")

async def main():
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing Binance API credentials")
    client = await AsyncClient.create(API_KEY, API_SECRET)
    atexit.register(save_klines)
    atexit.register(save_positions)
    try:
        await init_bot(client)
        price_task = asyncio.create_task(price_feed_loop(client))
        trade_task = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())
        logging.info("🚀 Bot started - TAMA STRATEGY")
        await asyncio.gather(price_task, trade_task, status_task)
    finally:
        await client.close_connection()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    print("=" * 80)
    print("TRIPLE-LAYER ADAPTIVE MOVING AVERAGE (TAMA) STRATEGY")
    print("=" * 80)
    print(f"Layer 1: Kalman Filter (Noise Reduction & Prediction)")
    print(f"  • Process Noise (Q): {KALMAN_Q}")
    print(f"  • Measurement Noise (R): {KALMAN_R}")
    print(f"")
    print(f"Layer 2: Jurik Moving Average (Low-Lag Smoothing)")
    print(f"  • HIGH band: {JMA_LENGTH_HIGH} periods")
    print(f"  • LOW band: {JMA_LENGTH_LOW} periods")
    print(f"  • CLOSE signal: {JMA_LENGTH_CLOSE} periods")
    print(f"  • Phase: {JMA_PHASE}, Power: {JMA_POWER}")
    print(f"")
    print(f"Layer 3: Efficiency Ratio Adaptation (Responsiveness Control)")
    print(f"  • ER Periods: {ER_PERIODS}")
    print(f"  • Alpha Weight (α): {ALPHA_WEIGHT}")
    print(f"  • Formula: TAMA = JMA + (α × ER × (Kalman_Price - JMA))")
    print(f"")
    print(f"Data Flow:")
    print(f"  Raw Price → Kalman Filter → JMA → ER Adaptation → TAMA")
    print("=" * 80)
    print(f"Mode: TRUE HEDGE (LONG + SHORT simultaneously)")
    print(f"Symbols: {len(SYMBOLS)} - {', '.join(SYMBOLS.keys())}")
    print(f"Timeframe: {BASE_TIMEFRAME}")
    print(f"Entry Strategy: {ENTRY_STRATEGY}")
    print(f"Trailing Stop: {TRAILING_STOP_PERCENT}% (~{TRAILING_STOP_PERCENT * LEVERAGE:.1f}% @ {LEVERAGE}x)")
    print("=" * 80)
    asyncio.run(main())
