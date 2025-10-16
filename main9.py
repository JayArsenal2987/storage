#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time
import atexit
from binance import AsyncClient
from collections import deque
from typing import Optional
from dotenv import load_dotenv

# ========================= CONFIG =========================
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE = int(os.getenv("LEVERAGE", "50"))

# Trailing Stop Configuration
TRAILING_STOP_CALLBACK = 0.5  # 0.5% trailing stop callback rate

# Indicator Settings
DI_PERIODS = int(os.getenv("DI_PERIODS", "10"))
USE_DI = False  # Toggle DMI indicator
USE_HEIKIN_ASHI = False  # Toggle Heikin Ashi candles

# Timeframe configuration
BASE_TIMEFRAME = "1h"  # Options: "1m", "15m", "1h"

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
elif BASE_TIMEFRAME == "15m":
    BASE_MINUTES = 15
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

# JMA parameters
JMA_LENGTH_CLOSE = 7      # JMA period for entry MA
JMA_LENGTH_OPEN = 40      # JMA period for trend MA
JMA_PHASE = 50            # -100 to 100, controls lag vs overshoot
JMA_POWER = 2             # Smoothness level, 1-3

# MA Type Configuration
ENTRY_MA_TYPE = "JMA"   # Options: "JMA", "KAMA" - faster MA for entries
EXIT_MA_TYPE = "KAMA"   # Options: "JMA", "KAMA" - slower MA for trend confirmation

# KAMA parameters
KAMA_ER_PERIOD = 9     # Efficiency Ratio lookback period
KAMA_FAST = 2          # Fast EMA period
KAMA_SLOW = 30         # Slow EMA period

# KAMA Source Configuration (Double-Smoothing Feature)
KAMA_USE_JMA_SOURCE = True   # True: JMA→KAMA (ultra-smooth)
                              # False: Raw price→KAMA (standard)
KAMA_JMA_SOURCE_LENGTH = 7    # JMA length when using JMA as KAMA source

# Efficiency Ratio (ER) Filter
USE_ER_FILTER = True          # Enable/disable ER filter
ER_THRESHOLD = 0.3            # Only trade when ER > threshold
USE_ER_FOR_ENTRY = True       # Apply ER filter to entry signals

# Chaikin Money Flow (CMF) Filter
USE_CMF_FILTER = True           # Enable/disable CMF filter
CMF_PERIOD = 27                 # CMF lookback period
CMF_THRESHOLD = 0.05            # Only trade when abs(CMF) > threshold
USE_CMF_FOR_ENTRY = True        # Apply CMF filter to entry signals
USE_CMF_DIRECTION = True        # Only LONG when CMF > 0, SHORT when CMF < 0

# Real-Time Calculation Mode
USE_REALTIME_CALCULATION = True   # Include current incomplete candle
REALTIME_EXIT_OVERRIDE = True     # Exit MA always uses completed candles

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
if KAMA_USE_JMA_SOURCE:
    ENTRY_MA_PERIOD = max(KAMA_ER_PERIOD, KAMA_JMA_SOURCE_LENGTH) if ENTRY_MA_TYPE == "KAMA" else JMA_LENGTH_CLOSE
    EXIT_MA_PERIOD = max(KAMA_ER_PERIOD, KAMA_JMA_SOURCE_LENGTH) if EXIT_MA_TYPE == "KAMA" else JMA_LENGTH_OPEN
else:
    ENTRY_MA_PERIOD = KAMA_ER_PERIOD if ENTRY_MA_TYPE == "KAMA" else JMA_LENGTH_CLOSE
    EXIT_MA_PERIOD = KAMA_ER_PERIOD if EXIT_MA_TYPE == "KAMA" else JMA_LENGTH_OPEN

MA_PERIODS = max(ENTRY_MA_PERIOD, EXIT_MA_PERIOD)
CMF_BUFFER = CMF_PERIOD if USE_CMF_FILTER else 0
KLINE_LIMIT = max(DI_PERIODS + 100 if USE_DI else 100, MA_PERIODS + 100, CMF_BUFFER + 100)

# ENTRY STRATEGY
ENTRY_STRATEGY = "CROSSOVER"  # or "SYMMETRIC"

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "long_signal": None,    # Independent LONG signal
        "short_signal": None,   # Independent SHORT signal
        "long_position": 0.0,   # LONG position size
        "short_position": 0.0,  # SHORT position size
        "ma_close": None,
        "ma_open": None,
        "prev_ma_close": None,
        "prev_ma_open": None,
        "plus_di": None,
        "minus_di": None,
        "ready": False,
        "ha_prev_close": None,
        "ha_prev_open": None,
        "efficiency_ratio": None,
        "cmf": None,
        "last_filter_block_log": 0,
        "last_long_entry_time": 0,  # Prevent duplicate LONG entries
        "last_short_entry_time": 0, # Prevent duplicate SHORT entries
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
    """Save current positions and signals"""
    position_data = {
        sym: {
            "long_signal": state[sym]["long_signal"],
            "short_signal": state[sym]["short_signal"],
            "long_position": state[sym]["long_position"],
            "short_position": state[sym]["short_position"],
        }
        for sym in SYMBOLS
    }
    with open('positions.json', 'w') as f:
        json.dump(position_data, f)

