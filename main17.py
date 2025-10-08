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
DI_PERIODS = int(os.getenv("DI_PERIODS", "9"))
USE_DI = True  # Changed from os.getenv("USE_DI", "True").lower() == "true"

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

# Base timeframe configuration
BASE_TIMEFRAME = "1h"  # or "15m" or "1m"

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
    AGGREGATION_MINUTES = 3     # Aggregate to 3 minutes instead of 180
    DEMA_PERIODS = 600          # 600 * 3m = 30 hours
elif BASE_TIMEFRAME == "15m":
    BASE_MINUTES = 15
    AGGREGATION_MINUTES = 45    # Aggregate to 45 minutes instead of 180
    DEMA_PERIODS = 40           # 40 * 45m = 30 hours
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
    AGGREGATION_MINUTES = 180   # Keep at 3 hours
    DEMA_PERIODS = 3           # 3 * 180m = 9 hours
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

# Compute limits
agg_factor = AGGREGATION_MINUTES // BASE_MINUTES
KLINE_LIMIT_BASE = max(DI_PERIODS + 100 if USE_DI else 100, DEMA_PERIODS * agg_factor + 100)
KLINE_LIMIT_AGG = DEMA_PERIODS + 20

# ENTRY STRATEGY TOGGLE
ENTRY_STRATEGY = "SYMMETRIC" # or "CROSSOVER"

# EXIT STRATEGY TOGGLE
EXIT_STRATEGY = "SYMMETRIC" # or "CROSSOVER"

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines_base": deque(maxlen=KLINE_LIMIT_BASE),    # Base timeframe candles
        "klines_agg": deque(maxlen=KLINE_LIMIT_AGG),  # Aggregated candles
        "current_signal": None,
        "last_signal_change": 0,
        "current_position": 0.0,
        "dema_close": None,
        "dema_open": None,
        "prev_dema_close": None,
        "prev_dema_open": None,
        "plus_di": None,
        "minus_di": None,
        "di_ready": False,
        "ready": False,
        "last_exec_ts": 0.0,
        "last_target": None,
    }
    for symbol in SYMBOLS
}

# Rate limiting
api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE FUNCTIONS =========================
def save_klines():
    """Save base klines to JSON"""
    save_data = {sym: list(state[sym]["klines_base"]) for sym in SYMBOLS}
    with open('klines.json', 'w') as f:
        json.dump(save_data, f)
    logging.info("📥 Saved klines to klines.json")

def load_klines():
    """Load base klines from JSON"""
    try:
        with open('klines.json', 'r') as f:
            load_data = json.load(f)
        for sym in SYMBOLS:
            state[sym]["klines_base"] = deque(load_data.get(sym, []), maxlen=KLINE_LIMIT_BASE)
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
            "last_signal_change": state[sym]["last_signal_change"]
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
        logging.info("💾 Loaded positions from positions.json")
    except FileNotFoundError:
        logging.info("No positions.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load positions: {e} - starting fresh")

