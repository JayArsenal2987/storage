#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time, math
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
DI_PERIODS = int(os.getenv("DI_PERIODS", "10"))
USE_DI = False  # Toggle DMI indicator
USE_HEIKIN_ASHI = False  # Toggle Heikin Ashi candles

# Timeframe configuration
BASE_TIMEFRAME = "1h"  # Options: "1m", "5m", "1h"

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
elif BASE_TIMEFRAME == "5m":
    BASE_MINUTES = 5
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

# JMA parameters - FULL IMPLEMENTATION
JMA_LENGTH_CLOSE = 10      # JMA period for close
JMA_LENGTH_OPEN = 30       # JMA period for open
JMA_PHASE = 50             # -100 to 100, controls lag vs overshoot
JMA_AVG_LEN = 65           # Fixed at 65 for volatility averaging

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
MA_PERIODS = max(JMA_LENGTH_CLOSE, JMA_LENGTH_OPEN)
KLINE_LIMIT = max(DI_PERIODS + 100 if USE_DI else 100, MA_PERIODS + JMA_AVG_LEN + 100)

# ENTRY STRATEGY TOGGLE
ENTRY_STRATEGY = "CROSSOVER"  # or "SYMMETRIC"

# EXIT STRATEGY TOGGLE
EXIT_STRATEGY = "TRAILING_STOP"  # or "CROSSOVER" or "SYMMETRIC" or "TRAILING_STOP"

# TRAILING STOP PERCENT (used for BOTH client-side and exchange-side)
TRAILING_STOP_PERCENT = 0.5   # Trailing stop percentage (0.5%)

# EXCHANGE-SIDE TRAILING STOP (shows in Binance open orders)
USE_EXCHANGE_TRAILING_STOP = True  # Set to False for client-side trailing stops

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "ma_close": None,
        "ma_open": None,
        "prev_ma_close": None,
        "prev_ma_open": None,
        "plus_di": None,
        "minus_di": None,
        "di_ready": False,
        "ready": False,
        "ha_prev_close": None,
        "ha_prev_open": None,
        
        # LONG position tracking
        "long_active": False,
        "long_position": 0.0,
        "long_entry_price": None,
        "long_trailing_peak": None,
        "long_trailing_stop_order_id": None,
        "long_last_entry_ts": 0.0,
        
        # SHORT position tracking
        "short_active": False,
        "short_position": 0.0,
        "short_entry_price": None,
        "short_trailing_peak": None,
        "short_trailing_stop_order_id": None,
        "short_last_entry_ts": 0.0,
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
            "long_active": state[sym]["long_active"],
            "long_position": state[sym]["long_position"],
            "long_entry_price": state[sym]["long_entry_price"],
            "long_trailing_peak": state[sym]["long_trailing_peak"],
            "long_trailing_stop_order_id": state[sym]["long_trailing_stop_order_id"],
            "short_active": state[sym]["short_active"],
            "short_position": state[sym]["short_position"],
            "short_entry_price": state[sym]["short_entry_price"],
            "short_trailing_peak": state[sym]["short_trailing_peak"],
            "short_trailing_stop_order_id": state[sym]["short_trailing_stop_order_id"],
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
                state[sym]["long_active"] = position_data[sym].get("long_active", False)
                state[sym]["long_position"] = position_data[sym].get("long_position", 0.0)
                state[sym]["long_entry_price"] = position_data[sym].get("long_entry_price")
                state[sym]["long_trailing_peak"] = position_data[sym].get("long_trailing_peak")
                state[sym]["long_trailing_stop_order_id"] = position_data[sym].get("long_trailing_stop_order_id")
                state[sym]["short_active"] = position_data[sym].get("short_active", False)
                state[sym]["short_position"] = position_data[sym].get("short_position", 0.0)
                state[sym]["short_entry_price"] = position_data[sym].get("short_entry_price")
                state[sym]["short_trailing_peak"] = position_data[sym].get("short_trailing_peak")
                state[sym]["short_trailing_stop_order_id"] = position_data[sym].get("short_trailing_stop_order_id")
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
            "close": ha_close
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

