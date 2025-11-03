#!/usr/bin/env python3
"""
Leo Moving Average (LMA) Hedge Trading Bot - Improved / Defensive Edition

Summary of improvements (non-breaking, conservative):
- Safer import for Binance AsyncClient with helpful error messages.
- CLI flag --dry-run to start without API keys and without placing orders (useful for mobile / Render debugging).
- Improved safe_api_call backoff logic and clearer handling of rate-limit errors.
- More defensive websockets handling and clearer reconnect logs.
- Added graceful handling when client doesn't implement expected futures_* functions.
- Extra logging when saving/loading files fails; explicit fallback behavior.
- Slightly tightened rounding / validation paths; no change to core strategy.
- Maintained HEDGE mode behavior; no flipping or cross-exit changes.
- All original functions preserved where possible; added defensive wrappers where they were brittle.

How to run:
- Dry run: `python3 bot.py --dry-run`
- Normal run: ensure .env contains BINANCE_API_KEY and BINANCE_API_SECRET, then `python3 bot.py`

Notes:
- If the asyncio Binance client class cannot be imported from either `binance` or `binance.async_client`,
  the script will print a helpful error and exit (unless --dry-run is used).
- Dry-run avoids any network calls to Binance API order endpoints; it will still attempt the websocket feed
  (unless you also set `DISABLE_WS=true` in environment, or run in a networkless environment where ws will fail).
"""
import os
import json
import asyncio
import logging
import time
import atexit
import sys
from pathlib import Path
from collections import deque
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
import argparse

# Try to import websockets with helpful error message if unavailable
try:
    import websockets
except Exception as e:
    print("❌ The 'websockets' library is required. Install with `pip install websockets`.")
    raise

# Try to import AsyncClient from common locations and provide helpful guidance if missing.
AsyncClient = None
_try_clients = []
try:
    # common old import
    from binance import AsyncClient as _AsyncClient1
    AsyncClient = _AsyncClient1
    _try_clients.append("binance.AsyncClient")
except Exception:
    try:
        from binance.async_client import AsyncClient as _AsyncClient2
        AsyncClient = _AsyncClient2
        _try_clients.append("binance.async_client.AsyncClient")
    except Exception:
        AsyncClient = None

# ========================= CONFIG =========================
load_dotenv()

# CLI args
parser = argparse.ArgumentParser(description="LMA Hedge Bot (defensive edition)")
parser.add_argument("--dry-run", action="store_true", help="Do not use API keys or place real orders (test mode)")
parser.add_argument("--disable-ws", action="store_true", help="Disable websocket feed (useful for debugging)")
args = parser.parse_args()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

# If dry-run mode specified, do not require API keys
if not args.dry_run:
    if not API_KEY or not API_SECRET or API_KEY == "your_api_key_here":
        print("❌ MISSING API CREDENTIALS! Check your .env file or run with --dry-run for testing.")
        sys.exit(1)
else:
    # warn user
    print("⚠️ Running in --dry-run mode. No real orders will be sent to the exchange.")

LEVERAGE = int(os.getenv("LEVERAGE", "50"))
USE_LIVE_CANDLE = True  # True = use live updating candle, False = wait for close

# ========================= LMA PARAMETERS =========================
LMA_LENGTH = 15  # Leo Moving Average period

# ========================= FIXED TP/SL PERCENTAGES =========================
LONG_TP_PERCENT = 1.3   # Long take profit: 1.3%
LONG_SL_PERCENT = 1.0   # Long stop loss: 1.0%
SHORT_TP_PERCENT = 1.3  # Short take profit: 1.3%
SHORT_SL_PERCENT = 1.0  # Short stop loss: 1.0%

# Validate percentages are positive
if not (LONG_TP_PERCENT > 0 and LONG_SL_PERCENT > 0 and SHORT_TP_PERCENT > 0 and SHORT_SL_PERCENT > 0):
    print("❌ TP/SL percents must be positive numbers")
    sys.exit(1)

# ========================= TIMEFRAME CONFIG =========================
BASE_TIMEFRAME = "15m"

SUPPORTED_TIMEFRAMES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440
}

if BASE_TIMEFRAME not in SUPPORTED_TIMEFRAMES:
    print(f"❌ Invalid timeframe: {BASE_TIMEFRAME}")
    sys.exit(1)

BASE_MINUTES = SUPPORTED_TIMEFRAMES[BASE_TIMEFRAME]

# ========================= TRADING CONFIG =========================
SYMBOLS = {
    "SOLUSDT": 0.1,
}

PRECISIONS = {
    "SOLUSDT": 3,
}

# Validate all symbols have precision defined
for sym in SYMBOLS:
    if sym not in PRECISIONS:
        PRECISIONS[sym] = 3

KLINE_LIMIT = max(200, LMA_LENGTH + 100)

# ========================= ANTI-SPAM CONFIG =========================
SIGNAL_COOLDOWN_SECONDS = 5.0  # Minimum time between same-side signals
ORDER_COOLDOWN_SECONDS = 3.0    # Minimum time between API orders

# ========================= FILE PATHS =========================
DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)

KLINES_FILE = DATA_DIR / "klines.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
LOG_FILE = DATA_DIR / "bot.log"

# ========================= STATE INITIALIZATION =========================
def create_clean_state():
    """Create a fresh state dict with all required fields"""
    return {
        symbol: {
            "price": None,
            "klines": deque(maxlen=KLINE_LIMIT),
            "lma": None,
            "prev_lma": None,
            "ready": False,
            "long_position": 0.0,
            "long_entry_price": None,
            "long_tp_price": None,
            "long_sl_price": None,
            "long_entry_allowed": True,
            "last_long_signal_ts": 0.0,
            "last_long_order_ts": 0.0,
            "short_position": 0.0,
            "short_entry_price": None,
            "short_tp_price": None,
            "short_sl_price": None,
            "short_entry_allowed": True,
            "last_short_signal_ts": 0.0,
            "last_short_order_ts": 0.0,
        }
        for symbol in SYMBOLS
    }

state = create_clean_state()

