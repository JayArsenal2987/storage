#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time, math
import atexit
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple
from dotenv import load_dotenv

# ========================= CONFIG =========================
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE = int(os.getenv("LEVERAGE", "10"))

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

DONCHIAN_PERIODS = 1200
ADX_PERIODS      = 1200
ADX_THRESHOLD    = 25
RSI_PERIODS      = 14
RSI_SHORT_THRESHOLD = 49
RSI_LONG_THRESHOLD = 51
KLINE_LIMIT      = 1300

# ========================= ADJUSTABLE BUFFERS =========================
ENTRY_BUFFER_PERCENT = float(os.getenv("ENTRY_BUFFER_PERCENT", "0.30"))
EXIT_BUFFER_PERCENT = float(os.getenv("EXIT_BUFFER_PERCENT", "0.40"))

if EXIT_BUFFER_PERCENT <= ENTRY_BUFFER_PERCENT:
    raise ValueError(f"EXIT_BUFFER_PERCENT ({EXIT_BUFFER_PERCENT}) must be greater than ENTRY_BUFFER_PERCENT ({ENTRY_BUFFER_PERCENT})")
if ENTRY_BUFFER_PERCENT < 0 or ENTRY_BUFFER_PERCENT > 1:
    raise ValueError(f"ENTRY_BUFFER_PERCENT must be between 0 and 1, got {ENTRY_BUFFER_PERCENT}")
if EXIT_BUFFER_PERCENT < 0 or EXIT_BUFFER_PERCENT > 1:
    raise ValueError(f"EXIT_BUFFER_PERCENT must be between 0 and 1, got {EXIT_BUFFER_PERCENT}")

SAFETY_MARGIN_PERCENT = EXIT_BUFFER_PERCENT - ENTRY_BUFFER_PERCENT

# Daily PNL limits
DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", "200.0"))
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "-100.0"))

# ========================= SQUEEZE MOMENTUM CONFIG =========================
ENABLE_BOUNCE_MODE = os.getenv("ENABLE_BOUNCE_MODE", "true").lower() == "true"
SQUEEZE_LENGTH = 20
SQUEEZE_BB_MULT = 2.0
SQUEEZE_KC_MULT = 1.5
USE_TRUE_RANGE = True

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "current_signal": None,
        "last_signal_change": 0,
        "current_position": 0.0,
        "adx": None,
        "rsi": None,
        "adx_ready": False,
        "ready": False,
        "last_exec_ts": 0.0,
        "last_target": None,
        "zone_info": None,
        "daily_pnl": 0.0,
        "daily_pnl_reset_time": time.time(),
        "daily_limit_reached": False,
        "entry_price": None,
        "squeeze_momentum": None,
        "momentum_history": [],
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

