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

# Trading configuration
TRAILING_CALLBACK_RATE = 0.5  # 0.5% trailing stop distance
CHECK_INTERVAL = 60  # Check every 60 seconds (after each 1-minute candle closes)

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

# ========================= STATE =========================
state = {
    symbol: {
        "price": None,
        "klines": deque(maxlen=4),  # Keep last 4 candles (current forming + 3 closed for momentum check)
        "current_signal": None,
        "current_position": 0.0,
        "last_check_time": 0,
        "ready": False,
    }
    for symbol in SYMBOLS
}

# Rate limiting
api_calls_count = 0
api_calls_reset_time = time.time()

# ========================= PERSISTENCE FUNCTIONS =========================
def save_positions():
    """Save current positions and signals"""
    position_data = {
        sym: {
            "current_signal": state[sym]["current_signal"],
            "current_position": state[sym]["current_position"],
        }
        for sym in SYMBOLS
    }
    with open('positions.json', 'w') as f:
        json.dump(position_data, f)
    logging.info("💾 Saved positions to positions.json")

def load_positions():
    """Load positions from JSON"""
    try:
        with open('positions.json', 'r') as f:
            position_data = json.load(f)
        for sym in SYMBOLS:
            if sym in position_data:
                state[sym]["current_signal"] = position_data[sym].get("current_signal")
                state[sym]["current_position"] = position_data[sym].get("current_position", 0.0)
        logging.info("💾 Loaded positions from positions.json")
    except FileNotFoundError:
        logging.info("No positions.json found - starting fresh")
    except Exception as e:
        logging.error(f"Failed to load positions: {e} - starting fresh")

# ========================= HELPERS =========================
def round_size(size: float, symbol: str) -> float:
    """Round position size to appropriate precision"""
    prec = PRECISIONS.get(symbol, 3)
    return round(size, prec)

