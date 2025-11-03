#!/usr/bin/env python3
"""
Leo Moving Average (LMA) Hedge Trading Bot
Production-grade with comprehensive error handling
"""
import os
import json
import asyncio
import logging
import websockets
import time
import atexit
import sys
from pathlib import Path
from binance import AsyncClient
from collections import deque
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

# ========================= CONFIG =========================
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

# Validate credentials at startup
if not API_KEY or not API_SECRET or API_KEY == "your_api_key_here":
    logging.error("❌ MISSING API CREDENTIALS! Check your .env file")
    sys.exit(1)

LEVERAGE = int(os.getenv("LEVERAGE", "50"))
USE_LIVE_CANDLE = True  # True = use live updating candle, False = wait for close

# ========================= LMA PARAMETERS =========================
LMA_LENGTH = 15  # Leo Moving Average period

# ========================= FIXED TP/SL PERCENTAGES =========================
# These are the ONLY exit conditions - no cross-signals
LONG_TP_PERCENT = 1.5   # Long take profit: 1.5%
LONG_SL_PERCENT = 1.2   # Long stop loss: 1.2%
SHORT_TP_PERCENT = 1.5  # Short take profit: 1.5%
SHORT_SL_PERCENT = 1.2  # Short stop loss: 1.2%

# Validate percentages are positive
assert LONG_TP_PERCENT > 0 and LONG_SL_PERCENT > 0, "TP/SL must be positive"
assert SHORT_TP_PERCENT > 0 and SHORT_SL_PERCENT > 0, "TP/SL must be positive"

# ========================= TIMEFRAME CONFIG =========================
BASE_TIMEFRAME = "15m"

SUPPORTED_TIMEFRAMES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440
}

if BASE_TIMEFRAME not in SUPPORTED_TIMEFRAMES:
    logging.error(f"❌ Invalid timeframe: {BASE_TIMEFRAME}")
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
        PRECISIONS[sym] = 3  # Default precision
        logging.warning(f"⚠️ No precision for {sym}, using default: 3")

KLINE_LIMIT = max(200, LMA_LENGTH + 100)

# ========================= ANTI-SPAM CONFIG =========================
# Prevent signal spam and rapid re-entry
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
            # Price data
            "price": None,
            "klines": deque(maxlen=KLINE_LIMIT),
            
            # LMA indicator
            "lma": None,
            "prev_lma": None,
            "ready": False,
            
            # LONG position
            "long_position": 0.0,
            "long_entry_price": None,
            "long_tp_price": None,
            "long_sl_price": None,
            "long_entry_allowed": True,
            "last_long_signal_ts": 0.0,
            "last_long_order_ts": 0.0,
            
            # SHORT position
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
        # Clear existing handlers
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        
        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)
        
        # File handler
        file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        
        # Configure root logger
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
    """Safely convert to float with fallback"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert to int with fallback"""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default

def safe_bool(value: Any, default: bool = True) -> bool:
    """Safely convert to bool with fallback"""
    try:
        if value is None:
            return default
        return bool(value)
    except (TypeError, ValueError):
        return default

def validate_price(price: Any, symbol: str) -> Optional[float]:
    """Validate price is a positive number"""
    try:
        price_float = safe_float(price, None)
        if price_float is None or price_float <= 0:
            return None
        return price_float
    except Exception:
        return None

