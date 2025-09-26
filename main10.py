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

# Donchian settings
DONCHIAN_PERIODS = 1200  # 20 hours (1200 minutes) - matches RSI period
BOUNCE_THRESHOLD = 0.005  # 0.5% of channel width
KLINE_LIMIT = 1300  # Keep last 1300 1m candles

# RSI settings (pure anti-churn basis)
RSI_PERIODS = 300  # 5 hours (300 minutes) as requested
RSI_UPPER_BLOCK = 50.1  # Block SHORT above this (bullish momentum)
RSI_LOWER_BLOCK = 49.9  # Block LONG below this (bearish momentum)

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=KLINE_LIMIT),
        "breakout_signal": None,  # None, "LONG", "SHORT" 
        "bounce_signal": None,
        "bounce_breach": None,    # None, "HIGH", "LOW"
        "last_breakout_change": 0,
        "last_bounce_change": 0,
        "current_position": 0.0,  # Current position size
        "rsi": None,  # Current RSI value
        "ready": False,  # True when enough data to trade
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

# ========================= DONCHIAN LOGIC =========================
def get_donchian_levels(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    """Get Donchian channel high/low from last N periods"""
    klines = state[symbol]["klines"]
    
    if len(klines) < DONCHIAN_PERIODS:
        return None, None
        
    # Use last N complete periods
    recent = list(klines)[-DONCHIAN_PERIODS:]
    
    if not recent:
        return None, None
        
    highs = [k["high"] for k in recent]
    lows = [k["low"] for k in recent]
    
    return min(lows), max(highs)

def get_bounce_levels(d_low: float, d_high: float) -> Tuple[float, float]:
    """Calculate bounce levels (0.5% inside the channel)"""
    channel_width = d_high - d_low
    offset = channel_width * BOUNCE_THRESHOLD
    
    bounce_low = d_low + offset    # Support level
    bounce_high = d_high - offset  # Resistance level
    
    return bounce_low, bounce_high

def calculate_rsi(symbol: str) -> Optional[float]:
    """Calculate RSI using last N periods"""
    klines = state[symbol]["klines"]
    
    if len(klines) < RSI_PERIODS + 1:
        return None
        
    # Get recent closes
    recent = list(klines)[-(RSI_PERIODS + 1):]
    closes = [k["close"] for k in recent]
    
    if len(closes) < RSI_PERIODS + 1:
        return None
    
    # Calculate price changes
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
    
    # Calculate average gain and loss
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    # Update state
    state[symbol]["rsi"] = rsi
    
    return rsi

# ========================= LANE LOGIC =========================
def update_breakout_lane(symbol: str) -> dict:
    """BREAKOUT lane: enters at Donchian edges, exits at bounce threshold"""
    st = state[symbol]
    price = st["price"]
    current_signal = st["breakout_signal"]
    
    if not price or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    d_low, d_high = get_donchian_levels(symbol)
    if not d_low or not d_high:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    # Calculate RSI for filtering
    rsi = calculate_rsi(symbol)
    
    bounce_low, bounce_high = get_bounce_levels(d_low, d_high)
    now = time.time()
    
    # Exit logic first (exit to flat only)
    if current_signal == "LONG" and price <= bounce_high:
        if now - st["last_breakout_change"] >= 0.25:
            st["breakout_signal"] = None
            st["last_breakout_change"] = now
            logging.info(f"📤 {symbol} [BREAKOUT] EXIT LONG → FLAT (price {price:.6f} ≤ bounce_high {bounce_high:.6f})")
            return {"changed": True, "action": "EXIT", "signal": None}
    
    elif current_signal == "SHORT" and price >= bounce_low:
        if now - st["last_breakout_change"] >= 0.25:
            st["breakout_signal"] = None  
            st["last_breakout_change"] = now
            logging.info(f"📤 {symbol} [BREAKOUT] EXIT SHORT → FLAT (price {price:.6f} ≥ bounce_low {bounce_low:.6f})")
            return {"changed": True, "action": "EXIT", "signal": None}
    
    # Entry logic (only when flat)
    if current_signal is None:
        if price >= d_high:  # Breakout above
            # RSI Filter: Block LONG breakouts when RSI < 49.9 (bearish momentum)
            if rsi is not None and rsi < RSI_LOWER_BLOCK:
                logging.info(f"🚫 {symbol} [BREAKOUT] BLOCKED LONG ENTRY - RSI {rsi:.2f} < {RSI_LOWER_BLOCK} (bearish momentum)")
                return {"changed": False, "action": "NONE", "signal": current_signal}
                
            st["breakout_signal"] = "LONG"
            st["last_breakout_change"] = now
            logging.info(f"🚀 {symbol} [BREAKOUT] ENTRY LONG (price {price:.6f} ≥ high {d_high:.6f}) RSI:{rsi:.2f if rsi is not None else 'N/A'}")
            return {"changed": True, "action": "ENTRY", "signal": "LONG"}
            
        elif price <= d_low:  # Breakout below
            # RSI Filter: Block SHORT breakouts when RSI > 50.1 (bullish momentum)
            if rsi is not None and rsi > RSI_UPPER_BLOCK:
                logging.info(f"🚫 {symbol} [BREAKOUT] BLOCKED SHORT ENTRY - RSI {rsi:.2f} > {RSI_UPPER_BLOCK} (bullish momentum)")
                return {"changed": False, "action": "NONE", "signal": current_signal}
                
            st["breakout_signal"] = "SHORT"
            st["last_breakout_change"] = now
            logging.info(f"🚀 {symbol} [BREAKOUT] ENTRY SHORT (price {price:.6f} ≤ low {d_low:.6f}) RSI:{rsi:.2f if rsi is not None else 'N/A'}")
            return {"changed": True, "action": "ENTRY", "signal": "SHORT"}
    
    return {"changed": False, "action": "NONE", "signal": current_signal}

def update_bounce_lane(symbol: str) -> dict:
    """BOUNCE lane: breach→confirm entries, instant flips"""
    st = state[symbol]
    price = st["price"]
    current_signal = st["bounce_signal"]
    
    if not price or not st["ready"]:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    d_low, d_high = get_donchian_levels(symbol)
    if not d_low or not d_high:
        return {"changed": False, "action": "NONE", "signal": current_signal}
    
    # Calculate RSI for filtering
    rsi = calculate_rsi(symbol)
    
    bounce_low, bounce_high = get_bounce_levels(d_low, d_high)
    now = time.time()
    
    # Track breaches
    if price <= d_low and st["bounce_breach"] != "LOW":
        st["bounce_breach"] = "LOW"
        logging.info(f"⚠️ {symbol} [BOUNCE] BREACH LOW at {price:.6f} (channel_low: {d_low:.6f})")
        
    elif price >= d_high and st["bounce_breach"] != "HIGH":
        st["bounce_breach"] = "HIGH"  
        logging.info(f"⚠️ {symbol} [BOUNCE] BREACH HIGH at {price:.6f} (channel_high: {d_high:.6f})")
    
    # Confirm entries/flips with RSI filter
    new_signal = None
    
    if st["bounce_breach"] == "LOW" and price >= bounce_low:
        # RSI Filter: Block LONG when RSI < 49.9 (bearish momentum)
        if rsi is not None and rsi < RSI_LOWER_BLOCK:
            logging.info(f"🚫 {symbol} [BOUNCE] BLOCKED LONG ENTRY - RSI {rsi:.2f} < {RSI_LOWER_BLOCK} (bearish momentum)")
            return {"changed": False, "action": "NONE", "signal": current_signal}
        new_signal = "LONG"  # Bounce off support
        
    elif st["bounce_breach"] == "HIGH" and price <= bounce_high:
        # RSI Filter: Block SHORT when RSI > 50.1 (bullish momentum)
        if rsi is not None and rsi > RSI_UPPER_BLOCK:
            logging.info(f"🚫 {symbol} [BOUNCE] BLOCKED SHORT ENTRY - RSI {rsi:.2f} > {RSI_UPPER_BLOCK} (bullish momentum)")
            return {"changed": False, "action": "NONE", "signal": current_signal}
        new_signal = "SHORT"  # Bounce off resistance
    
    if new_signal and new_signal != current_signal:
        action = "FLIP" if current_signal else "ENTRY"
        st["bounce_signal"] = new_signal
        st["last_bounce_change"] = now
        
        if action == "ENTRY":
            logging.info(f"🎯 {symbol} [BOUNCE] ENTRY {new_signal} RSI:{rsi:.2f if rsi is not None else 'N/A'}")
        else:
            logging.info(f"🔄 {symbol} [BOUNCE] FLIP {current_signal} → {new_signal} RSI:{rsi:.2f if rsi is not None else 'N/A'}")
        
        return {"changed": True, "action": action, "signal": new_signal}
    
    return {"changed": False, "action": "NONE", "signal": current_signal}

# ========================= MAIN LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    """WebSocket price feed"""
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
                                klines[-1]["high"] = max(klines[-1]["high"], price)
                                klines[-1]["low"] = min(klines[-1]["low"], price)
                                klines[-1]["close"] = price
                            
                            # Check if symbol is ready to trade
                            if len(klines) >= DONCHIAN_PERIODS and not state[symbol]["ready"]:
                                state[symbol]["ready"] = True
                                logging.info(f"✅ {symbol} ready for trading ({len(klines)} candles)")
                                
                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
                        
        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)

