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
USE_LIVE_CANDLE = True  # Toggle real-time JMA calculation

# Efficiency Ratio (Kaufman) - Filters choppy markets
USE_ER = False  # Toggle Efficiency Ratio filter
ER_PERIODS = 10  # Period for ER calculation (typical: 10-20)
ER_THRESHOLD = 0.3  # Minimum ER to allow entries (0.3 = 30% efficiency, range: 0.0-1.0)
# ER > 0.5 = strong trend, ER < 0.3 = choppy market

# Trailing Stop Configuration
TRAILING_STOP_PERCENT = 0.5  # 0.5% price movement

# Timeframe configuration
BASE_TIMEFRAME = "5m"  # Options: "1m", "5m", "1h"

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
elif BASE_TIMEFRAME == "5m":
    BASE_MINUTES = 5
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

# JMA RIBBON parameters - Uses HIGH and LOW to create stable band
JMA_LENGTH_HIGH = 35     # JMA period for candle highs (upper band)
JMA_LENGTH_LOW = 35      # JMA period for candle lows (lower band)
JMA_LENGTH_CLOSE = 7     # JMA period for close (signal line)
JMA_PHASE = 50           # -100 to 100, controls lag vs overshoot
JMA_POWER = 2            # Smoothness level, 1-3

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

# Hardcoded precisions
PRECISIONS = {
    "ETHUSDT": 3, "BNBUSDT": 2, "XRPUSDT": 1, "SOLUSDT": 3, "ADAUSDT": 0, "DOGEUSDT": 0, "TRXUSDT": 0
}

# Calculate kline limits
MA_PERIODS = max(JMA_LENGTH_HIGH, JMA_LENGTH_LOW, JMA_LENGTH_CLOSE)
ER_PERIODS_NEEDED = ER_PERIODS if USE_ER else 0
KLINE_LIMIT = max(DI_PERIODS + 100 if USE_DI else 100, MA_PERIODS + 100, ER_PERIODS_NEEDED + 100)

# ENTRY STRATEGY TOGGLE
ENTRY_STRATEGY = "SYMMETRIC"  # or "SYMMETRIC"
# CROSSOVER: JMA close crosses through ribbon (high/low bands)
# SYMMETRIC: Price breaks above/below ribbon bands

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "jma_high": None,
        "jma_low": None,
        "jma_close": None,
        "prev_jma_high": None,
        "prev_jma_low": None,
        "prev_jma_close": None,
        "prev_tama_high": None,
        "prev_tama_low": None,
        "prev_tama_close": None,
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
        # Flag to prevent spam warnings
        "stop_warning_logged": False,
    }
    for symbol in SYMBOLS
}

# Rate limiting
api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE FUNCTIONS =========================
def save_klines():
    """Save klines to JSON"""
    save_data = {sym: list(state[sym]["klines"]) for sym in SYMBOLS}
    with open('klines.json', 'w') as f:
        json.dump(save_data, f)
    logging.info("📥 Saved klines to klines.json")

def load_klines():
    """Load klines from JSON"""
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
    """
    Save current positions and trailing stops
    CRITICAL: Each symbol's data saved independently - no cross-contamination
    """
    position_data = {}
    
    # Save each symbol's data independently
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
        json.dump(position_data, f, indent=2)  # Pretty print for debugging

