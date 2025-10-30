#!/usr/bin/env python3
import os, json, asyncio, logging, websockets, time, math
import atexit
from binance import AsyncClient
from collections import deque
from typing import Optional, Dict, Any
from dotenv import load_dotenv

# ========================= CONFIG =========================
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE = int(os.getenv("LEVERAGE", "50"))
USE_LIVE_CANDLE = True

# ========================= HEDGE MODE TOGGLE =========================
USE_HEDGE_MODE = True  # NEW: Enable true hedge mode (both positions simultaneously)

# ========================= ENTRY MODE TOGGLE =========================
USE_CROSSOVER_ENTRY = False

# TAMA (Triple-Layer Adaptive Moving Average) PARAMETERS
USE_TAMA = True

# Layer 1: Kalman Filter Parameters
KALMAN_Q = 0.001
KALMAN_R = 0.01

# Layer 2: JMA Parameters
JMA_LENGTH_FAST = 4
JMA_LENGTH_SLOW = 10
JMA_PHASE = 0
JMA_POWER = 3

# Layer 3: Hurst + FDI Parameters (REPLACES ER)
HURST_PERIODS_FAST = 30
HURST_PERIODS_SLOW = 100
FDI_PERIODS_FAST = 10
FDI_PERIODS_SLOW = 30
ALPHA_WEIGHT = 1.0

# Layer 4: CMA (Corrected Moving Average) Parameters
CMA_PERIOD = 10  # Period for correcting TAMA Slow

# Trailing Stop Configuration - Asymmetric
TRAILING_GAIN_PERCENT = 1.5
TRAILING_LOSS_PERCENT = 0.8

# Timeframe configuration
BASE_TIMEFRAME = "15m"

# Validate timeframe
SUPPORTED_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]
if BASE_TIMEFRAME not in SUPPORTED_TIMEFRAMES:
    raise ValueError(f"Unsupported BASE_TIMEFRAME")

if BASE_TIMEFRAME == "1m":
    BASE_MINUTES = 1
elif BASE_TIMEFRAME == "3m":
    BASE_MINUTES = 3
elif BASE_TIMEFRAME == "5m":
    BASE_MINUTES = 5
elif BASE_TIMEFRAME == "15m":
    BASE_MINUTES = 15
elif BASE_TIMEFRAME == "30m":
    BASE_MINUTES = 30
elif BASE_TIMEFRAME == "1h":
    BASE_MINUTES = 60
elif BASE_TIMEFRAME == "2h":
    BASE_MINUTES = 120
elif BASE_TIMEFRAME == "4h":
    BASE_MINUTES = 240
elif BASE_TIMEFRAME == "6h":
    BASE_MINUTES = 360
elif BASE_TIMEFRAME == "8h":
    BASE_MINUTES = 480
elif BASE_TIMEFRAME == "12h":
    BASE_MINUTES = 720
elif BASE_TIMEFRAME == "1d":
    BASE_MINUTES = 1440
else:
    raise ValueError("Unsupported BASE_TIMEFRAME")

SYMBOLS = {"SOLUSDT": 0.1}
PRECISIONS = {"SOLUSDT": 3}
MA_PERIODS = max(JMA_LENGTH_FAST, JMA_LENGTH_SLOW)
KLINE_LIMIT = max(100, MA_PERIODS + 100, HURST_PERIODS_FAST + 100, HURST_PERIODS_SLOW + 100)

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "kalman_x": None,
        "kalman_p": 1.0,
        "kalman_close": None,
        "jma_fast": None,
        "jma_slow": None,
        "tama_fast": None,
        "tama_slow": None,
        "cma_slow": None,
        "prev_tama_fast": None,
        "prev_cma_slow": None,
        "hurst_fast": None,
        "hurst_slow": None,
        "fdi_fast": None,
        "fdi_slow": None,
        "combined_efficiency_fast": None,
        "combined_efficiency_slow": None,
        "market_regime": None,
        "ready": False,
        "long_position": 0.0,
        "long_entry_price": None,
        "long_trailing_stop_price": None,
        "long_peak_price": None,
        "last_long_exec_ts": 0.0,
        "long_entry_allowed": True,
        "long_signal_active": False,
        "short_position": 0.0,
        "short_entry_price": None,
        "short_trailing_stop_price": None,
        "short_lowest_price": None,
        "last_short_exec_ts": 0.0,
        "short_entry_allowed": True,
        "short_signal_active": False,
        "stop_warning_logged": False,
        "last_long_exit_signal_ts": 0.0,
        "last_short_exit_signal_ts": 0.0,
        "last_signal_check_time": 0.0,
    }
    for symbol in SYMBOLS
}

api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE =========================
def save_klines():
    try:
        save_data = {sym: list(state[sym]["klines"]) for sym in SYMBOLS}
        if os.path.exists('klines.json'):
            try:
                os.rename('klines.json', 'klines.json.backup')
            except Exception:
                pass
        with open('klines.json', 'w') as f:
            json.dump(save_data, f)
        logging.info("📥 Saved klines")
    except Exception as e:
        logging.error(f"Failed to save klines: {e}")

def load_klines():
    try:
        with open('klines.json', 'r') as f:
            load_data = json.load(f)
        if not isinstance(load_data, dict):
            return
        for sym in SYMBOLS:
            if sym in load_data and isinstance(load_data[sym], list):
                state[sym]["klines"] = deque(load_data[sym], maxlen=KLINE_LIMIT)
        logging.info("📤 Loaded klines")
    except FileNotFoundError:
        logging.info("No klines.json")
    except json.JSONDecodeError:
        logging.error("Corrupt klines.json")
    except Exception as e:
        logging.error(f"Failed to load klines: {e}")