async def status_logger():
    """Log status every 5 minutes"""
    while True:
        await asyncio.sleep(300)  # 5 minutes = 300 seconds
        
        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"📊 === STATUS REPORT {current_time} ===")
        
        for symbol in SYMBOLS:
            st = state[symbol]
            
            if not st["ready"]:
                candle_count = len(st["klines"])
                remaining = DONCHIAN_PERIODS - candle_count
                logging.info(f"{symbol}: Not ready - {candle_count}/1200 candles ({remaining} needed)")
                continue
            
            price = st["price"]
            d_low, d_high = get_donchian_levels(symbol)
            rsi = calculate_rsi(symbol)
            
            if d_low and d_high and price:
                bounce_low, bounce_high = get_bounce_levels(d_low, d_high)
                
                # Signal status
                breakout_sig = st["breakout_signal"] or "FLAT"
                bounce_sig = st["bounce_signal"] or "FLAT"
                
                rsi_str = f"{rsi:.2f}" if rsi is not None else "N/A"
                logging.info(f"{symbol}: Price={price:.6f} | RSI={rsi_str}")
                logging.info(f"  Donchian: LOW={d_low:.6f} HIGH={d_high:.6f}")
                logging.info(f"  Bounce: LOW={bounce_low:.6f} HIGH={bounce_high:.6f}")
                logging.info(f"  Signals: BREAKOUT={breakout_sig} BOUNCE={bounce_sig}")
                
                # RSI filter status
                if rsi is not None:
                    if rsi < RSI_LOWER_BLOCK:
                        logging.info(f"  RSI Filter: BLOCKING LONG entries (RSI {rsi:.2f} < {RSI_LOWER_BLOCK})")
                    elif rsi > RSI_UPPER_BLOCK:
                        logging.info(f"  RSI Filter: BLOCKING SHORT entries (RSI {rsi:.2f} > {RSI_UPPER_BLOCK})")
                    else:
                        logging.info(f"  RSI Filter: ALLOWING all entries (RSI {rsi:.2f} in neutral zone)")
        
        logging.info("📊 === END STATUS REPORT ===")