# ========================= BASIC INDICATORS =========================
def get_donchian_levels(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    klines = state[symbol]["klines"]
    if len(klines) < DONCHIAN_PERIODS:
        return None, None
    recent = list(klines)[:-1][-DONCHIAN_PERIODS:]
    if not recent:
        return None, None
    highs = [k["high"] for k in recent]
    lows  = [k["low"]  for k in recent]
    return min(lows), max(highs)

def calculate_rsi(symbol: str, period: int = 14) -> Optional[float]:
    klines = state[symbol]["klines"]
    if len(klines) < period + 2:
        return None
    completed = list(klines)[:-1]
    if len(completed) < period + 1:
        return None
    closes = [k["close"] for k in completed[-(period + 1):]]
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
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    state[symbol]["rsi"] = rsi
    return rsi

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

# ========================= SQUEEZE MOMENTUM FUNCTIONS =========================
def calculate_sma(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def calculate_std_dev(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    recent = values[-period:]
    mean = sum(recent) / period
    variance = sum((x - mean) ** 2 for x in recent) / period
    return math.sqrt(variance)

def calculate_linear_regression(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    recent = values[-period:]
    n = len(recent)
    sum_x = sum(range(n))
    sum_y = sum(recent)
    sum_xy = sum(i * recent[i] for i in range(n))
    sum_x2 = sum(i * i for i in range(n))
    denominator = (n * sum_x2 - sum_x * sum_x)
    if denominator == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n
    return intercept + slope * (n - 1)

def calculate_squeeze_momentum(symbol: str) -> Optional[dict]:
    klines = state[symbol]["klines"]
    if len(klines) < SQUEEZE_LENGTH + 1:
        return None
    completed = list(klines)[:-1]
    if len(completed) < SQUEEZE_LENGTH + 1:
        return None
    recent = completed[-SQUEEZE_LENGTH:]
    closes = [k["close"] for k in recent]
    highs = [k["high"] for k in recent]
    lows = [k["low"] for k in recent]
    
    basis = calculate_sma(closes, SQUEEZE_LENGTH)
    if basis is None:
        return None
    std_dev = calculate_std_dev(closes, SQUEEZE_LENGTH)
    if std_dev is None:
        return None
    upper_bb = basis + (SQUEEZE_BB_MULT * std_dev)
    lower_bb = basis - (SQUEEZE_BB_MULT * std_dev)
    
    ma = calculate_sma(closes, SQUEEZE_LENGTH)
    if ma is None:
        return None
    
    if USE_TRUE_RANGE:
        tr_values = []
        for i in range(1, len(recent)):
            high = recent[i]["high"]
            low = recent[i]["low"]
            prev_close = recent[i-1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
        if len(tr_values) < SQUEEZE_LENGTH - 1:
            return None
        range_ma = sum(tr_values[-(SQUEEZE_LENGTH-1):]) / (SQUEEZE_LENGTH - 1)
    else:
        ranges = [h - l for h, l in zip(highs, lows)]
        range_ma = calculate_sma(ranges, SQUEEZE_LENGTH)
        if range_ma is None:
            return None
    
    upper_kc = ma + (range_ma * SQUEEZE_KC_MULT)
    lower_kc = ma - (range_ma * SQUEEZE_KC_MULT)
    
    squeeze_on = (lower_bb > lower_kc) and (upper_bb < upper_kc)
    squeeze_off = (lower_bb < lower_kc) and (upper_bb > upper_kc)
    no_squeeze = not squeeze_on and not squeeze_off
    
    highest_high = max(highs)
    lowest_low = min(lows)
    mid_channel = (highest_high + lowest_low) / 2
    avg_mid = (mid_channel + ma) / 2
    momentum_source = [c - avg_mid for c in closes]
    momentum = calculate_linear_regression(momentum_source, SQUEEZE_LENGTH)
    
    if momentum is None:
        return None
    
    return {
        "squeeze_on": squeeze_on,
        "squeeze_off": squeeze_off,
        "no_squeeze": no_squeeze,
        "momentum": momentum
    }

def get_momentum_color(momentum: float, prev_momentum: Optional[float]) -> str:
    if momentum > 0:
        if prev_momentum is None:
            return "GREEN"
        return "LIME" if momentum > prev_momentum else "GREEN"
    else:
        if prev_momentum is None:
            return "MAROON"
        return "RED" if momentum < prev_momentum else "MAROON"

def update_momentum_history(symbol: str, momentum: float, max_history: int = 10):
    state[symbol]["momentum_history"].append(momentum)
    if len(state[symbol]["momentum_history"]) > max_history:
        state[symbol]["momentum_history"].pop(0)

def detect_bounce_mode(symbol: str) -> Optional[dict]:
    momentum_history = state[symbol]["momentum_history"]
    if len(momentum_history) < 2:
        return None
    current_momentum = momentum_history[-1]
    prev_momentum = momentum_history[-2]
    prev_prev_momentum = momentum_history[-3] if len(momentum_history) >= 3 else None
    
    current_color = get_momentum_color(current_momentum, prev_momentum)
    prev_color = get_momentum_color(prev_momentum, prev_prev_momentum)
    
    bounce_type = None
    if current_color == "LIME" and prev_color == "MAROON":
        bounce_type = "BULLISH"
    elif current_color == "RED" and prev_color == "GREEN":
        bounce_type = "BEARISH"
    
    return {
        "bounce_type": bounce_type,
        "current_color": current_color,
        "prev_color": prev_color,
        "momentum": current_momentum
    }

def check_combined_entry_conditions(symbol: str, direction: str, rsi: float, adx: float) -> Tuple[bool, str]:
    if adx < ADX_THRESHOLD:
        return False, f"ADX too low ({adx:.1f} < {ADX_THRESHOLD})"
    
    rsi_ok = False
    rsi_reason = ""
    
    if direction == "LONG":
        rsi_ok = rsi > RSI_LONG_THRESHOLD
        rsi_reason = f"RSI {rsi:.1f} {'>' if rsi_ok else '<='} {RSI_LONG_THRESHOLD}"
    elif direction == "SHORT":
        rsi_ok = rsi < RSI_SHORT_THRESHOLD
        rsi_reason = f"RSI {rsi:.1f} {'<' if rsi_ok else '>='} {RSI_SHORT_THRESHOLD}"
    
    if not ENABLE_BOUNCE_MODE:
        if rsi_ok:
            return True, f"RSI filter passed: {rsi_reason}"
        else:
            return False, f"RSI filter failed: {rsi_reason}"
    
    squeeze_data = state[symbol]["squeeze_momentum"]
    if squeeze_data is None:
        if rsi_ok:
            return True, f"RSI only (no squeeze data): {rsi_reason}"
        else:
            return False, f"RSI failed (no bounce data): {rsi_reason}"
    
    bounce_data = detect_bounce_mode(symbol)
    if bounce_data is None:
        if rsi_ok:
            return True, f"RSI only (insufficient bounce history): {rsi_reason}"
        else:
            return False, f"RSI failed: {rsi_reason}"
    
    bounce_type = bounce_data["bounce_type"]
    bounce_color = bounce_data["current_color"]
    
    if direction == "LONG":
        bounce_ok = (bounce_type == "BULLISH" or bounce_color == "LIME")
        if rsi_ok and bounce_ok:
            return True, f"STRONG LONG: RSI {rsi:.1f} + Bounce {bounce_color}/{bounce_type or 'None'}"
        elif rsi_ok and not bounce_ok:
            if bounce_color == "RED" or bounce_type == "BEARISH":
                return False, f"RSI ok but BEARISH bounce ({bounce_color}/{bounce_type})"
            return True, f"RSI ok, neutral bounce: {rsi_reason}"
        elif not rsi_ok and bounce_ok:
            return True, f"BOUNCE override: {bounce_color}/{bounce_type} (RSI {rsi:.1f})"
        else:
            return False, f"Both failed: RSI {rsi:.1f} and bounce {bounce_color}"
    
    elif direction == "SHORT":
        bounce_ok = (bounce_type == "BEARISH" or bounce_color == "RED")
        if rsi_ok and bounce_ok:
            return True, f"STRONG SHORT: RSI {rsi:.1f} + Bounce {bounce_color}/{bounce_type or 'None'}"
        elif rsi_ok and not bounce_ok:
            if bounce_color == "LIME" or bounce_type == "BULLISH":
                return False, f"RSI ok but BULLISH bounce ({bounce_color}/{bounce_type})"
            return True, f"RSI ok, neutral bounce: {rsi_reason}"
        elif not rsi_ok and bounce_ok:
            return True, f"BOUNCE override: {bounce_color}/{bounce_type} (RSI {rsi:.1f})"
        else:
            return False, f"Both failed: RSI {rsi:.1f} and bounce {bounce_color}"
    
    return False, "Unknown direction"

def get_filter_status(symbol: str, rsi: float) -> str:
    if not ENABLE_BOUNCE_MODE:
        return f"RSI: {rsi:.1f} (Bounce: Disabled)"
    squeeze_data = state[symbol]["squeeze_momentum"]
    if squeeze_data is None:
        return f"RSI: {rsi:.1f} | Bounce: No data"
    bounce_data = detect_bounce_mode(symbol)
    if bounce_data is None:
        return f"RSI: {rsi:.1f} | Bounce: Insufficient history"
    bounce_color = bounce_data["current_color"]
    bounce_type = bounce_data["bounce_type"] or "None"
    squeeze_state = "ON" if squeeze_data["squeeze_on"] else ("OFF" if squeeze_data["squeeze_off"] else "Normal")
    return f"RSI: {rsi:.1f} | Bounce: {bounce_color}/{bounce_type} | Squeeze: {squeeze_state}"

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
    
    d_low, d_high = get_donchian_levels(symbol)
    adx = calculate_adx(symbol)
    rsi = calculate_rsi(symbol, RSI_PERIODS)
    
    # Calculate squeeze momentum
    squeeze_data = calculate_squeeze_momentum(symbol)
    if squeeze_data:
        st["squeeze_momentum"] = squeeze_data
        update_momentum_history(symbol, squeeze_data["momentum"])
    
    if d_low is None or d_high is None or adx is None or rsi is None:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    channel_height = d_high - d_low
    buffer_entry = channel_height * ENTRY_BUFFER_PERCENT
    buffer_exit = channel_height * EXIT_BUFFER_PERCENT
    
    short_entry_upper = d_low + buffer_entry
    long_entry_lower = d_high - buffer_entry
    short_exit_upper = d_low + buffer_exit
    long_exit_lower = d_high - buffer_exit
    
    in_short_entry_zone = price <= short_entry_upper
    in_long_entry_zone = price >= long_entry_lower
    in_blocking_gap = price > short_exit_upper and price < long_exit_lower
    
    short_should_exit = price > short_exit_upper
    long_should_exit = price < long_exit_lower
    
    st["zone_info"] = {
        "in_short_entry_zone": in_short_entry_zone,
        "in_long_entry_zone": in_long_entry_zone,
        "in_blocking_gap": in_blocking_gap,
        "short_entry_upper": short_entry_upper,
        "short_exit_upper": short_exit_upper,
        "long_entry_lower": long_entry_lower,
        "long_exit_lower": long_exit_lower
    }
    
    new_signal = current_signal
    action_type = "NONE"
    
    if current_signal is None:
        if in_short_entry_zone:
            allowed, reason = check_combined_entry_conditions(symbol, "SHORT", rsi, adx)
            if allowed:
                new_signal = "SHORT"
                action_type = "ENTRY"
                logging.info(f"{symbol} ENTRY SHORT - {reason}")
            else:
                logging.debug(f"{symbol} SHORT blocked: {reason}")
        
        elif in_long_entry_zone:
            allowed, reason = check_combined_entry_conditions(symbol, "LONG", rsi, adx)
            if allowed:
                new_signal = "LONG"
                action_type = "ENTRY"
                logging.info(f"{symbol} ENTRY LONG - {reason}")
            else:
                logging.debug(f"{symbol} LONG blocked: {reason}")
        
        elif price <= d_low:
            allowed, reason = check_combined_entry_conditions(symbol, "SHORT", rsi, adx)
            if allowed:
                new_signal = "SHORT"
                action_type = "BREAKOUT"
                logging.info(f"{symbol} BREAKOUT SHORT - {reason}")
        
        elif price >= d_high:
            allowed, reason = check_combined_entry_conditions(symbol, "LONG", rsi, adx)
            if allowed:
                new_signal = "LONG"
                action_type = "BREAKOUT"
                logging.info(f"{symbol} BREAKOUT LONG - {reason}")
    
    elif current_signal == "LONG":
        if long_should_exit:
            new_signal = None
            action_type = "EXIT"
            logging.info(f"{symbol} EXIT LONG (price {price:.6f} < exit threshold {long_exit_lower:.6f})")
    
    elif current_signal == "SHORT":
        if short_should_exit:
            new_signal = None
            action_type = "EXIT"
            logging.info(f"{symbol} EXIT SHORT (price {price:.6f} > exit threshold {short_exit_upper:.6f})")
    
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
                            if len(klines) >= DONCHIAN_PERIODS and not state[symbol]["ready"]:
                                dlow, dhigh = get_donchian_levels(symbol)
                                adx = calculate_adx(symbol)
                                rsi = calculate_rsi(symbol, RSI_PERIODS)
                                if (dlow is not None) and (dhigh is not None) and (adx is not None and adx >= ADX_THRESHOLD) and (rsi is not None):
                                    state[symbol]["ready"] = True
                                    logging.info(f"{symbol} ready ({len(klines)} candles, ADX {adx:.1f}>={ADX_THRESHOLD}, RSI {rsi:.1f})")
                                else:
                                    calculate_adx(symbol)
                                    calculate_rsi(symbol, RSI_PERIODS)
                            else:
                                calculate_adx(symbol)
                                calculate_rsi(symbol, RSI_PERIODS)
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
                remaining = max(0, DONCHIAN_PERIODS - candle_count)
                logging.info(f"{symbol}: Not ready - {candle_count}/{DONCHIAN_PERIODS} candles ({remaining} more needed)")
                continue
            price = st["price"]
            d_low, d_high = get_donchian_levels(symbol)
            adx = st.get("adx")
            rsi = st.get("rsi")
            zone_info = st.get("zone_info")
            
            if d_low and d_high and price and zone_info:
                current_sig = st["current_signal"]
                display_sig = current_sig or "FLAT"
                
                logging.info(f"{symbol}: Price={price:.6f} | ADX={f'{adx:.1f}' if adx is not None else 'N/A'}")
                logging.info(f"  Donchian: LOW={d_low:.6f} HIGH={d_high:.6f}")
                logging.info(f"  Signal: {display_sig}")
                
                # Display combined filter status
                if rsi is not None:
                    filter_status = get_filter_status(symbol, rsi)
                    logging.info(f"  Filters: {filter_status}")
                
                daily_pnl_color = "+" if st["daily_pnl"] >= 0 else ""
                logging.info(f"  Daily PNL: {daily_pnl_color}{st['daily_pnl']:.2f}% (Target: +{DAILY_PROFIT_TARGET}% / Limit: {DAILY_LOSS_LIMIT}%)")
                
                if st["daily_limit_reached"]:
                    if st["daily_pnl"] >= DAILY_PROFIT_TARGET:
                        logging.info(f"  STATUS: PROFIT TARGET REACHED - Trading paused until next day")
                    else:
                        logging.info(f"  STATUS: LOSS LIMIT HIT - Trading paused until next day")
                else:
                    entry_pct = int(ENTRY_BUFFER_PERCENT * 100)
                    exit_pct = int(EXIT_BUFFER_PERCENT * 100)
                    logging.info(f"  Zones:")
                    logging.info(f"    SHORT entry: <={zone_info['short_entry_upper']:.6f} (LOW to LOW+{entry_pct}%)")
                    logging.info(f"    SHORT exit: >{zone_info['short_exit_upper']:.6f} (LOW+{exit_pct}%)")
                    logging.info(f"    BLOCKING GAP: {zone_info['short_exit_upper']:.6f} to {zone_info['long_exit_lower']:.6f}")
                    logging.info(f"    LONG exit: <{zone_info['long_exit_lower']:.6f} (HIGH-{exit_pct}%)")
                    logging.info(f"    LONG entry: >={zone_info['long_entry_lower']:.6f} (HIGH-{entry_pct}% to HIGH)")
                    logging.info(f"  Current location:")
                    logging.info(f"    In SHORT entry zone: {zone_info['in_short_entry_zone']}")
                    logging.info(f"    In BLOCKING GAP: {zone_info['in_blocking_gap']}")
                    logging.info(f"    In LONG entry zone: {zone_info['in_long_entry_zone']}")
                    
                    if current_sig is None:
                        logging.info(f"  FLAT - Waiting for entry zone with filter confirmation")
                    elif current_sig == "LONG":
                        current_pnl = calculate_pnl_percent(symbol, price)
                        logging.info(f"  LONG - Current PNL: {current_pnl:+.2f}% | Will exit if price <{zone_info['long_exit_lower']:.6f}")
                    elif current_sig == "SHORT":
                        current_pnl = calculate_pnl_percent(symbol, price)
                        logging.info(f"  SHORT - Current PNL: {current_pnl:+.2f}% | Will exit if price >{zone_info['short_exit_upper']:.6f}")
        
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
        d_ready = len(klines) >= DONCHIAN_PERIODS and get_donchian_levels(symbol)[0] is not None
        a_value = calculate_adx(symbol)
        r_value = calculate_rsi(symbol, RSI_PERIODS)
        a_ready = (a_value is not None and a_value >= ADX_THRESHOLD)
        r_ready = (r_value is not None)
        if d_ready and a_ready and r_ready:
            state[symbol]["ready"] = True
            logging.info(f"{symbol} ready ({len(klines)} candles, ADX {a_value:.1f}>={ADX_THRESHOLD}, RSI {r_value:.1f})")
        else:
            symbols_needing_data.append(symbol)
            logging.info(f"{symbol} needs data ({len(klines)}/{DONCHIAN_PERIODS} candles)")
    if symbols_needing_data:
        logging.info(f"Fetching historical data for {len(symbols_needing_data)} symbols...")
        successful_fetches = 0
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"Fetching {symbol} ({i+1}/{len(symbols_needing_data)})...")
                klines_data = await safe_api_call(client.futures_mark_price_klines, symbol=symbol, interval="1m", limit=DONCHIAN_PERIODS + 50)
                st = state[symbol]
                st["klines"].clear()
                for kline in klines_data:
                    minute = int(float(kline[0]) / 1000 // 60)
                    st["klines"].append({"minute": minute, "high": float(kline[2]), "low": float(kline[3]), "close": float(kline[4])})
                d_ok = get_donchian_levels(symbol)[0] is not None
                a_val = calculate_adx(symbol)
                r_val = calculate_rsi(symbol, RSI_PERIODS)
                a_ok = (a_val is not None and a_val >= ADX_THRESHOLD)
                r_ok = (r_val is not None)
                if d_ok and a_ok and r_ok:
                    st["ready"] = True
                    logging.info(f"{symbol} ready ({len(st['klines'])} candles, ADX {a_val:.1f}, RSI {r_val:.1f})")
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
    client = await AsyncClient.create(API_KEY, API_SECRET)
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
    print("DONCHIAN + ADX + RSI + BOUNCE MODE BOT")
    print("=" * 80)
    print("STRATEGY: Counter-Trend with Combined RSI + Momentum Bounce Filter")
    print("")
    print("BUFFER CONFIGURATION:")
    print(f"  Entry Buffer: {ENTRY_BUFFER_PERCENT*100:.0f}% from channel edge")
    print(f"  Exit Buffer: {EXIT_BUFFER_PERCENT*100:.0f}% from channel edge")
    print(f"  Safety Margin: {SAFETY_MARGIN_PERCENT*100:.0f}% (prevents oscillation)")
    print("")
    print("ENTRY FILTERS:")
    print(f"  ADX >= {ADX_THRESHOLD} (required)")
    print(f"  RSI < {RSI_SHORT_THRESHOLD} for SHORT | RSI > {RSI_LONG_THRESHOLD} for LONG")
    if ENABLE_BOUNCE_MODE:
        print(f"  Bounce Mode: ENABLED")
        print("    - LIME after MAROON = Bullish bounce (allows/boosts LONG)")
        print("    - RED after GREEN = Bearish bounce (allows/boosts SHORT)")
        print("    - Bounce can override weak RSI signals")
        print("    - Contradicting bounces block entries")
    else:
        print(f"  Bounce Mode: DISABLED (RSI only)")
    print("")
    print("ENTRY/EXIT ZONES:")
    print(f"  SHORT: Entry <={ENTRY_BUFFER_PERCENT*100:.0f}% | Exit >{EXIT_BUFFER_PERCENT*100:.0f}%")
    print(f"  LONG: Entry >={100-ENTRY_BUFFER_PERCENT*100:.0f}% | Exit <{100-EXIT_BUFFER_PERCENT*100:.0f}%")
    print("")
    print("DAILY PNL LIMITS:")
    print(f"  Profit Target: +{DAILY_PROFIT_TARGET}% → Stop trading")
    print(f"  Loss Limit: {DAILY_LOSS_LIMIT}% → Stop trading")
    print("  Resets: Midnight UTC")
    print("")
    print("CONFIGURATION (.env):")
    print("  ENTRY_BUFFER_PERCENT=0.30")
    print("  EXIT_BUFFER_PERCENT=0.40")
    print("  ENABLE_BOUNCE_MODE=true")
    print("  DAILY_PROFIT_TARGET=200.0")
    print("  DAILY_LOSS_LIMIT=-100.0")
    print("")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print(f"Leverage: {LEVERAGE}x")
    print(f"Donchian: {DONCHIAN_PERIODS}min | ADX: {ADX_PERIODS}min | RSI: {RSI_PERIODS}min")
    print(f"Squeeze: {SQUEEZE_LENGTH}min | BB: {SQUEEZE_BB_MULT}x | KC: {SQUEEZE_KC_MULT}x")
    print("=" * 80)
    asyncio.run(main())