def load_positions():
    """Load positions from JSON"""
    try:
        with open('positions.json', 'r') as f:
            position_data = json.load(f)
        for sym in SYMBOLS:
            if sym in position_data:
                state[sym]["long_signal"] = position_data[sym].get("long_signal")
                state[sym]["short_signal"] = position_data[sym].get("short_signal")
                state[sym]["long_position"] = position_data[sym].get("long_position", 0.0)
                state[sym]["short_position"] = position_data[sym].get("short_position", 0.0)
        logging.info("💾 Loaded positions from positions.json")
    except FileNotFoundError:
        logging.info("No positions.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load positions: {e} - starting fresh")

# ========================= HEIKIN ASHI TRANSFORMATION =========================
def convert_to_heikin_ashi(candles: list, symbol: str) -> list:
    """Convert regular candles to Heikin Ashi candles"""
    if not candles or not USE_HEIKIN_ASHI:
        return candles
    
    ha_candles = []
    prev_ha_open = state[symbol].get("ha_prev_open")
    prev_ha_close = state[symbol].get("ha_prev_close")
    
    for i, candle in enumerate(candles):
        ha_close = (candle["open"] + candle["high"] + candle["low"] + candle["close"]) / 4
        
        if i == 0 and prev_ha_open is not None and prev_ha_close is not None:
            ha_open = (prev_ha_open + prev_ha_close) / 2
        elif i == 0:
            ha_open = (candle["open"] + candle["close"]) / 2
        else:
            ha_open = (ha_candles[i-1]["open"] + ha_candles[i-1]["close"]) / 2
        
        ha_high = max(candle["high"], ha_open, ha_close)
        ha_low = min(candle["low"], ha_open, ha_close)
        
        ha_candles.append({
            "open_time": candle["open_time"],
            "open": ha_open,
            "high": ha_high,
            "low": ha_low,
            "close": ha_close,
            "volume": candle.get("volume", 0)
        })
    
    if ha_candles:
        state[symbol]["ha_prev_open"] = ha_candles[-1]["open"]
        state[symbol]["ha_prev_close"] = ha_candles[-1]["close"]
    
    return ha_candles

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

async def place_market_order(client: AsyncClient, symbol: str, side: str, quantity: float, position_side: str):
    """Place market order and return order result"""
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            return None

        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side
        }

        result = await safe_api_call(client.futures_create_order, **params)
        logging.info(f"🚀 {symbol} {position_side} ENTRY - {side} {quantity} - OrderID: {result.get('orderId')}")
        return result

    except Exception as e:
        logging.error(f"❌ {symbol} {position_side} ENTRY FAILED: {e}")
        return None

async def place_trailing_stop(client: AsyncClient, symbol: str, position_side: str, quantity: float):
    """Place trailing stop loss order"""
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            return None

        # Determine side for trailing stop (opposite of position)
        side = "SELL" if position_side == "LONG" else "BUY"

        params = {
            "symbol": symbol,
            "side": side,
            "type": "TRAILING_STOP_MARKET",
            "quantity": quantity,
            "callbackRate": TRAILING_STOP_CALLBACK,
            "positionSide": position_side
        }

        result = await safe_api_call(client.futures_create_order, **params)
        logging.info(f"🛡️ {symbol} {position_side} TRAILING STOP placed - {TRAILING_STOP_CALLBACK}% callback - OrderID: {result.get('orderId')}")
        return result

    except Exception as e:
        logging.error(f"❌ {symbol} {position_side} TRAILING STOP FAILED: {e}")
        return None

# ========================= INDICATOR CALCULATIONS =========================
def calculate_jma(symbol: str, field: str, length: int, phase: int = 50, power: int = 2, force_completed: bool = False) -> Optional[float]:
    """Jurik Moving Average (JMA) calculation"""
    klines = state[symbol]["klines"]

    if len(klines) < length + 1:
        return None

    if USE_REALTIME_CALCULATION and not force_completed:
        completed = list(klines)
    else:
        completed = list(klines)[:-1]
    
    if len(completed) < length:
        return None

    if USE_HEIKIN_ASHI:
        completed = convert_to_heikin_ashi(completed, symbol)

    values = [k.get(field, None) for k in completed]
    if None in values:
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

def calculate_kama(symbol: str, field: str, er_period: int = 10, fast: int = 2, slow: int = 30, source_values: list = None, force_completed: bool = False) -> Optional[tuple]:
    """Kaufman's Adaptive Moving Average (KAMA) calculation"""
    klines = state[symbol]["klines"]

    if len(klines) < er_period + 1:
        return None

    if USE_REALTIME_CALCULATION and not force_completed:
        completed = list(klines)
    else:
        completed = list(klines)[:-1]
    
    if len(completed) < er_period + 1:
        return None

    if USE_HEIKIN_ASHI:
        completed = convert_to_heikin_ashi(completed, symbol)

    if source_values is not None:
        values = source_values
    else:
        values = [k.get(field, None) for k in completed]
        
    if None in values:
        return None

    fast_alpha = 2.0 / (fast + 1)
    slow_alpha = 2.0 / (slow + 1)

    kama = values[0]
    latest_er = 0.0

    for i in range(er_period, len(values)):
        change = abs(values[i] - values[i - er_period])
        
        volatility = 0.0
        for j in range(i - er_period + 1, i + 1):
            volatility += abs(values[j] - values[j - 1])
        
        if volatility == 0:
            er = 0.0
        else:
            er = change / volatility
        
        if i == len(values) - 1:
            latest_er = er

        sc = (er * (fast_alpha - slow_alpha) + slow_alpha) ** 2
        kama = kama + sc * (values[i] - kama)

    return (kama, latest_er)

