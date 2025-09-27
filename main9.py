#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time
import atexit  # Added for auto-save
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple
from dotenv import load_dotenv

# ========================= CONFIG =========================
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE = int(os.getenv("LEVERAGE", "10"))

# Trading symbols and sizes
SYMBOLS = {
    "BTCUSDT": 0.001,
    "ETHUSDT": 0.01,
    "BNBUSDT": 0.03,
    "XRPUSDT": 10.0,
    "SOLUSDT": 0.1,
    "ADAUSDT": 10.0,
    "DOGEUSDT": 40.0,
    "TRXUSDT": 20.0,
}

# Hardcoded precisions to avoid API calls
PRECISIONS = {
    "BTCUSDT": 3, "ETHUSDT": 3, "BNBUSDT": 2, "XRPUSDT": 1,
    "SOLUSDT": 3, "ADAUSDT": 0, "DOGEUSDT": 0, "TRXUSDT": 0
}

# Indicator settings
DONCHIAN_PERIODS = 1200   # 20 hours (1200 minutes) for Donchian
WMA10_PERIODS    = 600    # 10 hours (600 minutes) for 10h Weighted Moving Average (closes)
ADX_PERIODS      = 1200   # ADX calculation window (20h)
ADX_THRESHOLD    = 25     # Minimum ADX to allow trading
KLINE_LIMIT      = 1300   # Keep last 1300 1m candles

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "current_signal": None,  # None, "LONG", "SHORT"
        "last_signal_change": 0,
        "current_position": 0.0,  # Current position size
        "wma10": None,            # 10h Weighted MA (closes)
        "adx": None,              # ADX value
        "adx_ready": False,       # ADX availability flag
        "ready": False,           # True when Donchian+WMA10+ADX are available (ADX≥25)
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
    """Save klines to JSON on shutdown"""
    save_data = {sym: list(state[sym]["klines"]) for sym in SYMBOLS}  # Convert deque to list for JSON
    with open('klines.json', 'w') as f:
        json.dump(save_data, f)
    logging.info("📥 Saved klines to klines.json")

def load_klines():
    """Load klines from JSON on startup"""
    try:
        with open('klines.json', 'r') as f:
            load_data = json.load(f)
        for sym in SYMBOLS:
            state[sym]["klines"] = deque(load_data.get(sym, []), maxlen=KLINE_LIMIT)  # Restore as deque
        logging.info("📤 Loaded klines from klines.json")
    except FileNotFoundError:
        logging.info("No klines.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load klines: {e} - starting fresh")

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    """Round position size to appropriate precision"""
    prec = PRECISIONS.get(symbol, 3)
    return round(size, prec)