def save_positions():
    try:
        position_data = {}
        for sym in SYMBOLS:
            position_data[sym] = {
                "long_position": float(state[sym]["long_position"]),
                "long_entry_price": float(state[sym]["long_entry_price"]) if state[sym]["long_entry_price"] is not None else None,
                "long_trailing_stop_price": float(state[sym]["long_trailing_stop_price"]) if state[sym]["long_trailing_stop_price"] is not None else None,
                "long_peak_price": float(state[sym]["long_peak_price"]) if state[sym]["long_peak_price"] is not None else None,
                "long_entry_allowed": bool(state[sym]["long_entry_allowed"]),
                "short_position": float(state[sym]["short_position"]),
                "short_entry_price": float(state[sym]["short_entry_price"]) if state[sym]["short_entry_price"] is not None else None,
                "short_trailing_stop_price": float(state[sym]["short_trailing_stop_price"]) if state[sym]["short_trailing_stop_price"] is not None else None,
                "short_lowest_price": float(state[sym]["short_lowest_price"]) if state[sym]["short_lowest_price"] is not None else None,
                "short_entry_allowed": bool(state[sym]["short_entry_allowed"]),
            }
        if os.path.exists('positions.json'):
            try:
                os.rename('positions.json', 'positions.json.backup')
            except Exception:
                pass
        with open('positions.json', 'w') as f:
            json.dump(position_data, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save positions: {e}")

def load_positions():
    try:
        with open('positions.json', 'r') as f:
            position_data = json.load(f)
        if not isinstance(position_data, dict):
            return
        logging.info("💾 Loading positions...")
        for sym in SYMBOLS:
            if sym not in position_data:
                continue
            try:
                if not isinstance(position_data[sym], dict):
                    continue
                long_pos = position_data[sym].get("long_position", 0.0)
                short_pos = position_data[sym].get("short_position", 0.0)
                loaded_long = float(long_pos) if long_pos is not None else 0.0
                loaded_short = float(short_pos) if short_pos is not None else 0.0
                state[sym]["long_position"] = loaded_long
                state[sym]["long_entry_price"] = position_data[sym].get("long_entry_price")
                state[sym]["long_trailing_stop_price"] = position_data[sym].get("long_trailing_stop_price")
                state[sym]["long_peak_price"] = position_data[sym].get("long_peak_price")
                state[sym]["long_entry_allowed"] = bool(position_data[sym].get("long_entry_allowed", True))
                state[sym]["short_position"] = loaded_short
                state[sym]["short_entry_price"] = position_data[sym].get("short_entry_price")
                state[sym]["short_trailing_stop_price"] = position_data[sym].get("short_trailing_stop_price")
                state[sym]["short_lowest_price"] = position_data[sym].get("short_lowest_price")
                state[sym]["short_entry_allowed"] = bool(position_data[sym].get("short_entry_allowed", True))
                if loaded_long > 0:
                    logging.info(f"✅ [{sym}] LONG loaded: {loaded_long}")
                if loaded_short > 0:
                    logging.info(f"✅ [{sym}] SHORT loaded: {loaded_short}")
            except (TypeError, ValueError) as e:
                logging.error(f"❌ [{sym}] Invalid data: {e}")
    except FileNotFoundError:
        logging.info("💾 No positions.json")
    except json.JSONDecodeError:
        logging.error("❌ Corrupt positions.json")
    except Exception as e:
        logging.error(f"❌ Failed to load positions: {e}")

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    try:
        prec = PRECISIONS.get(symbol, 3)
        return round(float(size), prec)
    except (TypeError, ValueError):
        return 0.0

async def safe_api_call(func, *args, **kwargs):
    global api_calls_count, api_calls_reset_time
    now = time.time()
    if now - api_calls_reset_time > 60:
        api_calls_count = 0
        api_calls_reset_time = now
    if api_calls_count >= 10:
        wait_time = 60 - (now - api_calls_reset_time)
        if wait_time > 0:
            await asyncio.sleep(wait_time)
            api_calls_count = 0
            api_calls_reset_time = time.time()
    for attempt in range(3):
        try:
            api_calls_count += 1
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            error_str = str(e)
            if "-1003" in error_str or "too many requests" in error_str.lower():
                wait_time = (2 ** attempt) * 60
                logging.warning(f"Rate limited, retry {attempt+1}/3, wait {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                if attempt == 2:
                    raise e
                await asyncio.sleep(2 ** attempt)
    raise Exception("Max retries reached")

async def place_order(client: AsyncClient, symbol: str, side: str, quantity: float, action: str) -> bool:
    try:
        quantity = round_size(abs(quantity), symbol)
        if quantity == 0:
            return True
        if "LONG" in action.upper():
            position_side = "LONG"
        elif "SHORT" in action.upper():
            position_side = "SHORT"
        else:
            logging.error(f"Unknown action: {action}")
            return False
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side
        }
        result = await safe_api_call(client.futures_create_order, **params)
        if result and 'orderId' in result:
            logging.info(f"🚀 {symbol} {action} OK - {side} {quantity}")
            return True
        return False
    except Exception as e:
        logging.error(f"❌ {symbol} {action} FAILED: {e}")
        return False

# ========================= TRAILING STOPS =========================
def initialize_trailing_stop(symbol: str, side: str, entry_price: float):
    try:
        st = state[symbol]
        if entry_price is None or entry_price <= 0:
            return
        if side == "LONG":
            st["long_entry_price"] = float(entry_price)
            st["long_peak_price"] = float(entry_price)
            st["long_trailing_stop_price"] = float(entry_price) * (1 - TRAILING_LOSS_PERCENT / 100)
            logging.info(f"🎯 {symbol} LONG Stop: Entry={entry_price:.6f}, Stop={st['long_trailing_stop_price']:.6f}")
        elif side == "SHORT":
            st["short_entry_price"] = float(entry_price)
            st["short_lowest_price"] = float(entry_price)
            st["short_trailing_stop_price"] = float(entry_price) * (1 + TRAILING_LOSS_PERCENT / 100)
            logging.info(f"🎯 {symbol} SHORT Stop: Entry={entry_price:.6f}, Stop={st['short_trailing_stop_price']:.6f}")
        st["stop_warning_logged"] = False
        save_positions()
    except Exception as e:
        logging.error(f"❌ Init stop failed {symbol} {side}: {e}")

def update_trailing_stop(symbol: str, current_price: float) -> Dict[str, bool]:
    st = state[symbol]
    result = {"long_hit": False, "short_hit": False}
    try:
        if current_price is None or current_price <= 0:
            return result
        current_price = float(current_price)
        if st["long_position"] > 0:
            if st["long_peak_price"] is None or st["long_trailing_stop_price"] is None:
                if not st["stop_warning_logged"]:
                    logging.warning(f"⚠️ {symbol} LONG missing stop")
                    st["stop_warning_logged"] = True
                return result
            if current_price > st["long_peak_price"]:
                st["long_peak_price"] = float(current_price)
                new_stop = float(current_price) * (1 - TRAILING_LOSS_PERCENT / 100)
                if new_stop > st["long_trailing_stop_price"]:
                    st["long_trailing_stop_price"] = new_stop
                    save_positions()
            if current_price <= st["long_trailing_stop_price"]:
                logging.info(f"🛑 {symbol} LONG Stop HIT: {current_price:.6f} <= {st['long_trailing_stop_price']:.6f}")
                result["long_hit"] = True
        if st["short_position"] > 0:
            if st["short_lowest_price"] is None or st["short_trailing_stop_price"] is None:
                if not st["stop_warning_logged"]:
                    logging.warning(f"⚠️ {symbol} SHORT missing stop")
                    st["stop_warning_logged"] = True
                return result
            if current_price < st["short_lowest_price"]:
                st["short_lowest_price"] = float(current_price)
                new_stop = float(current_price) * (1 + TRAILING_LOSS_PERCENT / 100)
                if new_stop < st["short_trailing_stop_price"]:
                    st["short_trailing_stop_price"] = new_stop
                    save_positions()
            if current_price >= st["short_trailing_stop_price"]:
                logging.info(f"🛑 {symbol} SHORT Stop HIT: {current_price:.6f} >= {st['short_trailing_stop_price']:.6f}")
                result["short_hit"] = True
    except Exception as e:
        logging.error(f"❌ Update stop error {symbol}: {e}")
    return result

def reset_trailing_stop(symbol: str, side: str):
    try:
        st = state[symbol]
        if side == "LONG":
            st["long_entry_price"] = None
            st["long_trailing_stop_price"] = None
            st["long_peak_price"] = None
            st["long_entry_allowed"] = True
            st["long_signal_active"] = False
            logging.info(f"🔓 {symbol} LONG re-enabled")
        elif side == "SHORT":
            st["short_entry_price"] = None
            st["short_trailing_stop_price"] = None
            st["short_lowest_price"] = None
            st["short_entry_allowed"] = True
            st["short_signal_active"] = False
            logging.info(f"🔓 {symbol} SHORT re-enabled")
        save_positions()
    except Exception as e:
        logging.error(f"❌ Reset stop error {symbol}: {e}")

# ========================= CALCULATIONS =========================
def kalman_filter(symbol: str, measurement: float) -> Optional[float]:
    try:
        st = state[symbol]
        if measurement is None:
            return None
        measurement = float(measurement)
        if st["kalman_x"] is None:
            st["kalman_x"] = measurement
            st["kalman_p"] = 1.0
            return measurement
        x_pred = st["kalman_x"]
        p_pred = st["kalman_p"] + KALMAN_Q
        kalman_gain = p_pred / (p_pred + KALMAN_R)
        x_updated = x_pred + kalman_gain * (measurement - x_pred)
        p_updated = (1 - kalman_gain) * p_pred
        st["kalman_x"] = x_updated
        st["kalman_p"] = p_updated
        return x_updated
    except Exception as e:
        logging.error(f"Kalman error {symbol}: {e}")
        return None

def apply_kalman_to_klines(symbol: str):
    try:
        klines = state[symbol]["klines"]
        if len(klines) == 0:
            return
        latest = klines[-1]
        if "close" not in latest:
            return
        state[symbol]["kalman_close"] = kalman_filter(symbol, latest["close"])
    except Exception as e:
        logging.error(f"Apply Kalman error {symbol}: {e}")

def calculate_jma_from_kalman(symbol: str, length: int, phase: int = 50, power: int = 2) -> Optional[float]:
    try:
        klines = state[symbol]["klines"]
        if len(klines) < length + 1:
            return None
        if USE_LIVE_CANDLE:
            completed = list(klines)
        else:
            completed = list(klines)[:-1]
        if len(completed) < length:
            return None
        values = []
        temp_kalman_x = None
        temp_kalman_p = 1.0
        for k in completed:
            if "close" not in k:
                continue
            close_val = float(k["close"])
            if temp_kalman_x is None:
                temp_kalman_x = close_val
                temp_kalman_p = 1.0
                values.append(close_val)
            else:
                x_pred = temp_kalman_x
                p_pred = temp_kalman_p + KALMAN_Q
                kalman_gain = p_pred / (p_pred + KALMAN_R)
                temp_kalman_x = x_pred + kalman_gain * (close_val - x_pred)
                temp_kalman_p = (1 - kalman_gain) * p_pred
                values.append(temp_kalman_x)
        if len(values) < length:
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
    except Exception as e:
        logging.error(f"JMA error {symbol}: {e}")
        return None

def calculate_hurst_simplified(symbol: str, periods: int = 50) -> Optional[float]:
    """
    Simplified Hurst using variance method (faster than R/S)
    Returns 0-1 where:
    > 0.5 = trending/persistent
    = 0.5 = random walk
    < 0.5 = mean-reverting
    """
    try:
        klines = list(state[symbol]["klines"])
        if USE_LIVE_CANDLE:
            completed = klines
        else:
            completed = klines[:-1]
        
        if len(completed) < periods + 1:
            return None
        
        closes = [float(k["close"]) for k in completed[-(periods+1):]]
        
        if len(closes) < periods:
            return None
        
        # Calculate log returns
        log_returns = [
            math.log(closes[i] / closes[i-1])
            for i in range(1, len(closes))
            if closes[i-1] > 0 and closes[i] > 0
        ]
        
        if len(log_returns) < 10:
            return None
        
        # Variance method for Hurst
        lags = [2, 4, 8, 16]
        variances = []
        valid_lags = []
        
        for lag in lags:
            if lag >= len(log_returns):
                break
            
            # Calculate variance at this lag
            lagged_returns = [
                sum(log_returns[i:i+lag])
                for i in range(len(log_returns) - lag)
            ]
            
            if lagged_returns:
                variance = sum(r**2 for r in lagged_returns) / len(lagged_returns)
                variances.append(variance)
                valid_lags.append(lag)
        
        if len(variances) < 2:
            return 0.5
        
        # Linear regression on log-log plot
        log_lags = [math.log(l) for l in valid_lags]
        log_vars = [math.log(v) if v > 0 else 0 for v in variances]
        
        n = len(log_lags)
        sum_x = sum(log_lags)
        sum_y = sum(log_vars)
        sum_xy = sum(x * y for x, y in zip(log_lags, log_vars))
        sum_x2 = sum(x * x for x in log_lags)
        
        denominator = n * sum_x2 - sum_x * sum_x
        if denominator == 0:
            return 0.5
        
        # Slope = 2*H in variance method
        slope = (n * sum_xy - sum_x * sum_y) / denominator
        hurst = slope / 2.0
        
        # Clamp between 0 and 1
        hurst = max(0.0, min(1.0, hurst))
        
        return hurst
        
    except Exception as e:
        logging.error(f"Hurst simplified error {symbol}: {e}")
        return None

def calculate_fdi(symbol: str, periods: int = 20) -> Optional[float]:
    """
    Fractal Dimension Index - measures path complexity
    Returns normalized 0-1 where:
    1.0 = perfectly smooth trend
    0.0 = maximum choppiness
    """
    try:
        klines = list(state[symbol]["klines"])
        if USE_LIVE_CANDLE:
            completed = klines
        else:
            completed = klines[:-1]
        
        if len(completed) < periods + 1:
            return None
        
        closes = [float(k["close"]) for k in completed[-(periods+1):]]
        
        if len(closes) < periods:
            return None
        
        # Method 1: Ruler Method (fast)
        n = len(closes)
        
        # Linear distance (straight line from start to end)
        price_range = max(closes) - min(closes)
        if price_range == 0:
            return 0.5
        
        # Normalize prices to 0-1 range
        normalized = [(c - min(closes)) / price_range for c in closes]
        
        # Calculate path length
        path_length = sum(
            math.sqrt((normalized[i] - normalized[i-1])**2 + (1/(n-1))**2)
            for i in range(1, len(normalized))
        )
        
        # Fractal dimension
        if path_length <= 0:
            return 0.5
        
        fd = 1 + (math.log(path_length) / math.log(2 * (n - 1)))
        
        # Normalize: FD ranges from 1 (smooth) to 2 (rough)
        # Convert to 0-1 where 1 = smooth, 0 = rough
        normalized_fd = 2 - fd  # Invert so 1=smooth, 0=rough
        normalized_fd = max(0.0, min(1.0, normalized_fd))
        
        return normalized_fd
        
    except Exception as e:
        logging.error(f"FDI error {symbol}: {e}")
        return None

def calculate_hurst_fdi_adaptive(symbol: str,
                                 hurst_periods: int = 50,
                                 fdi_periods: int = 20) -> Optional[float]:
    """
    Dynamically weight based on market regime
    
    Trending regime (H > 0.6): Use Hurst more (trust persistence)
    Choppy regime (H < 0.4): Use FDI more (trust geometry)
    Transitional (0.4-0.6): Balance both
    """
    try:
        hurst = calculate_hurst_simplified(symbol, hurst_periods)
        fdi = calculate_fdi(symbol, fdi_periods)
        
        if hurst is None or fdi is None:
            return hurst if hurst is not None else fdi
        
        # Determine regime based on Hurst
        if hurst > 0.6:
            # Strong trending regime - trust Hurst more
            regime = "trending"
            hurst_weight = 0.75
            fdi_weight = 0.25
        elif hurst < 0.4:
            # Mean-reverting regime - trust FDI more
            regime = "mean_revert"
            hurst_weight = 0.25
            fdi_weight = 0.75
        else:
            # Transitional regime - balance
            regime = "transitional"
            hurst_weight = 0.5
            fdi_weight = 0.5
        
        # Normalize Hurst to 0-1 range
        # Map [0, 0.5, 1] → [0, 0.5, 1]
        # But handle mean-reversion differently
        if hurst >= 0.5:
            # Trending: linear scaling
            hurst_normalized = hurst
        else:
            # Mean-reverting: inverse scaling
            # H=0.4 → 0.2 (reduce adaptation)
            # H=0.0 → 0.0 (no adaptation)
            hurst_normalized = hurst * 0.4
        
        # Combine with adaptive weights
        efficiency = (hurst_weight * hurst_normalized) + (fdi_weight * fdi)
        
        # Store regime for logging
        state[symbol]["market_regime"] = regime
        
        return max(0.0, min(1.0, efficiency))
        
    except Exception as e:
        logging.error(f"Hurst-FDI adaptive error {symbol}: {e}")
        return None

def calculate_tama(symbol: str, jma_value: Optional[float], kalman_price: float, efficiency: Optional[float]) -> Optional[float]:
    try:
        if jma_value is None or kalman_price is None:
            return None
        jma_value = float(jma_value)
        kalman_price = float(kalman_price)
        if not USE_TAMA or efficiency is None:
            return jma_value
        efficiency = float(efficiency)
        adjustment = ALPHA_WEIGHT * efficiency * (kalman_price - jma_value)
        tama = jma_value + adjustment
        return tama
    except Exception as e:
        logging.error(f"TAMA error {symbol}: {e}")
        return None

def calculate_cma(tama_value: Optional[float], kalman_close: float, period: int) -> Optional[float]:
    """
    Calculate Corrected Moving Average (CMA)
    Formula: CMA = TAMA + (Kalman_Close - TAMA) / Period
    """
    try:
        if tama_value is None or kalman_close is None:
            return None
        tama_value = float(tama_value)
        kalman_close = float(kalman_close)
        period = int(period)
        if period <= 0:
            return tama_value
        cma = tama_value + (kalman_close - tama_value) / period
        return cma
            except Exception as e:
                logging.error(f"CMA error: {e}")
                return None

# ========================= TRADING LOGIC =========================
def update_trading_signals(symbol: str) -> Dict[str, bool]:
    st = state[symbol]
    price = st["price"]
    result = {"long_entry": False, "short_entry": False, "long_exit": False, "short_exit": False}
    try:
        now = time.time()
        if (now - st["last_signal_check_time"]) < 1.0:
            return result
        st["last_signal_check_time"] = now
        if price is None or not st["ready"]:
            return result
        try:
            price = float(price)
        except (TypeError, ValueError):
            return result
        apply_kalman_to_klines(symbol)
        kalman_close = st["kalman_close"]
        if kalman_close is None:
            return result
        jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
        jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
        
        # Calculate Hurst + FDI Combined Efficiency (REPLACES ER)
        efficiency_fast = calculate_hurst_fdi_adaptive(
            symbol, 
            hurst_periods=HURST_PERIODS_FAST,
            fdi_periods=FDI_PERIODS_FAST
        )
        efficiency_slow = calculate_hurst_fdi_adaptive(
            symbol,
            hurst_periods=HURST_PERIODS_SLOW,
            fdi_periods=FDI_PERIODS_SLOW
        )
        
        if jma_fast is None or jma_slow is None or efficiency_fast is None or efficiency_slow is None:
            return result
        
        # Store for logging
        st["combined_efficiency_fast"] = efficiency_fast
        st["combined_efficiency_slow"] = efficiency_slow
        
        tama_fast = calculate_tama(symbol, jma_fast, kalman_close, efficiency_fast)
        tama_slow = calculate_tama(symbol, jma_slow, kalman_close, efficiency_slow)
        if tama_fast is None or tama_slow is None:
            return result
        cma_slow = calculate_cma(tama_slow, kalman_close, CMA_PERIOD)
        if cma_slow is None:
            return result
        try:
            tama_fast = float(tama_fast)
            cma_slow = float(cma_slow)
        except (TypeError, ValueError):
            return result
        st["tama_fast"] = tama_fast
        st["tama_slow"] = tama_slow
        st["cma_slow"] = cma_slow
        st["jma_fast"] = jma_fast
        st["jma_slow"] = jma_slow
        prev_tama_fast = st["prev_tama_fast"]
        prev_cma_slow = st["prev_cma_slow"]
        if prev_tama_fast is None or prev_cma_slow is None:
            st["prev_tama_fast"] = tama_fast
            st["prev_cma_slow"] = cma_slow
            return result
        try:
            prev_tama_fast = float(prev_tama_fast)
            prev_cma_slow = float(prev_cma_slow)
        except (TypeError, ValueError):
            st["prev_tama_fast"] = tama_fast
            st["prev_cma_slow"] = cma_slow
            return result
        if USE_CROSSOVER_ENTRY:
            bullish_signal = (tama_fast > cma_slow) and (prev_tama_fast <= prev_cma_slow)
            bearish_signal = (tama_fast < cma_slow) and (prev_tama_fast >= prev_cma_slow)
        else:
            bullish_signal = (price > tama_fast) and (tama_fast > cma_slow)
            bearish_signal = (price < tama_fast) and (tama_fast < cma_slow)
        try:
            long_pos = float(st["long_position"])
            short_pos = float(st["short_position"])
        except (TypeError, ValueError):
            return result
        if USE_HEDGE_MODE:
            if bullish_signal and long_pos == 0 and st["long_entry_allowed"]:
                result["long_entry"] = True
                st["long_entry_allowed"] = False
                save_positions()
                mode_str = "Crossover" if USE_CROSSOVER_ENTRY else "Symmetrical"
                hedge_status = " (HEDGE MODE)" if short_pos > 0 else ""
                logging.info(f"🟢 {symbol} LONG ENTRY ({mode_str}){hedge_status}")
                logging.info(f"   Price={price:.6f}, Fast={tama_fast:.6f}, CMA_Slow={cma_slow:.6f}")
            if bearish_signal and short_pos == 0 and st["short_entry_allowed"]:
                result["short_entry"] = True
                st["short_entry_allowed"] = False
                save_positions()
                mode_str = "Crossover" if USE_CROSSOVER_ENTRY else "Symmetrical"
                hedge_status = " (HEDGE MODE)" if long_pos > 0 else ""
                logging.info(f"🔴 {symbol} SHORT ENTRY ({mode_str}){hedge_status}")
                logging.info(f"   Price={price:.6f}, Fast={tama_fast:.6f}, CMA_Slow={cma_slow:.6f}")
        else:
            if bullish_signal and long_pos == 0 and short_pos == 0 and st["long_entry_allowed"]:
                result["long_entry"] = True
                st["long_entry_allowed"] = False
                save_positions()
                mode_str = "Crossover" if USE_CROSSOVER_ENTRY else "Symmetrical"
                logging.info(f"🟢 {symbol} LONG ENTRY ({mode_str})")
                logging.info(f"   Price={price:.6f}, Fast={tama_fast:.6f}, CMA_Slow={cma_slow:.6f}")
            if bearish_signal and short_pos == 0 and long_pos == 0 and st["short_entry_allowed"]:
                result["short_entry"] = True
                st["short_entry_allowed"] = False
                save_positions()
                mode_str = "Crossover" if USE_CROSSOVER_ENTRY else "Symmetrical"
                logging.info(f"🔴 {symbol} SHORT ENTRY ({mode_str})")
                logging.info(f"   Price={price:.6f}, Fast={tama_fast:.6f}, CMA_Slow={cma_slow:.6f}")
        
        # NO CROSSOVER EXITS - Only trailing stops exit positions!
        # This prevents flip-flopping and lets trends run
        
        st["prev_tama_fast"] = tama_fast
        st["prev_cma_slow"] = cma_slow
    except Exception as e:
        logging.error(f"❌ Signal error {symbol}: {e}")
    return result

async def execute_open_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    try:
        st = state[symbol]
        now = time.time()
        if side == "LONG":
            if (now - st["last_long_exec_ts"]) < 2.0:
                return False
            st["last_long_exec_ts"] = now
        elif side == "SHORT":
            if (now - st["last_short_exec_ts"]) < 2.0:
                return False
            st["last_short_exec_ts"] = now
        else:
            return False
        order_side = "BUY" if side == "LONG" else "SELL"
        success = await place_order(client, symbol, order_side, size, f"{side} ENTRY")
        return success
    except Exception as e:
        logging.error(f"❌ Open error {symbol} {side}: {e}")
        return False

async def execute_close_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    try:
        order_side = "SELL" if side == "LONG" else "BUY"
        success = await place_order(client, symbol, order_side, size, f"{side} CLOSE")
        return success
    except Exception as e:
        logging.error(f"❌ Close error {symbol} {side}: {e}")
        return False

async def price_feed_loop(client: AsyncClient):
    streams = [f"{s.lower()}@kline_{BASE_TIMEFRAME.lower()}" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("📡 WebSocket connected")
                async for message in ws:
                    try:
                        data = json.loads(message)
                        if not isinstance(data, dict) or "data" not in data:
                            continue
                        data = data.get("data", {})
                        if not isinstance(data, dict) or "k" not in data:
                            continue
                        k = data.get("k", {})
                        if not isinstance(k, dict):
                            continue
                        symbol = k.get("s")
                        if not symbol or symbol not in SYMBOLS:
                            continue
                        required_fields = ["c", "o", "h", "l", "t"]
                        if not all(field in k for field in required_fields):
                            continue
                        try:
                            state[symbol]["price"] = float(k["c"])
                        except (TypeError, ValueError):
                            continue
                        try:
                            kline_data = {
                                "open_time": int(k["t"] / 1000),
                                "open": float(k["o"]),
                                "high": float(k["h"]),
                                "low": float(k["l"]),
                                "close": float(k["c"])
                            }
                        except (TypeError, ValueError, KeyError):
                            continue
                        klines = state[symbol]["klines"]
                        if len(klines) > 0 and klines[-1]["open_time"] == kline_data["open_time"]:
                            klines[-1] = kline_data
                        else:
                            klines.append(kline_data)
                        if len(state[symbol]["klines"]) >= MA_PERIODS and not state[symbol]["ready"]:
                            apply_kalman_to_klines(symbol)
                            jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
                            jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
                            efficiency_fast = calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_FAST, FDI_PERIODS_FAST)
                            efficiency_slow = calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_SLOW, FDI_PERIODS_SLOW)
                            if (jma_fast is not None) and (jma_slow is not None) and (efficiency_fast is not None) and (efficiency_slow is not None):
                                state[symbol]["ready"] = True
                                logging.info(f"✅ {symbol} ready")
                        else:
                            # Keep calculating efficiency in background
                            calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_FAST, FDI_PERIODS_FAST)
                            calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_SLOW, FDI_PERIODS_SLOW)
                    except Exception:
                        continue
        except websockets.exceptions.ConnectionClosed:
            logging.warning("WS closed, reconnecting...")
            await asyncio.sleep(5)
        except Exception as e:
            logging.warning(f"WS error: {e}")
            await asyncio.sleep(5)