# ========================= EXCHANGE-SIDE TRAILING STOP FUNCTIONS =========================
async def place_trailing_stop_order(client: AsyncClient, symbol: str, position_side: str, quantity: float, callback_rate: float):
    """
    Place an exchange-side TRAILING_STOP_MARKET order
    
    Args:
        symbol: Trading symbol
        position_side: "LONG" or "SHORT"
        quantity: Position size (absolute value)
        callback_rate: Callback rate as percentage (e.g., 0.5 for 0.5%)
    
    Returns:
        Order ID if successful, None otherwise
    """
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            logging.error(f"{symbol} Cannot place trailing stop with 0 quantity")
            return None

        # For LONG positions, we need a SELL stop
        # For SHORT positions, we need a BUY stop
        if position_side == "LONG":
            side = "SELL"
        elif position_side == "SHORT":
            side = "BUY"
        else:
            logging.error(f"Invalid position side: {position_side}")
            return None

        params = {
            "symbol": symbol,
            "side": side,
            "type": "TRAILING_STOP_MARKET",
            "quantity": quantity,
            "callbackRate": callback_rate,  # Binance expects percentage (0.5 = 0.5%)
            "positionSide": position_side,
            "reduceOnly": True  # Important: only reduce position, don't reverse
        }

        result = await safe_api_call(client.futures_create_order, **params)
        order_id = result.get('orderId')
        
        logging.info(
            f"🎯 {symbol} EXCHANGE TRAILING STOP placed: {position_side} "
            f"quantity={quantity}, callback={callback_rate}%, OrderID={order_id}"
        )
        
        return order_id

    except Exception as e:
        logging.error(f"❌ {symbol} TRAILING STOP placement failed: {e}")
        return None

async def cancel_trailing_stop_order(client: AsyncClient, symbol: str, order_id: int):
    """Cancel an existing trailing stop order"""
    if order_id is None:
        return True
    
    try:
        await safe_api_call(
            client.futures_cancel_order,
            symbol=symbol,
            orderId=order_id
        )
        logging.info(f"🗑️ {symbol} Cancelled trailing stop order {order_id}")
        return True
    except Exception as e:
        # Order might already be filled or cancelled
        if "Unknown order" in str(e) or "-2011" in str(e):
            logging.info(f"ℹ️ {symbol} Trailing stop order {order_id} already gone (likely filled)")
            return True
        else:
            logging.error(f"❌ {symbol} Failed to cancel trailing stop {order_id}: {e}")
            return False

async def check_open_orders(client: AsyncClient, symbol: str):
    """Check for existing open orders (useful for recovery)"""
    try:
        open_orders = await safe_api_call(client.futures_get_open_orders, symbol=symbol)
        
        trailing_stops = {
            "LONG": None,
            "SHORT": None
        }
        
        for order in open_orders:
            if order['type'] == 'TRAILING_STOP_MARKET':
                position_side = order['positionSide']
                if position_side in trailing_stops:
                    trailing_stops[position_side] = {
                        "order_id": order['orderId'],
                        "quantity": float(order['origQty']),
                        "callback_rate": float(order.get('priceRate', 0))
                    }
        
        return trailing_stops
    except Exception as e:
        logging.error(f"❌ {symbol} Failed to check open orders: {e}")
        return {"LONG": None, "SHORT": None}