# API rate limiting
api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= LOGGING SETUP =========================
def setup_logging():
    """Setup logging with both file and console output"""
    try:
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)
        
        file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        
        logging.root.setLevel(logging.DEBUG)
        logging.root.addHandler(console_handler)
        logging.root.addHandler(file_handler)
        
        logging.info("=" * 80)
        logging.info("🚀 Bot logging initialized")
        logging.info("=" * 80)
    except Exception as e:
        print(f"❌ Logging setup failed: {e}")
        sys.exit(1)

setup_logging()

# ========================= VALIDATION HELPERS =========================
def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default

def safe_bool(value: Any, default: bool = True) -> bool:
    try:
        if value is None:
            return default
        return bool(value)
    except (TypeError, ValueError):
        return default

def validate_price(price: Any, symbol: str) -> Optional[float]:
    try:
        price_float = safe_float(price, None)
        if price_float is None or price_float <= 0:
            return None
        return price_float
    except Exception:
        return None

def validate_kline(kline: Dict) -> bool:
    required = ["open_time", "open", "high", "low", "close"]
    if not isinstance(kline, dict):
        return False
    for field in required:
        if field not in kline:
            return False
        if kline[field] is None:
            return False
    return True

# ========================= PERSISTENCE (with corruption handling) =========================
def safe_json_save(filepath: Path, data: Any):
    try:
        if filepath.exists():
            backup = filepath.with_suffix('.json.backup')
            try:
                filepath.rename(backup)
            except Exception as e:
                logging.debug(f"Backup creation skipped: {e}")
        
        temp_file = filepath.with_suffix('.json.temp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        
        with open(temp_file, 'r', encoding='utf-8') as f:
            json.load(f)
        
        temp_file.rename(filepath)
        logging.debug(f"✅ Saved: {filepath.name}")
    except Exception as e:
        logging.error(f"❌ Failed to save {filepath.name}: {e}")

def safe_json_load(filepath: Path) -> Optional[Dict]:
    if filepath.exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except json.JSONDecodeError:
            logging.warning(f"⚠️ Corrupt {filepath.name}, trying backup...")
        except Exception as e:
            logging.warning(f"⚠️ Can't read {filepath.name}: {e}")
    
    backup = filepath.with_suffix('.json.backup')
    if backup.exists():
        try:
            with open(backup, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    logging.info(f"✅ Restored from backup: {filepath.name}")
                    return data
        except Exception as e:
            logging.warning(f"⚠️ Backup also failed: {e}")
    
    return None

def save_klines():
    try:
        save_data = {}
        for sym in SYMBOLS:
            klines_list = []
            for k in state[sym]["klines"]:
                if validate_kline(k):
                    klines_list.append(k)
            save_data[sym] = klines_list
        safe_json_save(KLINES_FILE, save_data)
    except Exception as e:
        logging.error(f"❌ Save klines failed: {e}")

def load_klines():
    try:
        data = safe_json_load(KLINES_FILE)
        if not data:
            logging.info("📂 No klines file, starting fresh")
            return
        
        loaded_count = 0
        for sym in SYMBOLS:
            if sym not in data or not isinstance(data[sym], list):
                continue
            
            valid_klines = []
            for k in data[sym]:
                if validate_kline(k):
                    valid_klines.append(k)
            
            if valid_klines:
                state[sym]["klines"] = deque(valid_klines, maxlen=KLINE_LIMIT)
                loaded_count += 1
        
        logging.info(f"📤 Loaded klines for {loaded_count} symbols")
    except Exception as e:
        logging.error(f"❌ Load klines failed: {e}")

def save_positions():
    try:
        position_data = {}
        for sym in SYMBOLS:
            st = state[sym]
            position_data[sym] = {
                "long_position": safe_float(st["long_position"]),
                "long_entry_price": safe_float(st["long_entry_price"], None),
                "long_tp_price": safe_float(st["long_tp_price"], None),
                "long_sl_price": safe_float(st["long_sl_price"], None),
                "long_entry_allowed": safe_bool(st["long_entry_allowed"]),
                "short_position": safe_float(st["short_position"]),
                "short_entry_price": safe_float(st["short_entry_price"], None),
                "short_tp_price": safe_float(st["short_tp_price"], None),
                "short_sl_price": safe_float(st["short_sl_price"], None),
                "short_entry_allowed": safe_bool(st["short_entry_allowed"]),
            }
        safe_json_save(POSITIONS_FILE, position_data)
    except Exception as e:
        logging.error(f"❌ Save positions failed: {e}")

def load_positions():
    try:
        data = safe_json_load(POSITIONS_FILE)
        if not data:
            logging.info("📂 No positions file, starting fresh")
            return
        
        logging.info("💾 Loading positions...")
        for sym in SYMBOLS:
            if sym not in data or not isinstance(data[sym], dict):
                continue
            
            try:
                pos_data = data[sym]
                st = state[sym]
                
                st["long_position"] = safe_float(pos_data.get("long_position"))
                st["long_entry_price"] = safe_float(pos_data.get("long_entry_price"), None)
                st["long_tp_price"] = safe_float(pos_data.get("long_tp_price"), None)
                st["long_sl_price"] = safe_float(pos_data.get("long_sl_price"), None)
                st["long_entry_allowed"] = safe_bool(pos_data.get("long_entry_allowed"))
                
                st["short_position"] = safe_float(pos_data.get("short_position"))
                st["short_entry_price"] = safe_float(pos_data.get("short_entry_price"), None)
                st["short_tp_price"] = safe_float(pos_data.get("short_tp_price"), None)
                st["short_sl_price"] = safe_float(pos_data.get("short_sl_price"), None)
                st["short_entry_allowed"] = safe_bool(pos_data.get("short_entry_allowed"))
                
                if st["long_position"] > 0:
                    logging.info(f"✅ [{sym}] LONG loaded: {st['long_position']}")
                if st["short_position"] > 0:
                    logging.info(f"✅ [{sym}] SHORT loaded: {st['short_position']}")
            except Exception as e:
                logging.error(f"❌ [{sym}] Load position error: {e}")
        
        logging.info("💾 Position loading complete")
    except Exception as e:
        logging.error(f"❌ Load positions failed: {e}")

# ========================= API HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    try:
        size_float = safe_float(size)
        if size_float <= 0:
            return 0.0
        prec = PRECISIONS.get(symbol, 3)
        return round(size_float, prec)
    except Exception:
        return 0.0

async def safe_api_call(func, *args, **kwargs):
    """API call with rate limiting and retries"""
    global api_calls_count, api_calls_reset_time
    now = time.time()
    if now - api_calls_reset_time > 60:
        api_calls_count = 0
        api_calls_reset_time = now
    
    # Basic per-minute allowance (conservative)
    MAX_CALLS_PER_MINUTE = 200  # reduced from 2400 to safe default for single-bot setups
    if api_calls_count >= MAX_CALLS_PER_MINUTE:
        wait_time = 60 - (now - api_calls_reset_time)
        if wait_time > 0:
            logging.warning(f"⏳ API minute quota reached, sleeping {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            api_calls_count = 0
            api_calls_reset_time = time.time()
    
    # Retry loop with exponential backoff; more specific handling for rate-limit strings
    for attempt in range(4):
        try:
            api_calls_count += 1
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            err_str = str(e).lower()
            # Rate-limit patterns from binance: code -1003, 429, 'too many requests'
            if ("1003" in err_str) or ("too many requests" in err_str) or ("429" in err_str) or ("rate limit" in err_str):
                backoff = (2 ** attempt) * 5
                logging.warning(f"⏳ Rate-limited or throttled, attempt {attempt+1}/4, sleeping {backoff}s: {e}")
                await asyncio.sleep(backoff)
                continue
            # Connection errors / timeouts
            if attempt < 3:
                backoff = (2 ** attempt)
                logging.warning(f"⚠️ API call error, attempt {attempt+1}/4 retrying in {backoff}s: {e}")
                await asyncio.sleep(backoff)
                continue
            # Final failure
            logging.error(f"❌ API call failed after retries: {e}")
            raise
    raise Exception("Max retries exceeded for safe_api_call")

async def place_order(client: Optional[Any], symbol: str, side: str, quantity: float, action: str) -> bool:
    """Place order with validation"""
    try:
        if args.dry_run:
            logging.info(f"[DRY-RUN] Would place order: {symbol} {action} {side} {quantity}")
            return True
        
        if client is None:
            logging.error("❌ No API client available to place orders")
            return False
        
        if not symbol or symbol not in SYMBOLS:
            logging.error(f"❌ Invalid symbol: {symbol}")
            return False
        
        if side not in ["BUY", "SELL"]:
            logging.error(f"❌ Invalid side: {side}")
            return False
        
        quantity = round_size(abs(quantity), symbol)
        if quantity <= 0:
            logging.warning(f"⚠️ Zero quantity for {symbol}, skipping order")
            return True
        
        if "LONG" in action.upper():
            position_side = "LONG"
        elif "SHORT" in action.upper():
            position_side = "SHORT"
        else:
            logging.error(f"❌ Unknown action: {action}")
            return False
        
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side
        }
        
        # Some AsyncClient implementations may have futures_create_order, others may use client.futures_create_order
        create_order_fn = getattr(client, "futures_create_order", None)
        if not create_order_fn:
            create_order_fn = getattr(client, "create_order", None)
        if not create_order_fn:
            logging.error("❌ The Binance client does not expose a known order creation function.")
            return False
        
        result = await safe_api_call(create_order_fn, **params)
        if result and isinstance(result, dict) and ('orderId' in result or 'orderId' in json.dumps(result)):
            logging.info(f"🚀 {symbol} {action} SUCCESS - {side} {quantity}")
            return True
        else:
            # Some APIs return different shapes; log full result for debugging
            logging.debug(f"Order response: {result}")
            logging.error(f"❌ {symbol} {action} - Unexpected order response")
            return False
    except Exception as e:
        logging.error(f"❌ {symbol} {action} FAILED: {e}")
        return False

# ========================= LMA CALCULATIONS =========================
def calculate_wma(values: List[float], length: int) -> Optional[float]:
    try:
        if not values or len(values) < length:
            return None
        clean_values = []
        for v in values[-length:]:
            v_float = safe_float(v, None)
            if v_float is None:
                return None
            clean_values.append(v_float)
        if len(clean_values) < length:
            return None
        weights = list(range(1, length + 1))
        weighted_sum = sum(clean_values[i] * weights[i] for i in range(length))
        weight_sum = sum(weights)
        if weight_sum == 0:
            return None
        return weighted_sum / weight_sum
    except Exception as e:
        logging.debug(f"WMA calculation error: {e}")
        return None

def calculate_sma(values: List[float], length: int) -> Optional[float]:
    try:
        if not values or len(values) < length:
            return None
        clean_values = []
        for v in values[-length:]:
            v_float = safe_float(v, None)
            if v_float is None:
                return None
            clean_values.append(v_float)
        if len(clean_values) < length:
            return None
        return sum(clean_values) / length
    except Exception as e:
        logging.debug(f"SMA calculation error: {e}")
        return None

def calculate_lma(symbol: str) -> Optional[float]:
    try:
        st = state[symbol]
        klines = st["klines"]
        
        if len(klines) < LMA_LENGTH:
            return None
        
        if USE_LIVE_CANDLE:
            completed = list(klines)
        else:
            completed = list(klines)[:-1]
        
        if len(completed) < LMA_LENGTH:
            return None
        
        closes = []
        for k in completed[-LMA_LENGTH:]:
            if not validate_kline(k):
                continue
            close_price = safe_float(k.get("close"), None)
            if close_price is None or close_price <= 0:
                continue
            closes.append(close_price)
        
        if len(closes) < LMA_LENGTH:
            return None
        
        wma_value = calculate_wma(closes, LMA_LENGTH)
        sma_value = calculate_sma(closes, LMA_LENGTH)
        
        if wma_value is None or sma_value is None:
            return None
        
        lma = 2 * wma_value - sma_value
        return lma
    except Exception as e:
        logging.debug(f"❌ LMA calc error {symbol}: {e}")
        return None

# ========================= TP/SL MANAGEMENT =========================
def initialize_tpsl(symbol: str, side: str, entry_price: float):
    try:
        st = state[symbol]
        entry_price = validate_price(entry_price, symbol)
        if entry_price is None:
            logging.error(f"❌ {symbol} Invalid entry price for {side}")
            return
        if side == "LONG":
            st["long_entry_price"] = entry_price
            st["long_tp_price"] = entry_price * (1 + LONG_TP_PERCENT / 100)
            st["long_sl_price"] = entry_price * (1 - LONG_SL_PERCENT / 100)
            logging.info(f"🎯 {symbol} LONG initialized Entry:{entry_price:.6f} TP:{st['long_tp_price']:.6f} SL:{st['long_sl_price']:.6f}")
        elif side == "SHORT":
            st["short_entry_price"] = entry_price
            st["short_tp_price"] = entry_price * (1 - SHORT_TP_PERCENT / 100)
            st["short_sl_price"] = entry_price * (1 + SHORT_SL_PERCENT / 100)
            logging.info(f"🎯 {symbol} SHORT initialized Entry:{entry_price:.6f} TP:{st['short_tp_price']:.6f} SL:{st['short_sl_price']:.6f}")
        else:
            logging.error(f"❌ {symbol} Unknown side: {side}")
            return
        save_positions()
    except Exception as e:
        logging.error(f"❌ Init TP/SL failed {symbol} {side}: {e}")

def check_tpsl(symbol: str, current_price: float) -> Dict[str, str]:
    st = state[symbol]
    result = {"long": "none", "short": "none"}
    
    try:
        current_price = validate_price(current_price, symbol)
        if current_price is None:
            return result
        
        long_pos = safe_float(st["long_position"])
        if long_pos > 0:
            tp_price = safe_float(st["long_tp_price"], None)
            sl_price = safe_float(st["long_sl_price"], None)
            entry_price = safe_float(st["long_entry_price"], None)
            if tp_price is None or sl_price is None or entry_price is None:
                logging.warning(f"⚠️ {symbol} LONG missing TP/SL prices")
            else:
                if current_price >= tp_price:
                    profit_pct = ((current_price - entry_price) / entry_price) * 100
                    logging.info(f"🎯 {symbol} LONG TP HIT: {current_price:.6f} >= {tp_price:.6f} (+{profit_pct:.2f}%)")
                    result["long"] = "tp"
                elif current_price <= sl_price:
                    loss_pct = ((entry_price - current_price) / entry_price) * 100
                    logging.info(f"🛑 {symbol} LONG SL HIT: {current_price:.6f} <= {sl_price:.6f} (-{loss_pct:.2f}%)")
                    result["long"] = "sl"
        
        short_pos = safe_float(st["short_position"])
        if short_pos > 0:
            tp_price = safe_float(st["short_tp_price"], None)
            sl_price = safe_float(st["short_sl_price"], None)
            entry_price = safe_float(st["short_entry_price"], None)
            if tp_price is None or sl_price is None or entry_price is None:
                logging.warning(f"⚠️ {symbol} SHORT missing TP/SL prices")
            else:
                if current_price <= tp_price:
                    profit_pct = ((entry_price - current_price) / entry_price) * 100
                    logging.info(f"🎯 {symbol} SHORT TP HIT: {current_price:.6f} <= {tp_price:.6f} (+{profit_pct:.2f}%)")
                    result["short"] = "tp"
                elif current_price >= sl_price:
                    loss_pct = ((current_price - entry_price) / entry_price) * 100
                    logging.info(f"🛑 {symbol} SHORT SL HIT: {current_price:.6f} >= {sl_price:.6f} (-{loss_pct:.2f}%)")
                    result["short"] = "sl"
    except Exception as e:
        logging.error(f"❌ TP/SL check error {symbol}: {e}")
    return result

def reset_position(symbol: str, side: str):
    try:
        st = state[symbol]
        if side == "LONG":
            st["long_entry_price"] = None
            st["long_tp_price"] = None
            st["long_sl_price"] = None
            st["long_entry_allowed"] = True
            logging.info(f"🔓 {symbol} LONG re-enabled")
        elif side == "SHORT":
            st["short_entry_price"] = None
            st["short_tp_price"] = None
            st["short_sl_price"] = None
            st["short_entry_allowed"] = True
            logging.info(f"🔓 {symbol} SHORT re-enabled")
        else:
            logging.error(f"❌ {symbol} Unknown side in reset: {side}")
            return
        save_positions()
    except Exception as e:
        logging.error(f"❌ Reset position error {symbol} {side}: {e}")

# ========================= TRADING SIGNALS =========================
def update_trading_signals(symbol: str) -> Dict[str, bool]:
    st = state[symbol]
    result = {"long_entry": False, "short_entry": False}
    
    try:
        if not st["ready"]:
            return result
        price = validate_price(st["price"], symbol)
        if price is None:
            return result
        lma = calculate_lma(symbol)
        if lma is None:
            return result
        st["lma"] = lma
        prev_lma = safe_float(st["prev_lma"], None)
        if prev_lma is None:
            st["prev_lma"] = lma
            return result
        long_pos = safe_float(st["long_position"])
        short_pos = safe_float(st["short_position"])
        now = time.time()
        last_long_signal = safe_float(st["last_long_signal_ts"])
        last_short_signal = safe_float(st["last_short_signal_ts"])
        lma_rising = lma > prev_lma
        price_above_lma = price > lma
        
        if (lma_rising and price_above_lma and 
            long_pos == 0 and 
            st["long_entry_allowed"] and 
            (now - last_long_signal) >= SIGNAL_COOLDOWN_SECONDS):
            result["long_entry"] = True
            st["long_entry_allowed"] = False
            st["last_long_signal_ts"] = now
            save_positions()
            logging.info(f"🟢 {symbol} LONG ENTRY SIGNAL Price:{price:.6f} LMA:{lma:.6f} (rising from {prev_lma:.6f})")
        
        lma_falling = lma < prev_lma
        price_below_lma = price < lma
        
        if (lma_falling and price_below_lma and 
            short_pos == 0 and 
            st["short_entry_allowed"] and 
            (now - last_short_signal) >= SIGNAL_COOLDOWN_SECONDS):
            result["short_entry"] = True
            st["short_entry_allowed"] = False
            st["last_short_signal_ts"] = now
            save_positions()
            logging.info(f"🔴 {symbol} SHORT ENTRY SIGNAL Price:{price:.6f} LMA:{lma:.6f} (falling from {prev_lma:.6f})")
        
        st["prev_lma"] = lma
    except Exception as e:
        logging.error(f"❌ Signal generation error {symbol}: {e}")
    return result

# ========================= EXECUTION =========================
async def execute_open_position(client: Optional[Any], symbol: str, side: str, size: float) -> bool:
    try:
        st = state[symbol]
        now = time.time()
        if side == "LONG":
            if (now - st["last_long_order_ts"]) < ORDER_COOLDOWN_SECONDS:
                logging.debug(f"⏳ {symbol} LONG order cooldown active")
                return False
            st["last_long_order_ts"] = now
        elif side == "SHORT":
            if (now - st["last_short_order_ts"]) < ORDER_COOLDOWN_SECONDS:
                logging.debug(f"⏳ {symbol} SHORT order cooldown active")
                return False
            st["last_short_order_ts"] = now
        else:
            logging.error(f"❌ {symbol} Unknown side: {side}")
            return False
        
        order_side = "BUY" if side == "LONG" else "SELL"
        success = await place_order(client, symbol, order_side, size, f"{side} ENTRY")
        return success
    except Exception as e:
        logging.error(f"❌ Execute open error {symbol} {side}: {e}")
        return False

async def execute_close_position(client: Optional[Any], symbol: str, side: str, size: float) -> bool:
    try:
        order_side = "SELL" if side == "LONG" else "BUY"
        success = await place_order(client, symbol, order_side, size, f"{side} CLOSE")
        return success
    except Exception as e:
        logging.error(f"❌ Execute close error {symbol} {side}: {e}")
        return False

# ========================= WEBSOCKET PRICE FEED =========================
async def price_feed_loop(client: Optional[Any]):
    """WebSocket price feed with reconnection logic"""
    if args.disable_ws:
        logging.info("⚠️ WebSocket feed disabled via CLI (--disable-ws).")
        return
    streams = [f"{s.lower()}@kline_{BASE_TIMEFRAME.lower()}" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    reconnect_delay = 1
    max_reconnect_delay = 60
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("📡 WebSocket connected")
                reconnect_delay = 1
                async for message in ws:
                    try:
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if "data" not in data:
                            continue
                        data = data.get("data", {})
                        if not isinstance(data, dict):
                            continue
                        if "k" not in data:
                            continue
                        k = data.get("k", {})
                        if not isinstance(k, dict):
                            continue
                        symbol = k.get("s")
                        if not symbol or symbol not in SYMBOLS:
                            continue
                        required_fields = ["c", "o", "h", "l", "t"]
                        if not all(field in k for field in required_fields):
                            logging.debug(f"{symbol} Missing kline fields")
                            continue
                        price = validate_price(k.get("c"), symbol)
                        if price is None:
                            logging.debug(f"{symbol} Invalid price")
                            continue
                        state[symbol]["price"] = price
                        try:
                            kline_data = {
                                "open_time": safe_int(k.get("t", 0)) // 1000,
                                "open": safe_float(k.get("o")),
                                "high": safe_float(k.get("h")),
                                "low": safe_float(k.get("l")),
                                "close": price
                            }
                        except Exception as e:
                            logging.debug(f"{symbol} Kline parse error: {e}")
                            continue
                        if not validate_kline(kline_data):
                            continue
                        klines = state[symbol]["klines"]
                        if len(klines) > 0 and klines[-1]["open_time"] == kline_data["open_time"]:
                            klines[-1] = kline_data
                        else:
                            klines.append(kline_data)
                        if not state[symbol]["ready"]:
                            if len(klines) >= LMA_LENGTH:
                                lma = calculate_lma(symbol)
                                if lma is not None:
                                    state[symbol]["ready"] = True
                                    logging.info(f"✅ {symbol} ready (LMA calculated)")
                    except Exception as e:
                        logging.debug(f"Message processing error: {e}")
                        continue
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"⚠️ WebSocket closed ({e}), reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
        except Exception as e:
            logging.error(f"❌ WebSocket error: {e}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

# ========================= TRADING LOOP =========================
async def trading_loop(client: Optional[Any]):
    loop_iteration = 0
    while True:
        try:
            await asyncio.sleep(0.5)
            loop_iteration += 1
            for symbol in SYMBOLS:
                try:
                    st = state[symbol]
                    if not st["ready"]:
                        continue
                    price = validate_price(st["price"], symbol)
                    if price is None:
                        continue
                    long_pos = safe_float(st["long_position"])
                    short_pos = safe_float(st["short_position"])
                    # Priority: TP/SL check
                    if long_pos > 0:
                        if st["long_tp_price"] is None or st["long_sl_price"] is None:
                            logging.warning(f"⚠️ {symbol} LONG missing TP/SL, initializing...")
                            initialize_tpsl(symbol, "LONG", price)
                    if short_pos > 0:
                        if st["short_tp_price"] is None or st["short_sl_price"] is None:
                            logging.warning(f"⚠️ {symbol} SHORT missing TP/SL, initializing...")
                            initialize_tpsl(symbol, "SHORT", price)
                    tpsl_result = check_tpsl(symbol, price)
                    if tpsl_result["long"] != "none" and long_pos > 0:
                        success = await execute_close_position(client, symbol, "LONG", long_pos)
                        if success:
                            st["long_position"] = 0.0
                            reset_position(symbol, "LONG")
                            save_positions()
                            continue
                    if tpsl_result["short"] != "none" and short_pos > 0:
                        success = await execute_close_position(client, symbol, "SHORT", short_pos)
                        if success:
                            st["short_position"] = 0.0
                            reset_position(symbol, "SHORT")
                            save_positions()
                            continue
                    # Entry signals
                    signals = update_trading_signals(symbol)
                    long_pos = safe_float(st["long_position"])
                    short_pos = safe_float(st["short_position"])
                    if signals["long_entry"] and long_pos == 0:
                        target_size = SYMBOLS[symbol]
                        success = await execute_open_position(client, symbol, "LONG", target_size)
                        if success:
                            st["long_position"] = target_size
                            initialize_tpsl(symbol, "LONG", price)
                            save_positions()
                        else:
                            st["long_entry_allowed"] = True
                            save_positions()
                    if signals["short_entry"] and short_pos == 0:
                        target_size = SYMBOLS[symbol]
                        success = await execute_open_position(client, symbol, "SHORT", target_size)
                        if success:
                            st["short_position"] = target_size
                            initialize_tpsl(symbol, "SHORT", price)
                            save_positions()
                        else:
                            st["short_entry_allowed"] = True
                            save_positions()
                except Exception as e:
                    logging.error(f"❌ Trading loop error for {symbol}: {e}")
                    continue
            if loop_iteration % 100 == 0:
                save_klines()
        except Exception as e:
            logging.error(f"❌ Critical trading loop error: {e}")
            await asyncio.sleep(1)

# ========================= STATUS LOGGER =========================
async def status_logger():
    while True:
        try:
            await asyncio.sleep(120)
            current_time = time.strftime("%H:%M:%S", time.localtime())
            logging.info("=" * 80)
            logging.info(f"📊 STATUS UPDATE - {current_time}")
            logging.info("=" * 80)
            for symbol in SYMBOLS:
                st = state[symbol]
                if not st["ready"]:
                    candle_count = len(st["klines"])
                    logging.info(f"{symbol}: Loading... ({candle_count}/{LMA_LENGTH} candles)")
                    continue
                price = validate_price(st["price"], symbol)
                lma = safe_float(st.get("lma"), None)
                prev_lma = safe_float(st.get("prev_lma"), None)
                if price and lma is not None:
                    if prev_lma is not None:
                        if lma > prev_lma:
                            trend = "BULL ▲"
                        elif lma < prev_lma:
                            trend = "BEAR ▼"
                        else:
                            trend = "FLAT ═"
                    else:
                        trend = "INIT"
                    logging.info(f"\n{symbol}:")
                    logging.info(f"  Price: ${price:.6f} | LMA: ${lma:.6f} | {trend}")
                    long_pos = safe_float(st["long_position"])
                    if long_pos > 0:
                        entry = safe_float(st["long_entry_price"], None)
                        tp = safe_float(st["long_tp_price"], None)
                        sl = safe_float(st["long_sl_price"], None)
                        if entry and entry > 0:
                            pnl = ((price - entry) / entry) * 100
                            pnl_sign = "+" if pnl >= 0 else ""
                            logging.info(f"  🟢 LONG: {long_pos} units | PnL: {pnl_sign}{pnl:.2f}%")
                            if tp:
                                logging.info(f"     Entry: ${entry:.6f} | TP: ${tp:.6f} | SL: ${sl:.6f}")
                    else:
                        lock = "🔒" if not st["long_entry_allowed"] else "🔓"
                        logging.info(f"  🟢 LONG: Flat {lock}")
                    short_pos = safe_float(st["short_position"])
                    if short_pos > 0:
                        entry = safe_float(st["short_entry_price"], None)
                        tp = safe_float(st["short_tp_price"], None)
                        sl = safe_float(st["short_sl_price"], None)
                        if entry and entry > 0:
                            pnl = ((entry - price) / entry) * 100
                            pnl_sign = "+" if pnl >= 0 else ""
                            logging.info(f"  🔴 SHORT: {short_pos} units | PnL: {pnl_sign}{pnl:.2f}%")
                            if tp:
                                logging.info(f"     Entry: ${entry:.6f} | TP: ${tp:.6f} | SL: ${sl:.6f}")
                    else:
                        lock = "🔒" if not st["short_entry_allowed"] else "🔓"
                        logging.info(f"  🔴 SHORT: Flat {lock}")
            logging.info("=" * 80)
        except Exception as e:
            logging.error(f"❌ Status logger error: {e}")

# ========================= POSITION SANITY CHECK =========================
async def position_sanity_check(client: Optional[Any]):
    while True:
        try:
            await asyncio.sleep(300)
            logging.info("🔍 Running position sanity check...")
            if args.dry_run:
                logging.debug("Dry-run: skipping exchange sanity check")
                continue
            account_info = None
            try:
                # Some clients expose futures_account, others expose futures_account_balance; be defensive.
                if hasattr(client, "futures_account"):
                    account_info = await safe_api_call(client.futures_account)
                elif hasattr(client, "futures_account_balance"):
                    account_info = await safe_api_call(client.futures_account_balance)
                else:
                    logging.warning("⚠️ Client does not expose known account fetching functions")
                    continue
            except Exception as e:
                logging.warning(f"⚠️ Could not fetch account info: {e}")
                continue
            if not account_info or not isinstance(account_info, dict):
                logging.warning("⚠️ Could not fetch account info or invalid format")
                continue
            positions = account_info.get('positions', [])
            if not isinstance(positions, list):
                logging.warning("⚠️ Invalid positions data")
                continue
            exchange_positions = {}
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                symbol = pos.get('symbol')
                if symbol not in SYMBOLS:
                    continue
                amt = safe_float(pos.get('positionAmt'))
                side = pos.get('positionSide')
                if side in ["LONG", "SHORT"]:
                    exchange_positions[f"{symbol}_{side}"] = abs(amt)
            mismatches = 0
            for symbol in SYMBOLS:
                st = state[symbol]
                local_long = safe_float(st["long_position"])
                local_short = safe_float(st["short_position"])
                exchange_long = exchange_positions.get(f"{symbol}_LONG", 0.0)
                exchange_short = exchange_positions.get(f"{symbol}_SHORT", 0.0)
                if abs(local_long - exchange_long) > 0.001:
                    logging.warning(f"⚠️ [{symbol}] LONG mismatch: Local={local_long:.4f}, Exchange={exchange_long:.4f}")
                    if exchange_long == 0 and local_long > 0:
                        logging.warning(f"🔄 [{symbol}] Clearing phantom LONG")
                        st["long_position"] = 0.0
                        reset_position(symbol, "LONG")
                        mismatches += 1
                    elif exchange_long > 0 and local_long == 0:
                        logging.warning(f"🔄 [{symbol}] Syncing missing LONG")
                        st["long_position"] = exchange_long
                        st["long_entry_allowed"] = False
                        if st["price"]:
                            initialize_tpsl(symbol, "LONG", st["price"])
                        mismatches += 1
                if abs(local_short - exchange_short) > 0.001:
                    logging.warning(f"⚠️ [{symbol}] SHORT mismatch: Local={local_short:.4f}, Exchange={exchange_short:.4f}")
                    if exchange_short == 0 and local_short > 0:
                        logging.warning(f"🔄 [{symbol}] Clearing phantom SHORT")
                        st["short_position"] = 0.0
                        reset_position(symbol, "SHORT")
                        mismatches += 1
                    elif exchange_short > 0 and local_short == 0:
                        logging.warning(f"🔄 [{symbol}] Syncing missing SHORT")
                        st["short_position"] = exchange_short
                        st["short_entry_allowed"] = False
                        if st["price"]:
                            initialize_tpsl(symbol, "SHORT", st["price"])
                        mismatches += 1
            if mismatches > 0:
                logging.info(f"✅ Synced {mismatches} position(s)")
                save_positions()
            else:
                logging.info("✅ All positions in sync")
        except Exception as e:
            logging.error(f"❌ Sanity check error: {e}")

# ========================= POSITION RECOVERY =========================
async def recover_positions_from_exchange(client: Optional[Any]):
    logging.info("🔍 Checking exchange for existing positions...")
    if args.dry_run:
        logging.debug("Dry-run: skipping recover_positions_from_exchange")
        return
    try:
        account_info = None
        if hasattr(client, "futures_account"):
            account_info = await safe_api_call(client.futures_account)
        elif hasattr(client, "futures_account_balance"):
            account_info = await safe_api_call(client.futures_account_balance)
        else:
            logging.warning("⚠️ Client does not expose known account info functions")
            return
        if not account_info or not isinstance(account_info, dict):
            logging.warning("⚠️ Could not fetch account info")
            return
        positions = account_info.get('positions', [])
        if not isinstance(positions, list):
            logging.warning("⚠️ Invalid positions data")
            return
        recovered_count = 0
        for position in positions:
            try:
                if not isinstance(position, dict):
                    continue
                symbol = position.get('symbol')
                if not symbol or symbol not in SYMBOLS:
                    continue
                position_amt = safe_float(position.get('positionAmt'))
                entry_price = safe_float(position.get('entryPrice'), None)
                mark_price = safe_float(position.get('markPrice'), None)
                position_side = position.get('positionSide')
                if abs(position_amt) > 0.0001:
                    if position_side == "LONG" and position_amt > 0:
                        logging.info(f"♻️ [{symbol}] RECOVERED LONG: {position_amt}")
                        state[symbol]["long_position"] = position_amt
                        state[symbol]["long_entry_price"] = entry_price
                        state[symbol]["long_entry_allowed"] = False
                        recovered_count += 1
                        init_price = mark_price if mark_price else entry_price
                        if init_price and init_price > 0:
                            initialize_tpsl(symbol, "LONG", init_price)
                    elif position_side == "SHORT" and position_amt < 0:
                        logging.info(f"♻️ [{symbol}] RECOVERED SHORT: {abs(position_amt)}")
                        state[symbol]["short_position"] = abs(position_amt)
                        state[symbol]["short_entry_price"] = entry_price
                        state[symbol]["short_entry_allowed"] = False
                        recovered_count += 1
                        init_price = mark_price if mark_price else entry_price
                        if init_price and init_price > 0:
                            initialize_tpsl(symbol, "SHORT", init_price)
            except Exception as e:
                logging.error(f"Error processing position: {e}")
                continue
        if recovered_count > 0:
            logging.info(f"✅ Recovered {recovered_count} position(s)")
            save_positions()
        else:
            logging.info("✅ No open positions on exchange")
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")

# ========================= INITIALIZATION =========================
async def init_bot(client: Optional[Any]):
    try:
        logging.info("=" * 80)
        logging.info("🔧 INITIALIZING BOT")
        logging.info("=" * 80)
        logging.info(f"📊 Strategy: Leo Moving Average (LMA)")
        logging.info(f"📊 Formula: LMA = 2 × WMA({LMA_LENGTH}) - SMA({LMA_LENGTH})")
        logging.info(f"📊 Timeframe: {BASE_TIMEFRAME}")
        logging.info(f"📊 Mode: HEDGE (LONG + SHORT simultaneous)")
        logging.info("")
        logging.info(f"📊 TP/SL Configuration:")
        logging.info(f"   LONG:  TP = +{LONG_TP_PERCENT}% | SL = -{LONG_SL_PERCENT}%")
        logging.info(f"   SHORT: TP = +{SHORT_TP_PERCENT}% | SL = -{SHORT_SL_PERCENT}%")
        logging.info("")
        logging.info(f"📊 Entry Signals:")
        logging.info(f"   LONG:  LMA rising + Price > LMA")
        logging.info(f"   SHORT: LMA falling + Price < LMA")
        logging.info("")
        logging.info(f"📊 Exit: Fixed TP/SL only (no cross-signal exits)")
        logging.info("=" * 80)
        load_klines()
        load_positions()
        await recover_positions_from_exchange(client)
        symbols_needing_data = []
        for symbol in SYMBOLS:
            klines = state[symbol]["klines"]
            if len(klines) >= LMA_LENGTH:
                lma = calculate_lma(symbol)
                if lma is not None:
                    state[symbol]["ready"] = True
                    logging.info(f"✅ {symbol} ready (loaded from disk)")
                else:
                    symbols_needing_data.append(symbol)
            else:
                symbols_needing_data.append(symbol)
        if symbols_needing_data:
            logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbol(s)...")
            for i, symbol in enumerate(symbols_needing_data):
                try:
                    if args.dry_run:
                        logging.info(f"[DRY-RUN] Skipping historical fetch for {symbol}")
                        continue
                    logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})")
                    needed_candles = LMA_LENGTH + 50
                    # Defensive: prefer futures_mark_price_klines but fallback to client.get_klines
                    klines_data = None
                    if hasattr(client, "futures_mark_price_klines"):
                        klines_data = await safe_api_call(
                            client.futures_mark_price_klines,
                            symbol=symbol,
                            interval=BASE_TIMEFRAME,
                            limit=min(needed_candles, 1500)
                        )
                    elif hasattr(client, "futures_klines"):
                        klines_data = await safe_api_call(
                            client.futures_klines,
                            symbol=symbol,
                            interval=BASE_TIMEFRAME,
                            limit=min(needed_candles, 1500)
                        )
                    elif hasattr(client, "get_klines"):
                        klines_data = await safe_api_call(
                            client.get_klines,
                            symbol=symbol,
                            interval=BASE_TIMEFRAME,
                            limit=min(needed_candles, 1500)
                        )
                    else:
                        logging.warning("⚠️ Client does not provide known kline fetching functions; skipping historical fetch.")
                        continue
                    if not klines_data or not isinstance(klines_data, list):
                        logging.warning(f"⚠️ {symbol} No data returned")
                        continue
                    state[symbol]["klines"].clear()
                    for kline in klines_data:
                        try:
                            # kline may be list-like or dict-like depending on API
                            if isinstance(kline, dict):
                                kline_data = {
                                    "open_time": safe_int(kline.get('openTime', kline.get('t', 0))) // 1000,
                                    "open": safe_float(kline.get('open', kline.get('o'))),
                                    "high": safe_float(kline.get('high', kline.get('h'))),
                                    "low": safe_float(kline.get('low', kline.get('l'))),
                                    "close": safe_float(kline.get('close', kline.get('c')))
                                }
                            else:
                                # list-format: [open_time, open, high, low, close, ...]
                                kline_data = {
                                    "open_time": safe_int(kline[0]) // 1000,
                                    "open": safe_float(kline[1]),
                                    "high": safe_float(kline[2]),
                                    "low": safe_float(kline[3]),
                                    "close": safe_float(kline[4])
                                }
                            if validate_kline(kline_data):
                                state[symbol]["klines"].append(kline_data)
                        except Exception as e:
                            logging.debug(f"Kline parse error: {e}")
                            continue
                    lma = calculate_lma(symbol)
                    if lma is not None:
                        state[symbol]["ready"] = True
                        logging.info(f"✅ {symbol} ready ({len(state[symbol]['klines'])} candles)")
                    else:
                        logging.warning(f"⚠️ {symbol} LMA calculation failed")
                    if i < len(symbols_needing_data) - 1:
                        await asyncio.sleep(2)
                except Exception as e:
                    logging.error(f"❌ {symbol} fetch failed: {e}")
                    if i < len(symbols_needing_data) - 1:
                        await asyncio.sleep(2)
        save_klines()
        logging.info("=" * 80)
        logging.info("🚀 INITIALIZATION COMPLETE")
        logging.info("=" * 80)
    except Exception as e:
        logging.error(f"❌ Initialization failed: {e}")
        raise

# ========================= MAIN =========================
async def main():
    # If AsyncClient couldn't be imported and not running dry-run, error out with guidance
    if not args.dry_run and AsyncClient is None:
        logging.error("❌ Could not import Binance AsyncClient. Try `pip install python-binance` or similar.")
        sys.exit(1)
    client = None
    if not args.dry_run:
        try:
            # create client, but be defensive: library may require different create signature
            if AsyncClient is None:
                raise RuntimeError("AsyncClient class not available")
            # Many versions require AsyncClient.create(api_key, api_secret)
            if hasattr(AsyncClient, "create"):
                client = await AsyncClient.create(API_KEY, API_SECRET)
            else:
                # Try direct instantiation (less common)
                client = AsyncClient(API_KEY, API_SECRET)
            logging.info("✅ Binance client created")
        except Exception as e:
            logging.error(f"❌ Failed to create client: {e}")
            # If client creation failed, and not dry-run, stop;
            # but provide option to continue in dry-run if user set that flag (handled earlier).
            sys.exit(1)
    else:
        client = None
    atexit.register(save_klines)
    atexit.register(save_positions)
    try:
        await init_bot(client)
        tasks = []
        if not args.disable_ws:
            tasks.append(asyncio.create_task(price_feed_loop(client)))
        tasks.append(asyncio.create_task(trading_loop(client)))
        tasks.append(asyncio.create_task(status_logger()))
        tasks.append(asyncio.create_task(position_sanity_check(client)))
        logging.info("🚀 BOT STARTED - All systems running")
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logging.info("\n🛑 Bot stopped by user")
    except Exception as e:
        logging.error(f"❌ Critical error: {e}")
        raise
    finally:
        logging.info("🔄 Saving final state...")
        save_klines()
        save_positions()
        try:
            if client and hasattr(client, "close_connection"):
                await client.close_connection()
            elif client and hasattr(client, "close"):
                await client.close()
        except Exception as e:
            logging.debug(f"Error closing client: {e}")
        logging.info("✅ Shutdown complete")

# ========================= ENTRY POINT =========================
if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("LEO MOVING AVERAGE (LMA) TRADING BOT - DEFENSIVE EDITION")
    print("=" * 80)
    print(f"Strategy: LMA = 2 × WMA({LMA_LENGTH}) - SMA({LMA_LENGTH})")
    print(f"Timeframe: {BASE_TIMEFRAME}")
    print("")
    print("ENTRY SIGNALS:")
    print("  🟢 LONG:  LMA rising (green) + Price above LMA")
    print("  🔴 SHORT: LMA falling (red) + Price below LMA")
    print("")
    print("EXIT STRATEGY (Fixed TP/SL - NO flip-flopping):")
    print(f"  LONG:  TP = +{LONG_TP_PERCENT}%  |  SL = -{LONG_SL_PERCENT}%")
    print(f"  SHORT: TP = +{SHORT_TP_PERCENT}%  |  SL = -{SHORT_SL_PERCENT}%")
    print("")
    print("HEDGE MODE FEATURES:")
    print("  ✅ Both LONG and SHORT can be open simultaneously")
    print("  ✅ Each position has independent entry/exit")
    print("  ✅ Positions exit ONLY on TP or SL hit")
    print("  ✅ No cross-signal exits or flip-flopping")
    print("")
    print("SAFETY FEATURES:")
    print("  ✅ Defensive API retries/backoff")
    print("  ✅ Dry-run mode (--dry-run)")
    print("  ✅ Signal cooldowns and order cooldowns")
    print("  ✅ Backup file system and restore")
    print("=" * 80)
    print(f"Symbols: {len(SYMBOLS)} - {', '.join(SYMBOLS.keys())}")
    print(f"Data Directory: {DATA_DIR}")
    print("=" * 80)
    print("")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        logging.error(f"❌ Fatal error: {e}")
        sys.exit(1)