async def trading_loop(client: AsyncClient):
    while True:
        try:
            await asyncio.sleep(1.0)
            for symbol in SYMBOLS:
                try:
                    st = state[symbol]
                    if not st["ready"]:
                        continue
                    price = st["price"]
                    if price is None:
                        continue
                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        continue
                    try:
                        long_pos = float(st["long_position"])
                        short_pos = float(st["short_position"])
                    except (TypeError, ValueError):
                        st["long_position"] = 0.0
                        st["short_position"] = 0.0
                        save_positions()
                        continue
                    if long_pos > 0 and (st["long_trailing_stop_price"] is None or st["long_peak_price"] is None):
                        initialize_trailing_stop(symbol, "LONG", price)
                    if short_pos > 0 and (st["short_trailing_stop_price"] is None or st["short_lowest_price"] is None):
                        initialize_trailing_stop(symbol, "SHORT", price)
                    stop_result = update_trailing_stop(symbol, price)
                    if stop_result["long_hit"] and long_pos > 0:
                        success = await execute_close_position(client, symbol, "LONG", long_pos)
                        if success:
                            st["long_position"] = 0.0
                            reset_trailing_stop(symbol, "LONG")
                            save_positions()
                    if stop_result["short_hit"] and short_pos > 0:
                        success = await execute_close_position(client, symbol, "SHORT", short_pos)
                        if success:
                            st["short_position"] = 0.0
                            reset_trailing_stop(symbol, "SHORT")
                            save_positions()
                    signals = update_trading_signals(symbol)
                    
                    # Only process entries, NO crossover exits
                    try:
                        long_pos = float(st["long_position"])
                        short_pos = float(st["short_position"])
                    except (TypeError, ValueError):
                        long_pos = 0.0
                        short_pos = 0.0
                    
                    if signals["long_entry"] and long_pos == 0:
                        target_size = SYMBOLS[symbol]
                        success = await execute_open_position(client, symbol, "LONG", target_size)
                        if success:
                            st["long_position"] = target_size
                            initialize_trailing_stop(symbol, "LONG", price)
                            save_positions()
                        else:
                            st["long_entry_allowed"] = True
                            save_positions()
                    if signals["short_entry"] and short_pos == 0:
                        target_size = SYMBOLS[symbol]
                        success = await execute_open_position(client, symbol, "SHORT", target_size)
                        if success:
                            st["short_position"] = target_size
                            initialize_trailing_stop(symbol, "SHORT", price)
                            save_positions()
                        else:
                            st["short_entry_allowed"] = True
                            save_positions()
                except Exception as e:
                    logging.error(f"❌ Trade loop error {symbol}: {e}")
                    continue
        except Exception as e:
            logging.error(f"❌ Critical trade loop error: {e}")
            await asyncio.sleep(1)