def load_positions():
    """
    Load positions from JSON
    CRITICAL: Each symbol's data is independent - no cross-contamination
    """
    try:
        with open('positions.json', 'r') as f:
            position_data = json.load(f)
        
        logging.info("💾 Loading positions from positions.json...")
        
        # Process each symbol independently
        for sym in SYMBOLS:
            if sym not in position_data:
                logging.info(f"   [{sym}]: No saved data - starting fresh")
                continue
                
            # Load THIS symbol's data only
            loaded_long = position_data[sym].get("long_position", 0.0)
            loaded_short = position_data[sym].get("short_position", 0.0)
            
            state[sym]["long_position"] = loaded_long
            state[sym]["long_trailing_stop_price"] = position_data[sym].get("long_trailing_stop_price")
            state[sym]["long_peak_price"] = position_data[sym].get("long_peak_price")
            state[sym]["short_position"] = loaded_short
            state[sym]["short_trailing_stop_price"] = position_data[sym].get("short_trailing_stop_price")
            state[sym]["short_lowest_price"] = position_data[sym].get("short_lowest_price")
            
            # DIAGNOSTIC: Detect stale positions for THIS symbol
            if loaded_long > 0:
                if state[sym]["long_trailing_stop_price"] is None or state[sym]["long_peak_price"] is None:
                    logging.warning(f"⚠️ [{sym}] STALE LONG DATA: position={loaded_long} but stops missing")
                    logging.warning(f"   [{sym}] Likely failed exit - will verify with exchange")
                else:
                    logging.info(f"✅ [{sym}] LONG loaded: pos={loaded_long}, peak={state[sym]['long_peak_price']:.6f}, stop={state[sym]['long_trailing_stop_price']:.6f}")
            
            if loaded_short > 0:
                if state[sym]["short_trailing_stop_price"] is None or state[sym]["short_lowest_price"] is None:
                    logging.warning(f"⚠️ [{sym}] STALE SHORT DATA: position={loaded_short} but stops missing")
                    logging.warning(f"   [{sym}] Likely failed exit - will verify with exchange")
                else:
                    logging.info(f"✅ [{sym}] SHORT loaded: pos={loaded_short}, low={state[sym]['short_lowest_price']:.6f}, stop={state[sym]['short_trailing_stop_price']:.6f}")
            
            if loaded_long == 0 and loaded_short == 0:
                logging.info(f"   [{sym}]: FLAT (no positions)")
                        
        logging.info("💾 Position loading complete - each symbol independent")
        
    except FileNotFoundError:
        logging.info("💾 No positions.json found - all symbols starting fresh")
    except Exception as e:
        logging.error(f"❌ Failed to load positions: {e} - all symbols starting fresh")

def kalman_filter(prices, R=0.01**2):
    """
    Apply a 1D Kalman Filter to a series of prices.
    - R: Measurement noise covariance (tune this value)
    - Returns: A smoothed series of prices
    """
    n_iter = len(prices)
    sz = (n_iter,)

    # Allocate space for arrays
    xhat = np.zeros(sz)      # a posteriori estimate of x
    P = np.zeros(sz)         # a posteriori error estimate
    xhatminus = np.zeros(sz) # a priori estimate of x
    Pminus = np.zeros(sz)    # a priori error estimate
    K = np.zeros(sz)         # gain or blending factor

    Q = 1e-5 # Process noise covariance

    # Initial guesses
    xhat[0] = prices[0]
    P[0] = 1.0

    for k in range(1, n_iter):
        # Time update
        xhatminus[k] = xhat[k-1]
        Pminus[k] = P[k-1] + Q

        # Measurement update
        K[k] = Pminus[k] / (Pminus[k] + R)
        xhat[k] = xhatminus[k] + K[k] * (prices[k] - xhatminus[k])
        P[k] = (1 - K[k]) * Pminus[k]

    return xhat

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    """Round position size to appropriate precision"""
    prec = PRECISIONS.get(symbol, 3)
    return round(size, prec)

async def safe_api_call(func, *args, **kwargs):
    """Make API call with exponential backoff"""
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
    """Place market order"""
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
    """Initialize trailing stop when entering a position"""
    st = state[symbol]
    
    if side == "LONG":
        st["long_peak_price"] = entry_price
        st["long_trailing_stop_price"] = entry_price * (1 - TRAILING_STOP_PERCENT / 100)
        logging.info(f"🎯 {symbol} LONG Trailing Stop initialized: Peak={entry_price:.6f}, Stop={st['long_trailing_stop_price']:.6f}")
    
    elif side == "SHORT":
        st["short_lowest_price"] = entry_price
        st["short_trailing_stop_price"] = entry_price * (1 + TRAILING_STOP_PERCENT / 100)
        logging.info(f"🎯 {symbol} SHORT Trailing Stop initialized: Lowest={entry_price:.6f}, Stop={st['short_trailing_stop_price']:.6f}")
    
    # Reset spam warning flag since stop is now initialized
    st["stop_warning_logged"] = False
    save_positions()