async def trading_loop(client: AsyncClient):
    """Main trading logic with minimal API calls"""
    while True:
        await asyncio.sleep(1)  # Reduced frequency to 1 FPS for fewer operations
        
        for symbol in SYMBOLS:
            st = state[symbol]
            
            if not st["ready"]:
                continue
                
            # Update both lanes independently
            breakout_result = update_breakout_lane(symbol)
            bounce_result = update_bounce_lane(symbol)
            
            # Calculate target position (ANY_FULL aggregation)
            target_size = SYMBOLS[symbol]
            
            breakout_signal = st["breakout_signal"] 
            bounce_signal = st["bounce_signal"]
            
            # Determine final position
            final_position = 0.0
            
            if breakout_signal == "LONG" or bounce_signal == "LONG":
                final_position = target_size
            elif breakout_signal == "SHORT" or bounce_signal == "SHORT": 
                final_position = -target_size
            
            # Execute position change (only makes API calls when necessary)
            if breakout_result["changed"] or bounce_result["changed"]:
                current_pos = st["current_position"]
                if abs(final_position - current_pos) > 0.001:  # Only if significant change
                    await execute_position_change(client, symbol, final_position, current_pos)

async def execute_position_change(client: AsyncClient, symbol: str, target: float, current: float):
    """Execute position changes with minimal API calls"""
    delta = target - current
    
    try:
        if delta > 0:  # Need to go more LONG
            if current < 0:  # Currently SHORT, close first
                await place_order(client, symbol, "BUY", abs(current), "SHORT CLOSE")
            # Then open LONG
            await place_order(client, symbol, "BUY", abs(delta), "LONG ENTRY")
            
        elif delta < 0:  # Need to go more SHORT
            if current > 0:  # Currently LONG, close first
                await place_order(client, symbol, "SELL", current, "LONG CLOSE")
            # Then open SHORT
            await place_order(client, symbol, "SELL", abs(delta), "SHORT ENTRY")
        
        # Update position state (approximate)
        state[symbol]["current_position"] = target
        
    except Exception as e:
        logging.error(f"❌ {symbol} position change failed: {e}")