async def status_logger():
    while True:
        try:
            await asyncio.sleep(120)
            current_time = time.strftime("%H:%M", time.localtime())
            logging.info(f"📊 === STATUS {current_time} ===")
            for symbol in SYMBOLS:
                st = state[symbol]
                if not st["ready"]:
                    candle_count = len(st["klines"])
                    logging.info(f"{symbol}: {candle_count} candles (not ready)")
                    continue
                price = st["price"]
                tama_fast = st.get("tama_fast")
                cma_slow = st.get("cma_slow")
                eff_fast = st.get("combined_efficiency_fast")
                eff_slow = st.get("combined_efficiency_slow")
                regime = st.get("market_regime", "unknown")
                if price and tama_fast and cma_slow and eff_fast is not None and eff_slow is not None:
                    trend = "BULL ▲" if tama_fast > cma_slow else ("BEAR ▼" if tama_fast < cma_slow else "FLAT ═")
                    regime_emoji = {
                        "trending": "📈",
                        "mean_revert": "↔️",
                        "transitional": "🔄",
                        "unknown": "❓"
                    }.get(regime, "❓")
                    logging.info(f"{symbol}: ${price:.6f} | {trend} | {regime_emoji} {regime.upper()}")
                    logging.info(f"  Efficiency: Fast={eff_fast:.3f} Slow={eff_slow:.3f}")
                    logging.info(f"  TAMA Fast=${tama_fast:.6f} | CMA_Slow=${cma_slow:.6f}")
                    long_lock = "🔒" if not st['long_entry_allowed'] else "🔓"
                    short_lock = "🔒" if not st['short_entry_allowed'] else "🔓"
                    if st["long_position"] > 0:
                        long_pos = st["long_position"]
                        peak = st.get("long_peak_price")
                        stop = st.get("long_trailing_stop_price")
                        entry = st.get("long_entry_price")
                        logging.info(f"  LONG: {long_pos} {long_lock}")
                        if entry:
                            pnl_pct = ((price - entry) / entry) * 100
                            logging.info(f"    Entry=${entry:.6f} | PnL={pnl_pct:+.2f}%")
                        if peak and stop:
                            distance_to_stop = ((price - stop) / price) * 100
                            peak_gain = ((peak - entry) / entry) * 100 if entry else 0
                            logging.info(f"    Peak=${peak:.6f} (+{peak_gain:.2f}%) | Stop=${stop:.6f}")
                            logging.info(f"    Distance to Stop: {distance_to_stop:.2f}%")
                    if st["short_position"] > 0:
                        short_pos = st["short_position"]
                        lowest = st.get("short_lowest_price")
                        stop = st.get("short_trailing_stop_price")
                        entry = st.get("short_entry_price")
                        logging.info(f"  SHORT: {short_pos} {short_lock}")
                        if entry:
                            pnl_pct = ((entry - price) / entry) * 100
                            logging.info(f"    Entry=${entry:.6f} | PnL={pnl_pct:+.2f}%")
                        if lowest and stop:
                            distance_to_stop = ((stop - price) / price) * 100
                            lowest_gain = ((entry - lowest) / entry) * 100 if entry else 0
                            logging.info(f"    Lowest=${lowest:.6f} (+{lowest_gain:.2f}%) | Stop=${stop:.6f}")
                            logging.info(f"    Distance to Stop: {distance_to_stop:.2f}%")
                    if st["long_position"] > 0 and st["short_position"] > 0:
                        logging.info(f"  ⚖️ HEDGE MODE ACTIVE - Both positions running")
                    if st["long_position"] == 0 and not st['long_entry_allowed']:
                        logging.info(f"  LONG: Flat {long_lock} (waiting for signal)")
                    if st["short_position"] == 0 and not st['short_entry_allowed']:
                        logging.info(f"  SHORT: Flat {short_lock} (waiting for signal)")
            logging.info("📊 === END STATUS ===")
        except Exception as e:
            logging.error(f"Status error: {e}")

