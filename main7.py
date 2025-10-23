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
USE_TAMA = True

# Layer 1: Kalman Filter Parameters
KALMAN_Q = 0.001  # Process noise covariance (lower = smoother)
KALMAN_R = 0.01   # Measurement noise covariance (lower = more responsive)

# Layer 2: JMA Parameters
JMA_LENGTH_FAST = 7   # Fast JMA (signal line)
JMA_LENGTH_SLOW = 100  # Slow JMA (baseline)
JMA_PHASE = 0
JMA_POWER = 3

# Layer 3: Efficiency Ratio Parameters
ER_PERIODS = 100
ALPHA_WEIGHT = 1.0  # ER influence multiplier

# Trailing Stop Configuration
TRAILING_STOP_PERCENT = 0.7
TRAILING_UPDATE_THRESHOLD = 0.1  # % change required to update trailing stop

# Timeframe configuration
BASE_TIMEFRAME = "15m"

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
elif BASE_TIMEFRAME == "15m":
    BASE_MINUTES = 15
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

# Trading symbols and sizes
SYMBOLS = {
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

MA_PERIODS = max(JMA_LENGTH_FAST, JMA_LENGTH_SLOW)
KLINE_LIMIT = max(100, MA_PERIODS + 100, ER_PERIODS + 100)

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        # Kalman Filter state (Layer 1)
        "kalman_x": None,
        "kalman_p": 1.0,
        "kalman_close": None,
        # JMA values (Layer 2)
        "jma_fast": None,      # JMA-7
        "jma_slow": None,      # JMA-50
        # TAMA final values (Layer 3 - adaptive)
        "tama_fast": None,
        "tama_slow": None,
        # Previous TAMA values for crossover detection
        "prev_tama_fast": None,
        "prev_tama_slow": None,
        # Efficiency Ratio
        "efficiency_ratio": None,
        "er_ready": False,
        "ready": False,
        # LONG position tracking
        "long_position": 0.0,
        "long_trailing_stop_price": None,
        "long_peak_price": None,
        "last_long_exec_ts": 0.0,
        "long_entry_allowed": True,  # Crossover lock
        "long_stop_order_id": None,
        # SHORT position tracking
        "short_position": 0.0,
        "short_trailing_stop_price": None,
        "short_lowest_price": None,
        "last_short_exec_ts": 0.0,
        "short_entry_allowed": True,  # Crossover lock
        "short_stop_order_id": None,
        # Stop warning flag
        "stop_warning_logged": False,
        # Last close ts for dedup
        "last_long_close_ts": 0.0,
        "last_short_close_ts": 0.0,
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

def save_positions():
    position_data = {}
    for sym in SYMBOLS:
        position_data[sym] = {
            "long_position": state[sym]["long_position"],
            "long_trailing_stop_price": state[sym]["long_trailing_stop_price"],
            "long_peak_price": state[sym]["long_peak_price"],
            "long_entry_allowed": state[sym]["long_entry_allowed"],
            "long_stop_order_id": state[sym]["long_stop_order_id"],
            "short_position": state[sym]["short_position"],
            "short_trailing_stop_price": state[sym]["short_trailing_stop_price"],
            "short_lowest_price": state[sym]["short_lowest_price"],
            "short_entry_allowed": state[sym]["short_entry_allowed"],
            "short_stop_order_id": state[sym]["short_stop_order_id"],
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
            state[sym]["long_entry_allowed"] = position_data[sym].get("long_entry_allowed", True)
            state[sym]["long_stop_order_id"] = position_data[sym].get("long_stop_order_id")
            state[sym]["short_position"] = loaded_short
            state[sym]["short_trailing_stop_price"] = position_data[sym].get("short_trailing_stop_price")
            state[sym]["short_lowest_price"] = position_data[sym].get("short_lowest_price")
            state[sym]["short_entry_allowed"] = position_data[sym].get("short_entry_allowed", True)
            state[sym]["short_stop_order_id"] = position_data[sym].get("short_stop_order_id")
            
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
    if api_calls_count >= 50:  # Increased from 10 to 50
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

async def place_order(client: AsyncClient, symbol: str, side: str, quantity: float, action: str, order_type: str = "MARKET", stop_price: Optional[float] = None):
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            return None
        if "LONG" in action:
            position_side = "LONG"
        elif "SHORT" in action:
            position_side = "SHORT"
        else:
            logging.error(f"Unknown action type: {action}")
            return None
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
            "positionSide": position_side,
        }
        if "CLOSE" in action or order_type == "STOP_MARKET":
            params["reduceOnly"] = True
        if order_type == "STOP_MARKET" and stop_price is not None:
            params["stopPrice"] = stop_price
        result = await safe_api_call(client.futures_create_order, **params)
        logging.info(f"🚀 {symbol} {action} EXECUTED - {side} {quantity} - OrderID: {result.get('orderId')} Type: {order_type}")
        return result
    except Exception as e:
        logging.error(f"❌ {symbol} {action} FAILED: {e}")
        return None

async def cancel_order(client: AsyncClient, symbol: str, order_id: int):
    try:
        result = await safe_api_call(client.futures_cancel_order, symbol=symbol, orderId=order_id)
        logging.info(f"🗑️ Order {order_id} canceled for {symbol}")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to cancel order {order_id} for {symbol}: {e}")
        return False

# ========================= TRAILING STOP FUNCTIONS =========================
async def initialize_trailing_stop(client: AsyncClient, symbol: str, side: str, entry_price: float):
    st = state[symbol]
    if side == "LONG":
        st["long_peak_price"] = entry_price
        st["long_trailing_stop_price"] = entry_price * (1 - TRAILING_STOP_PERCENT / 100)
        logging.info(f"🎯 {symbol} LONG Trailing Stop initialized: Peak={entry_price:.6f}, Stop={st['long_trailing_stop_price']:.6f}")
        await place_trailing_stop_order(client, symbol, "LONG")
    elif side == "SHORT":
        st["short_lowest_price"] = entry_price
        st["short_trailing_stop_price"] = entry_price * (1 + TRAILING_STOP_PERCENT / 100)
        logging.info(f"🎯 {symbol} SHORT Trailing Stop initialized: Lowest={entry_price:.6f}, Stop={st['short_trailing_stop_price']:.6f}")
        await place_trailing_stop_order(client, symbol, "SHORT")
    st["stop_warning_logged"] = False
    save_positions()

async def place_trailing_stop_order(client: AsyncClient, symbol: str, side: str):
    st = state[symbol]
    if side == "LONG":
        order_side = "SELL"
        quantity = st["long_position"]
        stop_price = st["long_trailing_stop_price"]
    else:
        order_side = "BUY"
        quantity = st["short_position"]
        stop_price = st["short_trailing_stop_price"]
    result = await place_order(client, symbol, order_side, quantity, f"{side} TRAILING STOP", order_type="STOP_MARKET", stop_price=stop_price)
    if result:
        st[f"{side.lower()}_stop_order_id"] = result.get('orderId')
        save_positions()

async def update_trailing_stop(client: AsyncClient, symbol: str, current_price: float) -> dict:
    st = state[symbol]
    result = {"long_updated": False, "short_updated": False}
    
    if st["long_position"] > 0:
        if st["long_peak_price"] is None or st["long_trailing_stop_price"] is None:
            if not st["stop_warning_logged"]:
                logging.warning(f"⚠️ {symbol} LONG position exists but stop is None - will auto-fix")
                st["stop_warning_logged"] = True
            return result
        if current_price > st["long_peak_price"]:
            st["long_peak_price"] = current_price
            new_stop = current_price * (1 - TRAILING_STOP_PERCENT / 100)
            if new_stop > st["long_trailing_stop_price"] * (1 + TRAILING_UPDATE_THRESHOLD / 100):
                await cancel_trailing_stop_order(client, symbol, "LONG")
                st["long_trailing_stop_price"] = new_stop
                await place_trailing_stop_order(client, symbol, "LONG")
                result["long_updated"] = True
                save_positions()
    
    if st["short_position"] > 0:
        if st["short_lowest_price"] is None or st["short_trailing_stop_price"] is None:
            if not st["stop_warning_logged"]:
                logging.warning(f"⚠️ {symbol} SHORT position exists but stop is None - will auto-fix")
                st["stop_warning_logged"] = True
            return result
        if current_price < st["short_lowest_price"]:
            st["short_lowest_price"] = current_price
            new_stop = current_price * (1 + TRAILING_STOP_PERCENT / 100)
            if new_stop < st["short_trailing_stop_price"] * (1 - TRAILING_UPDATE_THRESHOLD / 100):
                await cancel_trailing_stop_order(client, symbol, "SHORT")
                st["short_trailing_stop_price"] = new_stop
                await place_trailing_stop_order(client, symbol, "SHORT")
                result["short_updated"] = True
                save_positions()
    
    return result

async def cancel_trailing_stop_order(client: AsyncClient, symbol: str, side: str):
    st = state[symbol]
    order_id = st[f"{side.lower()}_stop_order_id"]
    if order_id:
        success = await cancel_order(client, symbol, order_id)
        if success:
            st[f"{side.lower()}_stop_order_id"] = None
            save_positions()

def reset_trailing_stop(symbol: str, side: str):
    st = state[symbol]
    if side == "LONG":
        st["long_trailing_stop_price"] = None
        st["long_peak_price"] = None
        st["long_entry_allowed"] = True  # Re-enable crossover entries
        st["long_stop_order_id"] = None
        logging.info(f"🔓 {symbol} LONG entry re-enabled (position closed)")
    elif side == "SHORT":
        st["short_trailing_stop_price"] = None
        st["short_lowest_price"] = None
        st["short_entry_allowed"] = True  # Re-enable crossover entries
        st["short_stop_order_id"] = None
        logging.info(f"🔓 {symbol} SHORT entry re-enabled (position closed)")
    save_positions()

# ========================= TAMA INDICATOR CALCULATIONS =========================

def kalman_filter(symbol: str, measurement: float) -> Optional[float]:
    """
    LAYER 1: Kalman Filter
    Removes noise from raw price data and provides predictive smoothing
    """
    st = state[symbol]
    
    if st["kalman_x"] is None:
        st["kalman_x"] = measurement
        st["kalman_p"] = 1.0
        return measurement
    
    # Prediction step
    x_pred = st["kalman_x"]
    p_pred = st["kalman_p"] + KALMAN_Q
    
    # Update step
    kalman_gain = p_pred / (p_pred + KALMAN_R)
    x_updated = x_pred + kalman_gain * (measurement - x_pred)
    p_updated = (1 - kalman_gain) * p_pred
    
    # Store updated state
    st["kalman_x"] = x_updated
    st["kalman_p"] = p_updated
    
    return x_updated

def apply_kalman_to_klines(symbol: str):
    """Apply Kalman filter to close price of latest kline"""
    klines = state[symbol]["klines"]
    if len(klines) == 0:
        return
    latest = klines[-1]
    state[symbol]["kalman_close"] = kalman_filter(symbol, latest["close"])

def calculate_jma_from_kalman(symbol: str, length: int, phase: int = 50, power: int = 2) -> Optional[float]:
    """
    LAYER 2: Jurik Moving Average
    Feeds Kalman-filtered data into JMA for low-lag smoothing
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
    
    # Build Kalman-filtered values for historical data
    values = []
    temp_kalman_x = None
    temp_kalman_p = 1.0
    
    for k in completed:
        close_val = k["close"]
        if temp_kalman_x is None:
            temp_kalman_x = close_val
            temp_kalman_p = 1.0
            values.append(close_val)
        else:
            # Kalman prediction
            x_pred = temp_kalman_x
            p_pred = temp_kalman_p + KALMAN_Q
            # Kalman update
            kalman_gain = p_pred / (p_pred + KALMAN_R)
            temp_kalman_x = x_pred + kalman_gain * (close_val - x_pred)
            temp_kalman_p = (1 - kalman_gain) * p_pred
            values.append(temp_kalman_x)
    
    if len(values) < length:
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

def calculate_tama(symbol: str, jma_value: Optional[float], kalman_price: float, er: Optional[float]) -> Optional[float]:
    """
    LAYER 3: Triple-Layer Adaptive Moving Average (TAMA)
    Uses Kalman-filtered price (not raw price) for consistency
    
    Formula: TAMA = JMA + (α × ER × (Kalman_Price - JMA))
    
    Flow:
    1. Raw Price → Kalman Filter (noise reduction)
    2. Kalman Output → JMA (low-lag smoothing)
    3. JMA + ER + Kalman_Price → TAMA (adaptive responsiveness)
    """
    if jma_value is None:
        return None
    
    if not USE_TAMA or er is None:
        return jma_value
    
    # Use Kalman-filtered price for consistency across all layers
    adjustment = ALPHA_WEIGHT * er * (kalman_price - jma_value)
    tama = jma_value + adjustment
    
    return tama

# ========================= TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> dict:
    """
    TAMA CROSSOVER Strategy
    Entry Rules:
    - LONG: TAMA-Fast crosses ABOVE TAMA-Slow (once per trend)
    - SHORT: TAMA-Fast crosses BELOW TAMA-Slow (once per trend)
    
    Exit Rules:
    - Trailing stop only (no forced exits from opposite crossovers)
    
    Anti-Ping-Pong:
    - Once position opened, lock further entries until position fully closed
    - Only trailing stop can close position
    - After close, crossover lock is released for next signal
    """
    st = state[symbol]
    price = st["price"]
    result = {"long_entry": False, "short_entry": False}
    
    if price is None or not st["ready"]:
        return result
    
    # Apply Kalman filter to current kline
    apply_kalman_to_klines(symbol)
    kalman_close = st["kalman_close"]
    
    if kalman_close is None:
        return result
    
    # Calculate JMA from Kalman-filtered data (Layer 1 → Layer 2)
    jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
    jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
    
    # Calculate Efficiency Ratio for adaptive weighting
    er = calculate_efficiency_ratio(symbol)
    
    if jma_fast is None or jma_slow is None or er is None:
        return result
    
    # Apply TAMA formula (Layer 2 → Layer 3)
    # IMPORTANT: Use kalman_close instead of raw price
    tama_fast = calculate_tama(symbol, jma_fast, kalman_close, er)
    tama_slow = calculate_tama(symbol, jma_slow, kalman_close, er)
    
    if tama_fast is None or tama_slow is None:
        return result
    
    # Store current TAMA values
    st["tama_fast"] = tama_fast
    st["tama_slow"] = tama_slow
    st["jma_fast"] = jma_fast
    st["jma_slow"] = jma_slow
    
    prev_tama_fast = st["prev_tama_fast"]
    prev_tama_slow = st["prev_tama_slow"]
    
    # Initialize previous values
    if prev_tama_fast is None or prev_tama_slow is None:
        st["prev_tama_fast"] = tama_fast
        st["prev_tama_slow"] = tama_slow
        return result
    
    # Detect crossovers
    bullish_cross = (tama_fast > tama_slow) and (prev_tama_fast <= prev_tama_slow)
    bearish_cross = (tama_fast < tama_slow) and (prev_tama_fast >= prev_tama_slow)
    
    # LONG Entry: Only if position is flat AND crossover lock is released
    if bullish_cross and st["long_position"] == 0 and st["long_entry_allowed"]:
        result["long_entry"] = True
        st["long_entry_allowed"] = False  # Lock further LONG entries
        save_positions()
        logging.info(f"🟢 {symbol} ENTRY LONG (TAMA CROSSOVER)")
        logging.info(f"   TAMA-Fast={tama_fast:.6f} crossed ABOVE TAMA-Slow={tama_slow:.6f}")
        logging.info(f"   Kalman_Close={kalman_close:.6f}, ER={er:.3f}, α={ALPHA_WEIGHT}")
        logging.info(f"🔒 {symbol} LONG crossover lock engaged (no re-entry until stop closes position)")
    
    # SHORT Entry: Only if position is flat AND crossover lock is released
    if bearish_cross and st["short_position"] == 0 and st["short_entry_allowed"]:
        result["short_entry"] = True
        st["short_entry_allowed"] = False  # Lock further SHORT entries
        save_positions()
        logging.info(f"🟢 {symbol} ENTRY SHORT (TAMA CROSSOVER)")
        logging.info(f"   TAMA-Fast={tama_fast:.6f} crossed BELOW TAMA-Slow={tama_slow:.6f}")
        logging.info(f"   Kalman_Close={kalman_close:.6f}, ER={er:.3f}, α={ALPHA_WEIGHT}")
        logging.info(f"🔒 {symbol} SHORT crossover lock engaged (no re-entry until stop closes position)")
    
    # Log ignored crossovers (anti-ping-pong in action)
    if bullish_cross and st["long_position"] == 0 and not st["long_entry_allowed"]:
        logging.info(f"⚠️ {symbol} Bullish crossover IGNORED (LONG lock active - waiting for SHORT to close)")
    
    if bearish_cross and st["short_position"] == 0 and not st["short_entry_allowed"]:
        logging.info(f"⚠️ {symbol} Bearish crossover IGNORED (SHORT lock active - waiting for LONG to close)")
    
    # Update previous values
    st["prev_tama_fast"] = tama_fast
    st["prev_tama_slow"] = tama_slow
    
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
                                    jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
                                    jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
                                    er = calculate_efficiency_ratio(symbol)
                                    if (jma_fast is not None) and (jma_slow is not None) and (er is not None):
                                        state[symbol]["ready"] = True
                                        candle_mode = " [LIVE]" if USE_LIVE_CANDLE else " [COMPLETED]"
                                        logging.info(f"✅ {symbol} ready - TAMA initialized{candle_mode}")
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
            tama_fast = st.get("tama_fast")
            tama_slow = st.get("tama_slow")
            kalman_close = st.get("kalman_close")
            er = st.get("efficiency_ratio")
            
            if price and tama_fast and tama_slow and kalman_close and er is not None:
                # Determine crossover state
                if tama_fast > tama_slow:
                    trend = "BULLISH ▲"
                elif tama_fast < tama_slow:
                    trend = "BEARISH ▼"
                else:
                    trend = "NEUTRAL ═"
                
                distance = abs(tama_fast - tama_slow)
                distance_pct = (distance / price) * 100
                
                logging.info(f"{symbol}: Price={price:.6f} | Kalman={kalman_close:.6f}")
                logging.info(f"  TAMA-Fast({JMA_LENGTH_FAST})={tama_fast:.6f} | TAMA-Slow({JMA_LENGTH_SLOW})={tama_slow:.6f}")
                logging.info(f"  Trend: {trend} | Distance={distance:.6f} ({distance_pct:.3f}%) | ER={er:.3f}")
                
                # Show positions and lock status
                long_status = f"LONG: {st['long_position']}" if st['long_position'] > 0 else "LONG: None"
                short_status = f"SHORT: {st['short_position']}" if st['short_position'] > 0 else "SHORT: None"
                long_lock = "🔒" if not st['long_entry_allowed'] else "🔓"
                short_lock = "🔒" if not st['short_entry_allowed'] else "🔓"
                logging.info(f"  {long_status} {long_lock} | {short_status} {short_lock}")
                
                # Show trailing stops
                if st["long_position"] > 0:
                    if st["long_trailing_stop_price"] and st["long_peak_price"]:
                        distance_stop = ((price - st["long_trailing_stop_price"]) / price) * 100
                        logging.info(f"  LONG Stop: ${st['long_trailing_stop_price']:.6f} (Peak: ${st['long_peak_price']:.6f}, Dist: {distance_stop:.2f}%)")
                    else:
                        logging.warning(f"  ⚠️ LONG Stop: MISSING!")
                
                if st["short_position"] > 0:
                    if st["short_trailing_stop_price"] and st["short_lowest_price"]:
                        distance_stop = ((st["short_trailing_stop_price"] - price) / price) * 100
                        logging.info(f"  SHORT Stop: ${st['short_trailing_stop_price']:.6f} (Low: ${st['short_lowest_price']:.6f}, Dist: {distance_stop:.2f}%)")
                    else:
                        logging.warning(f"  ⚠️ SHORT Stop: MISSING!")
        
        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic - Crossover Strategy with Anti-Ping-Pong"""
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
            if st["long_position"] > 0 and (st["long_trailing_stop_price"] is None or st["long_peak_price"] is None or st["long_stop_order_id"] is None):
                logging.warning(f"🔧 [{symbol}] SAFETY: Re-initializing missing LONG stop")
                await initialize_trailing_stop(client, symbol, "LONG", price)
            if st["short_position"] > 0 and (st["short_trailing_stop_price"] is None or st["short_lowest_price"] is None or st["short_stop_order_id"] is None):
                logging.warning(f"🔧 [{symbol}] SAFETY: Re-initializing missing SHORT stop")
                await initialize_trailing_stop(client, symbol, "SHORT", price)
            
            # Update trailing stops (dynamic STOP_MARKET orders)
            await update_trailing_stop(client, symbol, price)
            
            # Check entry signals (respects crossover locks)
            signals = update_trading_signals(symbol)
            
            # Handle LONG entry (only if lock is released)
            if signals["long_entry"] and st["long_position"] == 0:
                target_size = SYMBOLS[symbol]
                success = await execute_open_position(client, symbol, "LONG", target_size)
                if success:
                    st["long_position"] = target_size
                    await initialize_trailing_stop(client, symbol, "LONG", price)
                    save_positions()
                    logging.info(f"✅ [{symbol}] LONG position opened")
            
            # Handle SHORT entry (only if lock is released)
            if signals["short_entry"] and st["short_position"] == 0:
                target_size = SYMBOLS[symbol]
                success = await execute_open_position(client, symbol, "SHORT", target_size)
                if success:
                    st["short_position"] = target_size
                    await initialize_trailing_stop(client, symbol, "SHORT", price)
                    save_positions()
                    logging.info(f"✅ [{symbol}] SHORT position opened")

async def position_polling_loop(client: AsyncClient):
    """Poll positions to detect closures by trailing stops and sync locks"""
    while True:
        await asyncio.sleep(30)  # Increased from 5 to 30 seconds
        try:
            positions = await safe_api_call(client.futures_position_information)
            for pos in positions:
                symbol = pos['symbol']
                if symbol not in SYMBOLS:
                    continue
                st = state[symbol]
                position_side = pos['positionSide']
                amt = float(pos['positionAmt'])
                if position_side == "LONG":
                    if amt <= 0:
                        if st["long_position"] > 0:
                            logging.info(f"🛑 [{symbol}] LONG position closed by trailing stop (detected via poll)")
                            st["long_position"] = 0.0
                            reset_trailing_stop(symbol, "LONG")
                            save_positions()
                        if not st["long_entry_allowed"]:
                            logging.warning(f"🔧 [{symbol}] LONG lock stuck without position - forcing unlock")
                            st["long_entry_allowed"] = True
                            save_positions()
                elif position_side == "SHORT":
                    if amt >= 0:
                        if st["short_position"] > 0:
                            logging.info(f"🛑 [{symbol}] SHORT position closed by trailing stop (detected via poll)")
                            st["short_position"] = 0.0
                            reset_trailing_stop(symbol, "SHORT")
                            save_positions()
                        if not st["short_entry_allowed"]:
                            logging.warning(f"🔧 [{symbol}] SHORT lock stuck without position - forcing unlock")
                            st["short_entry_allowed"] = True
                            save_positions()
        except Exception as e:
            logging.error(f"❌ Position poll failed: {e}")

async def execute_open_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Open new position with 2-second duplicate protection and retries"""
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
    for attempt in range(3):
        result = await place_order(client, symbol, order_side, size, f"{side} ENTRY")
        if result:
            return True
        logging.warning(f"⚠️ {symbol} {side} ENTRY attempt {attempt+1}/3 failed - retrying...")
        await asyncio.sleep(1)
    return False

async def execute_close_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Close existing position with dedup and retries"""
    st = state[symbol]
    now = time.time()
    if side == "LONG":
        if (now - st["last_long_close_ts"]) < 2.0:
            logging.info(f"🛡️ {symbol} LONG dedup: skipping duplicate close")
            return False
        st["last_long_close_ts"] = now
    else:
        if (now - st["last_short_close_ts"]) < 2.0:
            logging.info(f"🛡️ {symbol} SHORT dedup: skipping duplicate close")
            return False
        st["last_short_close_ts"] = now
    order_side = "SELL" if side == "LONG" else "BUY"
    for attempt in range(3):
        result = await place_order(client, symbol, order_side, size, f"{side} CLOSE")
        if result:
            # Log slippage if possible
            order_id = result.get('orderId')
            order_details = await safe_api_call(client.futures_get_order, symbol=symbol, orderId=order_id)
            if order_details and 'avgPrice' in order_details:
                avg_fill = float(order_details['avgPrice'])
                expected_stop = st[f"{side.lower()}_trailing_stop_price"]
                slippage = abs(avg_fill - expected_stop) / expected_stop * 100 if expected_stop else 0
                logging.info(f"📉 {symbol} {side} CLOSE slippage: {slippage:.2f}% (Fill: {avg_fill:.6f} vs Stop: {expected_stop:.6f})")
            return True
        logging.warning(f"⚠️ {symbol} {side} CLOSE attempt {attempt+1}/3 failed - retrying...")
        await asyncio.sleep(1)
    logging.error(f"❌ {symbol} {side} CLOSE FAILED after retries - position remains open!")
    return False

async def recover_positions_from_exchange(client: AsyncClient):
    """Recover actual positions from Binance and open orders"""
    logging.info("🔍 Checking exchange for existing positions and orders...")
    try:
        positions = await safe_api_call(client.futures_position_information)
        all_open_orders = await safe_api_call(client.futures_get_open_orders)
        for pos in positions:
            symbol = pos['symbol']
            if symbol not in SYMBOLS:
                continue
            st = state[symbol]
            position_side = pos['positionSide']
            amt = float(pos['positionAmt'])
            if position_side == "LONG" and amt > 0.0001:
                entry_price = float(pos['entryPrice'])
                mark_price = float(pos['markPrice'])
                logging.info(f"♻️ [{symbol}] RECOVERED LONG: Amt={amt}, Entry={entry_price:.6f}")
                st["long_position"] = abs(amt)
                st["long_entry_allowed"] = False
                init_price = mark_price if mark_price > 0 else (st["price"] if st["price"] else entry_price)
                if init_price:
                    await initialize_trailing_stop(client, symbol, "LONG", init_price)
                    logging.info(f"✅ [{symbol}] LONG stop initialized at {init_price:.6f}")
                    logging.info(f"🔒 [{symbol}] LONG crossover lock engaged (existing position)")
            elif position_side == "SHORT" and amt < -0.0001:
                entry_price = float(pos['entryPrice'])
                mark_price = float(pos['markPrice'])
                logging.info(f"♻️ [{symbol}] RECOVERED SHORT: Amt={abs(amt)}, Entry={entry_price:.6f}")
                st["short_position"] = abs(amt)
                st["short_entry_allowed"] = False
                init_price = mark_price if mark_price > 0 else (st["price"] if st["price"] else entry_price)
                if init_price:
                    await initialize_trailing_stop(client, symbol, "SHORT", init_price)
                    logging.info(f"✅ [{symbol}] SHORT stop initialized at {init_price:.6f}")
                    logging.info(f"🔒 [{symbol}] SHORT crossover lock engaged (existing position)")
            else:
                # No position on exchange - force unlock if stuck
                if position_side == "LONG" and not st["long_entry_allowed"]:
                    logging.warning(f"🔧 [{symbol}] LONG lock stuck without position - forcing unlock")
                    st["long_entry_allowed"] = True
                if position_side == "SHORT" and not st["short_entry_allowed"]:
                    logging.warning(f"🔧 [{symbol}] SHORT lock stuck without position - forcing unlock")
                    st["short_entry_allowed"] = True
        # Check for existing stop orders
        for order in all_open_orders:
            symbol = order['symbol']
            if symbol not in SYMBOLS:
                continue
            st = state[symbol]
            if order['type'] == 'STOP_MARKET' and order['reduceOnly']:
                if order['positionSide'] == 'LONG' and order['side'] == 'SELL':
                    st["long_stop_order_id"] = order['orderId']
                    st["long_trailing_stop_price"] = float(order['stopPrice'])
                elif order['positionSide'] == 'SHORT' and order['side'] == 'BUY':
                    st["short_stop_order_id"] = order['orderId']
                    st["short_trailing_stop_price"] = float(order['stopPrice'])
        save_positions()
        logging.info("✅ Recovery complete")
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")

async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 STRATEGY: TAMA CROSSOVER (Anti-Ping-Pong)")
    logging.info(f"📊   Layer 1: Kalman Filter (Q={KALMAN_Q}, R={KALMAN_R})")
    logging.info(f"📊   Layer 2: JMA Fast={JMA_LENGTH_FAST}, Slow={JMA_LENGTH_SLOW}")
    logging.info(f"📊   Layer 3: ER Adaptation (periods={ER_PERIODS}, α={ALPHA_WEIGHT})")
    logging.info(f"📊 ENTRY: TAMA-{JMA_LENGTH_FAST} crosses TAMA-{JMA_LENGTH_SLOW} (once per trend)")
    logging.info(f"📊 EXIT: Trailing stop ONLY ({TRAILING_STOP_PERCENT}%)")
    logging.info(f"📊 ANTI-PING-PONG: Crossover lock until position closed")
    logging.info(f"📊 SYMBOLS: {len(SYMBOLS)} symbols")
    logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
    
    load_klines()
    load_positions()
    
    logging.info("📋 Initial state per symbol:")
    for sym in SYMBOLS:
        st = state[sym]
        long_str = f"LONG={st['long_position']}" if st['long_position'] > 0 else "LONG=None"
        short_str = f"SHORT={st['short_position']}" if st['short_position'] > 0 else "SHORT=None"
        long_lock = "🔒" if not st['long_entry_allowed'] else "🔓"
        short_lock = "🔒" if not st['short_entry_allowed'] else "🔓"
        logging.info(f"   [{sym}]: {long_str} {long_lock}, {short_str} {short_lock}")
    
    await recover_positions_from_exchange(client)
    
    logging.info("📋 Final state after exchange verification:")
    for sym in SYMBOLS:
        st = state[sym]
        status_parts = []
        if st['long_position'] > 0:
            stop_status = "✅" if (st['long_trailing_stop_price'] and st['long_peak_price']) else "❌"
            lock_status = "🔒" if not st['long_entry_allowed'] else "🔓"
            status_parts.append(f"LONG={st['long_position']} {stop_status} {lock_status}")
        if st['short_position'] > 0:
            stop_status = "✅" if (st['short_trailing_stop_price'] and st['short_lowest_price']) else "❌"
            lock_status = "🔒" if not st['short_entry_allowed'] else "🔓"
            status_parts.append(f"SHORT={st['short_position']} {stop_status} {lock_status}")
        if not status_parts:
            status_parts.append("FLAT 🔓🔓")
        logging.info(f"   [{sym}]: {', '.join(status_parts)}")
    
    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        if len(klines) >= MA_PERIODS:
            apply_kalman_to_klines(symbol)
            jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
            jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
            er = calculate_efficiency_ratio(symbol)
            if (jma_fast is not None) and (jma_slow is not None) and (er is not None):
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
                    st["klines"].append({
                        "open_time": open_time,
                        "open": float(kline[1]),
                        "high": float(kline[2]),
                        "low": float(kline[3]),
                        "close": float(kline[4])
                    })
                apply_kalman_to_klines(symbol)
                jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
                jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
                er = calculate_efficiency_ratio(symbol)
                if (jma_fast is not None) and (jma_slow is not None) and (er is not None):
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
        poll_task = asyncio.create_task(position_polling_loop(client))
        status_task = asyncio.create_task(status_logger())
        logging.info("🚀 Bot started - TAMA CROSSOVER STRATEGY")
        await asyncio.gather(price_task, trade_task, poll_task, status_task)
    finally:
        await client.close_connection()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    print("=" * 80)
    print("TAMA CROSSOVER STRATEGY (Anti-Ping-Pong)")
    print("=" * 80)
    print(f"Triple-Layer Adaptive Moving Average:")
    print(f"  Layer 1: Kalman Filter (Q={KALMAN_Q}, R={KALMAN_R})")
    print(f"  Layer 2: JMA Fast={JMA_LENGTH_FAST}, Slow={JMA_LENGTH_SLOW}")
    print(f"  Layer 3: ER Adaptation (α={ALPHA_WEIGHT})")
    print(f"")
    print(f"Entry Rules:")
    print(f"  • LONG: TAMA-{JMA_LENGTH_FAST} crosses ABOVE TAMA-{JMA_LENGTH_SLOW}")
    print(f"  • SHORT: TAMA-{JMA_LENGTH_FAST} crosses BELOW TAMA-{JMA_LENGTH_SLOW}")
    print(f"  • One entry per trend (crossover lock until position closed)")
    print(f"")
    print(f"Exit Rules:")
    print(f"  • Trailing stop ONLY: {TRAILING_STOP_PERCENT}% (~{TRAILING_STOP_PERCENT * LEVERAGE:.1f}% @ {LEVERAGE}x)")
    print(f"  • No forced exits from opposite crossovers")
    print(f"")
    print(f"Anti-Ping-Pong Logic:")
    print(f"  • Position opened → Crossover lock engaged 🔒")
    print(f"  • Trailing stop closes position → Lock released 🔓")
    print(f"  • Fresh crossover after flat → New entry allowed")
    print("=" * 80)
    print(f"Symbols: {len(SYMBOLS)} - {', '.join(SYMBOLS.keys())}")
    print(f"Timeframe: {BASE_TIMEFRAME}")
    print(f"Data Flow: Raw Price → Kalman → JMA → TAMA (ER-adjusted)")
    print("=" * 80)
    asyncio.run(main())