# ========================= CANDLE AGGREGATION =========================
def aggregate_candles(symbol: str):
    """Aggregate base candles into larger timeframe candles"""
    klines_base = state[symbol]["klines_base"]
    
    agg_count = AGGREGATION_MINUTES // BASE_MINUTES
    
    if len(klines_base) < agg_count:
        return
    
    # Clear aggregated candles
    state[symbol]["klines_agg"].clear()
    
    # Group base candles into aggregated candles
    candles_base = list(klines_base)
    
    for i in range(0, len(candles_base), agg_count):
        chunk = candles_base[i:i + agg_count]
        
        if len(chunk) < agg_count:
            # Incomplete chunk - this is the forming aggregated candle
            continue
        
        # Aggregate the chunk
        agg_candle = {
            "open_time": chunk[0]["open_time"],  # Start timestamp
            "open": chunk[0]["open"],             # First open
            "high": max(c["high"] for c in chunk),
            "low": min(c["low"] for c in chunk),
            "close": chunk[-1]["close"]           # Last close
        }
        
        state[symbol]["klines_agg"].append(agg_candle)
    
    # Add forming aggregated candle (incomplete)
    remaining = len(candles_base) % agg_count
    if remaining > 0:
        forming_chunk = candles_base[-remaining:]
        forming_candle = {
            "open_time": forming_chunk[0]["open_time"],
            "open": forming_chunk[0]["open"],
            "high": max(c["high"] for c in forming_chunk),
            "low": min(c["low"] for c in forming_chunk),
            "close": forming_chunk[-1]["close"]
        }
        state[symbol]["klines_agg"].append(forming_candle)

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
def calculate_dema(symbol: str, field: str, period: int) -> Optional[float]:
    """DEMA on aggregated candles"""
    klines = state[symbol]["klines_agg"]

    if len(klines) < period + 1:
        return None

    completed = list(klines)[:-1]
    if len(completed) < period:
        return None

    values = [k.get(field, None) for k in completed]
    if None in values:
        return None

    alpha = 2 / (period + 1)

    ema_series = []
    ema = values[0]
    ema_series.append(ema)
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
        ema_series.append(ema)

    ema1 = ema_series[-1]

    ema2 = ema_series[0]
    for es in ema_series[1:]:
        ema2 = alpha * es + (1 - alpha) * ema2

    dema_val = 2 * ema1 - ema2
    state[symbol][f"dema_{field}"] = dema_val

    return dema_val

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
    """DI on base candles"""
    klines = state[symbol]["klines_base"]
    if len(klines) < DI_PERIODS + 1:
        return None

    completed = list(klines)[:-1]
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
    """Trading signals with dual-timeframe indicators"""
    st = state[symbol]
    price = st["price"]
    current_signal = st["current_signal"]

    if price is None or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}

    dema_close = calculate_dema(symbol, "close", DEMA_PERIODS)
    dema_open = calculate_dema(symbol, "open", DEMA_PERIODS)
    if USE_DI:
        calculate_di(symbol)

    if dema_close is None or dema_open is None or (USE_DI and (st["plus_di"] is None or st["minus_di"] is None)):
        return {"changed": False, "action": "NONE", "signal": current_signal}

    prev_close = st["prev_dema_close"]
    prev_open = st["prev_dema_open"]

    if prev_close is None or prev_open is None:
        st["prev_dema_close"] = dema_close
        st["prev_dema_open"] = dema_open
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    cross_up = (dema_close > dema_open) and (prev_close <= prev_open)
    cross_down = (dema_close < dema_open) and (prev_close >= prev_open)
    
    price_above_both = (price > dema_close) and (price > dema_open)
    price_below_both = (price < dema_close) and (price < dema_open)
    
    # DEMA trend states
    dema_bullish = dema_close > dema_open
    dema_bearish = dema_close < dema_open

    plus_di = st["plus_di"]
    minus_di = st["minus_di"]
    di_bull = plus_di > minus_di if USE_DI else True
    di_bear = minus_di > plus_di if USE_DI else True

    new_signal = current_signal
    action_type = "NONE"

    # Check for flips (common to both strategies)
    if current_signal == "LONG":
        if cross_down and price_below_both and (di_bear if USE_DI else True):
            new_signal = "SHORT"
            action_type = "FLIP"
            log_msg = f"🔄 {symbol} FLIP LONG→SHORT (close_DEMA {dema_close:.6f} crossunder open_DEMA {dema_open:.6f} & price {price:.6f} < both DEMAs" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")"
            logging.info(log_msg)
    elif current_signal == "SHORT":
        if cross_up and price_above_both and (di_bull if USE_DI else True):
            new_signal = "LONG"
            action_type = "FLIP"
            log_msg = f"🔄 {symbol} FLIP SHORT→LONG (close_DEMA {dema_close:.6f} crossover open_DEMA {dema_open:.6f} & price {price:.6f} > both DEMAs" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")"
            logging.info(log_msg)

    # Check for exits if SYMMETRIC exit and not already flipped
    if EXIT_STRATEGY == "SYMMETRIC" and new_signal == current_signal:
        if current_signal == "LONG" and price_below_both and (di_bear if USE_DI else True):
            new_signal = None
            action_type = "EXIT"
            logging.info(f"🔴 {symbol} EXIT LONG (price {price:.6f} fell below BOTH DEMAs: close={dema_close:.6f}, open={dema_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")
        elif current_signal == "SHORT" and price_above_both and (di_bull if USE_DI else True):
            new_signal = None
            action_type = "EXIT"
            logging.info(f"🔴 {symbol} EXIT SHORT (price {price:.6f} rose above BOTH DEMAs: close={dema_close:.6f}, open={dema_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
            
    # If no signal (was None or exited), check for entry
    if new_signal is None:
        entry_long = False
        entry_short = False
        is_cross_entry = False

        if ENTRY_STRATEGY == "CROSSOVER":
            entry_long = cross_up and price_above_both and (di_bull if USE_DI else True)
            entry_short = cross_down and price_below_both and (di_bear if USE_DI else True)
            is_cross_entry = True
        elif ENTRY_STRATEGY == "SYMMETRIC":
            entry_long = price_above_both and (di_bull if USE_DI else True)
            entry_short = price_below_both and (di_bear if USE_DI else True)
            is_cross_entry = False

        if entry_long:
            new_signal = "LONG"
            action_type = "ENTRY" if current_signal is None else "REENTRY"
            logging.info(f"🟢 {symbol} {action_type} LONG (price {price:.6f} > both DEMAs: close={dema_close:.6f}, open={dema_open:.6f}" + (f" & +DI {plus_di:.4f} > -DI {minus_di:.4f}" if USE_DI else "") + ")")
        elif entry_short:
            new_signal = "SHORT"
            action_type = "ENTRY" if current_signal is None else "REENTRY"
            logging.info(f"🟢 {symbol} {action_type} SHORT (price {price:.6f} < both DEMAs: close={dema_close:.6f}, open={dema_open:.6f}" + (f" & -DI {minus_di:.4f} > +DI {plus_di:.4f}" if USE_DI else "") + ")")

    st["prev_dema_close"] = dema_close
    st["prev_dema_open"] = dema_open

    if new_signal != current_signal:
        st["current_signal"] = new_signal
        st["last_signal_change"] = time.time()
        save_positions()
        return {"changed": True, "action": action_type, "signal": new_signal}

    return {"changed": False, "action": "NONE", "signal": current_signal}
    
# ========================= MAIN LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    """WebSocket feed - builds base candles and aggregates them"""
    streams = [f"{s.lower()}@markPrice@1s" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

    base_interval_seconds = BASE_MINUTES * 60

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logging.info("📡 WebSocket connected")

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
                            
                            # Build base candles
                            current_open_time = int(event_time // base_interval_seconds) * base_interval_seconds
                            klines_base = state[symbol]["klines_base"]

                            if not klines_base or klines_base[-1]["open_time"] != current_open_time:
                                klines_base.append({
                                    "open_time": current_open_time,
                                    "open": price,
                                    "high": price,
                                    "low": price,
                                    "close": price
                                })
                                
                                # Aggregate candles when new base candle starts
                                aggregate_candles(symbol)
                            else:
                                klines_base[-1]["high"]  = max(klines_base[-1]["high"],  price)
                                klines_base[-1]["low"]   = min(klines_base[-1]["low"],   price)
                                klines_base[-1]["close"] = price
                                
                                # Update aggregation for forming candle
                                aggregate_candles(symbol)

                            # Check readiness
                            if len(state[symbol]["klines_agg"]) >= DEMA_PERIODS and not state[symbol]["ready"]:
                                dema_close = calculate_dema(symbol, "close", DEMA_PERIODS)
                                dema_open = calculate_dema(symbol, "open", DEMA_PERIODS)
                                if USE_DI:
                                    calculate_di(symbol)
                                if (dema_close is not None) and (dema_open is not None) and (not USE_DI or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)):
                                    state[symbol]["ready"] = True
                                    log_msg = f"✅ {symbol} ready for trading ({len(state[symbol]['klines_agg'])} {AGGREGATION_MINUTES}m candles, close_DEMA {dema_close:.6f}, open_DEMA {dema_open:.6f})"
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
                candle_count_base = len(st["klines_base"])
                candle_count_agg = len(st["klines_agg"])
                price = st["price"]
                
                dema_close = calculate_dema(symbol, "close", DEMA_PERIODS)
                dema_open = calculate_dema(symbol, "open", DEMA_PERIODS)
                if USE_DI:
                    calculate_di(symbol)
                
                reasons = []
                if candle_count_agg < DEMA_PERIODS + 1:
                    needed = (DEMA_PERIODS + 1) - candle_count_agg
                    reasons.append(f"need {needed} more {AGGREGATION_MINUTES}m candles")
                if dema_close is None:
                    reasons.append("DEMA close not calculated")
                if dema_open is None:
                    reasons.append("DEMA open not calculated")
                if USE_DI and (st["plus_di"] is None or st["minus_di"] is None):
                    reasons.append("DI not calculated")
                
                reason_str = ", ".join(reasons) if reasons else "unknown"
                price_str = f"Price={price:.6f} | " if price else ""
                logging.info(f"{symbol}: {price_str}Not ready - {candle_count_base} {BASE_TIMEFRAME} candles ({candle_count_agg} {AGGREGATION_MINUTES}m) - Waiting for: {reason_str}")
                continue

            price = st["price"]
            dema_close = calculate_dema(symbol, "close", DEMA_PERIODS)
            dema_open = calculate_dema(symbol, "open", DEMA_PERIODS)
            plus_di = st.get("plus_di")
            minus_di = st.get("minus_di")

            if price and dema_close and dema_open:
                current_sig = st["current_signal"] or "FLAT"

                plus_di_str = f"{plus_di:.4f}" if USE_DI and plus_di is not None else "N/A"
                minus_di_str = f"{minus_di:.4f}" if USE_DI and minus_di is not None else "N/A"

                logging.info(f"{symbol}: Price={price:.6f} | close_DEMA={dema_close:.6f} | open_DEMA={dema_open:.6f} | +DI={plus_di_str} | -DI={minus_di_str}")
                logging.info(f"  Signal: {current_sig}")

                trend_up = (dema_close > dema_open)
                trend_down = (dema_close < dema_open)
                price_above_both = (price > dema_close) and (price > dema_open)
                price_below_both = (price < dema_close) and (price < dema_open)
                
                logging.info(f"  Current Trend: {'UP' if trend_up else 'DOWN' if trend_down else 'FLAT'}")
                logging.info(f"  Price Position: {'Above Both DEMAs' if price_above_both else 'Below Both DEMAs' if price_below_both else 'Between DEMAs'}")
                if USE_DI and plus_di is not None and minus_di is not None:
                    di_direction = "Bullish" if plus_di > minus_di else "Bearish" if minus_di > plus_di else "Neutral"
                    plus_di_str = f"{plus_di:.4f}"
                    minus_di_str = f"{minus_di:.4f}"
                else:
                    di_direction = "N/A"
                    plus_di_str = "N/A"
                    minus_di_str = "N/A"
                logging.info(f"  DI Direction: {di_direction} (+DI={plus_di_str}, -DI={minus_di_str})")

        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic"""
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

        else:  # target < 0
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
        save_positions()

    except Exception as e:
        logging.error(f"❌ {symbol} position change failed: {e}")

# ========================= POSITION RECOVERY =========================
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

# ========================= INITIALIZATION =========================
async def init_bot(client: AsyncClient):
    """Initialize bot with historical data"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 Base: {BASE_TIMEFRAME} candles")
    logging.info(f"📊 Aggregated: {AGGREGATION_MINUTES}-minute ({AGGREGATION_MINUTES/60:.1f}h) candles")
    logging.info(f"📊 DEMA: {DEMA_PERIODS} periods on {AGGREGATION_MINUTES}m = {DEMA_PERIODS * AGGREGATION_MINUTES / 60:.1f} hours")
    if USE_DI:
        logging.info(f"📊 DMI: {DI_PERIODS} periods on {BASE_TIMEFRAME} = {DI_PERIODS * BASE_MINUTES / 60:.1f} hours")
    else:
        logging.info("📊 DMI: Disabled")
    logging.info(f"📊 Entry Strategy: {ENTRY_STRATEGY}")
    logging.info(f"📊 Exit Strategy: {EXIT_STRATEGY}")

    load_klines()
    load_positions()
    
    await recover_positions_from_exchange(client)

    symbols_needing_data = []
    for symbol in SYMBOLS:
        # Aggregate loaded base candles
        if len(state[symbol]["klines_base"]) >= (AGGREGATION_MINUTES // BASE_MINUTES):
            aggregate_candles(symbol)
        
        klines_agg = state[symbol]["klines_agg"]
        d_close_ready = len(klines_agg) >= DEMA_PERIODS and calculate_dema(symbol, "close", DEMA_PERIODS) is not None
        d_open_ready = len(klines_agg) >= DEMA_PERIODS and calculate_dema(symbol, "open", DEMA_PERIODS) is not None
        if USE_DI:
            calculate_di(symbol)
        di_ready = (not USE_DI) or (state[symbol]["plus_di"] is not None and state[symbol]["minus_di"] is not None)

        if d_close_ready and d_open_ready and di_ready:
            state[symbol]["ready"] = True
            logging.info(f"✅ {symbol} ready from loaded data")
        else:
            symbols_needing_data.append(symbol)

    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")

                # Fetch base candles
                needed_candles = max(DEMA_PERIODS * (AGGREGATION_MINUTES // BASE_MINUTES) + 100, (DI_PERIODS + 100 if USE_DI else 100))
                klines_data = await safe_api_call(
                    client.futures_mark_price_klines,
                    symbol=symbol,
                    interval=BASE_TIMEFRAME,
                    limit=min(needed_candles, 1500)  # Binance limit is 1500
                )

                st = state[symbol]
                st["klines_base"].clear()

                for kline in klines_data:
                    open_time = int(float(kline[0]) / 1000)
                    st["klines_base"].append({
                        "open_time": open_time,
                        "open": float(kline[1]),
                        "high": float(kline[2]),
                        "low": float(kline[3]),
                        "close": float(kline[4])
                    })

                # Aggregate the base candles
                aggregate_candles(symbol)

                d_close_ok = calculate_dema(symbol, "close", DEMA_PERIODS) is not None
                d_open_ok = calculate_dema(symbol, "open", DEMA_PERIODS) is not None
                if USE_DI:
                    calculate_di(symbol)
                di_ok = (not USE_DI) or (st["plus_di"] is not None and st["minus_di"] is not None)

                if d_close_ok and d_open_ok and di_ok:
                    st["ready"] = True
                    logging.info(f"✅ {symbol} ready from API")

                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)

            except Exception as e:
                logging.error(f"❌ {symbol} fetch failed: {e}")
                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)

    else:
        logging.info("🎯 All symbols ready!")

    save_klines()
    await asyncio.sleep(2)
    logging.info("🚀 Initialization complete")

# ========================= MAIN =========================
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
    print(f"DEMA STRATEGY - {BASE_TIMEFRAME.upper()} BASE → {AGGREGATION_MINUTES}M AGG - {ENTRY_STRATEGY} ENTRY - {EXIT_STRATEGY} EXIT")
    print("=" * 80)
    print(f"Base Resolution: {BASE_TIMEFRAME} candles (updates every {BASE_MINUTES} minutes)")
    print(f"Aggregation: {AGGREGATION_MINUTES} minutes ({AGGREGATION_MINUTES/60:.1f} hours)")
    print(f"DEMA: {DEMA_PERIODS} periods on {AGGREGATION_MINUTES}m = {DEMA_PERIODS * AGGREGATION_MINUTES / 60:.1f} hours total")
    if USE_DI:
        print(f"DMI: {DI_PERIODS} periods on {BASE_TIMEFRAME} = {DI_PERIODS * BASE_MINUTES / 60:.1f} hours total")
    else:
        print("DMI: Disabled")
    print(f"Entry Strategy: {ENTRY_STRATEGY}")
    print(f"Exit Strategy: {EXIT_STRATEGY}")
    print("")
    if ENTRY_STRATEGY == "CROSSOVER":
        print("ENTRY: Crossover + price confirmation + DI direction confirmation")
    else:
        print("ENTRY: Price confirmation + DI direction confirmation")
    if EXIT_STRATEGY == "CROSSOVER":
        print("EXIT: Only on opposite crossover")
    else:
        print("EXIT: Price breaks DEMA range (early exit)")
    print("")
    print("Position recovery enabled")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print("=" * 80)

    asyncio.run(main())
