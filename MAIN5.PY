#!/usr/bin/env python3
import os, json, asyncio, threading, logging, websockets
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
import time

# ────────────── CONFIG ──────────────
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

SYMBOLS = {
    "ADAUSDT": 10,  "BNBUSDT": 0.03,  "SOLUSDT": 0.10,
    "XRPUSDT": 10,
}

# Simple configuration
ROLLING_PERIOD_MINUTES = 720  # 12 hours rolling window
ENTRY_THRESHOLD_PCT = 0.02    # 2% above lowest / below highest for entry
TRAILING_STOP_PCT = 0.02      # 2% trailing stop for exits/flips
MAX_LOSS_PCT = 0.02           # 2% maximum loss protection (MANDATORY EXIT)
ANALYSIS_INTERVAL_SECONDS = 60  # Update highest/lowest every 60 seconds
FLIP_COOLDOWN_SECS = 1.0      # 1 second cooldown after flip

# ────────────── QUIET /ping ──────────
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
    def log_message(self, *_) : pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

# ───────────── ORDER HELPERS ─────────
def open_long(sym):
    return dict(symbol=sym, side="BUY",  type="MARKET",
                quantity=SYMBOLS[sym], positionSide="LONG")
def close_long(sym):
    return dict(symbol=sym, side="SELL", type="MARKET",
                quantity=SYMBOLS[sym], positionSide="LONG")
def open_short(sym):
    return dict(symbol=sym, side="SELL", type="MARKET",
                quantity=SYMBOLS[sym], positionSide="SHORT")
def close_short(sym):
    return dict(symbol=sym, side="BUY",  type="MARKET",
                quantity=SYMBOLS[sym], positionSide="SHORT")

# ───────────── RUNTIME STATE ─────────
state = {
    s: {
        # Position flags
        "in_long": False, "long_pending": False,
        "in_short": False, "short_pending": False,

        # Entry prices
        "long_entry_price": None,
        "short_entry_price": None,

        # Rolling 720-minute data (updated every minute)
        "rolling_highest": None,      # Highest price in last 720 minutes
        "rolling_lowest": None,       # Lowest price in last 720 minutes
        "last_analysis_time": 0,

        # Trailing stop tracking
        "trail_extremum": None,       # Peak for long, trough for short
        "trail_side": None,           # "LONG" or "SHORT" when active

        # Entry triggers (calculated from rolling data)
        "long_entry_trigger": None,   # rolling_lowest * 1.02
        "short_entry_trigger": None,  # rolling_highest * 0.98

        # Flip cooldown
        "cooldown_until": 0.0,

        "last_price": None,
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
    }
    for s in SYMBOLS
}

# ───────────── POSITION VALIDATION ─────────
async def get_position_size(cli: AsyncClient, symbol: str, side: str) -> float:
    """Get current position size from Binance"""
    try:
        positions = await cli.futures_position_information(symbol=symbol)
        for pos in positions:
            if pos['positionSide'] == side:
                return abs(float(pos['positionAmt']))
    except Exception as e:
        logging.error(f"Failed to get position for {symbol} {side}: {e}")
    return 0.0

async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    """Execute order with duplicate prevention and validation"""
    try:
        # Validate close-size roughly
        if action.startswith("CLOSE") or "EXIT" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size(cli, symbol, side)
            required_qty = order_params['quantity']
            if current_pos < required_qty * 0.99:  # tolerance
                logging.warning(f"{symbol} {action}: Insufficient position size {current_pos} < {required_qty}")
                return False

        # Execute the order
        result = await cli.futures_create_order(**order_params)
        state[symbol]["last_order_id"] = result.get('orderId')
        logging.info(f"{symbol} {action} executed - OrderID: {state[symbol]['last_order_id']}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} failed: {e}")
        return False

# ───────────── ROLLING ANALYSIS ─────────
async def update_rolling_levels(cli: AsyncClient, symbol: str):
    """
    Update rolling 720-minute highest and lowest prices every minute
    """
    try:
        # Get rolling 720 minutes of 1-minute candles
        klines = await cli.get_klines(
            symbol=symbol, 
            interval="1m", 
            limit=ROLLING_PERIOD_MINUTES
        )
        
        # Find highest and lowest prices from the candles
        highest = max(float(kline[2]) for kline in klines)  # High prices
        lowest = min(float(kline[3]) for kline in klines)   # Low prices
        
        # Update state
        st = state[symbol]
        st["rolling_highest"] = highest
        st["rolling_lowest"] = lowest
        st["last_analysis_time"] = time.time()
        
        # Calculate entry triggers
        st["long_entry_trigger"] = lowest * (1 + ENTRY_THRESHOLD_PCT)   # 2% above lowest
        st["short_entry_trigger"] = highest * (1 - ENTRY_THRESHOLD_PCT) # 2% below highest
        
        logging.info(f"{symbol} ROLLING UPDATE: Highest=${highest:.4f}, Lowest=${lowest:.4f}")
        logging.info(f"{symbol} ENTRY TRIGGERS: LONG>${st['long_entry_trigger']:.4f}, SHORT<${st['short_entry_trigger']:.4f}")
        
        return True
        
    except Exception as e:
        logging.error(f"{symbol} Rolling analysis failed: {e}")
        return False