# ========================= INITIALIZATION WITH HISTORICAL DATA FETCHING =========================
async def init_bot(client: AsyncClient):
    """Initialize bot with historical data fetching and fallback to WebSocket-only"""
    logging.info("🔧 Initializing bot with safe historical data fetching...")
    logging.info("📊 Using hardcoded precisions to avoid API calls")
    logging.info("⚙️ Leverage must be set manually via Binance web interface")
    
    # First, try to load existing data
    load_klines()
    
    # Check which symbols need historical data
    symbols_needing_data = []
    for symbol in SYMBOLS:
        klines = state[symbol]["klines"]
        if len(klines) >= DONCHIAN_PERIODS:
            state[symbol]["ready"] = True
            calculate_rsi(symbol)  # Set initial RSI
            logging.info(f"✅ {symbol} ready for trading ({len(klines)} candles) from loaded data")
        else:
            symbols_needing_data.append(symbol)
            logging.info(f"📥 {symbol} needs historical data ({len(klines)}/{DONCHIAN_PERIODS} candles)")
    
    # Fetch historical data for symbols that need it (very conservatively)
    if symbols_needing_data:
        logging.info(f"🔄 Fetching historical data for {len(symbols_needing_data)} symbols...")
        logging.info("⚠️ Using ultra-conservative rate limiting (2-minute delays between symbols)")
        
        successful_fetches = 0
        
        for i, symbol in enumerate(symbols_needing_data):
            try:
                logging.info(f"📈 Fetching historical data for {symbol} ({i+1}/{len(symbols_needing_data)})...")
                
                # Fetch historical 1-minute candles
                klines_data = await safe_api_call(
                    client.futures_klines,
                    symbol=symbol,
                    interval="1m", 
                    limit=DONCHIAN_PERIODS + 50  # Extra buffer
                )
                
                # Process the data
                st = state[symbol]
                st["klines"].clear()  # Clear existing partial data
                
                for kline in klines_data:
                    minute = int(float(kline[0]) / 1000 // 60)
                    st["klines"].append({
                        "minute": minute,
                        "high": float(kline[2]),
                        "low": float(kline[3]), 
                        "close": float(kline[4])
                    })
                
                # Mark as ready
                if len(st["klines"]) >= DONCHIAN_PERIODS:
                    st["ready"] = True
                    calculate_rsi(symbol)  # Calculate initial RSI
                    logging.info(f"✅ {symbol} ready for trading ({len(st['klines'])} candles) from API")
                    successful_fetches += 1
                else:
                    logging.warning(f"⚠️ {symbol} insufficient data received ({len(st['klines'])} candles)")
                
                # Ultra-conservative delay between symbols (2 minutes)
                if i < len(symbols_needing_data) - 1:  # Don't wait after last symbol
                    logging.info(f"⏳ Waiting 120 seconds before next symbol to avoid rate limits...")
                    await asyncio.sleep(120)
                
            except Exception as e:
                logging.error(f"❌ {symbol} historical data fetch failed: {e}")
                logging.info(f"📡 {symbol} will build data from WebSocket feed instead")
                # Continue with other symbols
                
                # Still wait to avoid hammering the API after errors
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
            estimated_hours = DONCHIAN_PERIODS / 60  # Convert minutes to hours
            logging.info(f"📈 Estimated time to readiness: {estimated_hours} hours of live data")
    
    else:
        logging.info("🎯 All symbols ready from loaded data - no API calls needed!")
    
    # Save the data we have
    save_klines()
    
    await asyncio.sleep(2)  # Brief pause for logging
    logging.info("🚀 Initialization complete - Starting WebSocket feeds")

# ========================= MAIN =========================
async def main():
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing Binance API credentials in .env")
    
    client = await AsyncClient.create(API_KEY, API_SECRET)
    
    atexit.register(save_klines)  # Auto-save on exit
    
    try:
        await init_bot(client)
        
        # Start three concurrent tasks
        price_task = asyncio.create_task(price_feed_loop(client))
        trade_task = asyncio.create_task(trading_loop(client))
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
    
    print("=" * 60)
    print("🤖 SIMPLIFIED DONCHIAN BOT")
    print("=" * 60)
    print("📈 BREAKOUT: Edge entries → Exit to FLAT at bounce threshold")
    print("🎯 BOUNCE: Breach→Confirm entries → INSTANT FLIPS") 
    print(f"🎚️ Bounce threshold: {BOUNCE_THRESHOLD*100}%")
    print(f"📊 RSI Momentum Filter: {RSI_PERIODS} periods | Block LONG < {RSI_LOWER_BLOCK} | Block SHORT > {RSI_UPPER_BLOCK}")
    print("⚠️ Historical data fetching enabled with conservative rate limiting")
    print("📊 Data persistence enabled - faster restarts after first run")
    print(f"📊 Symbols: {list(SYMBOLS.keys())}")
    print("=" * 60)
    
    asyncio.run(main())