async def position_sanity_check(client: AsyncClient):
    while True:
        try:
            await asyncio.sleep(300)
            logging.info("🔍 Running position sanity check...")
            account_info = await safe_api_call(client.futures_account)
            if not account_info or 'positions' not in account_info:
                continue
            positions = account_info.get('positions', [])
            exchange_positions = {}
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                symbol = pos.get('symbol')
                if symbol not in SYMBOLS:
                    continue
                try:
                    amt = float(pos.get('positionAmt', 0))
                    side = pos.get('positionSide')
                    exchange_positions[f"{symbol}_{side}"] = abs(amt)
                except (TypeError, ValueError):
                    continue
            mismatches = 0
            for symbol in SYMBOLS:
                st = state[symbol]
                try:
                    local_long = float(st["long_position"])
                    local_short = float(st["short_position"])
                except (TypeError, ValueError):
                    local_long = 0.0
                    local_short = 0.0
                exchange_long = exchange_positions.get(f"{symbol}_LONG", 0.0)
                exchange_short = exchange_positions.get(f"{symbol}_SHORT", 0.0)
                if abs(local_long - exchange_long) > 0.001:
                    if exchange_long == 0 and local_long > 0:
                        st["long_position"] = 0.0
                        reset_trailing_stop(symbol, "LONG")
                        mismatches += 1
                    elif exchange_long > 0 and local_long == 0:
                        st["long_position"] = exchange_long
                        st["long_entry_allowed"] = False
                        if st["price"]:
                            initialize_trailing_stop(symbol, "LONG", st["price"])
                        mismatches += 1
                if abs(local_short - exchange_short) > 0.001:
                    if exchange_short == 0 and local_short > 0:
                        st["short_position"] = 0.0
                        reset_trailing_stop(symbol, "SHORT")
                        mismatches += 1
                    elif exchange_short > 0 and local_short == 0:
                        st["short_position"] = exchange_short
                        st["short_entry_allowed"] = False
                        if st["price"]:
                            initialize_trailing_stop(symbol, "SHORT", st["price"])
                        mismatches += 1
            if mismatches > 0:
                save_positions()
        except Exception as e:
            logging.error(f"❌ Sanity check error: {e}")

