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
DI_PERIODS = int(os.getenv("DI_PERIODS", "10"))
USE_DI = False  # Toggle DMI indicator
USE_HEIKIN_ASHI = False  # Toggle Heikin Ashi candles

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

# JMA parameters
JMA_LENGTH_CLOSE = 12      # JMA period for close
JMA_PHASE = 50             # -100 to 100, controls lag vs overshoot (default 50)
JMA_POWER = 3              # Smoothness level, 1-3 (default 2)

# HMA parameters (applied to JMA output)
HMA_LENGTH_OPEN = 13       # HMA period applied to JMA close values

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
MA_PERIODS = max(JMA_LENGTH_CLOSE, HMA_LENGTH_OPEN) + 50
KLINE_LIMIT = max(DI_PERIODS + 100 if USE_DI else 100, MA_PERIODS + 100)

# ENTRY STRATEGY TOGGLE
ENTRY_STRATEGY = "SYMMETRIC"  # or "CROSSOVER"

# EXIT STRATEGY TOGGLE
EXIT_STRATEGY = "CROSSOVER"  # or "SYMMETRIC"

# DAILY PNL LIMITS (Percentage-based, cumulative for the day)
DAILY_PROFIT_LIMIT_PERCENT = 200.0   # Stop trading if daily profit reaches +200%
DAILY_LOSS_LIMIT_PERCENT = 100.0     # Stop trading if daily loss reaches -100%
PAUSE_DURATION_HOURS = 24            # Pause trading for 24 hours after limit hit

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "current_signal": None,
        "last_signal_change": 0,
        "current_position": 0.0,
        "ma_close": None,
        "ma_open": None,
        "prev_ma_close": None,
        "prev_ma_open": None,
        "plus_di": None,
        "minus_di": None,
        "di_ready": False,
        "ready": False,
        "last_exec_ts": 0.0,
        "last_target": None,
        "ha_prev_close": None,
        "ha_prev_open": None,
        "entry_price": None,
        "daily_pnl_percent": 0.0,
        "daily_reset_time": 0,
        "paused_until": 0,
        "position_size_usdt": 0.0,
        "jma_close_history": deque(maxlen=200),
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
            "current_signal": state[sym]["current_signal"],
            "current_position": state[sym]["current_position"],
            "last_signal_change": state[sym]["last_signal_change"],
            "entry_price": state[sym]["entry_price"],
            "daily_pnl_percent": state[sym]["daily_pnl_percent"],
            "daily_reset_time": state[sym]["daily_reset_time"],
            "paused_until": state[sym]["paused_until"],
            "position_size_usdt": state[sym]["position_size_usdt"],
            "jma_close_history": list(state[sym]["jma_close_history"])
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
                state[sym]["current_signal"] = position_data[sym].get("current_signal")
                state[sym]["current_position"] = position_data[sym].get("current_position", 0.0)
                state[sym]["last_signal_change"] = position_data[sym].get("last_signal_change", 0)
                state[sym]["entry_price"] = position_data[sym].get("entry_price")
                state[sym]["daily_pnl_percent"] = position_data[sym].get("daily_pnl_percent", 0.0)
                state[sym]["daily_reset_time"] = position_data[sym].get("daily_reset_time", 0)
                state[sym]["paused_until"] = position_data[sym].get("paused_until", 0)
                state[sym]["position_size_usdt"] = position_data[sym].get("position_size_usdt", 0.0)
                jma_history = position_data[sym].get("jma_close_history", [])
                state[sym]["jma_close_history"] = deque(jma_history, maxlen=200)
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

# ========================= INDICATOR CALCULATIONS =========================
def calculate_jma(symbol: str, field: str, length: int, phase: int = 50, power: int = 2) -> Optional[float]:
    """
    Jurik Moving Average (JMA) calculation
    Copyright (c) 2007-present Jurik Research and Consulting
    Converted from Pine Script to Python
    """
    klines = state[symbol]["klines"]

    if len(klines) < length + 1:
        return None

    completed = list(klines)[:-1]
    if len(completed) < length:
        return None

    # Apply Heikin Ashi if enabled
    if USE_HEIKIN_ASHI:
        completed = convert_to_heikin_ashi(completed, symbol)

    values = [k.get(field, None) for k in completed]
    if None in values:
        return None

    # JMA parameters
    phaseRatio = 0.5 if phase < -100 else (2.5 if phase > 100 else phase / 100 + 1.5)
    beta = 0.45 * (length - 1) / (0.45 * (length - 1) + 2)
    alpha = beta ** power

    # Initialize state variables
    e0 = 0.0
    e1 = 0.0
    e2 = 0.0
    jma = 0.0

    # Calculate JMA for all values in sequence
    for src in values:
        e0 = (1 - alpha) * src + alpha * e0
        e1 = (src - e0) * (1 - beta) + beta * e1
        e2 = (e0 + phaseRatio * e1 - jma) * ((1 - alpha) ** 2) + (alpha ** 2) * e2
        jma = e2 + jma

    return jma