def validate_kline(kline: Dict) -> bool:
    """Validate kline has all required fields"""
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
    """Safely save JSON with backup and corruption protection"""
    try:
        # Create backup if file exists
        if filepath.exists():
            backup = filepath.with_suffix('.json.backup')
            try:
                filepath.rename(backup)
            except Exception as e:
                logging.debug(f"Backup creation skipped: {e}")
        
        # Write to temp file first
        temp_file = filepath.with_suffix('.json.temp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        
        # Verify it's valid JSON by reading it back
        with open(temp_file, 'r', encoding='utf-8') as f:
            json.load(f)
        
        # Only now replace the original
        temp_file.rename(filepath)
        logging.debug(f"✅ Saved: {filepath.name}")
    except Exception as e:
        logging.error(f"❌ Failed to save {filepath.name}: {e}")

def safe_json_load(filepath: Path) -> Optional[Dict]:
    """Safely load JSON with fallback to backup"""
    # Try main file
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
    
    # Try backup
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
    """Save klines to disk"""
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
    """Load klines from disk"""
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
    """Save positions to disk"""
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
    """Load positions from disk"""
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
                
                # Load LONG position
                st["long_position"] = safe_float(pos_data.get("long_position"))
                st["long_entry_price"] = safe_float(pos_data.get("long_entry_price"), None)
                st["long_tp_price"] = safe_float(pos_data.get("long_tp_price"), None)
                st["long_sl_price"] = safe_float(pos_data.get("long_sl_price"), None)
                st["long_entry_allowed"] = safe_bool(pos_data.get("long_entry_allowed"))
                
                # Load SHORT position
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
    """Round size to symbol precision"""
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
    
    # Rate limit check
    now = time.time()
    if now - api_calls_reset_time > 60:
        api_calls_count = 0
        api_calls_reset_time = now
    
    if api_calls_count >= 10:
        wait_time = 60 - (now - api_calls_reset_time)
        if wait_time > 0:
            logging.warning(f"⏳ Rate limit wait: {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            api_calls_count = 0
            api_calls_reset_time = time.time()
    
    # Retry logic
    for attempt in range(3):
        try:
            api_calls_count += 1
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            error_str = str(e).lower()
            
            # Handle rate limit
            if "1003" in str(e) or "too many requests" in error_str or "rate" in error_str:
                wait_time = (2 ** attempt) * 60
                logging.warning(f"⏳ Rate limited, retry {attempt+1}/3 in {wait_time}s")
                await asyncio.sleep(wait_time)
                continue
            
            # Handle other errors
            if attempt == 2:
                logging.error(f"❌ API call failed after 3 attempts: {e}")
                raise
            
            await asyncio.sleep(2 ** attempt)
    
    raise Exception("Max retries exceeded")

async def place_order(client: AsyncClient, symbol: str, side: str, quantity: float, action: str) -> bool:
    """Place order with validation"""
    try:
        # Validate inputs
        if not symbol or symbol not in SYMBOLS:
            logging.error(f"❌ Invalid symbol: {symbol}")
            return False
        
        if side not in ["BUY", "SELL"]:
            logging.error(f"❌ Invalid side: {side}")
            return False
        
        quantity = round_size(abs(quantity), symbol)
        if quantity <= 0:
            logging.warning(f"⚠️ Zero quantity for {symbol}")
            return True  # Not an error, just skip
        
        # Determine position side
        if "LONG" in action.upper():
            position_side = "LONG"
        elif "SHORT" in action.upper():
            position_side = "SHORT"
        else:
            logging.error(f"❌ Unknown action: {action}")
            return False
        
        # Build order params
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": position_side
        }
        
        # Execute order
        result = await safe_api_call(client.futures_create_order, **params)
        
        if result and isinstance(result, dict) and 'orderId' in result:
            logging.info(f"🚀 {symbol} {action} SUCCESS - {side} {quantity}")
            return True
        else:
            logging.error(f"❌ {symbol} {action} - No orderId in response")
            return False
            
    except Exception as e:
        logging.error(f"❌ {symbol} {action} FAILED: {e}")
        return False

# ========================= LMA CALCULATIONS =========================
def calculate_wma(values: List[float], length: int) -> Optional[float]:
    """Weighted Moving Average with validation"""
    try:
        if not values or len(values) < length:
            return None
        
        # Validate all values are numbers
        clean_values = []
        for v in values[-length:]:
            v_float = safe_float(v, None)
            if v_float is None:
                return None
            clean_values.append(v_float)
        
        if len(clean_values) < length:
            return None
        
        # Calculate WMA
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
    """Simple Moving Average with validation"""
    try:
        if not values or len(values) < length:
            return None
        
        # Validate all values are numbers
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
    """
    Leo Moving Average: LMA = 2 × WMA - SMA
    Returns None if insufficient data or calculation fails
    """
    try:
        st = state[symbol]
        klines = st["klines"]
        
        if len(klines) < LMA_LENGTH:
            return None
        
        # Choose which candles to use
        if USE_LIVE_CANDLE:
            completed = list(klines)
        else:
            completed = list(klines)[:-1]  # Exclude live candle
        
        if len(completed) < LMA_LENGTH:
            return None
        
        # Extract close prices
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
        
        # Calculate components
        wma_value = calculate_wma(closes, LMA_LENGTH)
        sma_value = calculate_sma(closes, LMA_LENGTH)
        
        if wma_value is None or sma_value is None:
            return None
        
        # LMA formula
        lma = 2 * wma_value - sma_value
        return lma
        
    except Exception as e:
        logging.debug(f"❌ LMA calc error {symbol}: {e}")
        return None

# ========================= TP/SL MANAGEMENT =========================
def initialize_tpsl(symbol: str, side: str, entry_price: float):
    """Initialize fixed TP and SL levels"""
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
            logging.info(
                f"🎯 {symbol} LONG initialized\n"
                f"   Entry: ${entry_price:.6f}\n"
                f"   TP:    ${st['long_tp_price']:.6f} (+{LONG_TP_PERCENT}%)\n"
                f"   SL:    ${st['long_sl_price']:.6f} (-{LONG_SL_PERCENT}%)"
            )
        elif side == "SHORT":
            st["short_entry_price"] = entry_price
            st["short_tp_price"] = entry_price * (1 - SHORT_TP_PERCENT / 100)
            st["short_sl_price"] = entry_price * (1 + SHORT_SL_PERCENT / 100)
            logging.info(
                f"🎯 {symbol} SHORT initialized\n"
                f"   Entry: ${entry_price:.6f}\n"
                f"   TP:    ${st['short_tp_price']:.6f} (-{SHORT_TP_PERCENT}%)\n"
                f"   SL:    ${st['short_sl_price']:.6f} (+{SHORT_SL_PERCENT}%)"
            )
        else:
            logging.error(f"❌ {symbol} Unknown side: {side}")
            return
        
        save_positions()
    except Exception as e:
        logging.error(f"❌ Init TP/SL failed {symbol} {side}: {e}")

def check_tpsl(symbol: str, current_price: float) -> Dict[str, str]:
    """
    Check if TP or SL is hit
    Returns: {"long": "tp"/"sl"/"none", "short": "tp"/"sl"/"none"}
    """
    st = state[symbol]
    result = {"long": "none", "short": "none"}
    
    try:
        current_price = validate_price(current_price, symbol)
        if current_price is None:
            return result
        
        # Check LONG position
        long_pos = safe_float(st["long_position"])
        if long_pos > 0:
            tp_price = safe_float(st["long_tp_price"], None)
            sl_price = safe_float(st["long_sl_price"], None)
            entry_price = safe_float(st["long_entry_price"], None)
            
            if tp_price is None or sl_price is None or entry_price is None:
                logging.warning(f"⚠️ {symbol} LONG missing TP/SL prices")
                return result
            
            # TP hit (price went UP past target)
            if current_price >= tp_price:
                profit_pct = ((current_price - entry_price) / entry_price) * 100
                logging.info(f"🎯 {symbol} LONG TP HIT: ${current_price:.6f} >= ${tp_price:.6f} (+{profit_pct:.2f}%)")
                result["long"] = "tp"
            
            # SL hit (price went DOWN past stop)
            elif current_price <= sl_price:
                loss_pct = ((entry_price - current_price) / entry_price) * 100
                logging.info(f"🛑 {symbol} LONG SL HIT: ${current_price:.6f} <= ${sl_price:.6f} (-{loss_pct:.2f}%)")
                result["long"] = "sl"
        
        # Check SHORT position
        short_pos = safe_float(st["short_position"])
        if short_pos > 0:
            tp_price = safe_float(st["short_tp_price"], None)
            sl_price = safe_float(st["short_sl_price"], None)
            entry_price = safe_float(st["short_entry_price"], None)
            
            if tp_price is None or sl_price is None or entry_price is None:
                logging.warning(f"⚠️ {symbol} SHORT missing TP/SL prices")
                return result
            
            # TP hit (price went DOWN past target)
            if current_price <= tp_price:
                profit_pct = ((entry_price - current_price) / entry_price) * 100
                logging.info(f"🎯 {symbol} SHORT TP HIT: ${current_price:.6f} <= ${tp_price:.6f} (+{profit_pct:.2f}%)")
                result["short"] = "tp"
            
            # SL hit (price went UP past stop)
            elif current_price >= sl_price:
                loss_pct = ((current_price - entry_price) / entry_price) * 100
                logging.info(f"🛑 {symbol} SHORT SL HIT: ${current_price:.6f} >= ${sl_price:.6f} (-{loss_pct:.2f}%)")
                result["short"] = "sl"
    
    except Exception as e:
        logging.error(f"❌ TP/SL check error {symbol}: {e}")
    
    return result

def reset_position(symbol: str, side: str):
    """Reset position after close and re-enable entry"""
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
    """
    Generate entry signals based on LMA
    LONG: LMA slope positive (rising) AND price > LMA
    SHORT: LMA slope negative (falling) AND price < LMA
    """
    st = state[symbol]
    result = {"long_entry": False, "short_entry": False}
    
    try:
        # Validate state is ready
        if not st["ready"]:
            return result
        
        # Validate current price
        price = validate_price(st["price"], symbol)
        if price is None:
            return result
        
        # Calculate current LMA
        lma = calculate_lma(symbol)
        if lma is None:
            return result
        
        # Store current LMA
        st["lma"] = lma
        
        # Get previous LMA
        prev_lma = safe_float(st["prev_lma"], None)
        if prev_lma is None:
            # First calculation, initialize and wait for next
            st["prev_lma"] = lma
            return result
        
        # Get current positions
        long_pos = safe_float(st["long_position"])
        short_pos = safe_float(st["short_position"])
        
        # Get timestamps for cooldowns
        now = time.time()
        last_long_signal = safe_float(st["last_long_signal_ts"])
        last_short_signal = safe_float(st["last_short_signal_ts"])
        
        # LONG ENTRY CONDITIONS
        # 1. LMA is rising (current > previous)
        # 2. Price is above LMA
        # 3. No existing LONG position
        # 4. Entry is allowed (not in cooldown)
        # 5. Signal cooldown has passed
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
            logging.info(
                f"🟢 {symbol} LONG ENTRY SIGNAL\n"
                f"   Price: ${price:.6f}\n"
                f"   LMA:   ${lma:.6f} (rising from ${prev_lma:.6f})\n"
                f"   Price above LMA: ✓"
            )
        
        # SHORT ENTRY CONDITIONS
        # 1. LMA is falling (current < previous)
        # 2. Price is below LMA
        # 3. No existing SHORT position
        # 4. Entry is allowed (not in cooldown)
        # 5. Signal cooldown has passed
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
            logging.info(
                f"🔴 {symbol} SHORT ENTRY SIGNAL\n"
                f"   Price: ${price:.6f}\n"
                f"   LMA:   ${lma:.6f} (falling from ${prev_lma:.6f})\n"
                f"   Price below LMA: ✓"
            )
        
        # Update previous LMA for next iteration
        st["prev_lma"] = lma
        
    except Exception as e:
        logging.error(f"❌ Signal generation error {symbol}: {e}")
    
    return result

# ========================= EXECUTION =========================
async def execute_open_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Execute open position with cooldown check"""
    try:
        st = state[symbol]
        now = time.time()
        
        # Check order cooldown
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
        
        # Determine order side
        order_side = "BUY" if side == "LONG" else "SELL"
        
        # Place order
        success = await place_order(client, symbol, order_side, size, f"{side} ENTRY")
        return success
        
    except Exception as e:
        logging.error(f"❌ Execute open error {symbol} {side}: {e}")
        return False

async def execute_close_position(client: AsyncClient, symbol: str, side: str, size: float) -> bool:
    """Execute close position"""
    try:
        # Determine order side (opposite of position)
        order_side = "SELL" if side == "LONG" else "BUY"
        
        # Place order
        success = await place_order(client, symbol, order_side, size, f"{side} CLOSE")
        return success
        
    except Exception as e:
        logging.error(f"❌ Execute close error {symbol} {side}: {e}")
        return False

# ========================= WEBSOCKET PRICE FEED =========================
async def price_feed_loop(client: AsyncClient):
    """WebSocket price feed with reconnection logic"""
    streams = [f"{s.lower()}@kline_{BASE_TIMEFRAME.lower()}" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
    
    reconnect_delay = 1
    max_reconnect_delay = 60
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("📡 WebSocket connected")
                reconnect_delay = 1  # Reset delay on successful connection
                
                async for message in ws:
                    try:
                        # Parse JSON
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        
                        # Validate structure
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
                        
                        # Extract symbol
                        symbol = k.get("s")
                        if not symbol or symbol not in SYMBOLS:
                            continue
                        
                        # Validate required fields
                        required_fields = ["c", "o", "h", "l", "t"]
                        if not all(field in k for field in required_fields):
                            logging.debug(f"{symbol} Missing kline fields")
                            continue
                        
                        # Extract and validate price
                        price = validate_price(k.get("c"), symbol)
                        if price is None:
                            logging.debug(f"{symbol} Invalid price")
                            continue
                        
                        # Update current price
                        state[symbol]["price"] = price
                        
                        # Build kline data
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
                        
                        # Validate kline
                        if not validate_kline(kline_data):
                            continue
                        
                        # Add or update kline
                        klines = state[symbol]["klines"]
                        if len(klines) > 0 and klines[-1]["open_time"] == kline_data["open_time"]:
                            # Update existing candle
                            klines[-1] = kline_data
                        else:
                            # New candle
                            klines.append(kline_data)
                        
                        # Check if symbol is ready
                        if not state[symbol]["ready"]:
                            if len(klines) >= LMA_LENGTH:
                                lma = calculate_lma(symbol)
                                if lma is not None:
                                    state[symbol]["ready"] = True
                                    logging.info(f"✅ {symbol} ready (LMA calculated)")
                        
                    except Exception as e:
                        logging.debug(f"Message processing error: {e}")
                        continue
                        
        except websockets.exceptions.ConnectionClosed:
            logging.warning(f"⚠️ WebSocket closed, reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
        except Exception as e:
            logging.error(f"❌ WebSocket error: {e}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

# ========================= TRADING LOOP =========================
async def trading_loop(client: AsyncClient):
    """Main trading loop with comprehensive error handling"""
    loop_iteration = 0
    
    while True:
        try:
            await asyncio.sleep(0.5)  # Check twice per second
            loop_iteration += 1
            
            # Process each symbol independently
            for symbol in SYMBOLS:
                try:
                    st = state[symbol]
                    
                    # Skip if not ready
                    if not st["ready"]:
                        continue
                    
                    # Validate price
                    price = validate_price(st["price"], symbol)
                    if price is None:
                        continue
                    
                    # Get current positions with validation
                    long_pos = safe_float(st["long_position"])
                    short_pos = safe_float(st["short_position"])
                    
                    # === PHASE 1: CHECK TP/SL (Priority) ===
                    # Initialize missing TP/SL
                    if long_pos > 0:
                        if st["long_tp_price"] is None or st["long_sl_price"] is None:
                            logging.warning(f"⚠️ {symbol} LONG missing TP/SL, initializing...")
                            initialize_tpsl(symbol, "LONG", price)
                    
                    if short_pos > 0:
                        if st["short_tp_price"] is None or st["short_sl_price"] is None:
                            logging.warning(f"⚠️ {symbol} SHORT missing TP/SL, initializing...")
                            initialize_tpsl(symbol, "SHORT", price)
                    
                    # Check TP/SL
                    tpsl_result = check_tpsl(symbol, price)
                    
                    # Handle LONG TP/SL
                    if tpsl_result["long"] != "none" and long_pos > 0:
                        success = await execute_close_position(client, symbol, "LONG", long_pos)
                        if success:
                            st["long_position"] = 0.0
                            reset_position(symbol, "LONG")
                            save_positions()
                            # Skip to next symbol to avoid entering immediately
                            continue
                    
                    # Handle SHORT TP/SL
                    if tpsl_result["short"] != "none" and short_pos > 0:
                        success = await execute_close_position(client, symbol, "SHORT", short_pos)
                        if success:
                            st["short_position"] = 0.0
                            reset_position(symbol, "SHORT")
                            save_positions()
                            # Skip to next symbol to avoid entering immediately
                            continue
                    
                    # === PHASE 2: CHECK ENTRY SIGNALS ===
                    signals = update_trading_signals(symbol)
                    
                    # Refresh positions (in case they were just closed)
                    long_pos = safe_float(st["long_position"])
                    short_pos = safe_float(st["short_position"])
                    
                    # Handle LONG entry
                    if signals["long_entry"] and long_pos == 0:
                        target_size = SYMBOLS[symbol]
                        success = await execute_open_position(client, symbol, "LONG", target_size)
                        if success:
                            st["long_position"] = target_size
                            initialize_tpsl(symbol, "LONG", price)
                            save_positions()
                        else:
                            # Re-enable entry if order failed
                            st["long_entry_allowed"] = True
                            save_positions()
                    
                    # Handle SHORT entry
                    if signals["short_entry"] and short_pos == 0:
                        target_size = SYMBOLS[symbol]
                        success = await execute_open_position(client, symbol, "SHORT", target_size)
                        if success:
                            st["short_position"] = target_size
                            initialize_tpsl(symbol, "SHORT", price)
                            save_positions()
                        else:
                            # Re-enable entry if order failed
                            st["short_entry_allowed"] = True
                            save_positions()
                    
                except Exception as e:
                    logging.error(f"❌ Trading loop error for {symbol}: {e}")
                    # Continue to next symbol instead of crashing
                    continue
            
            # Periodic kline save (every 100 iterations ≈ 50 seconds)
            if loop_iteration % 100 == 0:
                save_klines()
                
        except Exception as e:
            logging.error(f"❌ Critical trading loop error: {e}")
            await asyncio.sleep(1)

# ========================= STATUS LOGGER =========================
async def status_logger():
    """Periodic status logging"""
    while True:
        try:
            await asyncio.sleep(120)  # Every 2 minutes
            
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
                    # Determine trend
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
                    
                    # LONG position info
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
                    
                    # SHORT position info
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
async def position_sanity_check(client: AsyncClient):
    """Verify local state matches exchange"""
    while True:
        try:
            await asyncio.sleep(300)  # Every 5 minutes
            logging.info("🔍 Running position sanity check...")
            
            account_info = await safe_api_call(client.futures_account)
            if not account_info or not isinstance(account_info, dict):
                logging.warning("⚠️ Could not fetch account info")
                continue
            
            positions = account_info.get('positions', [])
            if not isinstance(positions, list):
                logging.warning("⚠️ Invalid positions data")
                continue
            
            # Build exchange position map
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
            
            # Compare with local state
            mismatches = 0
            for symbol in SYMBOLS:
                st = state[symbol]
                
                local_long = safe_float(st["long_position"])
                local_short = safe_float(st["short_position"])
                
                exchange_long = exchange_positions.get(f"{symbol}_LONG", 0.0)
                exchange_short = exchange_positions.get(f"{symbol}_SHORT", 0.0)
                
                # Check LONG
                if abs(local_long - exchange_long) > 0.001:
                    logging.warning(
                        f"⚠️ [{symbol}] LONG mismatch: "
                        f"Local={local_long:.4f}, Exchange={exchange_long:.4f}"
                    )
                    
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
                
                # Check SHORT
                if abs(local_short - exchange_short) > 0.001:
                    logging.warning(
                        f"⚠️ [{symbol}] SHORT mismatch: "
                        f"Local={local_short:.4f}, Exchange={exchange_short:.4f}"
                    )
                    
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
async def recover_positions_from_exchange(client: AsyncClient):
    """Recover positions from exchange on startup"""
    logging.info("🔍 Checking exchange for existing positions...")
    
    try:
        account_info = await safe_api_call(client.futures_account)
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
                        
                        # Initialize TP/SL
                        init_price = mark_price if mark_price else entry_price
                        if init_price and init_price > 0:
                            initialize_tpsl(symbol, "LONG", init_price)
                    
                    elif position_side == "SHORT" and position_amt < 0:
                        logging.info(f"♻️ [{symbol}] RECOVERED SHORT: {abs(position_amt)}")
                        state[symbol]["short_position"] = abs(position_amt)
                        state[symbol]["short_entry_price"] = entry_price
                        state[symbol]["short_entry_allowed"] = False
                        recovered_count += 1
                        
                        # Initialize TP/SL
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
async def init_bot(client: AsyncClient):
    """Initialize bot with comprehensive checks"""
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
        
        # Load saved data
        load_klines()
        load_positions()
        
        # Recover positions from exchange
        await recover_positions_from_exchange(client)
        
        # Check which symbols need data
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
        
        # Fetch missing data
        if symbols_needing_data:
            logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbol(s)...")
            
            for i, symbol in enumerate(symbols_needing_data):
                try:
                    logging.info(f"📈 Fetching {symbol} ({i+1}/{len(symbols_needing_data)})")
                    
                    needed_candles = LMA_LENGTH + 50
                    klines_data = await safe_api_call(
                        client.futures_mark_price_klines,
                        symbol=symbol,
                        interval=BASE_TIMEFRAME,
                        limit=min(needed_candles, 1500)
                    )
                    
                    if not klines_data or not isinstance(klines_data, list):
                        logging.warning(f"⚠️ {symbol} No data returned")
                        continue
                    
                    # Clear and rebuild klines
                    state[symbol]["klines"].clear()
                    
                    for kline in klines_data:
                        try:
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
                    
                    # Check if ready
                    lma = calculate_lma(symbol)
                    if lma is not None:
                        state[symbol]["ready"] = True
                        logging.info(f"✅ {symbol} ready ({len(state[symbol]['klines'])} candles)")
                    else:
                        logging.warning(f"⚠️ {symbol} LMA calculation failed")
                    
                    # Rate limit between API calls
                    if i < len(symbols_needing_data) - 1:
                        await asyncio.sleep(2)
                        
                except Exception as e:
                    logging.error(f"❌ {symbol} fetch failed: {e}")
                    if i < len(symbols_needing_data) - 1:
                        await asyncio.sleep(2)
        
        # Save initial state
        save_klines()
        
        logging.info("=" * 80)
        logging.info("🚀 INITIALIZATION COMPLETE")
        logging.info("=" * 80)
        
    except Exception as e:
        logging.error(f"❌ Initialization failed: {e}")
        raise

# ========================= MAIN =========================
async def main():
    """Main entry point"""
    # Validate API credentials
    if not API_KEY or not API_SECRET:
        logging.error("❌ Missing API credentials in .env file")
        sys.exit(1)
    
    # Create client
    try:
        client = await AsyncClient.create(API_KEY, API_SECRET)
        logging.info("✅ Binance client created")
    except Exception as e:
        logging.error(f"❌ Failed to create client: {e}")
        sys.exit(1)
    
    # Register cleanup
    atexit.register(save_klines)
    atexit.register(save_positions)
    
    try:
        # Initialize
        await init_bot(client)
        
        # Start all tasks
        price_task = asyncio.create_task(price_feed_loop(client))
        trade_task = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())
        sanity_task = asyncio.create_task(position_sanity_check(client))
        
        logging.info("🚀 BOT STARTED - All systems running")
        logging.info("   Press Ctrl+C to stop")
        
        # Run forever
        await asyncio.gather(price_task, trade_task, status_task, sanity_task)
        
    except KeyboardInterrupt:
        logging.info("\n🛑 Bot stopped by user")
    except Exception as e:
        logging.error(f"❌ Critical error: {e}")
        raise
    finally:
        logging.info("🔄 Saving final state...")
        save_klines()
        save_positions()
        await client.close_connection()
        logging.info("✅ Shutdown complete")

# ========================= ENTRY POINT =========================
if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("LEO MOVING AVERAGE (LMA) TRADING BOT - PRODUCTION GRADE")
    print("=" * 80)
    print(f"Strategy: LMA = 2 × WMA({LMA_LENGTH}) - SMA({LMA_LENGTH})")
    print(f"Timeframe: {BASE_TIMEFRAME}")
    print(f"")
    print(f"ENTRY SIGNALS:")
    print(f"  🟢 LONG:  LMA rising (green) + Price above LMA")
    print(f"  🔴 SHORT: LMA falling (red) + Price below LMA")
    print(f"")
    print(f"EXIT STRATEGY (Fixed TP/SL - NO flip-flopping):")
    print(f"  LONG:  TP = +{LONG_TP_PERCENT}%  |  SL = -{LONG_SL_PERCENT}%")
    print(f"  SHORT: TP = +{SHORT_TP_PERCENT}%  |  SL = -{SHORT_SL_PERCENT}%")
    print(f"")
    print(f"HEDGE MODE FEATURES:")
    print(f"  ✅ Both LONG and SHORT can be open simultaneously")
    print(f"  ✅ Each position has independent entry/exit")
    print(f"  ✅ Positions exit ONLY on TP or SL hit")
    print(f"  ✅ No cross-signal exits or flip-flopping")
    print(f"")
    print(f"SAFETY FEATURES:")
    print(f"  ✅ Comprehensive error handling")
    print(f"  ✅ Type validation on all data")
    print(f"  ✅ Position sync with exchange")
    print(f"  ✅ Signal cooldowns ({SIGNAL_COOLDOWN_SECONDS}s)")
    print(f"  ✅ Order cooldowns ({ORDER_COOLDOWN_SECONDS}s)")
    print(f"  ✅ Backup file system")
    print(f"  ✅ Auto-recovery on restart")
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