async def recover_positions_from_exchange(client: AsyncClient):
    try:
        account_info = await safe_api_call(client.futures_account)
        if not account_info or 'positions' not in account_info:
            return
        positions = account_info.get('positions', [])
        recovered_count = 0
        for position in positions:
            try:
                if not isinstance(position, dict):
                    continue
                symbol = position.get('symbol')
                if not symbol or symbol not in SYMBOLS:
                    continue
                position_amt = float(position.get('positionAmt', 0))
                entry_price = float(position.get('entryPrice', 0))
                mark_price = float(position.get('markPrice', 0))
                position_side = position.get('positionSide')
                if abs(position_amt) > 0.0001:
                    if position_side == "LONG" and position_amt > 0:
                        state[symbol]["long_position"] = position_amt
                        state[symbol]["long_entry_price"] = entry_price if entry_price > 0 else None
                        state[symbol]["long_entry_allowed"] = False
                        recovered_count += 1
                        init_price = mark_price if mark_price > 0 else entry_price
                        if init_price > 0:
                            initialize_trailing_stop(symbol, "LONG", init_price)
                    elif position_side == "SHORT" and position_amt < 0:
                        state[symbol]["short_position"] = abs(position_amt)
                        state[symbol]["short_entry_price"] = entry_price if entry_price > 0 else None
                        state[symbol]["short_entry_allowed"] = False
                        recovered_count += 1
                        init_price = mark_price if mark_price > 0 else entry_price
                        if init_price > 0:
                            initialize_trailing_stop(symbol, "SHORT", init_price)
            except (TypeError, ValueError, KeyError):
                continue
        if recovered_count > 0:
            save_positions()
    except Exception as e:
        logging.error(f"❌ Recovery failed: {e}")