# ───────────── LOSS PROTECTION ─────────
async def check_loss_protection(cli: AsyncClient, sym: str, price: float):
    """
    MANDATORY 2% loss protection - Exit immediately if loss reaches 2%
    """
    st = state[sym]
    
    # Only check if we have a position
    if not (st["in_long"] or st["in_short"]):
        return
    
    entry_price = st["long_entry_price"] if st["in_long"] else st["short_entry_price"]
    if not entry_price:
        return
    
    # Calculate current loss percentage
    if st["in_long"]:
        loss_pct = (entry_price - price) / entry_price  # Loss when price drops
    else:  # SHORT
        loss_pct = (price - entry_price) / entry_price  # Loss when price rises
    
    # MANDATORY EXIT: If loss reaches 2%, close position immediately
    if loss_pct >= MAX_LOSS_PCT:
        logging.warning(f"{sym} LOSS PROTECTION! Loss {loss_pct*100:.2f}% reached {MAX_LOSS_PCT*100:.1f}% limit")
        
        async with st["order_lock"]:
            if st["in_long"] and not st["long_pending"]:
                st["long_pending"] = True
                try:
                    if await safe_order_execution(cli, close_long(sym), sym, "LOSS PROTECTION EXIT"):
                        st["in_long"] = False
                        st["long_entry_price"] = None
                        st["trail_side"] = None
                        st["trail_extremum"] = None
                        logging.warning(f"{sym} LONG position closed at {loss_pct*100:.2f}% loss @ ${price:.4f}")
                finally:
                    st["long_pending"] = False
            
            elif st["in_short"] and not st["short_pending"]:
                st["short_pending"] = True  
                try:
                    if await safe_order_execution(cli, close_short(sym), sym, "LOSS PROTECTION EXIT"):
                        st["in_short"] = False
                        st["short_entry_price"] = None
                        st["trail_side"] = None
                        st["trail_extremum"] = None
                        logging.warning(f"{sym} SHORT position closed at {loss_pct*100:.2f}% loss @ ${price:.4f}")
                finally:
                    st["short_pending"] = False
async def enter_long(cli: AsyncClient, sym: str, price: float, reason: str = ""):
    st = state[sym]
    async with st["order_lock"]:
        if st["in_long"] or st["long_pending"] or st["in_short"] or st["short_pending"]:
            return False
        st["long_pending"] = True
    try:
        if await safe_order_execution(cli, open_long(sym), sym, f"LONG ENTRY {reason}"):
            st["in_long"] = True
            st["long_entry_price"] = price
            st["trail_side"] = "LONG"
            st["trail_extremum"] = price  # Start tracking peak from entry
            logging.info(f"{sym} LONG OPEN @ ${price:.4f} | trail={TRAILING_STOP_PCT*100:.1f}% below peak {reason}")
            return True
    finally:
        st["long_pending"] = False
    return False

async def enter_short(cli: AsyncClient, sym: str, price: float, reason: str = ""):
    st = state[sym]
    async with st["order_lock"]:
        if st["in_short"] or st["short_pending"] or st["in_long"] or st["long_pending"]:
            return False
        st["short_pending"] = True
    try:
        if await safe_order_execution(cli, open_short(sym), sym, f"SHORT ENTRY {reason}"):
            st["in_short"] = True
            st["short_entry_price"] = price
            st["trail_side"] = "SHORT"
            st["trail_extremum"] = price  # Start tracking trough from entry
            logging.info(f"{sym} SHORT OPEN @ ${price:.4f} | trail={TRAILING_STOP_PCT*100:.1f}% above trough {reason}")
            return True
    finally:
        st["short_pending"] = False
    return False

# ───────────── ENTRY SIGNAL DETECTION ─────────
async def check_entry_signals(cli: AsyncClient, sym: str, price: float):
    """
    Check if price crosses 2% thresholds for entry signals
    """
    st = state[sym]
    
    # Skip if no rolling data yet or already in position
    if not st["long_entry_trigger"] or not st["short_entry_trigger"]:
        return
    
    # Skip if already in a position
    if st["in_long"] or st["in_short"]:
        return
    
    # Check LONG entry: price crosses ABOVE (lowest * 1.02)
    if price >= st["long_entry_trigger"]:
        await enter_long(cli, sym, price, f"(CROSS ABOVE ${st['long_entry_trigger']:.4f})")
    
    # Check SHORT entry: price crosses BELOW (highest * 0.98) 
    elif price <= st["short_entry_trigger"]:
        await enter_short(cli, sym, price, f"(CROSS BELOW ${st['short_entry_trigger']:.4f})")