def calculate_kama_with_jma_source(symbol: str, field: str, jma_length: int, er_period: int, fast: int, slow: int, force_completed: bool = False) -> Optional[tuple]:
    """Calculate KAMA using JMA as the source (double-smoothing)"""
    klines = state[symbol]["klines"]
    
    if len(klines) < max(jma_length, er_period) + 1:
        return None
    
    if USE_REALTIME_CALCULATION and not force_completed:
        completed = list(klines)
    else:
        completed = list(klines)[:-1]
    
    if len(completed) < max(jma_length, er_period) + 1:
        return None
    
    if USE_HEIKIN_ASHI:
        completed = convert_to_heikin_ashi(completed, symbol)
    
    price_values = [k.get(field, None) for k in completed]
    if None in price_values:
        return None
    
    jma_values = []
    
    phaseRatio = 0.5 if JMA_PHASE < -100 else (2.5 if JMA_PHASE > 100 else JMA_PHASE / 100 + 1.5)
    beta = 0.45 * (jma_length - 1) / (0.45 * (jma_length - 1) + 2)
    alpha = beta ** JMA_POWER
    
    e0 = 0.0
    e1 = 0.0
    e2 = 0.0
    jma = 0.0
    
    for i, src in enumerate(price_values):
        e0 = (1 - alpha) * src + alpha * e0
        e1 = (src - e0) * (1 - beta) + beta * e1
        e2 = (e0 + phaseRatio * e1 - jma) * ((1 - alpha) ** 2) + (alpha ** 2) * e2
        jma = e2 + jma
        
        if i >= jma_length - 1:
            jma_values.append(jma)
    
    if len(jma_values) < er_period + 1:
        return None
    
    return calculate_kama(symbol, field, er_period, fast, slow, source_values=jma_values, force_completed=False)

def calculate_cmf(symbol: str, period: int = 27, force_completed: bool = False) -> Optional[float]:
    """Chaikin Money Flow (CMF) - Volume-weighted indicator"""
    klines = state[symbol]["klines"]
    
    if len(klines) < period + 1:
        return None
    
    if USE_REALTIME_CALCULATION and not force_completed:
        completed = list(klines)
    else:
        completed = list(klines)[:-1]
    
    if len(completed) < period:
        return None
    
    if USE_HEIKIN_ASHI:
        ha_candles = convert_to_heikin_ashi(completed, symbol)
        for i, ha_candle in enumerate(ha_candles):
            ha_candle["volume"] = completed[i].get("volume", 0)
        completed = ha_candles
    
    mf_volume_sum = 0.0
    volume_sum = 0.0
    
    for k in completed[-period:]:
        high = k["high"]
        low = k["low"]
        close = k["close"]
        volume = k.get("volume", 0)
        
        high_low = high - low
        if high_low == 0:
            mf_multiplier = 0
        else:
            mf_multiplier = ((close - low) - (high - close)) / high_low
        
        mf_volume = mf_multiplier * volume
        
        mf_volume_sum += mf_volume
        volume_sum += volume
    
    if volume_sum == 0:
        return 0.0
    
    cmf = mf_volume_sum / volume_sum
    state[symbol]["cmf"] = cmf
    
    return cmf

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

def calculate_di(symbol: str) -> Optional[float]:
    """DI calculation"""
    klines = list(state[symbol]["klines"])
    
    if USE_HEIKIN_ASHI:
        klines = convert_to_heikin_ashi(klines, symbol)
    
    if len(klines) < DI_PERIODS + 1:
        return None

    if USE_REALTIME_CALCULATION:
        completed = klines
    else:
        completed = klines[:-1]
    
    if len(completed) < DI_PERIODS + 1:
        return None

    tr_values = []
    plus_dm_values = []
    minus_dm_values = []
    for i in range(1, len(completed)):
        cur = completed[i]
        prev = completed[i - 1]
        tr = calculate_true_range(cur["high"], cur["low"], prev["close"])
        tr_values.append(tr)
        plus_dm, minus_dm = calculate_directional_movement(cur["high"], prev["high"], cur["low"], prev["low"])
        plus_dm_values.append(plus_dm)
        minus_dm_values.append(minus_dm)

    if len(tr_values) < DI_PERIODS:
        return None

    alpha = 1.0 / DI_PERIODS

    sm_tr = sum(tr_values[:DI_PERIODS]) / DI_PERIODS
    sm_pdm = sum(plus_dm_values[:DI_PERIODS]) / DI_PERIODS
    sm_mdm = sum(minus_dm_values[:DI_PERIODS]) / DI_PERIODS

    for i in range(DI_PERIODS, len(tr_values)):
        sm_tr = alpha * tr_values[i] + (1 - alpha) * sm_tr
        sm_pdm = alpha * plus_dm_values[i] + (1 - alpha) * sm_pdm
        sm_mdm = alpha * minus_dm_values[i] + (1 - alpha) * sm_mdm

    if sm_tr == 0:
        return None

    plus_di  = (sm_pdm / sm_tr) * 100
    minus_di = (sm_mdm / sm_tr) * 100

    state[symbol]["plus_di"] = plus_di
    state[symbol]["minus_di"] = minus_di
    return None