async def init_bot(client: AsyncClient):
    try:
        logging.info("🔧 Initializing...")
        if USE_HEDGE_MODE:
            logging.info(f"⚖️ HEDGE MODE: ENABLED")
        load_klines()
        load_positions()
        await recover_positions_from_exchange(client)
        symbols_needing_data = []
        for symbol in SYMBOLS:
            klines = state[symbol]["klines"]
            if len(klines) >= MA_PERIODS:
                apply_kalman_to_klines(symbol)
                jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
                jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
                efficiency_fast = calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_FAST, FDI_PERIODS_FAST)
                efficiency_slow = calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_SLOW, FDI_PERIODS_SLOW)
                if all([jma_fast, jma_slow, efficiency_fast is not None, efficiency_slow is not None]):
                    state[symbol]["ready"] = True
                else:
                    symbols_needing_data.append(symbol)
            else:
                symbols_needing_data.append(symbol)
        if symbols_needing_data:
            for i, symbol in enumerate(symbols_needing_data):
                try:
                    klines_data = await safe_api_call(client.futures_mark_price_klines, symbol=symbol, interval=BASE_TIMEFRAME, limit=min(MA_PERIODS + 100, 1500))
                    if klines_data:
                        st = state[symbol]
                        st["klines"].clear()
                        for kline in klines_data:
                            try:
                                st["klines"].append({"open_time": int(float(kline[0]) / 1000), "open": float(kline[1]), "high": float(kline[2]), "low": float(kline[3]), "close": float(kline[4])})
                            except:
                                continue
                        apply_kalman_to_klines(symbol)
                        jma_fast = calculate_jma_from_kalman(symbol, JMA_LENGTH_FAST, JMA_PHASE, JMA_POWER)
                        jma_slow = calculate_jma_from_kalman(symbol, JMA_LENGTH_SLOW, JMA_PHASE, JMA_POWER)
                        efficiency_fast = calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_FAST, FDI_PERIODS_FAST)
                        efficiency_slow = calculate_hurst_fdi_adaptive(symbol, HURST_PERIODS_SLOW, FDI_PERIODS_SLOW)
                        if all([jma_fast, jma_slow, efficiency_fast is not None, efficiency_slow is not None]):
                            st["ready"] = True
                    if i < len(symbols_needing_data) - 1:
                        await asyncio.sleep(15)
                except Exception as e:
                    logging.error(f"❌ {symbol} fetch failed: {e}")
        save_klines()
        logging.info("🚀 Init complete")
    except Exception as e:
        logging.error(f"❌ Init error: {e}")
        raise