# ───────────── TRAILING STOP LOGIC ─────────
async def handle_trailing_stops(cli: AsyncClient, sym: str, price: float):
    """
    Handle 2% trailing stops for position flips
    """
    st = state[sym]
    side = st["trail_side"]
    
    # Only run if we have a position
    if side not in ("LONG", "SHORT"):
        return

    now = time.time()
    # Check cooldown
    if st["cooldown_until"] and now < st["cooldown_until"]:
        # Still update extremum during cooldown
        if side == "LONG" and (st["trail_extremum"] is None or price > st["trail_extremum"]):
            st["trail_extremum"] = price
        elif side == "SHORT" and (st["trail_extremum"] is None or price < st["trail_extremum"]):
            st["trail_extremum"] = price
        return

    if side == "LONG":
        # Update peak
        if st["trail_extremum"] is None or price > st["trail_extremum"]:
            st["trail_extremum"] = price
        
        # Check for 2% pullback from peak → FLIP to SHORT
        trigger_price = st["trail_extremum"] * (1 - TRAILING_STOP_PCT)
        if price <= trigger_price:
            async with st["order_lock"]:
                if st["long_pending"] or st["short_pending"]:
                    return
                st["long_pending"] = True
            try:
                if await safe_order_execution(cli, close_long(sym), sym, "LONG EXIT (TRAIL)"):
                    st["in_long"] = False
                    st["long_entry_price"] = None
                    
                    if await safe_order_execution(cli, open_short(sym), sym, "SHORT ENTRY (FLIP)"):
                        st["in_short"] = True
                        st["short_entry_price"] = price
                        st["trail_side"] = "SHORT"
                        st["trail_extremum"] = price  # Reset to new trough
                        st["cooldown_until"] = time.time() + FLIP_COOLDOWN_SECS
                        
                        logging.info(f"{sym} FLIP: LONG→SHORT @ ${price:.4f} "
                                   f"(peak=${st['trail_extremum']/(1-TRAILING_STOP_PCT):.4f}, "
                                   f"trigger=${trigger_price:.4f}) | cooldown {FLIP_COOLDOWN_SECS:.1f}s")
            finally:
                st["long_pending"] = False

    elif side == "SHORT":
        # Update trough
        if st["trail_extremum"] is None or price < st["trail_extremum"]:
            st["trail_extremum"] = price
        
        # Check for 2% bounce from trough → FLIP to LONG
        trigger_price = st["trail_extremum"] * (1 + TRAILING_STOP_PCT)
        if price >= trigger_price:
            async with st["order_lock"]:
                if st["short_pending"] or st["long_pending"]:
                    return
                st["short_pending"] = True
            try:
                if await safe_order_execution(cli, close_short(sym), sym, "SHORT EXIT (TRAIL)"):
                    st["in_short"] = False
                    st["short_entry_price"] = None
                    
                    if await safe_order_execution(cli, open_long(sym), sym, "LONG ENTRY (FLIP)"):
                        st["in_long"] = True
                        st["long_entry_price"] = price
                        st["trail_side"] = "LONG"
                        st["trail_extremum"] = price  # Reset to new peak
                        st["cooldown_until"] = time.time() + FLIP_COOLDOWN_SECS
                        
                        logging.info(f"{sym} FLIP: SHORT→LONG @ ${price:.4f} "
                                   f"(trough=${st['trail_extremum']/(1+TRAILING_STOP_PCT):.4f}, "
                                   f"trigger=${trigger_price:.4f}) | cooldown {FLIP_COOLDOWN_SECS:.1f}s")
            finally:
                st["short_pending"] = False

# ───────────── ROLLING ANALYSIS SCHEDULER ─────────
async def rolling_analysis_scheduler(cli: AsyncClient):
    """
    Update rolling 720-minute highest/lowest every minute
    """
    while True:
        try:
            for symbol in SYMBOLS:
                st = state[symbol]
                
                now = time.time()
                if now - st["last_analysis_time"] >= ANALYSIS_INTERVAL_SECONDS:
                    await update_rolling_levels(cli, symbol)
            
            await asyncio.sleep(10)  # Check every 10 seconds if update is due
            
        except Exception as e:
            logging.error(f"Rolling analysis scheduler error: {e}")
            await asyncio.sleep(30)

# ───────────── MAIN LOOP ─────────
async def run(cli: AsyncClient):
    for s in SYMBOLS:
        await cli.futures_change_leverage(symbol=s, leverage=LEVERAGE)

    # Start rolling analysis scheduler (updates highest/lowest every minute)
    asyncio.create_task(rolling_analysis_scheduler(cli))
    
    streams = [f"{s.lower()}@trade" for s in SYMBOLS]
    url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    async with websockets.connect(url) as ws:
        async for raw in ws:
            m = json.loads(raw)
            stype = m["stream"]; d = m["data"]
            if not stype.endswith("@trade"):
                continue

            sym = d["s"]
            price = float(d["p"])
            st = state[sym]

            # Update current price
            st["last_price"] = price

            # PRIORITY 1: Check loss protection (prevent catastrophic losses)
            await check_loss_protection(cli, sym, price)

            # PRIORITY 2: Check for entry signals (only if no position)
            await check_entry_signals(cli, sym, price)

            # PRIORITY 3: Handle trailing stops (only if still in position)
            await handle_trailing_stops(cli, sym, price)

async def main():
    # /ping server
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API creds")
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    await run(cli)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%b %d %H:%M:%S"
    )
    asyncio.run(main())