# ========================= FULL JMA IMPLEMENTATION =========================
def calculate_jma(symbol: str, field: str, length: int, phase: int = 50, avg_len: int = 65) -> Optional[float]:
    """
    Full Jurik Moving Average (JMA) with volatility-based adaptation
    
    This is the complete implementation including:
    - Dynamic volatility calculation via Jurik Bands
    - Adaptive power based on relative volatility
    - Three-stage adaptive filtering
    
    Args:
        symbol: Trading symbol
        field: Price field ('close', 'open', 'high', 'low')
        length: Base length for moving average
        phase: Controls responsiveness vs smoothness (-100 to +100)
        avg_len: Fixed at 65 for volatility averaging
    """
    
    klines = state[symbol]["klines"]
    
    # Need enough data for volatility calculations
    if len(klines) < max(length + avg_len + 10, 100):
        return None
    
    completed = list(klines)[:-1]
    if len(completed) < max(length + avg_len + 10, 100):
        return None
    
    # Apply Heikin Ashi if enabled
    if USE_HEIKIN_ASHI:
        completed = convert_to_heikin_ashi(completed, symbol)
    
    # Extract price values
    values = [k.get(field) for k in completed]
    if None in values:
        return None
    
    # Initialize JMA state variables if not exists
    jma_key = f"jma_{field}_{length}"
    if jma_key not in state[symbol]:
        state[symbol][jma_key] = {
            "upper_band": None,
            "lower_band": None,
            "ma1": None,
            "det0": None,
            "det1": None,
            "jma": None,
            "volty_history": deque(maxlen=avg_len + 10),
            "vsum_history": deque(maxlen=avg_len),
        }
    
    jma_state = state[symbol][jma_key]
    
    # ==================== PRELIMINARY CALCULATIONS ====================
    
    # 1. Additional periodic factor (len1)
    len1 = math.log(math.sqrt(length)) / math.log(2.0) + 2.0
    if len1 < 0:
        len1 = 0.0
    
    # 2. Power of relative volatility (pow1)
    pow1 = len1 - 2.0
    if pow1 < 0.5:
        pow1 = 0.5
    
    # 3. Phase ratio (PR)
    if phase < -100:
        phase_ratio = 0.5
    elif phase > 100:
        phase_ratio = 2.5
    else:
        phase_ratio = phase / 100.0 + 1.5
    
    # 4. Beta (periodic ratio)
    beta = 0.45 * (length - 1.0) / (0.45 * (length - 1.0) + 2.0)
    
    # Initialize on first run
    if jma_state["upper_band"] is None:
        jma_state["upper_band"] = values[0]
        jma_state["lower_band"] = values[0]
        jma_state["ma1"] = values[0]
        jma_state["det0"] = 0.0
        jma_state["det1"] = 0.0
        jma_state["jma"] = values[0]
    
    # Process each price value
    for price in values:
        
        # ==================== VOLATILITY CALCULATION ====================
        
        # Calculate differences from Jurik Bands
        del1 = price - jma_state["upper_band"]
        del2 = price - jma_state["lower_band"]
        
        # Calculate volatility
        abs_del1 = abs(del1)
        abs_del2 = abs(del2)
        
        if abs_del1 == abs_del2:
            volty = 0.0
        else:
            volty = max(abs_del1, abs_del2)
        
        # Calculate Kv for band adaptation
        kv = beta ** math.sqrt(pow1)
        
        # Update Jurik Bands
        if del1 > 0:
            jma_state["upper_band"] = price
        else:
            jma_state["upper_band"] = price - kv * del1
        
        if del2 < 0:
            jma_state["lower_band"] = price
        else:
            jma_state["lower_band"] = price - kv * del2
        
        # Store volatility
        jma_state["volty_history"].append(volty)
        
        # Calculate vSum (incremental sum for volatility)
        if len(jma_state["volty_history"]) >= 11:
            volty_10_ago = jma_state["volty_history"][-11]
            vsum = (volty - volty_10_ago) / 10.0
        else:
            vsum = volty / 10.0
        
        jma_state["vsum_history"].append(vsum)
        
        # ==================== AVERAGE VOLATILITY ====================
        
        # Calculate average volatility over avg_len periods
        if len(jma_state["vsum_history"]) >= avg_len:
            avg_volty = sum(jma_state["vsum_history"]) / avg_len
        elif len(jma_state["vsum_history"]) > 0:
            avg_volty = sum(jma_state["vsum_history"]) / len(jma_state["vsum_history"])
        else:
            avg_volty = 1.0
        
        # Prevent division by zero
        if avg_volty == 0:
            avg_volty = 1.0
        
        # ==================== RELATIVE VOLATILITY & DYNAMIC POWER ====================
        
        # Calculate relative price volatility
        r_volty = volty / avg_volty
        
        # Apply limits to rVolty
        max_r_volty = len1 ** (1.0 / pow1)
        if r_volty > max_r_volty:
            r_volty = max_r_volty
        if r_volty < 1.0:
            r_volty = 1.0
        
        # Calculate dynamic power (KEY DIFFERENCE from simplified JMA)
        pow_dynamic = r_volty ** pow1
        
        # Calculate dynamic alpha based on volatility
        alpha = beta ** pow_dynamic
        
        # ==================== THREE-STAGE SMOOTHING ====================
        
        # 1st stage - Preliminary smoothing by adaptive EMA
        jma_state["ma1"] = (1.0 - alpha) * price + alpha * jma_state["ma1"]
        
        # 2nd stage - Preliminary smoothing by Kalman filter
        jma_state["det0"] = (price - jma_state["ma1"]) * (1.0 - beta) + beta * jma_state["det0"]
        ma2 = jma_state["ma1"] + phase_ratio * jma_state["det0"]
        
        # 3rd stage - Final smoothing by unique Jurik adaptive filter
        jma_state["det1"] = (ma2 - jma_state["jma"]) * ((1.0 - alpha) ** 2) + (alpha ** 2) * jma_state["det1"]
        jma_state["jma"] = jma_state["jma"] + jma_state["det1"]
    
    return jma_state["jma"]

# ========================= DMI INDICATOR =========================
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
    state[symbol]["di_ready"] = True
    return None