async def main():
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing API credentials")
    client = await AsyncClient.create(API_KEY, API_SECRET)
    atexit.register(save_klines)
    atexit.register(save_positions)
    try:
        await init_bot(client)
        await asyncio.gather(price_feed_loop(client), trading_loop(client), status_logger(), position_sanity_check(client))
    except Exception as e:
        logging.error(f"❌ Critical error: {e}")
        raise
    finally:
        await client.close_connection()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    print("=" * 80)
    print("TAMA TRADING STRATEGY - HEDGE MODE WITH HURST+FDI")
    print("=" * 80)
    print(f"Layer 1: Kalman Filter (Q={KALMAN_Q}, R={KALMAN_R})")
    print(f"Layer 2: JMA Fast={JMA_LENGTH_FAST}, JMA Slow={JMA_LENGTH_SLOW}")
    print(f"Layer 3: Hurst+FDI Adaptive (Fast: H={HURST_PERIODS_FAST}/FDI={FDI_PERIODS_FAST})")
    print(f"         Slow: H={HURST_PERIODS_SLOW}/FDI={FDI_PERIODS_SLOW}")
    print(f"Layer 4: CMA Correction (Period={CMA_PERIOD})")
    print(f"")
    print(f"Formula: CMA = TAMA_Slow + (Kalman_Close - TAMA_Slow) / {CMA_PERIOD}")
    print(f"")
    print("🎯 EXIT STRATEGY: TRAILING STOPS ONLY (No Flip-Flopping!)")
    print(f"  • LONG exits ONLY via trailing stop (-{TRAILING_LOSS_PERCENT}%)")
    print(f"  • SHORT exits ONLY via trailing stop (-{TRAILING_LOSS_PERCENT}%)")
    print(f"  • NO crossover exits = NO flip-flopping!")
    print(f"  • Let trends run until trailing stop hit")
    print(f"")
    if USE_HEDGE_MODE:
        print("⚖️ HEDGE MODE: ENABLED")
        print("  • LONG and SHORT can run simultaneously")
        print("  • Independent trailing stops")
    print("=" * 80)
    try:
        asyncio.run(main())