async def safe_api_call(func, *args, **kwargs):
    """Make API call with exponential backoff for rate limiting"""
    global api_calls_count, api_calls_reset_time

    # Reset counter every minute
    now = time.time()
    if now - api_calls_reset_time > 60:
        api_calls_count = 0
        api_calls_reset_time = now

    # Limit API calls to 10 per minute (very conservative)
    if api_calls_count >= 10:
        wait_time = 60 - (now - api_calls_reset_time)
        if wait_time > 0:
            logging.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            api_calls_count = 0
            api_calls_reset_time = time.time()

    # Exponential backoff retry logic
    for attempt in range(3):
        try:
            api_calls_count += 1
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            if "-1003" in str(e) or "Way too many requests" in str(e):
                wait_time = (2 ** attempt) * 60  # 1min, 2min, 4min
                logging.warning(f"Rate limited, attempt {attempt+1}/3, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                raise e

    raise Exception("Max API retry attempts reached")

async def place_order(client: AsyncClient, symbol: str, side: str, quantity: float, action: str):
    """Place market order with rate limiting"""
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            return True

        # Determine positionSide based on action
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
def get_donchian_levels(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """Get Donchian channel high/low from last N (completed) 1m periods"""
    klines = state[symbol]["klines"]

    if len(klines) < DONCHIAN_PERIODS:
        return None, None

    # Use last N complete periods (exclude current forming candle)
    recent = list(klines)[:-1][-DONCHIAN_PERIODS:]

    if not recent:
        return None, None

    highs = [k["high"] for k in recent]
    lows  = [k["low"]  for k in recent]

    return min(lows), max(highs)

def calculate_wma_close(symbol: str, period: int) -> Optional[float]:
    """Weighted Moving Average of closes over 'period' completed 1m candles (exclude forming)."""
    klines = state[symbol]["klines"]

    # Need at least 'period' completed candles (+1 forming)
    if len(klines) < period + 1:
        return None

    completed = list(klines)[:-1]
    if len(completed) < period:
        return None

    closes  = [k["close"] for k in completed[-period:]]
    weights = list(range(1, period + 1))
    denom   = sum(weights)
    num     = sum(c * w for c, w in zip(closes, weights))
    wma_val = num / denom

    # cache if it's the 10h WMA
    if period == WMA10_PERIODS:
        state[symbol]["wma10"] = wma_val

    return wma_val

# ---------- ADX ----------
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
    """Calculate ADX (Average Directional Index) for trend strength"""
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

    # Smooth TR
    sm_tr = tr_values[0]
    for tr in tr_values[1:]:
        sm_tr = alpha * tr + (1 - alpha) * sm_tr

    # Smooth +DM / -DM
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
    """
    BREAKOUT ENTRY → PULLBACK FLIPPING Strategy with 1% Buffer
    
    Phase 1 - INITIAL ENTRY (from FLAT, need ALL 3 conditions):
    - LONG:  price >= Donchian HIGH AND price > WMA10 AND ADX >= 25
    - SHORT: price <= Donchian LOW  AND price < WMA10 AND ADX >= 25
    
    Phase 2 - PULLBACK FLIPPING with 1% BUFFER (once in position, need only 2 conditions):
    - LONG → SHORT: price < WMA10 * 0.99 AND ADX >= 25 (1% below WMA10)
    - SHORT → LONG: price > WMA10 * 1.01 AND ADX >= 25 (1% above WMA10)
    
    Never go FLAT again after initial entry - always LONG or SHORT!
    1% buffer prevents whipsaw around WMA10 level.
    """
    st = state[symbol]
    price = st["price"]
    current_signal = st["current_signal"]

    if price is None or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}

    d_low, d_high = get_donchian_levels(symbol)
    wma10 = calculate_wma_close(symbol, WMA10_PERIODS)
    adx   = calculate_adx(symbol)

    if d_low is None or d_high is None or wma10 is None or adx is None:
        return {"changed": False, "action": "NONE", "signal": current_signal}

    # Phase 1: BREAKOUT entry conditions (ALL 3 must be true)
    adx_ok         = (adx >= ADX_THRESHOLD)
    breakout_long  = adx_ok and (price >= d_high) and (price > wma10)   # 3 conditions for entry
    breakout_short = adx_ok and (price <= d_low)  and (price < wma10)   # 3 conditions for entry
    
    # Phase 2: PULLBACK flipping conditions with 1% BUFFER (only 2 must be true)
    pullback_to_short = (price < wma10 * 0.99) and adx_ok   # 1% below WMA10 + ADX
    pullback_to_long  = (price > wma10 * 1.01) and adx_ok   # 1% above WMA10 + ADX

    new_signal = current_signal
    action_type = "NONE"

    # PHASE 1: Initial entry from FLAT (need strong breakout signal)
    if current_signal is None:
        if breakout_long:
            new_signal = "LONG"
            action_type = "ENTRY"
            logging.info(
                f"🚀 {symbol} BREAKOUT ENTRY LONG "
                f"(price {price:.6f} >= DonchianHIGH {d_high:.6f} & > WMA10 {wma10:.6f} & ADX {adx:.1f}≥{ADX_THRESHOLD})"
            )
        elif breakout_short:
            new_signal = "SHORT"
            action_type = "ENTRY"
            logging.info(
                f"🚀 {symbol} BREAKOUT ENTRY SHORT "
                f"(price {price:.6f} <= DonchianLOW {d_low:.6f} & < WMA10 {wma10:.6f} & ADX {adx:.1f}≥{ADX_THRESHOLD})"
            )
    
    # PHASE 2: Pullback flipping with 1% buffer (faster signals, only 2 conditions)
    elif current_signal == "LONG" and pullback_to_short:
        new_signal = "SHORT"
        action_type = "PULLBACK_FLIP"
        logging.info(
            f"🔄 {symbol} PULLBACK FLIP LONG→SHORT "
            f"(price {price:.6f} < WMA10*0.99 {wma10*0.99:.6f} & ADX {adx:.1f}≥{ADX_THRESHOLD})"
        )
    elif current_signal == "SHORT" and pullback_to_long:
        new_signal = "LONG"
        action_type = "PULLBACK_FLIP"
        logging.info(
            f"🔄 {symbol} PULLBACK FLIP SHORT→LONG "
            f"(price {price:.6f} > WMA10*1.01 {wma10*1.01:.6f} & ADX {adx:.1f}≥{ADX_THRESHOLD})"
        )
    
    # HOLD: All other cases (conditions not met, or price in 1% buffer zone)
    # No action - new_signal stays as current_signal

    # Update state if signal changed
    if new_signal != current_signal:
        st["current_signal"] = new_signal
        st["last_signal_change"] = time.time()
        return {"changed": True, "action": action_type, "signal": new_signal}

    return {"changed": False, "action": "NONE", "signal": current_signal}

# ========================= MAIN LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    """WebSocket price feed; builds 1m candles from markPrice@1s and sets readiness."""
    streams = [f"{s.lower()}@markPrice@1s" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

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

                            # Update 1-minute candles using event time
                            event_time /= 1000
                            minute = int(event_time // 60)
                            klines = state[symbol]["klines"]

                            if not klines or klines[-1]["minute"] != minute:
                                # New minute candle
                                klines.append({
                                    "minute": minute,
                                    "high": price,
                                    "low": price,
                                    "close": price
                                })
                            else:
                                # Update current candle
                                klines[-1]["high"]  = max(klines[-1]["high"],  price)
                                klines[-1]["low"]   = min(klines[-1]["low"],   price)
                                klines[-1]["close"] = price

                            # Readiness: require Donchian(20h) + WMA10 + ADX ≥ threshold
                            if len(klines) >= DONCHIAN_PERIODS and not state[symbol]["ready"]:
                                dlow, dhigh = get_donchian_levels(symbol)
                                wma10 = calculate_wma_close(symbol, WMA10_PERIODS)
                                adx   = calculate_adx(symbol)
                                if (dlow is not None) and (dhigh is not None) and (wma10 is not None) and (adx is not None and adx >= ADX_THRESHOLD):
                                    state[symbol]["ready"] = True
                                    logging.info(
                                        f"✅ {symbol} ready for trading "
                                        f"({len(klines)} candles, WMA10 {wma10:.6f}, ADX {adx:.1f}≥{ADX_THRESHOLD})"
                                    )
                                else:
                                    # ensure ADX keeps updating for display even if not ready yet
                                    calculate_adx(symbol)
                            else:
                                # keep ADX up-to-date (doesn't block trading once ready)
                                calculate_adx(symbol)

                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")

        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)

async def status_logger():
    """5-minute status showing Phase 1 (breakout entry) vs Phase 2 (pullback flipping) conditions"""
    while True:
        await asyncio.sleep(300)  # 5 minutes = 300 seconds

        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"📊 === STATUS REPORT {current_time} ===")

        for symbol in SYMBOLS:
            st = state[symbol]

            if not st["ready"]:
                candle_count = len(st["klines"])
                remaining = max(0, DONCHIAN_PERIODS - candle_count)
                logging.info(f"{symbol}: Not ready - {candle_count}/{DONCHIAN_PERIODS} candles ({remaining} more needed)")
                continue

            price = st["price"]
            d_low, d_high = get_donchian_levels(symbol)
            wma10 = calculate_wma_close(symbol, WMA10_PERIODS)
            adx = st.get("adx")

            if d_low and d_high and price and wma10:
                current_sig = st["current_signal"] or "WAIT"

                logging.info(f"{symbol}: Price={price:.6f} | WMA10={wma10:.6f} | ADX={f'{adx:.1f}' if adx is not None else 'N/A'}")
                logging.info(f"  Donchian: LOW={d_low:.6f} HIGH={d_high:.6f}")
                logging.info(f"  Signal: {current_sig}")

                # Show different conditions based on current phase
                adx_ok         = (adx is not None and adx >= ADX_THRESHOLD)
                breakout_long  = adx_ok and (price >= d_high) and (price > wma10)  # 3 conditions
                breakout_short = adx_ok and (price <= d_low)  and (price < wma10)  # 3 conditions
                pullback_short = (price < wma10 * 0.99) and adx_ok  # 2 conditions with 1% buffer
                pullback_long  = (price > wma10 * 1.01) and adx_ok  # 2 conditions with 1% buffer
                
                if current_sig is None:  # FLAT - waiting for breakout entry
                    logging.info(f"  📍 PHASE 1: Waiting for BREAKOUT ENTRY (need ALL 3 conditions):")
                    logging.info(f"    LONG: price≥DonchianHIGH={price >= d_high} & price>WMA10={price > wma10} & ADX≥{ADX_THRESHOLD}={adx_ok} → {breakout_long}")
                    logging.info(f"    SHORT: price≤DonchianLOW={price <= d_low} & price<WMA10={price < wma10} & ADX≥{ADX_THRESHOLD}={adx_ok} → {breakout_short}")
                    
                    if breakout_long:
                        logging.info(f"    ⚡ BREAKOUT LONG signal ready!")
                    elif breakout_short:
                        logging.info(f"    ⚡ BREAKOUT SHORT signal ready!")
                    else:
                        logging.info(f"    ⏸️ WAITING: Breakout conditions not fully met")
                        
                else:  # IN POSITION - pullback flipping mode with 1% buffer
                    flip_short_trigger = wma10 * 0.99
                    flip_long_trigger = wma10 * 1.01
                    
                    logging.info(f"  📍 PHASE 2: In position, watching for PULLBACK FLIPS with 1% BUFFER:")
                    logging.info(f"    FLIP to SHORT: price<{flip_short_trigger:.6f} (WMA10*0.99) & ADX≥{ADX_THRESHOLD}={adx_ok} → {pullback_short}")
                    logging.info(f"    FLIP to LONG:  price>{flip_long_trigger:.6f} (WMA10*1.01) & ADX≥{ADX_THRESHOLD}={adx_ok} → {pullback_long}")
                    
                    if current_sig == "LONG" and pullback_short:
                        logging.info(f"    ⚡ PULLBACK FLIP signal: LONG→SHORT ready!")
                    elif current_sig == "SHORT" and pullback_long:
                        logging.info(f"    ⚡ PULLBACK FLIP signal: SHORT→LONG ready!")
                    else:
                        buffer_zone = f"${flip_short_trigger:.2f} - ${flip_long_trigger:.2f}"
                        logging.info(f"    ⏸️ HOLDING {current_sig}: Price in buffer zone ({buffer_zone}) or ADX too low")

        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic: breakout entry from FLAT, then pullback flipping forever."""
    while True:
        await asyncio.sleep(0.1)  # 10 FPS

        for symbol in SYMBOLS:
            st = state[symbol]
            if not st["ready"]:
                continue

            # Update trading signals (no-FLAT; only change on explicit long/short)
            signal_result = update_trading_signals(symbol)

            # Calculate target position based on current signal
            target_size = SYMBOLS[symbol]
            current_signal = st["current_signal"]

            if current_signal == "LONG":
                final_position = target_size
            elif current_signal == "SHORT":
                final_position = -target_size
            elif current_signal is None:
                # FLAT: Only at startup, waiting for first breakout entry
                final_position = 0.0
            else:
                # Fallback: keep current position
                final_position = st["current_position"]

            # Execute position change only when signal changed AND target differs
            if signal_result["changed"]:
                current_pos = st["current_position"]
                if abs(final_position - current_pos) > 1e-12:
                    await execute_position_change(client, symbol, final_position, current_pos)

async def execute_position_change(client: AsyncClient, symbol: str, target: float, current: float):
    """Execute position changes with minimal API calls"""
    st = state[symbol]

    # Dedup guard
    now = time.time()
    last_target = st.get("last_target", None)
    last_when = st.get("last_exec_ts", 0.0)
    if last_target is not None and abs(target - last_target) < 1e-12 and (now - last_when) < 2.0:
        logging.info(f"🛡️ {symbol} dedup: skipping duplicate execution to target {target} within 2s window")
        return

    if abs(target - current) < 1e-12:
        return  # nothing to do

    try:
        # FLATTEN to zero (only if target is exactly 0.0 — won't happen from WAIT because we keep position)
        if target == 0.0:
            if current > 0:
                ok = await place_order(client, symbol, "SELL", current, "LONG CLOSE")
                if not ok:
                    logging.warning(f"{symbol} flatten LONG failed; aborting")
                    return
            elif current < 0:
                ok = await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
                if not ok:
                    logging.warning(f"{symbol} flatten SHORT failed; aborting")
                    return

        # TARGET LONG (>0)
        elif target > 0:
            if current < 0:
                ok = await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
                if not ok:
                    logging.warning(f"{symbol} SHORT close failed; aborting LONG open")
                    return
                ok = await place_order(client, symbol, "BUY", target, "LONG ENTRY")
                if not ok:
                    logging.warning(f"{symbol} LONG open failed after closing SHORT")
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

        # TARGET SHORT (<0)
        else:  # target < 0
            if current > 0:
                ok = await place_order(client, symbol, "SELL", current, "LONG CLOSE")
                if not ok:
                    logging.warning(f"{symbol} LONG close failed; aborting SHORT open")
                    return
                ok = await place_order(client, symbol, "SELL", abs(target), "SHORT ENTRY")
                if not ok:
                    logging.warning(f"{symbol} SHORT open failed after closing LONG")
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

        # update state only after successful path
        st["current_position"] = target
        st["last_target"] = target
        st["last_exec_ts"] = now

    except Exception as e:
        logging.error(f"❌ {symbol} position change failed: {e}")

# ========================= INITIALIZATION =========================
async def init_bot(client: AsyncClient):
    """Initialize bot with historical data fetching"""
    logging.info("🔧 Initializing bot with safe historical data fetching...")
    logging.info("📊 Using hardcoded precisions to avoid API calls")
    logging.info("⚙️ Leverage must be set manually via Binance web interface")

    # First, try to load existing data
    load_klines()

    # Check which symbols need historical data
    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        # Readiness requires Donchian+WMA10+ADX (ADX≥25)
        d_ready  = len(klines) >= DONCHIAN_PERIODS and get_donchian_levels(symbol)[0] is not None
        w_ready  = len(klines) >= WMA10_PERIODS and calculate_wma_close(symbol, WMA10_PERIODS) is not None
        a_value  = calculate_adx(symbol)  # compute ADX if possible
        a_ready  = (a_value is not None and a_value >= ADX_THRESHOLD)

        if d_ready and w_ready and a_ready:
            state[symbol]["ready"] = True
            wma_val = state[symbol]["wma10"]
            logging.info(f"✅ {symbol} ready for trading ({len(klines)} candles, WMA10: {wma_val:.6f}, ADX {a_value:.1f}≥{ADX_THRESHOLD}) from loaded data")
        else:
            symbols_needing_data.append(symbol)
            logging.info(f"📥 {symbol} needs historical data ({len(klines)}/{DONCHIAN_PERIODS} candles)")

    # Fetch historical data for symbols that need it
    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        logging.info("⚠️ Using ultra-conservative rate limiting (2-minute delays between symbols)")

        successful_fetches = 0

        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching historical data for {symbol} ({i+1}/{len(symbols_needing_data)})...")

                klines_data = await safe_api_call(
                    client.futures_mark_price_klines,
                    symbol=symbol,
                    interval="1m",
                    limit=max(DONCHIAN_PERIODS, WMA10_PERIODS) + 50
                )

                # Process the data
                st = state[symbol]
                st["klines"].clear()

                for kline in klines_data:
                    minute = int(float(kline[0]) / 1000 // 60)
                    st["klines"].append({
                        "minute": minute,
                        "high": float(kline[2]),
                        "low":  float(kline[3]),
                        "close": float(kline[4])
                    })

                # Mark as ready if key indicators are available (with ADX ≥ threshold)
                d_ok  = get_donchian_levels(symbol)[0] is not None
                w_ok  = calculate_wma_close(symbol, WMA10_PERIODS) is not None
                a_val = calculate_adx(symbol)
                a_ok  = (a_val is not None and a_val >= ADX_THRESHOLD)

                if d_ok and w_ok and a_ok:
                    st["ready"] = True
                    logging.info(f"✅ {symbol} ready for trading ({len(st['klines'])} candles, WMA10: {st['wma10']:.6f}, ADX {a_val:.1f}≥{ADX_THRESHOLD}) from API")
                    successful_fetches += 1
                else:
                    logging.warning(f"⚠️ {symbol} insufficient data received ({len(st['klines'])} candles)")

                # Ultra-conservative delay between symbols
                if i < len(symbols_needing_data) - 1:
                    logging.info(f"⏳ Waiting 120 seconds before next symbol to avoid rate limits...")
                    await asyncio.sleep(120)

            except Exception as e:
                logging.error(f"❌ {symbol} historical data fetch failed: {e}")
                logging.info(f"📡 {symbol} will build data from WebSocket feed instead")

                if i < len(symbols_needing_data) - 1:
                    await asyncio.sleep(120)

        logging.info(f"📊 Historical data fetch complete: {successful_fetches}/{len(symbols_needing_data)} successful")

        # Show final status
        ready_count = sum(1 for symbol in SYMBOLS if state[symbol]["ready"])
        pending_count = len(SYMBOLS) - ready_count

        if ready_count > 0:
            logging.info(f"🚀 {ready_count} symbols ready to trade immediately")

        if pending_count > 0:
            logging.info(f"⏳ {pending_count} symbols will build data from live WebSocket feed")

    else:
        logging.info("🎯 All symbols ready from loaded data - no API calls needed!")

    # Save the data we have
    save_klines()

    await asyncio.sleep(2)
    logging.info("🚀 Initialization complete - Starting WebSocket feeds")

# ========================= MAIN =========================
async def main():
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing Binance API credentials in .env")

    client = await AsyncClient.create(API_KEY, API_SECRET)

    atexit.register(save_klines)

    try:
        await init_bot(client)

        # Start three concurrent tasks
        price_task  = asyncio.create_task(price_feed_loop(client))
        trade_task  = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())

        logging.info("🚀 Bot started - Price feed, Trading logic, and Status reporting active")

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
    print("🤖 BREAKOUT ENTRY → PULLBACK FLIPPING BOT (with 1% Buffer)")
    print("=" * 80)
    print("📍 PHASE 1 - BREAKOUT ENTRY (from FLAT, need ALL 3 conditions):")
    print("   🚀 LONG:  price ≥ 20h Donchian HIGH  AND price > 10h WMA  AND ADX ≥ 25")
    print("   🚀 SHORT: price ≤ 20h Donchian LOW   AND price < 10h WMA  AND ADX ≥ 25")
    print("")
    print("📍 PHASE 2 - PULLBACK FLIPPING with 1% BUFFER (need only 2 conditions):")
    print("   🔄 LONG → SHORT: price < 10h WMA * 0.99  AND  ADX ≥ 25")
    print("   🔄 SHORT → LONG: price > 10h WMA * 1.01  AND  ADX ≥ 25")
    print("")
    print("🛡️ 0.5% BUFFER prevents whipsaw - creates 'dead zone' around WMA10")
    print("💡 STRATEGY: Strong breakout to enter, buffered pullback signals to flip")
    print("❌ NEVER go FLAT again after first entry - always LONG or SHORT!")
    print("⏱️ Live checks via markPrice@1s; 1m candles drive indicators")
    print("📊 Data persistence enabled - faster restarts after first run")
    print(f"📊 Symbols: {list(SYMBOLS.keys())}")
    print("=" * 80)

    asyncio.run(main())