def calculate_entry_ma(symbol: str, field: str = "close") -> Optional[float]:
    """Calculate entry MA based on ENTRY_MA_TYPE configuration"""
    if ENTRY_MA_TYPE == "KAMA":
        if KAMA_USE_JMA_SOURCE:
            result = calculate_kama_with_jma_source(symbol, field, KAMA_JMA_SOURCE_LENGTH, KAMA_ER_PERIOD, KAMA_FAST, KAMA_SLOW)
            if result is not None:
                kama_value, er_value = result
                state[symbol]["efficiency_ratio"] = er_value
                return kama_value
            return None
        else:
            result = calculate_kama(symbol, field, KAMA_ER_PERIOD, KAMA_FAST, KAMA_SLOW)
            if result is not None:
                kama_value, er_value = result
                state[symbol]["efficiency_ratio"] = er_value
                return kama_value
            return None
    elif ENTRY_MA_TYPE == "JMA":
        return calculate_jma(symbol, field, JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
    else:
        return calculate_jma(symbol, field, JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)

def calculate_exit_ma(symbol: str, field: str = "close") -> Optional[float]:
    """Calculate exit MA based on EXIT_MA_TYPE configuration"""
    use_completed = REALTIME_EXIT_OVERRIDE if USE_REALTIME_CALCULATION else False
    
    if EXIT_MA_TYPE == "KAMA":
        if KAMA_USE_JMA_SOURCE:
            result = calculate_kama_with_jma_source(symbol, field, KAMA_JMA_SOURCE_LENGTH, KAMA_ER_PERIOD, KAMA_FAST, KAMA_SLOW, force_completed=use_completed)
            if result is not None:
                kama_value, er_value = result
                state[symbol]["efficiency_ratio"] = er_value
                return kama_value
            return None
        else:
            result = calculate_kama(symbol, field, KAMA_ER_PERIOD, KAMA_FAST, KAMA_SLOW, force_completed=use_completed)
            if result is not None:
                kama_value, er_value = result
                state[symbol]["efficiency_ratio"] = er_value
                return kama_value
            return None
    elif EXIT_MA_TYPE == "JMA":
        return calculate_jma(symbol, field, JMA_LENGTH_OPEN, JMA_PHASE, JMA_POWER, force_completed=use_completed)
    else:
        return calculate_jma(symbol, field, JMA_LENGTH_OPEN, JMA_PHASE, JMA_POWER, force_completed=use_completed)

# ========================= SIMPLIFIED TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> dict:
    """
    SIMPLIFIED: Only generates ENTRY signals
    NO exit logic - trailing stops handle all exits
    NO flipping - LONG and SHORT are independent
    """
    st = state[symbol]
    price = st["price"]

    if price is None or not st["ready"]:
        return {"long_entry": False, "short_entry": False}

    ma_close = calculate_entry_ma(symbol, "close")
    ma_open = calculate_exit_ma(symbol, "close")
    if USE_DI:
        calculate_di(symbol)

    if ma_close is None or ma_open is None or (USE_DI and (st["plus_di"] is None or st["minus_di"] is None)):
        return {"long_entry": False, "short_entry": False}

    prev_close = st["prev_ma_close"]
    prev_open = st["prev_ma_open"]

    if prev_close is None or prev_open is None:
        st["prev_ma_close"] = ma_close
        st["prev_ma_open"] = ma_open
        return {"long_entry": False, "short_entry": False}
    
    # Calculate conditions
    price_above_both = (price > ma_close) and (price > ma_open)
    price_below_both = (price < ma_close) and (price < ma_open)
    
    crossover_aligned_long = (price > ma_close) and (ma_close > ma_open)
    crossover_aligned_short = (price < ma_close) and (ma_close < ma_open)

    plus_di = st["plus_di"]
    minus_di = st["minus_di"]
    di_bull = plus_di > minus_di if USE_DI else True
    di_bear = minus_di > plus_di if USE_DI else True
    
    # Get filters
    efficiency_ratio = st.get("efficiency_ratio")
    er_allows = efficiency_ratio > ER_THRESHOLD if (USE_ER_FILTER and USE_ER_FOR_ENTRY and efficiency_ratio is not None) else True

    cmf = calculate_cmf(symbol, CMF_PERIOD) if USE_CMF_FILTER else None
    cmf_allows_entry = abs(cmf) > CMF_THRESHOLD if (USE_CMF_FILTER and USE_CMF_FOR_ENTRY and cmf is not None) else True
    cmf_allows_long = cmf > 0 if (USE_CMF_FILTER and USE_CMF_DIRECTION and cmf is not None) else True
    cmf_allows_short = cmf < 0 if (USE_CMF_FILTER and USE_CMF_DIRECTION and cmf is not None) else True

    # MA type labels for logging
    entry_ma_label = "Entry_KAMA" if ENTRY_MA_TYPE == "KAMA" else "Entry_JMA"
    exit_ma_label = "Exit_KAMA" if EXIT_MA_TYPE == "KAMA" else "Exit_JMA"

    # Determine entry signals
    long_entry = False
    short_entry = False

    if ENTRY_STRATEGY == "CROSSOVER":
        long_entry = crossover_aligned_long and di_bull and er_allows and cmf_allows_entry and cmf_allows_long
        short_entry = crossover_aligned_short and di_bear and er_allows and cmf_allows_entry and cmf_allows_short
        
        if long_entry:
            er_msg = f" & ER={efficiency_ratio:.3f}" if USE_ER_FILTER and efficiency_ratio is not None else ""
            cmf_msg = f" & CMF={cmf:.3f}" if USE_CMF_FILTER and cmf is not None else ""
            logging.info(f"🟢 {symbol} LONG ENTRY SIGNAL (price {price:.6f} > {entry_ma_label} {ma_close:.6f} > {exit_ma_label} {ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + er_msg + cmf_msg + ")")
        elif short_entry:
            er_msg = f" & ER={efficiency_ratio:.3f}" if USE_ER_FILTER and efficiency_ratio is not None else ""
            cmf_msg = f" & CMF={cmf:.3f}" if USE_CMF_FILTER and cmf is not None else ""
            logging.info(f"🔴 {symbol} SHORT ENTRY SIGNAL (price {price:.6f} < {entry_ma_label} {ma_close:.6f} < {exit_ma_label} {ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + er_msg + cmf_msg + ")")
        elif not (er_allows and cmf_allows_entry) and (crossover_aligned_long or crossover_aligned_short):
            # Log when filters block an entry
            now = time.time()
            if now - st["last_filter_block_log"] > 300:  # 5 minutes cooldown
                block_reasons = []
                if not er_allows:
                    block_reasons.append(f"ER={efficiency_ratio:.3f} < {ER_THRESHOLD}")
                if not cmf_allows_entry:
                    block_reasons.append(f"|CMF|={abs(cmf):.3f} < {CMF_THRESHOLD}")
                if USE_CMF_DIRECTION and crossover_aligned_long and not cmf_allows_long:
                    block_reasons.append(f"CMF={cmf:.3f} < 0 (bearish flow blocks LONG)")
                if USE_CMF_DIRECTION and crossover_aligned_short and not cmf_allows_short:
                    block_reasons.append(f"CMF={cmf:.3f} > 0 (bullish flow blocks SHORT)")
                reason_str = " & ".join(block_reasons)
                logging.info(f"⚠️ {symbol} ENTRY BLOCKED by filter(s): {reason_str} [Next log in 5min]")
                st["last_filter_block_log"] = now
    
    elif ENTRY_STRATEGY == "SYMMETRIC":
        long_entry = price_above_both and di_bull and er_allows and cmf_allows_entry and cmf_allows_long
        short_entry = price_below_both and di_bear and er_allows and cmf_allows_entry and cmf_allows_short
        
        if long_entry:
            er_msg = f" & ER={efficiency_ratio:.3f}" if USE_ER_FILTER and efficiency_ratio is not None else ""
            cmf_msg = f" & CMF={cmf:.3f}" if USE_CMF_FILTER and cmf is not None else ""
            logging.info(f"🟢 {symbol} LONG ENTRY SIGNAL (price {price:.6f} > both MAs: {entry_ma_label}={ma_close:.6f}, {exit_ma_label}={ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + er_msg + cmf_msg + ")")
        elif short_entry:
            er_msg = f" & ER={efficiency_ratio:.3f}" if USE_ER_FILTER and efficiency_ratio is not None else ""
            cmf_msg = f" & CMF={cmf:.3f}" if USE_CMF_FILTER and cmf is not None else ""
            logging.info(f"🔴 {symbol} SHORT ENTRY SIGNAL (price {price:.6f} < both MAs: {entry_ma_label}={ma_close:.6f}, {exit_ma_label}={ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + er_msg + cmf_msg + ")")
        elif not (er_allows and cmf_allows_entry) and (price_above_both or price_below_both):
            # Log when filters block an entry
            now = time.time()
            if now - st["last_filter_block_log"] > 300:  # 5 minutes cooldown
                block_reasons = []
                if not er_allows:
                    block_reasons.append(f"ER={efficiency_ratio:.3f} < {ER_THRESHOLD}")
                if not cmf_allows_entry:
                    block_reasons.append(f"|CMF|={abs(cmf):.3f} < {CMF_THRESHOLD}")
                if USE_CMF_DIRECTION and price_above_both and not cmf_allows_long:
                    block_reasons.append(f"CMF={cmf:.3f} < 0 (bearish flow blocks LONG)")
                if USE_CMF_DIRECTION and price_below_both and not cmf_allows_short:
                    block_reasons.append(f"CMF={cmf:.3f} > 0 (bullish flow blocks SHORT)")
                reason_str = " & ".join(block_reasons)
                logging.info(f"⚠️ {symbol} ENTRY BLOCKED by filter(s): {reason_str} [Next log in 5min]")
                st["last_filter_block_log"] = now

    st["prev_ma_close"] = ma_close
    st["prev_ma_open"] = ma_open

    return {"long_entry": long_entry, "short_entry": short_entry}
       
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
                                    "close": float(k["c"]),
                                    "volume": float(k["v"])
                                }
                                
                                klines = state[symbol]["klines"]
                                
                                if klines and klines[-1]["open_time"] == kline_data["open_time"]:
                                    klines[-1] = kline_data
                                else:
                                    klines.append(kline_data)

                                if len(state[symbol]["klines"]) >= MA_PERIODS and not state[symbol]["ready"]:
                                    ma_close = calculate_entry_ma(symbol, "close")
                                    ma_open = calculate_exit_ma(symbol, "close")
                                    if USE_DI:
                                        calculate_di(symbol)
                                    if USE_CMF_FILTER:
                                        calculate_cmf(symbol, CMF_PERIOD)
                                    if (ma_close is not None) and (ma_open is not None) and (not USE_DI or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)):
                                        state[symbol]["ready"] = True
                                        ha_status = " (Heikin Ashi enabled)" if USE_HEIKIN_ASHI else ""
                                        entry_ma_label = "Entry_KAMA" if ENTRY_MA_TYPE == "KAMA" else "Entry_JMA"
                                        exit_ma_label = "Exit_KAMA" if EXIT_MA_TYPE == "KAMA" else "Exit_JMA"
                                        log_msg = f"✅ {symbol} ready for trading ({len(state[symbol]['klines'])} {BASE_TIMEFRAME} candles, {entry_ma_label} {ma_close:.6f}, {exit_ma_label} {ma_open:.6f}){ha_status}"
                                        logging.info(log_msg)
                                else:
                                    if USE_DI:
                                        calculate_di(symbol)
                                    if USE_CMF_FILTER:
                                        calculate_cmf(symbol, CMF_PERIOD)

                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")

        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)