def update_trailing_stop(symbol: str, current_price: float) -> dict:
    """
    Update trailing stops and check if either is hit
    Returns dict with 'long_hit' and 'short_hit' flags
    """
    st = state[symbol]
    result = {"long_hit": False, "short_hit": False}
    
    # Update LONG trailing stop
    if st["long_position"] > 0:
        if st["long_peak_price"] is None or st["long_trailing_stop_price"] is None:
            # Stop is missing - return early, trading_loop will fix it
            # Only log warning ONCE (not every 0.1 seconds!)
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
    
    # Update SHORT trailing stop
    if st["short_position"] > 0:
        if st["short_lowest_price"] is None or st["short_trailing_stop_price"] is None:
            # Stop is missing - return early, trading_loop will fix it
            # Only log warning ONCE (not every 0.1 seconds!)
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
    """Reset trailing stop when exiting position"""
    st = state[symbol]
    if side == "LONG":
        st["long_trailing_stop_price"] = None
        st["long_peak_price"] = None
    elif side == "SHORT":
        st["short_trailing_stop_price"] = None
        st["short_lowest_price"] = None
    save_positions()

# ========================= INDICATOR CALCULATIONS =========================
def calculate_jma(symbol: str, field: str, length: int, phase: int = 50, power: int = 2) -> Optional[float]:
    """Jurik Moving Average (JMA) calculation"""
    klines = state[symbol]["klines"]

    if len(klines) < length + 1:
        return None

    if USE_LIVE_CANDLE:
        completed = list(klines)
    else:
        completed = list(klines)[:-1]
    
    if len(completed) < length:
        return None

    # Kalman Filter integration
    raw_prices = [k.get(field, 0) for k in completed]
    kalman_prices = kalman_filter(raw_prices)
    values = kalman_prices.tolist()

    if not values:
        return None

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

def calculate_kaufman_er(symbol: str) -> Optional[float]:
    """
    Efficiency Ratio (ER) by Perry Kaufman
    Measures trending vs choppy markets
    ER = |Net Change| / Sum of |Price Changes|
    Range: 0.0 (choppy) to 1.0 (strong trend)
    """
    klines = list(state[symbol]["klines"])
    
    if USE_LIVE_CANDLE:
        completed = klines
    else:
        completed = klines[:-1]
    
    if len(completed) < ER_PERIODS + 1:
        return None

    # Get last ER_PERIODS + 1 closes
    closes = [k["close"] for k in completed[-(ER_PERIODS + 1):]]
    
    # Net change over period
    net_change = abs(closes[-1] - closes[0])
    
    # Sum of absolute price changes
    sum_changes = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes)))
    
    if sum_changes == 0:
        return None
    
    er = net_change / sum_changes
    
    state[symbol]["efficiency_ratio"] = er
    state[symbol]["er_ready"] = True
    
    return er