def calculate_hma(values: list, length: int) -> Optional[float]:
    """
    Hull Moving Average (HMA) calculation
    HMA = WMA(2 * WMA(n/2) - WMA(n), sqrt(n))
    """
    if len(values) < length:
        return None
    
    import math
    
    def wma(data: list, period: int) -> Optional[float]:
        """Weighted Moving Average"""
        if len(data) < period:
            return None
        subset = data[-period:]
        weights = list(range(1, period + 1))
        return sum(v * w for v, w in zip(subset, weights)) / sum(weights)
    
    half_length = max(1, int(length / 2))
    sqrt_length = max(1, int(math.sqrt(length)))
    
    # Calculate intermediate values for HMA
    intermediate = []
    for i in range(len(values) - length + 1):
        subset = values[i:i + length]
        wma_half = wma(subset, half_length)
        wma_full = wma(subset, length)
        if wma_half is not None and wma_full is not None:
            intermediate.append(2 * wma_half - wma_full)
    
    if len(intermediate) < sqrt_length:
        return None
    
    return wma(intermediate, sqrt_length)

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
    """Trading signals with JMA+HMA indicators"""
    st = state[symbol]
    price = st["price"]
    current_signal = st["current_signal"]

    if price is None or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}

    # Calculate JMA on close prices
    ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
    if USE_DI:
        calculate_di(symbol)

    if ma_close is None or (USE_DI and (st["plus_di"] is None or st["minus_di"] is None)):
        return {"changed": False, "action": "NONE", "signal": current_signal}

    # Store JMA close in history
    st["jma_close_history"].append(ma_close)

    # Calculate HMA from JMA close values
    ma_open = calculate_hma(list(st["jma_close_history"]), HMA_LENGTH_OPEN)

    if ma_open is None:
        return {"changed": False, "action": "NONE", "signal": current_signal}

    prev_close = st["prev_ma_close"]
    prev_open = st["prev_ma_open"]

    if prev_close is None or prev_open is None:
        st["prev_ma_close"] = ma_close
        st["prev_ma_open"] = ma_open
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
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

    new_signal = current_signal
    action_type = "NONE"

    if current_signal == "LONG":
        if cross_down and price_below_both and (di_bear if USE_DI else True):
            new_signal = "SHORT"
            action_type = "FLIP"
            log_msg = f"🔄 {symbol} FLIP LONG→SHORT (JMA {ma_close:.6f} crossunder HMA {ma_open:.6f} & price {price:.6f} < both MAs" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")"
            logging.info(log_msg)
    elif current_signal == "SHORT":
        if cross_up and price_above_both and (di_bull if USE_DI else True):
            new_signal = "LONG"
            action_type = "FLIP"
            log_msg = f"🔄 {symbol} FLIP SHORT→LONG (JMA {ma_close:.6f} crossover HMA {ma_open:.6f} & price {price:.6f} > both MAs" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")"
            logging.info(log_msg)

    # EXIT conditions - Based on EXIT_STRATEGY
    if new_signal == current_signal:
        if EXIT_STRATEGY == "SYMMETRIC":
            if current_signal == "LONG" and price_below_both and (di_bear if USE_DI else True):
                new_signal = None
                action_type = "EXIT"
                logging.info(f"🔴 {symbol} EXIT LONG (SYMMETRIC: price {price:.6f} fell below BOTH MAs: JMA={ma_close:.6f}, HMA={ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
            elif current_signal == "SHORT" and price_above_both and (di_bull if USE_DI else True):
                new_signal = None
                action_type = "EXIT"
                logging.info(f"🔴 {symbol} EXIT SHORT (SYMMETRIC: price {price:.6f} rose above BOTH MAs: JMA={ma_close:.6f}, HMA={ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
        
        elif EXIT_STRATEGY == "CROSSOVER":
            if current_signal == "LONG" and ma_trend_bearish and (di_bear if USE_DI else True):
                new_signal = None
                action_type = "EXIT"
                logging.info(f"🔴 {symbol} EXIT LONG (CROSSOVER: JMA trend reversed - JMA {ma_close:.6f} < HMA {ma_open:.6f}, price={price:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
            elif current_signal == "SHORT" and ma_trend_bullish and (di_bull if USE_DI else True):
                new_signal = None
                action_type = "EXIT"
                logging.info(f"🔴 {symbol} EXIT SHORT (CROSSOVER: JMA trend reversed - JMA {ma_close:.6f} > HMA {ma_open:.6f}, price={price:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
            
    if new_signal is None:
        entry_long = False
        entry_short = False

        if ENTRY_STRATEGY == "CROSSOVER":
            entry_long = crossover_aligned_long and (di_bull if USE_DI else True)
            entry_short = crossover_aligned_short and (di_bear if USE_DI else True)
            
            if entry_long:
                new_signal = "LONG"
                action_type = "ENTRY" if current_signal is None else "REENTRY"
                logging.info(f"🟢 {symbol} {action_type} LONG (CROSSOVER: price {price:.6f} > JMA {ma_close:.6f} > HMA {ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
            elif entry_short:
                new_signal = "SHORT"
                action_type = "ENTRY" if current_signal is None else "REENTRY"
                logging.info(f"🟢 {symbol} {action_type} SHORT (CROSSOVER: price {price:.6f} < JMA {ma_close:.6f} < HMA {ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
        
        elif ENTRY_STRATEGY == "SYMMETRIC":
            entry_long = price_above_both and (di_bull if USE_DI else True)
            entry_short = price_below_both and (di_bear if USE_DI else True)
            
            if entry_long:
                new_signal = "LONG"
                action_type = "ENTRY" if current_signal is None else "REENTRY"
                logging.info(f"🟢 {symbol} {action_type} LONG (SYMMETRIC: price {price:.6f} > both MAs: JMA={ma_close:.6f}, HMA={ma_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
            elif entry_short:
                new_signal = "SHORT"
                action_type = "ENTRY" if current_signal is None else "REENTRY"
                logging.info(f"🟢 {symbol} {action_type} SHORT (SYMMETRIC: price {price:.6f} < both MAs: JMA={ma_close:.6f}, HMA={ma_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")

    st["prev_ma_close"] = ma_close
    st["prev_ma_open"] = ma_open

    if new_signal != current_signal:
        st["current_signal"] = new_signal
        st["last_signal_change"] = time.time()
        save_positions()
        return {"changed": True, "action": action_type, "signal": new_signal}

    return {"changed": False, "action": "NONE", "signal": current_signal}

# ========================= PNL MANAGEMENT =========================
def reset_daily_pnl_if_needed(symbol: str):
    """Reset daily PNL after 24 hours"""
    st = state[symbol]
    now = time.time()
    
    # Initialize reset time if not set
    if st["daily_reset_time"] == 0:
        st["daily_reset_time"] = now
        return
    
    # Check if 24 hours have passed
    if now - st["daily_reset_time"] >= 86400:  # 24 hours in seconds
        if st["daily_pnl_percent"] != 0.0:
            logging.info(f"🔄 {symbol} Daily PNL reset: {st['daily_pnl_percent']:+.2f}% → 0.00%")
        st["daily_pnl_percent"] = 0.0
        st["daily_reset_time"] = now
        save_positions()

def update_daily_pnl(symbol: str, pnl_percent: float):
    """Add realized PNL to daily total"""
    st = state[symbol]
    st["daily_pnl_percent"] += pnl_percent
    logging.info(f"📊 {symbol} Position closed: {pnl_percent:+.2f}% | Daily Total: {st['daily_pnl_percent']:+.2f}%")
    save_positions()

def check_daily_pnl_limits(symbol: str) -> dict:
    """Check if daily profit or loss limit is hit"""
    st = state[symbol]
    daily_pnl = st["daily_pnl_percent"]
    
    # Check daily profit limit
    if daily_pnl >= DAILY_PROFIT_LIMIT_PERCENT:
        return {
            "hit": True,
            "type": "DAILY_PROFIT_LIMIT",
            "pnl_percent": daily_pnl
        }
    
    # Check daily loss limit
    if daily_pnl <= -DAILY_LOSS_LIMIT_PERCENT:
        return {
            "hit": True,
            "type": "DAILY_LOSS_LIMIT",
            "pnl_percent": daily_pnl
        }
    
    return {"hit": False, "pnl_percent": daily_pnl}

def calculate_position_pnl_percent(symbol: str) -> float:
    """Calculate current position PNL in percentage"""
    st = state[symbol]
    
    if st["current_position"] == 0 or st["entry_price"] is None:
        return 0.0
    
    current_price = st["price"]
    entry_price = st["entry_price"]
    
    if current_price is None:
        return 0.0
    
    # Calculate PNL percentage
    if st["current_position"] > 0:  # LONG
        pnl_percent = ((current_price - entry_price) / entry_price) * 100
    else:  # SHORT
        pnl_percent = ((entry_price - current_price) / entry_price) * 100
    
    return pnl_percent
    
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
                                    ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                                    if ma_close is not None:
                                        state[symbol]["jma_close_history"].append(ma_close)
                                    ma_open = calculate_hma(list(state[symbol]["jma_close_history"]), HMA_LENGTH_OPEN)
                                    if USE_DI:
                                        calculate_di(symbol)
                                    if (ma_close is not None) and (ma_open is not None) and (not USE_DI or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)):
                                        state[symbol]["ready"] = True
                                        ha_status = " (Heikin Ashi enabled)" if USE_HEIKIN_ASHI else ""
                                        log_msg = f"✅ {symbol} ready for trading ({len(state[symbol]['klines'])} {BASE_TIMEFRAME} candles, JMA {ma_close:.6f}, HMA {ma_open:.6f}){ha_status}"
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
            
            # Check if paused
            if st["paused_until"] > time.time():
                remaining_hours = (st["paused_until"] - time.time()) / 3600
                daily_pnl = st["daily_pnl_percent"]
                logging.info(f"{symbol}: ⏸️  PAUSED - Daily PNL: {daily_pnl:+.2f}% - Resumes in {remaining_hours:.1f} hours")
                continue

            if not st["ready"]:
                candle_count = len(st["klines"])
                price = st["price"]
                
                ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                if ma_close is not None:
                    st["jma_close_history"].append(ma_close)
                ma_open = calculate_hma(list(st["jma_close_history"]), HMA_LENGTH_OPEN)
                if USE_DI:
                    calculate_di(symbol)
                
                reasons = []
                if candle_count < MA_PERIODS + 1:
                    needed = (MA_PERIODS + 1) - candle_count
                    reasons.append(f"need {needed} more {BASE_TIMEFRAME} candles")
                if ma_close is None:
                    reasons.append("JMA not calculated")
                if ma_open is None:
                    reasons.append("HMA not calculated")
                if USE_DI and (st["plus_di"] is None or st["minus_di"] is None):
                    reasons.append("DI not calculated")
                
                reason_str = ", ".join(reasons) if reasons else "unknown"
                price_str = f"Price={price:.6f} | " if price else ""
                logging.info(f"{symbol}: {price_str}Not ready - {candle_count} {BASE_TIMEFRAME} candles - Waiting for: {reason_str}")
                continue

            price = st["price"]
            ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
            if ma_close is not None:
                st["jma_close_history"].append(ma_close)
            ma_open = calculate_hma(list(st["jma_close_history"]), HMA_LENGTH_OPEN)
            plus_di = st.get("plus_di")
            minus_di = st.get("minus_di")

            if price and ma_close and ma_open:
                current_sig = st["current_signal"] or "FLAT"

                plus_di_str = f"{plus_di:.4f}" if USE_DI and plus_di is not None else "N/A"
                minus_di_str = f"{minus_di:.4f}" if USE_DI and minus_di is not None else "N/A"

                # Calculate unrealized PNL for current position
                unrealized_pnl = calculate_position_pnl_percent(symbol)
                total_daily_pnl = st["daily_pnl_percent"] + unrealized_pnl

                logging.info(f"{symbol}: Price={price:.6f} | JMA={ma_close:.6f} | HMA={ma_open:.6f} | +DI={plus_di_str} | -DI={minus_di_str}")
                logging.info(f"  Signal: {current_sig}")
                logging.info(f"  Daily PNL: Realized={st['daily_pnl_percent']:+.2f}% | Unrealized={unrealized_pnl:+.2f}% | Total={total_daily_pnl:+.2f}%")

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
    """Main trading logic"""
    while True:
        await asyncio.sleep(0.1)

        for symbol in SYMBOLS:
            st = state[symbol]
            
            # Reset daily PNL if 24 hours passed
            reset_daily_pnl_if_needed(symbol)
            
            # Check if symbol is paused
            if st["paused_until"] > time.time():
                continue
            
            # Resume trading after pause expires
            if st["paused_until"] > 0 and st["paused_until"] <= time.time():
                st["paused_until"] = 0
                save_positions()
                logging.info(f"🔓 {symbol} trading RESUMED after 24-hour pause")
            
            if not st["ready"]:
                continue

            # Check daily PNL limits
            daily_check = check_daily_pnl_limits(symbol)
            if daily_check["hit"]:
                limit_type = daily_check["type"]
                pnl_pct = daily_check["pnl_percent"]
                
                # Close any open position
                current_pos = st["current_position"]
                if current_pos != 0:
                    await execute_position_change(client, symbol, 0.0, current_pos)
                
                # Pause trading for 24 hours
                st["paused_until"] = time.time() + (PAUSE_DURATION_HOURS * 3600)
                st["current_signal"] = None
                save_positions()
                
                emoji = "🎯" if "PROFIT" in limit_type else "🛑"
                logging.info(
                    f"{emoji} {symbol} {limit_type} REACHED: {pnl_pct:+.2f}% "
                    f"- PAUSED for {PAUSE_DURATION_HOURS}h"
                )
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
    """Execute position changes"""
    st = state[symbol]

    now = time.time()
    last_target = st.get("last_target", None)
    last_when = st.get("last_exec_ts", 0.0)
    if last_target is not None and abs(target - last_target) < 1e-12 and (now - last_when) < 2.0:
        logging.info(f"🛡️ {symbol} dedup: skipping duplicate execution")
        return

    if abs(target - current) < 1e-12:
        return

    try:
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

        st["current_position"] = target
        st["last_target"] = target
        st["last_exec_ts"] = now
        
        # Calculate and record PNL when closing position
        if current != 0.0 and target == 0.0 and st["entry_price"] is not None:
            # Position is being closed
            current_price = st["price"]
            entry_price = st["entry_price"]
            
            if current > 0:  # Closing LONG
                pnl_percent = ((current_price - entry_price) / entry_price) * 100
            else:  # Closing SHORT
                pnl_percent = ((entry_price - current_price) / entry_price) * 100
            
            # Add to daily PNL
            update_daily_pnl(symbol, pnl_percent)
            st["entry_price"] = None
            st["position_size_usdt"] = 0.0
        
        # Track entry price when opening new position
        elif target != 0.0 and st["entry_price"] is None:
            st["entry_price"] = st["price"]
            # Estimate position size in USDT
            st["position_size_usdt"] = abs(target) * st["price"]
        
        save_positions()

    except Exception as e:
        logging.error(f"❌ {symbol} position change failed: {e}")

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
            
            if abs(position_amt) > 0.0001:
                recovered_count += 1
                
                if position_amt > 0:
                    signal = "LONG"
                    state[symbol]["current_position"] = SYMBOLS[symbol]
                elif position_amt < 0:
                    signal = "SHORT"
                    state[symbol]["current_position"] = -SYMBOLS[symbol]
                
                state[symbol]["current_signal"] = signal
                
                entry_price = float(position['entryPrice'])
                unrealized_pnl = float(position['unrealizedProfit'])
                
                # Set entry price for PNL tracking
                state[symbol]["entry_price"] = entry_price
                
                logging.info(
                    f"♻️ {symbol} RECOVERED {signal} position: "
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

async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
    logging.info(f"📊 JMA: length={JMA_LENGTH_CLOSE}, phase={JMA_PHASE}, power={JMA_POWER}")
    logging.info(f"📊 HMA: length={HMA_LENGTH_OPEN} (applied to JMA)")
    
    if USE_HEIKIN_ASHI:
        logging.info("📊 Heikin Ashi: ENABLED")
    else:
        logging.info("📊 Heikin Ashi: DISABLED")
    if USE_DI:
        logging.info(f"📊 DMI: {DI_PERIODS} periods")
    else:
        logging.info("📊 DMI: DISABLED")
    logging.info(f"📊 Entry: {ENTRY_STRATEGY} | Exit: {EXIT_STRATEGY}")
    logging.info(f"📊 Daily Limits: Profit +{DAILY_PROFIT_LIMIT_PERCENT}% | Loss -{DAILY_LOSS_LIMIT_PERCENT}%")

    load_klines()
    load_positions()
    
    await recover_positions_from_exchange(client)

    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        
        if len(klines) >= JMA_LENGTH_CLOSE + HMA_LENGTH_OPEN + 50:
            # Build JMA history from existing klines
            state[symbol]["jma_close_history"].clear()
            
            # Calculate JMA for each point in history to build up JMA values
            klines_list = list(klines)
            for i in range(JMA_LENGTH_CLOSE, len(klines_list)):
                # Temporarily set klines to subset for JMA calculation
                temp_klines = deque(klines_list[:i+1], maxlen=KLINE_LIMIT)
                original_klines = state[symbol]["klines"]
                state[symbol]["klines"] = temp_klines
                
                jma = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                
                # Restore original klines
                state[symbol]["klines"] = original_klines
                
                if jma is not None:
                    state[symbol]["jma_close_history"].append(jma)
            
            # Check if we have enough data
            jma_close_ready = len(state[symbol]["jma_close_history"]) > 0
            hma_open_ready = len(state[symbol]["jma_close_history"]) >= HMA_LENGTH_OPEN
            
            if USE_DI:
                calculate_di(symbol)
            di_ready = (not USE_DI) or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)

            if jma_close_ready and hma_open_ready and di_ready:
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

                needed_candles = max(JMA_LENGTH_CLOSE + HMA_LENGTH_OPEN + 100, (DI_PERIODS + 100 if USE_DI else 100))
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

                # Build JMA history from fetched klines
                st["jma_close_history"].clear()
                klines_list = list(st["klines"])
                
                for j in range(JMA_LENGTH_CLOSE, len(klines_list)):
                    # Create subset for this calculation
                    temp_klines = deque(klines_list[:j+1], maxlen=KLINE_LIMIT)
                    original_klines = st["klines"]
                    st["klines"] = temp_klines
                    
                    jma = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                    
                    # Restore full klines
                    st["klines"] = original_klines
                    
                    if jma is not None:
                        st["jma_close_history"].append(jma)
                
                jma_close_ok = len(st["jma_close_history"]) > 0
                hma_open_ok = len(st["jma_close_history"]) >= HMA_LENGTH_OPEN
                
                if USE_DI:
                    calculate_di(symbol)
                di_ok = (not USE_DI) or (st["plus_di"] is not None and st["minus_di"] is not None)

                if jma_close_ok and hma_open_ok and di_ok:
                    st["ready"] = True
                    ma_close = calculate_jma(symbol, "close", JMA_LENGTH_CLOSE, JMA_PHASE, JMA_POWER)
                    ma_open = calculate_hma(list(st["jma_close_history"]), HMA_LENGTH_OPEN)
                    logging.info(f"✅ {symbol} ready from API (JMA: {ma_close:.6f}, HMA: {ma_open:.6f})")

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

        logging.info("🚀 Bot started")

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
    print(f"JMA + HMA CROSS STRATEGY - {ha_mode}")
    print(f"TIMEFRAME: {BASE_TIMEFRAME}")
    print("=" * 80)
    print(f"JMA Parameters: Length={JMA_LENGTH_CLOSE}, Phase={JMA_PHASE}, Power={JMA_POWER}")
    print(f"HMA Parameters: Length={HMA_LENGTH_OPEN} (applied to JMA)")
    print(f"Timeframe: {BASE_TIMEFRAME} ({BASE_MINUTES} min)")
    print(f"Heikin Ashi: {'ENABLED' if USE_HEIKIN_ASHI else 'DISABLED'}")
    print(f"DMI: {'ENABLED (' + str(DI_PERIODS) + ' periods)' if USE_DI else 'DISABLED'}")
    print(f"Entry Strategy: {ENTRY_STRATEGY}")
    print(f"Exit Strategy: {EXIT_STRATEGY}")
    print(f"Daily Limits: Profit +{DAILY_PROFIT_LIMIT_PERCENT}% | Loss -{DAILY_LOSS_LIMIT_PERCENT}%")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print("=" * 80)

    asyncio.run(main())