async def status_logger():
    """5-minute status report"""
    while True:
        await asyncio.sleep(300)

        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"📊 === STATUS REPORT {current_time} ===")

        for symbol in SYMBOLS:
            st = state[symbol]

            if not st["ready"]:
                candle_count = len(st["klines"])
                price = st["price"]
                
                ma_close = calculate_entry_ma(symbol, "close")
                ma_open = calculate_exit_ma(symbol, "close")
                if USE_DI:
                    calculate_di(symbol)
                if USE_CMF_FILTER:
                    calculate_cmf(symbol, CMF_PERIOD)
                
                reasons = []
                if candle_count < MA_PERIODS + 1:
                    needed = (MA_PERIODS + 1) - candle_count
                    reasons.append(f"need {needed} more {BASE_TIMEFRAME} candles")
                if ma_close is None:
                    entry_ma_label = "Entry KAMA" if ENTRY_MA_TYPE == "KAMA" else "Entry JMA"
                    reasons.append(f"{entry_ma_label} not calculated")
                if ma_open is None:
                    exit_ma_label = "Exit KAMA" if EXIT_MA_TYPE == "KAMA" else "Exit JMA"
                    reasons.append(f"{exit_ma_label} not calculated")
                if USE_DI and (st["plus_di"] is None or st["minus_di"] is None):
                    reasons.append("DI not calculated")
                
                reason_str = ", ".join(reasons) if reasons else "unknown"
                price_str = f"Price={price:.6f} | " if price else ""
                logging.info(f"{symbol}: {price_str}Not ready - {candle_count} {BASE_TIMEFRAME} candles - Waiting for: {reason_str}")
                continue

            price = st["price"]
            ma_close = calculate_entry_ma(symbol, "close")
            ma_open = calculate_exit_ma(symbol, "close")
            plus_di = st.get("plus_di")
            minus_di = st.get("minus_di")
            efficiency_ratio = st.get("efficiency_ratio")
            cmf = st.get("cmf")

            if price and ma_close and ma_open:
                long_pos = "OPEN" if st["long_position"] > 0 else "FLAT"
                short_pos = "OPEN" if st["short_position"] > 0 else "FLAT"

                plus_di_str = f"{plus_di:.4f}" if USE_DI and plus_di is not None else "N/A"
                minus_di_str = f"{minus_di:.4f}" if USE_DI and minus_di is not None else "N/A"
                er_str = f"{efficiency_ratio:.3f}" if USE_ER_FILTER and efficiency_ratio is not None else "N/A"
                cmf_str = f"{cmf:.3f}" if USE_CMF_FILTER and cmf is not None else "N/A"

                entry_ma_label = "Entry_KAMA" if ENTRY_MA_TYPE == "KAMA" else "Entry_JMA"
                exit_ma_label = "Exit_KAMA" if EXIT_MA_TYPE == "KAMA" else "Exit_JMA"
                logging.info(f"{symbol}: Price={price:.6f} | {entry_ma_label}={ma_close:.6f} | {exit_ma_label}={ma_open:.6f}")
                logging.info(f"  Positions: LONG={long_pos} | SHORT={short_pos}")
                logging.info(f"  Indicators: +DI={plus_di_str} | -DI={minus_di_str} | ER={er_str} | CMF={cmf_str}")

                trend_up = (ma_close > ma_open)
                trend_down = (ma_close < ma_open)
                price_above_both = (price > ma_close) and (price > ma_open)
                price_below_both = (price < ma_close) and (price < ma_open)
                
                logging.info(f"  MA Trend: {'UP' if trend_up else 'DOWN' if trend_down else 'FLAT'}")
                logging.info(f"  Price Position: {'Above Both MAs' if price_above_both else 'Below Both MAs' if price_below_both else 'Between MAs'}")
                
                if USE_CMF_FILTER and cmf is not None:
                    cmf_direction = "Bullish" if cmf > 0 else "Bearish" if cmf < 0 else "Neutral"
                    cmf_strength = "Strong" if abs(cmf) > 0.15 else "Moderate" if abs(cmf) > 0.10 else "Weak"
                    logging.info(f"  CMF: {cmf_direction} | Strength: {cmf_strength}")

        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic - ENTRY ONLY, exits handled by trailing stops"""
    while True:
        await asyncio.sleep(0.1)

        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                continue

            signals = update_trading_signals(symbol)
            
            now = time.time()
            position_size = SYMBOLS[symbol]

            # Handle LONG entry
            if signals["long_entry"]:
                # Check if we already have a LONG position or entered recently (prevent duplicates)
                if st["long_position"] == 0 and (now - st["last_long_entry_time"]) > 60:
                    # Place LONG market order
                    result = await place_market_order(client, symbol, "BUY", position_size, "LONG")
                    if result:
                        st["long_position"] = position_size
                        st["long_signal"] = "LONG"
                        st["last_long_entry_time"] = now
                        
                        # Place trailing stop
                        await place_trailing_stop(client, symbol, "LONG", position_size)
                        save_positions()

            # Handle SHORT entry
            if signals["short_entry"]:
                # Check if we already have a SHORT position or entered recently (prevent duplicates)
                if st["short_position"] == 0 and (now - st["last_short_entry_time"]) > 60:
                    # Place SHORT market order
                    result = await place_market_order(client, symbol, "SELL", position_size, "SHORT")
                    if result:
                        st["short_position"] = position_size
                        st["short_signal"] = "SHORT"
                        st["last_short_entry_time"] = now
                        
                        # Place trailing stop
                        await place_trailing_stop(client, symbol, "SHORT", position_size)
                        save_positions()

async def recover_positions_from_exchange(client: AsyncClient):
    """Recover actual positions from Binance"""
    logging.info("🔍 Checking exchange for existing positions...")
    
    try:
        account_info = await safe_api_call(client.futures_account)
        positions = account_info.get('positions', [])
        
        recovered_count = 0
        for position in positions:
            symbol = position['symbol']
            if symbol not in SYMBOLS:
                continue
            
            position_amt = float(position['positionAmt'])
            position_side = position['positionSide']
            
            if abs(position_amt) > 0.0001:
                recovered_count += 1
                
                if position_side == "LONG":
                    state[symbol]["long_position"] = SYMBOLS[symbol]
                    state[symbol]["long_signal"] = "LONG"
                elif position_side == "SHORT":
                    state[symbol]["short_position"] = SYMBOLS[symbol]
                    state[symbol]["short_signal"] = "SHORT"
                
                entry_price = float(position['entryPrice'])
                unrealized_pnl = float(position['unrealizedProfit'])
                
                logging.info(
                    f"♻️ {symbol} RECOVERED {position_side} position: "
                    f"Amount={position_amt}, Entry={entry_price:.6f}, "
                    f"PNL={unrealized_pnl:.2f} USDT"
                )
        
        if recovered_count > 0:
            logging.info(f"✅ Recovered {recovered_count} active positions")
            save_positions()
        else:
            logging.info("✅ No active positions found")
            
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")
        logging.warning("⚠️ Bot will start with empty positions - verify manually!")

async def enable_hedge_mode(client: AsyncClient):
    """Enable hedge mode (both LONG and SHORT positions simultaneously)"""
    try:
        # Check current position mode
        position_mode = await safe_api_call(client.futures_get_position_mode)
        dual_side = position_mode.get('dualSidePosition', False)
        
        if not dual_side:
            logging.info("🔄 Enabling hedge mode (dual-side positions)...")
            await safe_api_call(client.futures_change_position_mode, dualSidePosition=True)
            logging.info("✅ Hedge mode enabled - can hold LONG and SHORT simultaneously")
        else:
            logging.info("✅ Hedge mode already enabled")
            
    except Exception as e:
        logging.error(f"❌ Failed to enable hedge mode: {e}")
        logging.warning("⚠️ Bot may not function correctly without hedge mode!")

async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
    logging.info(f"📊 Trailing Stop: {TRAILING_STOP_CALLBACK}% callback rate")
    
    # Enable hedge mode first
    await enable_hedge_mode(client)
    
    if ENTRY_MA_TYPE == "KAMA":
        if KAMA_USE_JMA_SOURCE:
            logging.info(f"📊 Entry MA: KAMA(ER={KAMA_ER_PERIOD}, Fast={KAMA_FAST}, Slow={KAMA_SLOW}) with JMA({KAMA_JMA_SOURCE_LENGTH}) source")
        else:
            logging.info(f"📊 Entry MA: KAMA(ER={KAMA_ER_PERIOD}, Fast={KAMA_FAST}, Slow={KAMA_SLOW})")
    else:
        logging.info(f"📊 Entry MA: JMA(length={JMA_LENGTH_CLOSE}, phase={JMA_PHASE}, power={JMA_POWER})")
    
    if EXIT_MA_TYPE == "KAMA":
        if KAMA_USE_JMA_SOURCE:
            logging.info(f"📊 Trend MA: KAMA(ER={KAMA_ER_PERIOD}, Fast={KAMA_FAST}, Slow={KAMA_SLOW}) with JMA({KAMA_JMA_SOURCE_LENGTH}) source")
        else:
            logging.info(f"📊 Trend MA: KAMA(ER={KAMA_ER_PERIOD}, Fast={KAMA_FAST}, Slow={KAMA_SLOW})")
    else:
        logging.info(f"📊 Trend MA: JMA(length={JMA_LENGTH_OPEN}, phase={JMA_PHASE}, power={JMA_POWER})")
    
    if USE_HEIKIN_ASHI:
        logging.info("📊 Heikin Ashi: ENABLED")
    else:
        logging.info("📊 Heikin Ashi: DISABLED")
    if USE_DI:
        logging.info(f"📊 DMI: {DI_PERIODS} periods")
    else:
        logging.info("📊 DMI: DISABLED")
    
    if USE_ER_FILTER:
        logging.info(f"📊 ER Filter: Threshold={ER_THRESHOLD}")
    else:
        logging.info("📊 ER Filter: DISABLED")
    
    if USE_CMF_FILTER:
        cmf_direction = "ENABLED (directional)" if USE_CMF_DIRECTION else "DISABLED (strength only)"
        logging.info(f"📊 CMF Filter: Period={CMF_PERIOD} | Threshold={CMF_THRESHOLD} | Direction={cmf_direction}")
    else:
        logging.info("📊 CMF Filter: DISABLED")
    
    logging.info(f"📊 Entry Strategy: {ENTRY_STRATEGY}")
    logging.info(f"📊 Exit Strategy: Trailing Stop (Binance-managed)")

    load_klines()
    load_positions()
    
    await recover_positions_from_exchange(client)

    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        entry_ma_ready = len(klines) >= ENTRY_MA_PERIOD and calculate_entry_ma(symbol, "close") is not None
        exit_ma_ready = len(klines) >= EXIT_MA_PERIOD and calculate_exit_ma(symbol, "close") is not None
        if USE_DI:
            calculate_di(symbol)
        if USE_CMF_FILTER:
            calculate_cmf(symbol, CMF_PERIOD)
        di_ready = (not USE_DI) or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)
        cmf_ready = (not USE_CMF_FILTER) or (state[symbol]["cmf"] is not None)

        if entry_ma_ready and exit_ma_ready and di_ready and cmf_ready:
            state[symbol]["ready"] = True
            logging.info(f"✅ {symbol} ready from loaded data")
        else:
            symbols_needing_data.append(symbol)

    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")

                needed_candles = max(MA_PERIODS + 100, (DI_PERIODS + 100 if USE_DI else 100), (CMF_PERIOD + 100 if USE_CMF_FILTER else 100))
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
                        "close": float(kline[4]),
                        "volume": float(kline[5])
                    })

                jma_close_ok = calculate_entry_ma(symbol, "close") is not None
                exit_ma_ok = calculate_exit_ma(symbol, "close") is not None
                if USE_DI:
                    calculate_di(symbol)
                if USE_CMF_FILTER:
                    calculate_cmf(symbol, CMF_PERIOD)
                di_ok = (not USE_DI) or (st["plus_di"] is not None and st["minus_di"] is not None)
                cmf_ok = (not USE_CMF_FILTER) or (st["cmf"] is not None)

                if jma_close_ok and exit_ma_ok and di_ok and cmf_ok:
                    st["ready"] = True
                    logging.info(f"✅ {symbol} ready from API")

                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(150)

            except Exception as e:
                logging.error(f"❌ {symbol} fetch failed: {e}")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(150)

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

        logging.info("🚀 Bot started - ENTRY ONLY MODE (exits via trailing stops)")

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
    print("SIMPLIFIED DUAL MA STRATEGY - HEDGE MODE")
    print(f"TIMEFRAME: {BASE_TIMEFRAME} | TRAILING STOP: {TRAILING_STOP_CALLBACK}%")
    print("=" * 80)
    print("🎯 STRATEGY: Entry-only signals | Binance trailing stops handle exits")
    print("🔀 HEDGE MODE: Can hold LONG and SHORT positions simultaneously")
    print("=" * 80)
    
    if ENTRY_MA_TYPE == "KAMA":
        kama_mode = f" [JMA({KAMA_JMA_SOURCE_LENGTH})→KAMA]" if KAMA_USE_JMA_SOURCE else ""
        print(f"Entry MA: KAMA (ER={KAMA_ER_PERIOD}, Fast={KAMA_FAST}, Slow={KAMA_SLOW}){kama_mode}")
    else:
        print(f"Entry MA: JMA (Length={JMA_LENGTH_CLOSE}, Phase={JMA_PHASE}, Power={JMA_POWER})")
    
    if EXIT_MA_TYPE == "KAMA":
        kama_mode = f" [JMA({KAMA_JMA_SOURCE_LENGTH})→KAMA]" if KAMA_USE_JMA_SOURCE else ""
        print(f"Trend MA: KAMA (ER={KAMA_ER_PERIOD}, Fast={KAMA_FAST}, Slow={KAMA_SLOW}){kama_mode}")
    else:
        print(f"Trend MA: JMA (Length={JMA_LENGTH_OPEN}, Phase={JMA_PHASE}, Power={JMA_POWER})")
    
    print(f"Heikin Ashi: {'ENABLED' if USE_HEIKIN_ASHI else 'DISABLED'}")
    print(f"DMI: {'ENABLED (' + str(DI_PERIODS) + ' periods)' if USE_DI else 'DISABLED'}")
    
    if USE_ER_FILTER:
        print(f"ER Filter: Threshold={ER_THRESHOLD}")
    else:
        print("ER Filter: DISABLED")
    
    if USE_CMF_FILTER:
        cmf_direction = "ENABLED (directional)" if USE_CMF_DIRECTION else "DISABLED (strength only)"
        print(f"CMF Filter: Period={CMF_PERIOD} | Threshold={CMF_THRESHOLD} | Direction={cmf_direction}")
    else:
        print("CMF Filter: DISABLED")
    
    print(f"Entry Strategy: {ENTRY_STRATEGY}")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print("=" * 80)

    asyncio.run(main())