# ========================= TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> dict:
    """
    JMA RIBBON Strategy - Uses HIGH/LOW bands
    TRUE HEDGE MODE - can hold both LONG and SHORT simultaneously
    NO FLIPPING - positions are independent
    """
    st = state[symbol]
    price = st["price"]

    result = {
        "long_entry": False,
        "short_entry": False,
    }

    if price is None or not st["ready"]:
        return result

    # TAMA Calculation
    jma_high = calculate_jma(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
    jma_low = calculate_jma(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
    jma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)

    if jma_high is None or jma_low is None or jma_close is None:
        return result

    er = calculate_kaufman_er(symbol)
    if er is None:
        er = 0.5

    alpha = 2 / (JMA_LENGTH_CLOSE + 1)
    klines = list(st["klines"])

    raw_high_prices = [k['high'] for k in klines]
    kalman_high_prices = kalman_filter(raw_high_prices)
    tama_high = jma_high + alpha * er * (kalman_high_prices[-1] - jma_high)

    raw_low_prices = [k['low'] for k in klines]
    kalman_low_prices = kalman_filter(raw_low_prices)
    tama_low = jma_low + alpha * er * (kalman_low_prices[-1] - jma_low)

    raw_close_prices = [k['close'] for k in klines]
    kalman_close_prices = kalman_filter(raw_close_prices)
    tama_close = jma_close + alpha * er * (kalman_close_prices[-1] - jma_close)

    if USE_ER and st["efficiency_ratio"] is None:
        return result

    prev_jma_close = st["prev_jma_close"]

    if prev_jma_close is None:
        st["prev_jma_close"] = jma_close
        return result

    # Efficiency Ratio filter - only trade in trending markets
    er_allows_trading = (er >= ER_THRESHOLD) if USE_ER else True
    
    if USE_ER and not er_allows_trading:
        # Market too choppy - no entries
        return result

    # CROSSOVER STRATEGY: composite close crosses through composite bands
    if ENTRY_STRATEGY == "CROSSOVER":
        prev_tama_high = st.get("prev_tama_high", tama_high)
        prev_tama_low = st.get("prev_tama_low", tama_low)
        prev_tama_close = st.get("prev_tama_close", tama_close)

        # LONG: TAMA close crosses above TAMA high (upper band)
        cross_above_high = (tama_close > tama_high) and (prev_tama_close <= prev_tama_high)

        # SHORT: TAMA close crosses below TAMA low (lower band)
        cross_below_low = (tama_close < tama_low) and (prev_tama_close >= prev_tama_low)

        if st["long_position"] == 0:
            if cross_above_high:
                result["long_entry"] = True
                er_str = f", ER={er:.3f}" if USE_ER else ""
                logging.info(f"🟢 {symbol} ENTRY LONG (CROSSOVER: tama_close={tama_close:.6f} > tama_high={tama_high:.6f}{er_str})")

        if st["short_position"] == 0:
            if cross_below_low:
                result["short_entry"] = True
                er_str = f", ER={er:.3f}" if USE_ER else ""
                logging.info(f"🟢 {symbol} ENTRY SHORT (CROSSOVER: tama_close={tama_close:.6f} < tama_low={tama_low:.6f}{er_str})")

    # SYMMETRIC STRATEGY: Price breaks above/below composite bands
    elif ENTRY_STRATEGY == "SYMMETRIC":
        # LONG: Price above both HIGH and LOW bands
        price_above_ribbon = (price > tama_high) and (price > tama_low)

        # SHORT: Price below both HIGH and LOW bands
        price_below_ribbon = (price < tama_high) and (price < tama_low)

        if st["long_position"] == 0:
            if price_above_ribbon:
                result["long_entry"] = True
                er_str = f", ER={er:.3f}" if USE_ER else ""
                logging.info(f"🟢 {symbol} ENTRY LONG (SYMMETRIC: price={price:.6f} > tama_high={tama_high:.6f} & tama_low={tama_low:.6f}{er_str})")

        if st["short_position"] == 0:
            if price_below_ribbon:
                result["short_entry"] = True
                er_str = f", ER={er:.3f}" if USE_ER else ""
                logging.info(f"🟢 {symbol} ENTRY SHORT (SYMMETRIC: price={price:.6f} < tama_high={tama_high:.6f} & tama_low={tama_low:.6f}{er_str})")

    st["prev_jma_high"] = jma_high
    st["prev_jma_low"] = jma_low
    st["prev_jma_close"] = jma_close

    return result

# ========================= MAIN LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    """WebSocket feed - builds candles"""
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
                                    jma_high = calculate_jma(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
                                    jma_low = calculate_jma(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
                                    jma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                                    if USE_ER:
                                        calculate_kaufman_er(symbol)
                                    
                                    er_ok = (not USE_ER) or (state[symbol]["efficiency_ratio"] is not None)
                                    
                                    if (jma_high is not None) and (jma_low is not None) and (jma_close is not None) and er_ok:
                                        state[symbol]["ready"] = True
                                        candle_mode = " [LIVE]" if USE_LIVE_CANDLE else " [COMPLETED]"
                                        logging.info(f"✅ {symbol} ready - JMA Ribbon initialized{candle_mode}")
                                else:
                                    if USE_ER:
                                         calculate_kaufman_er(symbol)

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
            jma_high = calculate_jma(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER)
            jma_low = calculate_jma(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER)
            jma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)

            if price and jma_high and jma_low and jma_close:
                er_str = f"{st['efficiency_ratio']:.3f}" if USE_ER and st.get("efficiency_ratio") else "N/A"
                er_status = ""
                if USE_ER and st.get("efficiency_ratio"):
                    if st["efficiency_ratio"] >= ER_THRESHOLD:
                        er_status = " ✅ TREND"
                    else:
                        er_status = " ⚠️ CHOPPY"

                logging.info(f"{symbol}: Price={price:.6f}")
                logging.info(f"  TAMA Bands: HIGH={jma_high:.6f} | LOW={jma_low:.6f}")
                logging.info(f"  TAMA Close={jma_close:.6f} | ER={er_str}{er_status}")
                
                # Show positions
                long_status = f"LONG: {st['long_position']}" if st['long_position'] > 0 else "LONG: None"
                short_status = f"SHORT: {st['short_position']}" if st['short_position'] > 0 else "SHORT: None"
                logging.info(f"  {long_status} | {short_status}")
                
                # FORCE DISPLAY: Show trailing stops for ALL positions (even if None)
                if st["long_position"] > 0:
                    if st["long_trailing_stop_price"] and st["long_peak_price"]:
                        distance = ((price - st["long_trailing_stop_price"]) / price) * 100
                        logging.info(f"  LONG Stop: ${st['long_trailing_stop_price']:.6f} (Peak: ${st['long_peak_price']:.6f}, Dist: {distance:.2f}%)")
                    else:
                        logging.warning(f"  ⚠️ LONG Stop: MISSING! (Peak={st['long_peak_price']}, Stop={st['long_trailing_stop_price']}, Pos={st['long_position']})")
                
                if st["short_position"] > 0:
                    if st["short_trailing_stop_price"] and st["short_lowest_price"]:
                        distance = ((st["short_trailing_stop_price"] - price) / price) * 100
                        logging.info(f"  SHORT Stop: ${st['short_trailing_stop_price']:.6f} (Low: ${st['short_lowest_price']:.6f}, Dist: {distance:.2f}%)")
                    else:
                        logging.warning(f"  ⚠️ SHORT Stop: MISSING! (Low={st['short_lowest_price']}, Stop={st['short_trailing_stop_price']}, Pos={st['short_position']})")

        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """
    Main trading logic - TRUE HEDGE MODE with JMA RIBBON
    CRITICAL: Each symbol processed independently in isolated state
    """
    while True:
        await asyncio.sleep(0.1)

        # Process EACH symbol independently - no shared state between symbols
        for symbol in SYMBOLS:
            # Get THIS symbol's state only - completely isolated
            st = state[symbol]
            
            if not st["ready"]:
                continue

            price = st["price"]
            if price is None:
                continue

            # ===== PER-SYMBOL SAFETY CHECK =====
            # Check and fix missing stops for THIS symbol only
            if st["long_position"] > 0 and (st["long_trailing_stop_price"] is None or st["long_peak_price"] is None):
                logging.warning(f"🔧 [{symbol}] SAFETY: Re-initializing missing LONG stop for THIS symbol")
                initialize_trailing_stop(symbol, "LONG", price)
            
            if st["short_position"] > 0 and (st["short_trailing_stop_price"] is None or st["short_lowest_price"] is None):
                logging.warning(f"🔧 [{symbol}] SAFETY: Re-initializing missing SHORT stop for THIS symbol")
                initialize_trailing_stop(symbol, "SHORT", price)

            # ===== PER-SYMBOL TRAILING STOPS =====
            # Update and check stops for THIS symbol only
            stop_result = update_trailing_stop(symbol, price)
            
            # Handle LONG stop hit for THIS symbol
            if stop_result["long_hit"] and st["long_position"] > 0:
                success = await execute_close_position(client, symbol, "LONG", st["long_position"])
                if success:
                    # Only clear THIS symbol's LONG position
                    st["long_position"] = 0.0
                    reset_trailing_stop(symbol, "LONG")
                    save_positions()
                    logging.info(f"✅ [{symbol}] LONG position closed and cleared")
                else:
                    logging.error(f"❌ [{symbol}] LONG close failed - keeping THIS symbol's position active")
            
            # Handle SHORT stop hit for THIS symbol
            if stop_result["short_hit"] and st["short_position"] > 0:
                success = await execute_close_position(client, symbol, "SHORT", st["short_position"])
                if success:
                    # Only clear THIS symbol's SHORT position
                    st["short_position"] = 0.0
                    reset_trailing_stop(symbol, "SHORT")
                    save_positions()
                    logging.info(f"✅ [{symbol}] SHORT position closed and cleared")
                else:
                    logging.error(f"❌ [{symbol}] SHORT close failed - keeping THIS symbol's position active")

            # ===== PER-SYMBOL ENTRY SIGNALS =====
            # Check signals for THIS symbol only
            signals = update_trading_signals(symbol)
            
            # Handle LONG entry for THIS symbol
            if signals["long_entry"] and st["long_position"] == 0:
                target_size = SYMBOLS[symbol]
                success = await execute_open_position(client, symbol, "LONG", target_size)
                if success:
                    # Only set THIS symbol's LONG position
                    st["long_position"] = target_size
                    initialize_trailing_stop(symbol, "LONG", price)
                    save_positions()
                    logging.info(f"✅ [{symbol}] LONG position opened and stop initialized")
            
            # Handle SHORT entry for THIS symbol
            if signals["short_entry"] and st["short_position"] == 0:
                target_size = SYMBOLS[symbol]
                success = await execute_open_position(client, symbol, "SHORT", target_size)
                if success:
                    # Only set THIS symbol's SHORT position
                    st["short_position"] = target_size
                    initialize_trailing_stop(symbol, "SHORT", price)
                    save_positions()
                    logging.info(f"✅ [{symbol}] SHORT position opened and stop initialized")

async def execute_open_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Open new position with 2-second duplicate protection"""
    st = state[symbol]
    now = time.time()
    
    # Layer 3: Execution Dedup (2 seconds)
    if side == "LONG":
        if (now - st["last_long_exec_ts"]) < 2.0:
            logging.info(f"🛡️ {symbol} LONG dedup: skipping duplicate entry (2s protection)")
            return False
        st["last_long_exec_ts"] = now
    else:
        if (now - st["last_short_exec_ts"]) < 2.0:
            logging.info(f"🛡️ {symbol} SHORT dedup: skipping duplicate entry (2s protection)")
            return False
        st["last_short_exec_ts"] = now
    
    # Place order
    order_side = "BUY" if side == "LONG" else "SELL"
    success = await place_order(client, symbol, order_side, size, f"{side} ENTRY")
    return success

async def execute_close_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Close existing position"""
    order_side = "SELL" if side == "LONG" else "BUY"
    success = await place_order(client, symbol, order_side, size, f"{side} CLOSE")
    
    if not success:
        logging.error(f"❌ {symbol} {side} CLOSE FAILED - position remains open!")
        # Don't clear position state if close failed!
    
    return success

async def recover_positions_from_exchange(client: AsyncClient):
    """
    Recover actual positions from Binance
    CRITICAL: Each symbol handled independently to prevent cross-contamination
    """
    logging.info("🔍 Checking exchange for existing positions...")
    logging.info("   Each symbol will be verified and initialized separately")
    
    try:
        account_info = await safe_api_call(client.futures_account)
        positions = account_info.get('positions', [])
        
        # Track recovery per symbol
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
                    logging.info(f"♻️ [{symbol}] RECOVERED LONG: Amt={position_amt}, Entry={entry_price:.6f}, Mark={mark_price:.6f}, PNL={unrealized_pnl:.2f}")
                    
                    # Set position for THIS symbol only
                    state[symbol]["long_position"] = position_amt
                    recovered_symbols[symbol]["long"] = True
                    
                    # Initialize stop for THIS symbol only
                    init_price = mark_price if mark_price > 0 else (state[symbol]["price"] if state[symbol]["price"] else entry_price)
                    
                    if init_price:
                        initialize_trailing_stop(symbol, "LONG", init_price)
                        logging.info(f"✅ [{symbol}] LONG stop initialized independently at {init_price:.6f}")
                    else:
                        logging.warning(f"⚠️ [{symbol}] LONG recovered but NO PRICE - will auto-fix in trading loop")
                
                elif position_side == "SHORT" and position_amt < 0:
                    logging.info(f"♻️ [{symbol}] RECOVERED SHORT: Amt={position_amt}, Entry={entry_price:.6f}, Mark={mark_price:.6f}, PNL={unrealized_pnl:.2f}")
                    
                    # Set position for THIS symbol only
                    state[symbol]["short_position"] = abs(position_amt)
                    recovered_symbols[symbol]["short"] = True
                    
                    # Initialize stop for THIS symbol only
                    init_price = mark_price if mark_price > 0 else (state[symbol]["price"] if state[symbol]["price"] else entry_price)
                    
                    if init_price:
                        initialize_trailing_stop(symbol, "SHORT", init_price)
                        logging.info(f"✅ [{symbol}] SHORT stop initialized independently at {init_price:.6f}")
                    else:
                        logging.warning(f"⚠️ [{symbol}] SHORT recovered but NO PRICE - will auto-fix in trading loop")
        
        # Report recovery status PER SYMBOL
        recovered_count = sum(1 for sym_data in recovered_symbols.values() if sym_data["long"] or sym_data["short"])

        if recovered_count > 0:
            logging.info(f"✅ Recovery complete: {recovered_count} symbols with active positions")
            for sym in SYMBOLS:
                if recovered_symbols[sym]["long"] or recovered_symbols[sym]["short"]:
                    status = []
                    if recovered_symbols[sym]["long"]:
                        status.append("LONG")
                    if recovered_symbols[sym]["short"]:
                        status.append("SHORT")
                    logging.info(f"   [{sym}]: {' + '.join(status)} recovered")
            save_positions()
        else:
            logging.info("✅ No active positions found on exchange")
            
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")

async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 STRATEGY: TAMA (Triple-Layer Adaptive Moving Average)")
    logging.info(f"📊 MODE: TRUE HEDGE MODE (LONG + SHORT simultaneously)")
    logging.info(f"📊 SYMBOLS: {len(SYMBOLS)} symbols tracked INDEPENDENTLY")
    logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
    logging.info(f"📊 TAMA Bands: high={JMA_LENGTH_HIGH}, low={JMA_LENGTH_LOW}, close={JMA_LENGTH_CLOSE}, phase={JMA_PHASE}, power={JMA_POWER}")
    logging.info(f"📊 TAMA Mode: {'LIVE CANDLE' if USE_LIVE_CANDLE else 'COMPLETED ONLY'}")
    logging.info(f"📊 Entry Strategy: {ENTRY_STRATEGY}")
    logging.info(f"📊 Trailing Stop: {TRAILING_STOP_PERCENT}% (~{TRAILING_STOP_PERCENT * LEVERAGE:.1f}% pos risk @ {LEVERAGE}x)")
    logging.info(f"🛡️ Protection: 2-second duplicate prevention PER SYMBOL")
    logging.info(f"🔍 Diagnostics: Enhanced logging + automatic stop recovery PER SYMBOL")
    
    if USE_ER:
        logging.info(f"📊 Efficiency Ratio (Kaufman): {ER_PERIODS} periods, threshold={ER_THRESHOLD} (filters choppy markets)")

    load_klines()
    load_positions()
    
    # Show initial state PER SYMBOL before exchange recovery
    logging.info("📋 Initial state per symbol (before exchange verification):")
    for sym in SYMBOLS:
        st = state[sym]
        long_str = f"LONG={st['long_position']}" if st['long_position'] > 0 else "LONG=None"
        short_str = f"SHORT={st['short_position']}" if st['short_position'] > 0 else "SHORT=None"
        logging.info(f"   [{sym}]: {long_str}, {short_str}")
    
    await recover_positions_from_exchange(client)
    
    # Show final state PER SYMBOL after exchange recovery
    logging.info("📋 Final state per symbol (after exchange verification):")
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
        jma_high_ready = len(klines) >= JMA_LENGTH_HIGH and calculate_jma(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER) is not None
        jma_low_ready = len(klines) >= JMA_LENGTH_LOW and calculate_jma(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER) is not None
        jma_close_ready = len(klines) >= JMA_LENGTH_CLOSE and calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER) is not None
        if USE_ER:
            calculate_kaufman_er(symbol)
        er_ready = (not USE_ER) or (state[symbol]["efficiency_ratio"] is not None)

        if jma_high_ready and jma_low_ready and jma_close_ready and er_ready:
            state[symbol]["ready"] = True
            logging.info(f"✅ {symbol} ready from loaded data")
        else:
            symbols_needing_data.append(symbol)

    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")

                needed_candles = max(MA_PERIODS + 100, (DI_PERIODS + 100 if USE_DI else 100))
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

                jma_high_ok = calculate_jma(symbol, "high", JMA_LENGTH_HIGH, JMA_PHASE, JMA_POWER) is not None
                jma_low_ok = calculate_jma(symbol, "low", JMA_LENGTH_LOW, JMA_PHASE, JMA_POWER) is not None
                jma_close_ok = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER) is not None
                if USE_ER:
                    calculate_kaufman_er(symbol)
                er_ok = (not USE_ER) or (st["efficiency_ratio"] is not None)

                if jma_high_ok and jma_low_ok and jma_close_ok and er_ok:
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

        logging.info("🚀 Bot started - JMA RIBBON TRUE HEDGE MODE")

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
    print("TAMA STRATEGY - TRUE HEDGE MODE")
    print(f"TIMEFRAME: {BASE_TIMEFRAME}")
    print("ENHANCED DIAGNOSTICS + PER-SYMBOL ISOLATION")
    print("=" * 80)
    print(f"Strategy: TAMA (Triple-Layer Adaptive Moving Average)")
    print(f"Mode: TRUE HEDGE (can hold LONG + SHORT simultaneously)")
    print(f"Symbols: {len(SYMBOLS)} symbols - EACH TRACKED INDEPENDENTLY")
    print(f"  {', '.join(SYMBOLS.keys())}")
    print(f"TAMA Bands: high={JMA_LENGTH_HIGH}, low={JMA_LENGTH_LOW}, close={JMA_LENGTH_CLOSE}")
    print(f"TAMA Parameters: phase={JMA_PHASE}, power={JMA_POWER}")
    print(f"TAMA Calculation: {'LIVE CANDLE' if USE_LIVE_CANDLE else 'COMPLETED ONLY'}")
    print(f"Entry Strategy: {ENTRY_STRATEGY}")
    print(f"  - CROSSOVER: JMA close crosses through ribbon bands")
    print(f"  - SYMMETRIC: Price breaks above/below ribbon")
    print(f"Trailing Stop: {TRAILING_STOP_PERCENT}% price (~{TRAILING_STOP_PERCENT * LEVERAGE:.1f}% position @ {LEVERAGE}x)")
    print(f"Protection: 2-second duplicate order prevention PER SYMBOL")
    print(f"Diagnostics: Enhanced logging + automatic stop recovery PER SYMBOL")
    print(f"  - Each symbol has isolated state (no cross-contamination)")
    print(f"  - Warns when stops are missing for specific symbols")
    print(f"  - Auto-reinitializes missing stops per symbol")
    print(f"  - Forces display of all stop states per symbol")
    print(f"Efficiency Ratio (Kaufman): {'ENABLED (periods=' + str(ER_PERIODS) + ', threshold=' + str(ER_THRESHOLD) + ')' if USE_ER else 'DISABLED'}")
    print(f"Exit: TRAILING STOP ONLY (no strategy exits)")
    print("=" * 80)

    asyncio.run(main())