# ========================= TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> dict:
    """
    Trading signals with independent LONG and SHORT positions (NO FLIPPING)
    Returns dict with separate signals for LONG and SHORT sides
    """
    st = state[symbol]
    price = st["price"]

    if price is None or not st["ready"]:
        return {"long_action": "NONE", "short_action": "NONE"}

    ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_AVG_LEN)
    ma_open = calculate_jma(symbol, "open", JMA_LENGTH_OPEN, JMA_PHASE, JMA_AVG_LEN)
    if USE_DI:
        calculate_di(symbol)

    if ma_close is None or ma_open is None or (USE_DI and (st["plus_di"] is None or st["minus_di"] is None)):
        return {"long_action": "NONE", "short_action": "NONE"}

    prev_close = st["prev_ma_close"]
    prev_open = st["prev_ma_open"]

    if prev_close is None or prev_open is None:
        st["prev_ma_close"] = ma_close
        st["prev_ma_open"] = ma_open
        return {"long_action": "NONE", "short_action": "NONE"}
    
    # Market conditions
    cross_up = (ma_close > ma_open) and (prev_close <= prev_open)
    cross_down = (ma_close < ma_open) and (prev_close >= prev_open)
    
    price_above_both = (price > ma_close) and (price > ma_open)
    price_below_both = (price < ma_close) and (price < ma_open)
    
    crossover_aligned_long = (price > ma_close) and (ma_close > ma_open)
    crossover_aligned_short = (price < ma_close) and (ma_close < ma_open)
    
    ma_trend_bearish = ma_close < ma_open
    ma_trend_bullish = ma_close > ma_open

    plus_di = st["plus_di"]
    minus_di = st["minus_di"]
    di_bull = plus_di > minus_di if USE_DI else True
    di_bear = minus_di > plus_di if USE_DI else True

    long_action = "NONE"
    short_action = "NONE"

    # ==================== LONG POSITION LOGIC ====================
    if st["long_active"]:
        # EXIT CONDITIONS for LONG
        if EXIT_STRATEGY == "TRAILING_STOP" and not USE_EXCHANGE_TRAILING_STOP:
            # Client-side trailing stop for LONG
            trailing_pct = TRAILING_STOP_PERCENT / 100.0
            if st["long_trailing_peak"] is not None:
                if price > st["long_trailing_peak"]:
                    st["long_trailing_peak"] = price
                stop_level = st["long_trailing_peak"] * (1 - trailing_pct)
                if price <= stop_level:
                    long_action = "EXIT"
                    logging.info(f"🔴 {symbol} TRAILING EXIT LONG: price {price:.6f} <= {stop_level:.6f} ({TRAILING_STOP_PERCENT}% from peak {st['long_trailing_peak']:.6f})")
        
        elif EXIT_STRATEGY == "SYMMETRIC":
            if price_below_both and (di_bear if USE_DI else True):
                long_action = "EXIT"
                logging.info(f"🔴 {symbol} EXIT LONG (SYMMETRIC: price {price:.6f} fell below BOTH MAs: close={ma_close:.6f}, open={ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
        
        elif EXIT_STRATEGY == "CROSSOVER":
            if ma_trend_bearish and (di_bear if USE_DI else True):
                long_action = "EXIT"
                logging.info(f"🔴 {symbol} EXIT LONG (CROSSOVER: JMA trend reversed - close_JMA {ma_close:.6f} < open_JMA {ma_open:.6f}, price={price:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
    
    else:
        # ENTRY CONDITIONS for LONG
        if ENTRY_STRATEGY == "CROSSOVER":
            if crossover_aligned_long and (di_bull if USE_DI else True):
                long_action = "ENTRY"
                logging.info(f"🟢 {symbol} ENTRY LONG (CROSSOVER: price {price:.6f} > close_JMA {ma_close:.6f} > open_JMA {ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
        
        elif ENTRY_STRATEGY == "SYMMETRIC":
            if price_above_both and (di_bull if USE_DI else True):
                long_action = "ENTRY"
                logging.info(f"🟢 {symbol} ENTRY LONG (SYMMETRIC: price {price:.6f} > both MAs: close={ma_close:.6f}, open={ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")

    # ==================== SHORT POSITION LOGIC ====================
    if st["short_active"]:
        # EXIT CONDITIONS for SHORT
        if EXIT_STRATEGY == "TRAILING_STOP" and not USE_EXCHANGE_TRAILING_STOP:
            # Client-side trailing stop for SHORT
            trailing_pct = TRAILING_STOP_PERCENT / 100.0
            if st["short_trailing_peak"] is not None:
                if price < st["short_trailing_peak"]:
                    st["short_trailing_peak"] = price
                stop_level = st["short_trailing_peak"] * (1 + trailing_pct)
                if price >= stop_level:
                    short_action = "EXIT"
                    logging.info(f"🔴 {symbol} TRAILING EXIT SHORT: price {price:.6f} >= {stop_level:.6f} ({TRAILING_STOP_PERCENT}% from peak {st['short_trailing_peak']:.6f})")
        
        elif EXIT_STRATEGY == "SYMMETRIC":
            if price_above_both and (di_bull if USE_DI else True):
                short_action = "EXIT"
                logging.info(f"🔴 {symbol} EXIT SHORT (SYMMETRIC: price {price:.6f} rose above BOTH MAs: close={ma_close:.6f}, open={ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
        
        elif EXIT_STRATEGY == "CROSSOVER":
            if ma_trend_bullish and (di_bull if USE_DI else True):
                short_action = "EXIT"
                logging.info(f"🔴 {symbol} EXIT SHORT (CROSSOVER: JMA trend reversed - close_JMA {ma_close:.6f} > open_JMA {ma_open:.6f}, price={price:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
    
    else:
        # ENTRY CONDITIONS for SHORT
        if ENTRY_STRATEGY == "CROSSOVER":
            if crossover_aligned_short and (di_bear if USE_DI else True):
                short_action = "ENTRY"
                logging.info(f"🟢 {symbol} ENTRY SHORT (CROSSOVER: price {price:.6f} < close_JMA {ma_close:.6f} < open_JMA {ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
        
        elif ENTRY_STRATEGY == "SYMMETRIC":
            if price_below_both and (di_bear if USE_DI else True):
                short_action = "ENTRY"
                logging.info(f"🟢 {symbol} ENTRY SHORT (SYMMETRIC: price {price:.6f} < both MAs: close={ma_close:.6f}, open={ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")

    st["prev_ma_close"] = ma_close
    st["prev_ma_open"] = ma_open

    # Update client-side trailing peaks
    if not USE_EXCHANGE_TRAILING_STOP and EXIT_STRATEGY == "TRAILING_STOP":
        if st["long_active"] and st["long_trailing_peak"] is not None and price > st["long_trailing_peak"]:
            st["long_trailing_peak"] = price
        if st["short_active"] and st["short_trailing_peak"] is not None and price < st["short_trailing_peak"]:
            st["short_trailing_peak"] = price

    return {"long_action": long_action, "short_action": short_action}

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

                                if len(state[symbol]["klines"]) >= MA_PERIODS + JMA_AVG_LEN and not state[symbol]["ready"]:
                                    ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_AVG_LEN)
                                    ma_open = calculate_jma(symbol, "open", JMA_LENGTH_OPEN, JMA_PHASE, JMA_AVG_LEN)
                                    if USE_DI:
                                        calculate_di(symbol)
                                    if (ma_close is not None) and (ma_open is not None) and (not USE_DI or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)):
                                        state[symbol]["ready"] = True
                                        ha_status = " (Heikin Ashi enabled)" if USE_HEIKIN_ASHI else ""
                                        log_msg = f"✅ {symbol} ready for trading ({len(state[symbol]['klines'])} {BASE_TIMEFRAME} candles, close_JMA {ma_close:.6f}, open_JMA {ma_open:.6f}){ha_status}"
                                        logging.info(log_msg)
                                else:
                                    if USE_DI:
                                        calculate_di(symbol)

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
                
                ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_AVG_LEN)
                ma_open = calculate_jma(symbol, "open", JMA_LENGTH_OPEN, JMA_PHASE, JMA_AVG_LEN)
                if USE_DI:
                    calculate_di(symbol)
                
                reasons = []
                if candle_count < MA_PERIODS + JMA_AVG_LEN + 1:
                    needed = (MA_PERIODS + JMA_AVG_LEN + 1) - candle_count
                    reasons.append(f"need {needed} more {BASE_TIMEFRAME} candles")
                if ma_close is None:
                    reasons.append("JMA close not calculated")
                if ma_open is None:
                    reasons.append("JMA open not calculated")
                if USE_DI and (st["plus_di"] is None or st["minus_di"] is None):
                    reasons.append("DI not calculated")
                
                reason_str = ", ".join(reasons) if reasons else "unknown"
                price_str = f"Price={price:.6f} | " if price else ""
                logging.info(f"{symbol}: {price_str}Not ready - {candle_count} {BASE_TIMEFRAME} candles - Waiting for: {reason_str}")
                continue

            price = st["price"]
            ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_AVG_LEN)
            ma_open = calculate_jma(symbol, "open", JMA_LENGTH_OPEN, JMA_PHASE, JMA_AVG_LEN)
            plus_di = st.get("plus_di")
            minus_di = st.get("minus_di")

            if price and ma_close and ma_open:
                plus_di_str = f"{plus_di:.4f}" if USE_DI and plus_di is not None else "N/A"
                minus_di_str = f"{minus_di:.4f}" if USE_DI and minus_di is not None else "N/A"

                logging.info(f"{symbol}: Price={price:.6f} | close_JMA={ma_close:.6f} | open_JMA={ma_open:.6f} | +DI={plus_di_str} | -DI={minus_di_str}")
                
                # Show position status
                positions = []
                if st["long_active"]:
                    positions.append("LONG")
                    if USE_EXCHANGE_TRAILING_STOP and st["long_trailing_stop_order_id"]:
                        logging.info(f"  LONG: Active | Exchange Trailing Stop #{st['long_trailing_stop_order_id']}")
                    elif not USE_EXCHANGE_TRAILING_STOP and st["long_trailing_peak"]:
                        logging.info(f"  LONG: Active | Client Trailing Stop Peak={st['long_trailing_peak']:.6f}")
                    else:
                        logging.info(f"  LONG: Active")
                
                if st["short_active"]:
                    positions.append("SHORT")
                    if USE_EXCHANGE_TRAILING_STOP and st["short_trailing_stop_order_id"]:
                        logging.info(f"  SHORT: Active | Exchange Trailing Stop #{st['short_trailing_stop_order_id']}")
                    elif not USE_EXCHANGE_TRAILING_STOP and st["short_trailing_peak"]:
                        logging.info(f"  SHORT: Active | Client Trailing Stop Peak={st['short_trailing_peak']:.6f}")
                    else:
                        logging.info(f"  SHORT: Active")
                
                if not positions:
                    logging.info(f"  Positions: FLAT")

                trend_up = (ma_close > ma_open)
                trend_down = (ma_close < ma_open)
                price_above_both = (price > ma_close) and (price > ma_open)
                price_below_both = (price < ma_close) and (price < ma_open)
                
                logging.info(f"  Current Trend: {'UP' if trend_up else 'DOWN' if trend_down else 'FLAT'}")
                logging.info(f"  Price Position: {'Above Both MAs' if price_above_both else 'Below Both MAs' if price_below_both else 'Between MAs'}")
                if USE_DI and plus_di is not None and minus_di is not None:
                    di_direction = "Bullish" if plus_di > minus_di else "Bearish" if minus_di > plus_di else "Neutral"
                else:
                    di_direction = "N/A"
                logging.info(f"  DI Direction: {di_direction}")

        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic - handles independent LONG and SHORT positions"""
    while True:
        await asyncio.sleep(0.1)

        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                continue

            signal_result = update_trading_signals(symbol)
            
            # Handle LONG position changes
            if signal_result["long_action"] != "NONE":
                await handle_long_position(client, symbol, signal_result["long_action"])
            
            # Handle SHORT position changes
            if signal_result["short_action"] != "NONE":
                await handle_short_position(client, symbol, signal_result["short_action"])

async def handle_long_position(client: AsyncClient, symbol: str, action: str):
    """Handle LONG position entry/exit"""
    st = state[symbol]
    now = time.time()
    
    # Prevent duplicate entries within 2 seconds
    if action == "ENTRY" and (now - st["long_last_entry_ts"]) < 2.0:
        logging.info(f"🛡️ {symbol} LONG dedup: skipping duplicate entry")
        return
    
    try:
        if action == "ENTRY":
            target_size = SYMBOLS[symbol]
            
            # Cancel any existing trailing stop first
            if USE_EXCHANGE_TRAILING_STOP and st["long_trailing_stop_order_id"]:
                await cancel_trailing_stop_order(client, symbol, st["long_trailing_stop_order_id"])
                st["long_trailing_stop_order_id"] = None
            
            # Enter LONG position
            ok = await place_order(client, symbol, "BUY", target_size, "LONG ENTRY")
            if not ok:
                return
            
            st["long_active"] = True
            st["long_position"] = target_size
            st["long_entry_price"] = st["price"]
            st["long_last_entry_ts"] = now
            
            # Place trailing stop
            if USE_EXCHANGE_TRAILING_STOP and EXIT_STRATEGY == "TRAILING_STOP":
                await asyncio.sleep(1)
                order_id = await place_trailing_stop_order(client, symbol, "LONG", target_size, TRAILING_STOP_PERCENT)
                if order_id:
                    st["long_trailing_stop_order_id"] = order_id
                else:
                    logging.warning(f"⚠️ {symbol} LONG entered but trailing stop failed!")
            elif not USE_EXCHANGE_TRAILING_STOP and EXIT_STRATEGY == "TRAILING_STOP":
                st["long_trailing_peak"] = st["price"]
            
            save_positions()
        
        elif action == "EXIT":
            # Cancel trailing stop
            if USE_EXCHANGE_TRAILING_STOP and st["long_trailing_stop_order_id"]:
                await cancel_trailing_stop_order(client, symbol, st["long_trailing_stop_order_id"])
                st["long_trailing_stop_order_id"] = None
            
            # Close LONG position
            ok = await place_order(client, symbol, "SELL", st["long_position"], "LONG CLOSE")
            if not ok:
                return
            
            st["long_active"] = False
            st["long_position"] = 0.0
            st["long_entry_price"] = None
            st["long_trailing_peak"] = None
            
            save_positions()
    
    except Exception as e:
        logging.error(f"❌ {symbol} LONG position change failed: {e}")

async def handle_short_position(client: AsyncClient, symbol: str, action: str):
    """Handle SHORT position entry/exit"""
    st = state[symbol]
    now = time.time()
    
    # Prevent duplicate entries within 2 seconds
    if action == "ENTRY" and (now - st["short_last_entry_ts"]) < 2.0:
        logging.info(f"🛡️ {symbol} SHORT dedup: skipping duplicate entry")
        return
    
    try:
        if action == "ENTRY":
            target_size = SYMBOLS[symbol]
            
            # Cancel any existing trailing stop first
            if USE_EXCHANGE_TRAILING_STOP and st["short_trailing_stop_order_id"]:
                await cancel_trailing_stop_order(client, symbol, st["short_trailing_stop_order_id"])
                st["short_trailing_stop_order_id"] = None
            
            # Enter SHORT position
            ok = await place_order(client, symbol, "SELL", target_size, "SHORT ENTRY")
            if not ok:
                return
            
            st["short_active"] = True
            st["short_position"] = target_size
            st["short_entry_price"] = st["price"]
            st["short_last_entry_ts"] = now
            
            # Place trailing stop
            if USE_EXCHANGE_TRAILING_STOP and EXIT_STRATEGY == "TRAILING_STOP":
                await asyncio.sleep(1)
                order_id = await place_trailing_stop_order(client, symbol, "SHORT", target_size, TRAILING_STOP_PERCENT)
                if order_id:
                    st["short_trailing_stop_order_id"] = order_id
                else:
                    logging.warning(f"⚠️ {symbol} SHORT entered but trailing stop failed!")
            elif not USE_EXCHANGE_TRAILING_STOP and EXIT_STRATEGY == "TRAILING_STOP":
                st["short_trailing_peak"] = st["price"]
            
            save_positions()
        
        elif action == "EXIT":
            # Cancel trailing stop
            if USE_EXCHANGE_TRAILING_STOP and st["short_trailing_stop_order_id"]:
                await cancel_trailing_stop_order(client, symbol, st["short_trailing_stop_order_id"])
                st["short_trailing_stop_order_id"] = None
            
            # Close SHORT position
            ok = await place_order(client, symbol, "BUY", st["short_position"], "SHORT CLOSE")
            if not ok:
                return
            
            st["short_active"] = False
            st["short_position"] = 0.0
            st["short_entry_price"] = None
            st["short_trailing_peak"] = None
            
            save_positions()
    
    except Exception as e:
        logging.error(f"❌ {symbol} SHORT position change failed: {e}")

async def recover_positions_from_exchange(client: AsyncClient):
    """Recover actual positions from Binance - supports simultaneous LONG/SHORT"""
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
                entry_price = float(position['entryPrice'])
                unrealized_pnl = float(position['unrealizedProfit'])
                
                if position_side == "LONG":
                    state[symbol]["long_active"] = True
                    state[symbol]["long_position"] = SYMBOLS[symbol]
                    state[symbol]["long_entry_price"] = entry_price
                    
                    current_price = state[symbol].get("price")
                    if current_price:
                        state[symbol]["long_trailing_peak"] = current_price
                    else:
                        state[symbol]["long_trailing_peak"] = entry_price
                    
                    logging.info(f"♻️ {symbol} RECOVERED LONG: Amount={position_amt}, Entry={entry_price:.6f}, PNL={unrealized_pnl:.2f} USDT")
                
                elif position_side == "SHORT":
                    state[symbol]["short_active"] = True
                    state[symbol]["short_position"] = SYMBOLS[symbol]
                    state[symbol]["short_entry_price"] = entry_price
                    
                    current_price = state[symbol].get("price")
                    if current_price:
                        state[symbol]["short_trailing_peak"] = current_price
                    else:
                        state[symbol]["short_trailing_peak"] = entry_price
                    
                    logging.info(f"♻️ {symbol} RECOVERED SHORT: Amount={position_amt}, Entry={entry_price:.6f}, PNL={unrealized_pnl:.2f} USDT")
        
        # Check for existing trailing stops
        if USE_EXCHANGE_TRAILING_STOP:
            for symbol in SYMBOLS:
                if state[symbol]["long_active"] or state[symbol]["short_active"]:
                    trailing_stops = await check_open_orders(client, symbol)
                    
                    if trailing_stops["LONG"] and state[symbol]["long_active"]:
                        state[symbol]["long_trailing_stop_order_id"] = trailing_stops["LONG"]["order_id"]
                        logging.info(f"♻️ {symbol} LONG trailing stop recovered: Order #{trailing_stops['LONG']['order_id']}")
                    elif state[symbol]["long_active"]:
                        logging.warning(f"⚠️ {symbol} LONG position exists but NO trailing stop found!")
                    
                    if trailing_stops["SHORT"] and state[symbol]["short_active"]:
                        state[symbol]["short_trailing_stop_order_id"] = trailing_stops["SHORT"]["order_id"]
                        logging.info(f"♻️ {symbol} SHORT trailing stop recovered: Order #{trailing_stops['SHORT']['order_id']}")
                    elif state[symbol]["short_active"]:
                        logging.warning(f"⚠️ {symbol} SHORT position exists but NO trailing stop found!")
        
        if recovered_count > 0:
            logging.info(f"✅ Recovered {recovered_count} active positions")
            save_positions()
        else:
            logging.info("✅ No active positions found")
            
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")
        logging.warning("⚠️ Bot will start with empty positions - verify manually!")

async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
    logging.info(f"📊 FULL JMA: close_length={JMA_LENGTH_CLOSE}, open_length={JMA_LENGTH_OPEN}, phase={JMA_PHASE}, avg_len={JMA_AVG_LEN}")
    logging.info(f"📊 JMA Mode: FULL IMPLEMENTATION with volatility adaptation")
    logging.info(f"📊 Position Mode: INDEPENDENT LONG/SHORT (No Flipping)")
    
    if USE_HEIKIN_ASHI:
        logging.info("📊 Heikin Ashi: ENABLED")
    else:
        logging.info("📊 Heikin Ashi: DISABLED")
    if USE_DI:
        logging.info(f"📊 DMI: {DI_PERIODS} periods")
    else:
        logging.info("📊 DMI: DISABLED")
    logging.info(f"📊 Entry: {ENTRY_STRATEGY} | Exit: {EXIT_STRATEGY}")
    
    if EXIT_STRATEGY == "TRAILING_STOP":
        if USE_EXCHANGE_TRAILING_STOP:
            logging.info(f"📊 Trailing Stop: EXCHANGE-SIDE ({TRAILING_STOP_PERCENT}%) - will show in Binance open orders")
        else:
            logging.info(f"📊 Trailing Stop: CLIENT-SIDE ({TRAILING_STOP_PERCENT}%) - bot managed")

    load_klines()
    load_positions()
    
    # IMPORTANT: Recover positions AFTER loading prices
    await asyncio.sleep(2)  # Give WebSocket time to get current prices
    await recover_positions_from_exchange(client)

    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        jma_close_ready = len(klines) >= JMA_LENGTH_CLOSE + JMA_AVG_LEN and calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_AVG_LEN) is not None
        jma_open_ready = len(klines) >= JMA_LENGTH_OPEN + JMA_AVG_LEN and calculate_jma(symbol, "open", JMA_LENGTH_OPEN, JMA_PHASE, JMA_AVG_LEN) is not None
        if USE_DI:
            calculate_di(symbol)
        di_ready = (not USE_DI) or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)

        if jma_close_ready and jma_open_ready and di_ready:
            state[symbol]["ready"] = True
            logging.info(f"✅ {symbol} ready from loaded data")
        else:
            symbols_needing_data.append(symbol)

    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")

                needed_candles = max(MA_PERIODS + JMA_AVG_LEN + 100, (DI_PERIODS + 100 if USE_DI else 100))
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

                jma_close_ok = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_AVG_LEN) is not None
                jma_open_ok = calculate_jma(symbol, "open", JMA_LENGTH_OPEN, JMA_PHASE, JMA_AVG_LEN) is not None
                if USE_DI:
                    calculate_di(symbol)
                di_ok = (not USE_DI) or (st["plus_di"] is not None and st["minus_di"] is not None)

                if jma_close_ok and jma_open_ok and di_ok:
                    st["ready"] = True
                    logging.info(f"✅ {symbol} ready from API")

                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(60)

            except Exception as e:
                logging.error(f"❌ {symbol} fetch failed: {e}")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(60)

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
        # Start price feed first to get current prices
        price_task = asyncio.create_task(price_feed_loop(client))
        await asyncio.sleep(3)  # Let it connect and get initial prices
        
        # Then initialize bot (which needs current prices for recovery)
        await init_bot(client)

        trade_task = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())

        trailing_mode = "EXCHANGE-SIDE" if USE_EXCHANGE_TRAILING_STOP else "CLIENT-SIDE"
        logging.info(f"🚀 Bot started with independent LONG/SHORT positions ({trailing_mode} trailing stops)")

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
    ha_mode = "HEIKIN ASHI" if USE_HEIKIN_ASHI else "REGULAR CANDLES"
    print(f"FULL JMA OPEN/CLOSE CROSS STRATEGY - {ha_mode}")
    print(f"INDEPENDENT LONG/SHORT POSITIONS (NO FLIPPING)")
    print(f"TIMEFRAME: {BASE_TIMEFRAME}")
    print("=" * 80)
    print(f"FULL JMA Parameters: Close_Length={JMA_LENGTH_CLOSE}, Open_Length={JMA_LENGTH_OPEN}, Phase={JMA_PHASE}, AvgLen={JMA_AVG_LEN}")
    print(f"JMA Mode: FULL IMPLEMENTATION (volatility-adaptive)")
    print(f"Timeframe: {BASE_TIMEFRAME} ({BASE_MINUTES} min)")
    print(f"Heikin Ashi: {'ENABLED' if USE_HEIKIN_ASHI else 'DISABLED'}")
    print(f"DMI: {'ENABLED (' + str(DI_PERIODS) + ' periods)' if USE_DI else 'DISABLED'}")
    print(f"Entry Strategy: {ENTRY_STRATEGY}")
    print(f"Exit Strategy: {EXIT_STRATEGY}")
    if EXIT_STRATEGY == "TRAILING_STOP":
        trailing_type = "EXCHANGE-SIDE (Binance managed)" if USE_EXCHANGE_TRAILING_STOP else "CLIENT-SIDE (Bot managed)"
        print(f"Trailing Stop: {TRAILING_STOP_PERCENT}% - {trailing_type}")
    print(f"Position Mode: INDEPENDENT LONG/SHORT (Both can be active simultaneously)")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print("=" * 80)

    asyncio.run(main())