async def safe_api_call(func, *args, **kwargs):
    """Make API call with exponential backoff and rate limiting"""
    global api_calls_count, api_calls_reset_time

    now = time.time()
    if now - api_calls_reset_time > 60:
        api_calls_count = 0
        api_calls_reset_time = now

    # More conservative limit: 8 calls per minute instead of 10
    if api_calls_count >= 8:
        wait_time = 60 - (now - api_calls_reset_time)
        if wait_time > 0:
            logging.warning(f"⏳ Rate limit reached, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            api_calls_count = 0
            api_calls_reset_time = time.time()

    # Small delay between API calls to prevent bursts (reduced from 0.2s)
    await asyncio.sleep(0.1)

    for attempt in range(3):
        try:
            api_calls_count += 1
            result = await func(*args, **kwargs)
            return result
        except Exception as e:
            error_msg = str(e)
            if "-1003" in error_msg or "Way too many requests" in error_msg or "rate" in error_msg.lower():
                wait_time = (2 ** attempt) * 30  # 30s, 60s, 120s
                logging.warning(f"⏳ Rate limited (attempt {attempt+1}/3), waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                raise e

    raise Exception("Max API retry attempts reached")

def get_last_closed_candle(symbol: str) -> Optional[dict]:
    """Get the last CLOSED 1-minute candle with momentum and body size checks"""
    klines = list(state[symbol]["klines"])
    
    # Need at least 3 candles: current forming + 2 closed
    if len(klines) < 3:
        return None
    
    # klines[-1] is the current forming candle
    # klines[-2] is the last closed candle (1 minute ago)
    # klines[-3] is the previous closed candle (2 minutes ago)
    last_closed = klines[-2]
    prev_closed = klines[-3]
    
    open_price = last_closed["open"]
    close_price = last_closed["close"]
    prev_open = prev_closed["open"]
    prev_close = prev_closed["close"]
    
    # Calculate body sizes (absolute values)
    current_body_size = abs(close_price - open_price)
    prev_body_size = abs(prev_close - prev_open)
    
    is_green = close_price > open_price
    is_red = close_price < open_price
    
    # Momentum check: compare last close vs previous close
    has_upward_momentum = close_price > prev_close
    has_downward_momentum = close_price < prev_close
    
    # Body size comparison: current body must be larger than previous body
    has_larger_body = current_body_size > prev_body_size
    
    return {
        'open': open_price,
        'close': close_price,
        'high': last_closed["high"],
        'low': last_closed["low"],
        'prev_close': prev_close,
        'current_body_size': current_body_size,
        'prev_body_size': prev_body_size,
        'is_green': is_green,
        'is_red': is_red,
        'has_upward_momentum': has_upward_momentum,
        'has_downward_momentum': has_downward_momentum,
        'has_larger_body': has_larger_body,
        'timestamp': last_closed["open_time"]
    }

async def get_all_positions(client: AsyncClient) -> dict:
    """Get ALL positions at once (more efficient - only 1 API call)"""
    try:
        account_info = await safe_api_call(client.futures_account)
        positions = account_info.get('positions', [])
        
        result = {}
        for symbol in SYMBOLS:
            result[symbol] = {'LONG': False, 'SHORT': False}
        
        for position in positions:
            symbol = position['symbol']
            if symbol not in SYMBOLS:
                continue
            
            position_side = position.get('positionSide', '')
            pos_amt = float(position['positionAmt'])
            
            if position_side == 'LONG' and pos_amt > 0:
                result[symbol]['LONG'] = True
            elif position_side == 'SHORT' and abs(pos_amt) > 0:
                result[symbol]['SHORT'] = True
        
        return result
        
    except Exception as e:
        logging.error(f"Error fetching all positions: {e}")
        return {symbol: {'LONG': False, 'SHORT': False} for symbol in SYMBOLS}

async def open_long_with_trailing_stop(client: AsyncClient, symbol: str, quantity: float):
    """Open LONG position and immediately attach trailing stop"""
    try:
        quantity = round_size(quantity, symbol)
        logging.info(f"[{time.strftime('%H:%M:%S')}] 🟢 {symbol} Opening LONG position...")
        
        # Step 1: Open LONG position
        entry = await safe_api_call(
            client.futures_create_order,
            symbol=symbol,
            side='BUY',
            type='MARKET',
            quantity=quantity,
            positionSide='LONG'
        )
        logging.info(f"✓ {symbol} LONG entry executed: OrderID {entry['orderId']}")
        
        # Small delay between entry and trailing stop order
        await asyncio.sleep(0.5)
        
        # Step 2: Immediately attach trailing stop
        trail = await safe_api_call(
            client.futures_create_order,
            symbol=symbol,
            side='SELL',  # Opposite side to close LONG
            type='TRAILING_STOP_MARKET',
            quantity=quantity,
            callbackRate=TRAILING_CALLBACK_RATE,
            positionSide='LONG'
        )
        logging.info(f"✓ {symbol} Trailing stop (0.5%) attached: OrderID {trail['orderId']}")
        
        state[symbol]["current_signal"] = 'LONG'
        state[symbol]["current_position"] = quantity
        save_positions()
        
        return True
        
    except Exception as e:
        logging.error(f"✗ {symbol} Error opening LONG: {e}")
        return False

async def open_short_with_trailing_stop(client: AsyncClient, symbol: str, quantity: float):
    """Open SHORT position and immediately attach trailing stop"""
    try:
        quantity = round_size(quantity, symbol)
        logging.info(f"[{time.strftime('%H:%M:%S')}] 🔴 {symbol} Opening SHORT position...")
        
        # Step 1: Open SHORT position
        entry = await safe_api_call(
            client.futures_create_order,
            symbol=symbol,
            side='SELL',
            type='MARKET',
            quantity=quantity,
            positionSide='SHORT'
        )
        logging.info(f"✓ {symbol} SHORT entry executed: OrderID {entry['orderId']}")
        
        # Small delay between entry and trailing stop order
        await asyncio.sleep(0.5)
        
        # Step 2: Immediately attach trailing stop
        trail = await safe_api_call(
            client.futures_create_order,
            symbol=symbol,
            side='BUY',  # Opposite side to close SHORT
            type='TRAILING_STOP_MARKET',
            quantity=quantity,
            callbackRate=TRAILING_CALLBACK_RATE,
            positionSide='SHORT'
        )
        logging.info(f"✓ {symbol} Trailing stop (0.5%) attached: OrderID {trail['orderId']}")
        
        state[symbol]["current_signal"] = 'SHORT'
        state[symbol]["current_position"] = -quantity
        save_positions()
        
        return True
        
    except Exception as e:
        logging.error(f"✗ {symbol} Error opening SHORT: {e}")
        return False

# ========================= MAIN LOOPS =========================
async def price_feed_loop(client: AsyncClient):
    """WebSocket feed - receives 1-minute candles directly from Binance Futures"""
    # Use kline_1m stream for accurate 1-minute candles from Binance
    streams = [f"{s.lower()}@kline_1m" for s in SYMBOLS]
    url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logging.info("📡 WebSocket connected to Binance Futures kline stream")

                async for message in ws:
                    try:
                        data = json.loads(message).get("data", {})
                        
                        if data.get("e") == "kline":
                            kline = data.get("k", {})
                            symbol = kline.get("s")
                            
                            if symbol not in SYMBOLS:
                                continue
                            
                            # Update current price
                            close_price = float(kline.get("c"))
                            state[symbol]["price"] = close_price
                            
                            # Get candle data
                            open_time = int(kline.get("t") / 1000)  # Convert to seconds
                            is_closed = kline.get("x")  # True if candle is closed
                            
                            candle_data = {
                                "open_time": open_time,
                                "open": float(kline.get("o")),
                                "high": float(kline.get("h")),
                                "low": float(kline.get("l")),
                                "close": close_price
                            }
                            
                            klines = state[symbol]["klines"]
                            
                            # Check if this is a new candle or update to existing
                            if not klines or klines[-1]["open_time"] != open_time:
                                # New candle started
                                klines.append(candle_data)
                                
                                # Mark as ready once we have at least 3 candles (for momentum check)
                                if len(klines) >= 3 and not state[symbol]["ready"]:
                                    state[symbol]["ready"] = True
                                    logging.info(f"✅ {symbol} ready for trading (1-minute candles from Binance)")
                            else:
                                # Update current forming candle
                                klines[-1] = candle_data

                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")

        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(5)

async def trading_loop(client: AsyncClient):
    """Main trading logic - checks every minute after candle closes"""
    last_position_check = 0
    all_positions = {}
    
    while True:
        await asyncio.sleep(1)  # Check frequently
        
        current_time = time.time()
        
        # Batch position check once per minute for ALL symbols (saves API calls)
        if current_time - last_position_check >= CHECK_INTERVAL:
            all_positions = await get_all_positions(client)
            last_position_check = current_time
            logging.info("✓ Position check completed for all symbols")
        
        for symbol in SYMBOLS:
            st = state[symbol]
            
            if not st["ready"]:
                continue
            
            # Only check once per minute (after candle closes)
            if current_time - st["last_check_time"] < CHECK_INTERVAL:
                continue
            
            st["last_check_time"] = current_time
            
            # Get positions from batch check
            if symbol not in all_positions:
                continue
                
            positions = all_positions[symbol]
            has_long = positions['LONG']
            has_short = positions['SHORT']
            
            # Get last closed candle
            candle = get_last_closed_candle(symbol)
            
            if candle is None:
                continue
            
            # Safety check: ensure price is available
            price = st["price"]
            if price is None:
                logging.warning(f"⚠️ {symbol} price not available yet, skipping...")
                continue
            
            candle_type = "🟢 GREEN" if candle['is_green'] else "🔴 RED"
            momentum = ""
            if candle['has_upward_momentum']:
                momentum = "↗️ UP"
            elif candle['has_downward_momentum']:
                momentum = "↘️ DOWN"
            else:
                momentum = "→ FLAT"
            
            body_comparison = f"📏 Body: {candle['current_body_size']:.6f} vs {candle['prev_body_size']:.6f}"
            if candle['has_larger_body']:
                body_comparison += " ✅ LARGER"
            else:
                body_comparison += " ❌ smaller"
            
            position_status = []
            if has_long:
                position_status.append("LONG")
            if has_short:
                position_status.append("SHORT")
            position_str = " + ".join(position_status) if position_status else "NONE"
            
            logging.info(f"\n[{time.strftime('%H:%M:%S')}] {symbol} Last closed candle: {candle_type} {momentum}")
            logging.info(f"  Open: {candle['open']:.6f}, Close: {candle['close']:.6f}, Prev Close: {candle['prev_close']:.6f}")
            logging.info(f"  {body_comparison}")
            logging.info(f"  Current Price: {price:.6f}")
            logging.info(f"  Current positions: {position_str}")
            
            quantity = SYMBOLS[symbol]
            
            # Check for LONG entry: GREEN candle + upward momentum + larger body
            if candle['is_green'] and candle['has_upward_momentum'] and candle['has_larger_body'] and not has_long:
                logging.info(f"→ Signal: LONG (green candle + momentum + larger body)")
                success = await open_long_with_trailing_stop(client, symbol, quantity)
                if success:
                    all_positions[symbol]['LONG'] = True  # Update cache
                    await asyncio.sleep(1)  # Delay after placing order
            elif candle['is_green'] and not candle['has_upward_momentum'] and not has_long:
                logging.info(f"→ GREEN candle but NO upward momentum, skipping LONG")
            elif candle['is_green'] and not candle['has_larger_body'] and not has_long:
                logging.info(f"→ GREEN candle but body NOT larger than previous, skipping LONG")
            elif candle['is_green'] and has_long:
                logging.info(f"→ LONG position already exists, skipping LONG entry")
            
            # Check for SHORT entry: RED candle + downward momentum + larger body
            if candle['is_red'] and candle['has_downward_momentum'] and candle['has_larger_body'] and not has_short:
                logging.info(f"→ Signal: SHORT (red candle + momentum + larger body)")
                success = await open_short_with_trailing_stop(client, symbol, quantity)
                if success:
                    all_positions[symbol]['SHORT'] = True  # Update cache
                    await asyncio.sleep(1)  # Delay after placing order
            elif candle['is_red'] and not candle['has_downward_momentum'] and not has_short:
                logging.info(f"→ RED candle but NO downward momentum, skipping SHORT")
            elif candle['is_red'] and not candle['has_larger_body'] and not has_short:
                logging.info(f"→ RED candle but body NOT larger than previous, skipping SHORT")
            elif candle['is_red'] and has_short:
                logging.info(f"→ SHORT position already exists, skipping SHORT entry")

async def status_logger():
    """5-minute status report"""
    await asyncio.sleep(300)  # Wait 5 minutes before first report
    
    while True:
        current_time = time.strftime("%H:%M", time.localtime())
        logging.info(f"\n📊 === STATUS REPORT {current_time} ===")

        for symbol in SYMBOLS:
            st = state[symbol]

            if not st["ready"]:
                logging.info(f"{symbol}: Not ready - waiting for candle data")
                continue

            price = st["price"]
            
            candle = get_last_closed_candle(symbol)
            if candle:
                candle_color = "GREEN" if candle['is_green'] else "RED"
                logging.info(f"{symbol}: Price={price:.6f} | Last Candle={candle_color}")
            else:
                logging.info(f"{symbol}: Price={price:.6f}")

        logging.info("📊 === END STATUS REPORT ===\n")
        
        await asyncio.sleep(300)  # Wait 5 minutes for next report

async def recover_positions_from_exchange(client: AsyncClient):
    """Recover actual positions from Binance (can have both LONG and SHORT in Hedge Mode)"""
    logging.info("🔍 Checking exchange for existing positions...")
    
    try:
        account_info = await safe_api_call(client.futures_account)
        positions = account_info.get('positions', [])
        
        recovered_count = 0
        for position in positions:
            symbol = position['symbol']
            if symbol not in SYMBOLS:
                continue
            
            position_side = position.get('positionSide', '')
            position_amt = float(position['positionAmt'])
            
            if abs(position_amt) > 0.0001:
                recovered_count += 1
                entry_price = float(position['entryPrice'])
                unrealized_pnl = float(position['unrealizedProfit'])
                
                logging.info(
                    f"♻️ {symbol} RECOVERED {position_side} position: "
                    f"Amount={position_amt}, Entry={entry_price:.6f}, "
                    f"PNL={unrealized_pnl:.2f} USDT"
                )
        
        if recovered_count > 0:
            logging.info(f"✅ Recovered {recovered_count} active position(s)")
            save_positions()
        else:
            logging.info("✅ No active positions found")
            
    except Exception as e:
        logging.error(f"❌ Position recovery failed: {e}")
        logging.warning("⚠️ Bot will start with empty positions - verify manually!")

async def init_bot(client: AsyncClient):
    """Initialize bot"""
    logging.info("🔧 Initializing bot...")
    logging.info(f"📊 Strategy: 1-minute candle with momentum + body size confirmation")
    logging.info(f"📊 LONG: GREEN candle + close > prev close + larger body")
    logging.info(f"📊 SHORT: RED candle + close < prev close + larger body")
    logging.info(f"📊 Exit: 0.5% Trailing Stop (automatic)")
    logging.info(f"📊 Mode: HEDGE MODE - Can have BOTH LONG + SHORT simultaneously")
    logging.info(f"📊 Symbols: {list(SYMBOLS.keys())}")
    
    load_positions()
    await recover_positions_from_exchange(client)
    
    # Fetch initial 1-minute candles for each symbol
    logging.info("🔄 Fetching initial 1-minute candle data...")
    
    for i, symbol in enumerate(SYMBOLS):
        try:
            klines_data = await safe_api_call(
                client.futures_mark_price_klines,
                symbol=symbol,
                interval='1m',
                limit=4  # Get last 4 candles (for momentum check)
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
            
            if len(st["klines"]) >= 3:
                st["ready"] = True
                logging.info(f"✅ {symbol} ready")
            
            # Longer delay between symbols during init to avoid rate limits
            if i < len(SYMBOLS) - 1:
                await asyncio.sleep(2)  # 2 seconds between each symbol
            
        except Exception as e:
            logging.error(f"❌ {symbol} fetch failed: {e}")
            # Still wait even on error
            if i < len(SYMBOLS) - 1:
                await asyncio.sleep(2)
    
    logging.info("🚀 Initialization complete")

async def main():
    if not API_KEY or not API_SECRET:
        raise ValueError("Missing Binance API credentials")

    client = await AsyncClient.create(API_KEY, API_SECRET)

    atexit.register(save_positions)

    try:
        # Set leverage for all symbols (with delays to avoid rate limits)
        logging.info(f"⚙️ Setting leverage to {LEVERAGE}x for all symbols...")
        for i, symbol in enumerate(SYMBOLS):
            try:
                await safe_api_call(client.futures_change_leverage, symbol=symbol, leverage=LEVERAGE)
                logging.info(f"✓ {symbol} leverage set to {LEVERAGE}x")
                # Add delay between leverage settings
                if i < len(SYMBOLS) - 1:
                    await asyncio.sleep(1)
            except Exception as e:
                logging.warning(f"⚠️ {symbol} leverage setting: {e}")
        
        await init_bot(client)

        price_task = asyncio.create_task(price_feed_loop(client))
        trade_task = asyncio.create_task(trading_loop(client))
        status_task = asyncio.create_task(status_logger())

        logging.info("🚀 Bot started - Trading with momentum + body size confirmation + 0.5% trailing stops")

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
    print("1-MINUTE CANDLE STRATEGY WITH TRAILING STOPS + MOMENTUM + BODY SIZE")
    print("=" * 80)
    print(f"Strategy: Entry based on last closed 1-minute candle")
    print(f"  LONG Entry Conditions (ALL must be met):")
    print(f"    1. GREEN candle (close > open)")
    print(f"    2. Upward momentum (close > previous close)")
    print(f"    3. Larger body (current body > previous body)")
    print(f"  SHORT Entry Conditions (ALL must be met):")
    print(f"    1. RED candle (close < open)")
    print(f"    2. Downward momentum (close < previous close)")
    print(f"    3. Larger body (current body > previous body)")
    print(f"Exit: 0.5% Trailing Stop (managed by Binance)")
    print(f"Mode: HEDGE MODE - Can have BOTH LONG + SHORT positions simultaneously")
    print(f"Entry Rule: Opens LONG if no LONG exists, SHORT if no SHORT exists")
    print(f"Symbols: {list(SYMBOLS.keys())}")
    print(f"Leverage: {LEVERAGE}x")
    print("=" * 80)
    print("⚠️ IMPORTANT: Ensure your Binance account is in HEDGE MODE")
    print("   Futures → Preferences → Position Mode → Hedge Mode")
    print("=" * 80)

    asyncio.run(main())
