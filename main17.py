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
    "ETHUSDT": 0.01,  "BNBUSDT": 0.03,  "SOLUSDT": 0.10,
    "XRPUSDT": 10,
}

# 4-hour tick-based WMA configuration
TICK_WMA_PERIOD = 4 * 60 * 60  # 4 hours in seconds
WILDER_ALPHA = 1.0 / TICK_WMA_PERIOD

# Movement threshold: 10% accumulated movement
MOVEMENT_THRESHOLD = 0.10  # 10%

# Bot activation: Only start when price is near WMA
# 0.05% gate (tightened)
WMA_ACTIVATION_THRESHOLD = 0.0005  # 0.05%

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

# ───────────── TICK-BASED WMA ─────────
class TickWMA:
    def __init__(self, period_seconds: int):
        self.period_seconds = period_seconds
        self.alpha = 1.0 / period_seconds
        self.value = None
        self.last_update = None
        self.tick_buffer = deque(maxlen=10000)  # Keep recent ticks for initialization

    def update(self, price: float):
        current_time = time.time()
        # Store tick with timestamp
        self.tick_buffer.append((current_time, price))
        if self.value is None:
            # Initialize with first price
            self.value = price
            self.last_update = current_time
            return
        # Apply time-weighted Wilder's MA
        time_diff = current_time - self.last_update
        effective_alpha = min(self.alpha * time_diff, 1.0)
        self.value = price * effective_alpha + self.value * (1.0 - effective_alpha)
        self.last_update = current_time

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
        "wma": TickWMA(TICK_WMA_PERIOD),
        "bot_active": False,                # Bot only activates when price near WMA
        "in_long": False, "long_pending": False,
        "long_entry_price": None,           # Entry price for percentage calculation
        "long_last_tick_price": None,       # Last price tick for cumulative calculation
        "long_exit_history": [],            # Historical exit prices
        "long_accumulated_movement": 0.0,   # Accumulated percentage movement
        "in_short": False, "short_pending": False,
        "short_entry_price": None,          # Entry price for percentage calculation
        "short_last_tick_price": None,      # Last price tick for cumulative calculation
        "short_exit_history": [],           # Historical exit prices
        "short_accumulated_movement": 0.0,  # Accumulated percentage movement
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
        # Check if we have sufficient balance/position for the operation
        if action.startswith("CLOSE") or "EXIT" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size(cli, symbol, side)
            required_qty = order_params['quantity']
            if current_pos < required_qty * 0.99:  # 1% tolerance for rounding
                logging.warning(f"{symbol} {action}: Insufficient position size {current_pos} < {required_qty}")
                return False
        # Execute the order
        result = await cli.futures_create_order(**order_params)
        state[symbol]["last_order_id"] = result.get('orderId')
        logging.info(f"{symbol} {action} executed successfully - OrderID: {state[symbol]['last_order_id']}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} failed: {e}")
        return False

# ───────────── LOGGING HELPERS ─────────
def log_entry_details(symbol: str, entry_type: str, price: float, exit_history: list, direction: str):
    """Log entry/re-entry with exit history (WMA gating only where specified)"""
    history_str = f"[{', '.join([f'${p:.4f}' for p in exit_history])}]" if exit_history else "[]"
    exit_count = len(exit_history)
    logging.info(f"{symbol} {entry_type} @ ${price:.4f}")
    logging.info(f"Exit History: {history_str} ({exit_count} exits)")

def log_exit_details(symbol: str, direction: str, entry_price: float, exit_price: float, accumulated_movement: float, exit_number: int):
    """Log exit with accumulated movement and trade PNL"""
    if direction == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
    else:  # SHORT
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
    pnl_sign = "+" if pnl_pct >= 0 else ""
    logging.info(f"{symbol} {direction} EXIT @ ${exit_price:.4f}")
    logging.info(f"Accumulated Movement: {accumulated_movement:.2f}% (threshold: {MOVEMENT_THRESHOLD*100:.1f}%)")
    logging.info(f"Trade PNL: {pnl_sign}{pnl_pct:.2f}% [Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f}]")
    if exit_number > 0:
        logging.info(f"Exit recorded to history (Exit #{exit_number})")
    else:
        logging.info("Exit NOT recorded (unfavorable direction)")

def get_target_exit_price(exit_history: list, is_long: bool) -> float:
    """
    Get target exit price for re-entry
    Long: Return HIGHEST exit price from history
    Short: Return LOWEST exit price from history
    """
    if not exit_history:
        return None
    if is_long:
        target = max(exit_history)
    else:
        target = min(exit_history)
    return target

def update_cumulative_movement(st: dict, current_price: float, position_type: str):
    """Update cumulative movement based on tick-to-tick price changes"""
    if position_type == "long":
        last_tick_key = "long_last_tick_price"
        movement_key = "long_accumulated_movement"
    else:  # short
        last_tick_key = "short_last_tick_price"
        movement_key = "short_accumulated_movement"
    
    last_tick_price = st[last_tick_key]
    
    if last_tick_price is not None:
        # Calculate absolute percentage change from previous tick
        tick_change_pct = abs((current_price - last_tick_price) / last_tick_price)
        # Add to accumulated movement
        st[movement_key] += tick_change_pct
    
    # Update last tick price
    st[last_tick_key] = current_price

async def seed_tick_wma(cli: AsyncClient):
    """Initialize tick-based WMA using recent 1-minute candles"""
    for s in SYMBOLS:
        kl = await cli.get_klines(symbol=s, interval="1m", limit=240)  # 4 hours of 1-min candles
        closes = [float(k[4]) for k in kl]
        # Initialize tick WMA with historical closes
        wma_obj = state[s]["wma"]
        for close in closes:
            wma_obj.update(close)
        logging.info(f"{s} Tick WMA initialized: {wma_obj.value:.4f} (4-hour tick-based)")

async def run(cli: AsyncClient):
    for s in SYMBOLS:
        await cli.futures_change_leverage(symbol=s, leverage=LEVERAGE)
    await seed_tick_wma(cli)

    streams = [f"{s.lower()}@trade" for s in SYMBOLS]  # Only trade data
    url     = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    async with websockets.connect(url) as ws:
        async for raw in ws:
            m     = json.loads(raw)
            stype = m["stream"]; d = m["data"]

            if not stype.endswith("@trade"):
                continue

            sym   = d["s"]
            price = float(d["p"])
            st    = state[sym]

            # Update tick-based WMA on EVERY trade
            st["wma"].update(price)
            wma = st["wma"].value

            prev = st["last_price"]
            st["last_price"] = price

            # ═══════════════ BOT ACTIVATION CHECK ═══════════════
            # Bot only starts/runs when price is almost/exact at WMA
            if not st["bot_active"]:
                wma_diff_pct = abs(price - wma) / wma
                if wma_diff_pct <= WMA_ACTIVATION_THRESHOLD:
                    st["bot_active"] = True
                    logging.info(f"{sym} BOT ACTIVATED - Price ${price:.4f} within {WMA_ACTIVATION_THRESHOLD*100:.2f}% of WMA ${wma:.4f}")
                else:
                    # Skip all trading logic until bot is activated
                    continue

            # ═══════════════ LONG TRADING LOGIC ═══════════════
            # LONG ENTRY CONDITIONS
            if not st["in_long"] and not st["long_pending"]:
                entry_triggered = False
                entry_type = ""
                # Smart Reset: If price <= WMA while waiting for re-entry, reset everything (unchanged)
                if len(st["long_exit_history"]) > 0 and price <= wma:
                    st["long_exit_history"] = []
                    logging.info(f"{sym} LONG: Smart Reset - Price ${price:.4f} <= WMA ${wma:.4f}, clearing exit history")
                if len(st["long_exit_history"]) == 0:
                    # FIRST ENTRY: require price > WMA
                    if price > wma:
                        entry_triggered = True
                        entry_type = "LONG FIRST ENTRY (price > WMA)"
                else:
                    # RE-ENTRY: price > highest_exit > WMA
                    highest_exit = get_target_exit_price(st["long_exit_history"], is_long=True)
                    if highest_exit and (highest_exit > wma) and (price > highest_exit):
                        entry_triggered = True
                        entry_type = f"LONG RE-ENTRY (price ${price:.4f} > highest exit ${highest_exit:.4f} > WMA ${wma:.4f})"
                if entry_triggered:
                    async with st["order_lock"]:
                        if st["in_long"] or st["long_pending"]:
                            continue
                        st["long_pending"] = True
                    try:
                        success = await safe_order_execution(cli, open_long(sym), sym, entry_type)
                        if success:
                            st["in_long"] = True
                            st["long_entry_price"] = price
                            st["long_last_tick_price"] = price
                            st["long_accumulated_movement"] = 0.0
                            log_entry_details(sym, entry_type, price, st["long_exit_history"], "LONG")
                        else:
                            logging.error(f"{sym} {entry_type} failed")
                    except Exception as e:
                        logging.error(f"{sym} {entry_type} error: {e}")
                    finally:
                        st["long_pending"] = False

            # LONG EXIT CONDITIONS
            if st["in_long"] and not st["long_pending"]:
                # Update cumulative movement based on tick-to-tick changes
                update_cumulative_movement(st, price, "long")
                
                # Exit when 10% accumulated movement is reached
                if st["long_accumulated_movement"] >= MOVEMENT_THRESHOLD:
                    async with st["order_lock"]:
                        if not st["in_long"] or st["long_pending"]:
                            continue
                        st["long_pending"] = True
                    try:
                        entry_price_for_pnl = st["long_entry_price"]
                        accumulated_for_log = st["long_accumulated_movement"]
                        success = await safe_order_execution(cli, close_long(sym), sym, "LONG EXIT")
                        if success:
                            st["in_long"] = False
                            # Record exit price
                            st["long_exit_history"].append(price)
                            log_exit_details(sym, "LONG", entry_price_for_pnl, price,
                                             accumulated_for_log * 100, len(st["long_exit_history"]))
                            # Reset position tracking
                            st["long_entry_price"] = None
                            st["long_last_tick_price"] = None
                            st["long_accumulated_movement"] = 0.0
                        else:
                            logging.error(f"{sym} LONG EXIT failed")
                    except Exception as e:
                        logging.error(f"{sym} LONG EXIT error: {e}")
                    finally:
                        st["long_pending"] = False

            # ═══════════════ SHORT TRADING LOGIC ═══════════════
            # SHORT ENTRY CONDITIONS
            if not st["in_short"] and not st["short_pending"]:
                entry_triggered = False
                entry_type = ""
                # Smart Reset: If price >= WMA while waiting for re-entry, reset everything (unchanged)
                if len(st["short_exit_history"]) > 0 and price >= wma:
                    st["short_exit_history"] = []
                    logging.info(f"{sym} SHORT: Smart Reset - Price ${price:.4f} >= WMA ${wma:.4f}, clearing exit history")
                if len(st["short_exit_history"]) == 0:
                    # FIRST ENTRY: require price < WMA
                    if price < wma:
                        entry_triggered = True
                        entry_type = "SHORT FIRST ENTRY (price < WMA)"
                else:
                    # RE-ENTRY: price < lowest_exit < WMA
                    lowest_exit = get_target_exit_price(st["short_exit_history"], is_long=False)
                    if lowest_exit and (lowest_exit < wma) and (price < lowest_exit):
                        entry_triggered = True
                        entry_type = f"SHORT RE-ENTRY (price ${price:.4f} < lowest exit ${lowest_exit:.4f} < WMA ${wma:.4f})"
                if entry_triggered:
                    async with st["order_lock"]:
                        if st["in_short"] or st["short_pending"]:
                            continue
                        st["short_pending"] = True
                    try:
                        success = await safe_order_execution(cli, open_short(sym), sym, entry_type)
                        if success:
                            st["in_short"] = True
                            st["short_entry_price"] = price
                            st["short_last_tick_price"] = price
                            st["short_accumulated_movement"] = 0.0
                            log_entry_details(sym, entry_type, price, st["short_exit_history"], "SHORT")
                        else:
                            logging.error(f"{sym} {entry_type} failed")
                    except Exception as e:
                        logging.error(f"{sym} {entry_type} error: {e}")
                    finally:
                        st["short_pending"] = False

            # SHORT EXIT CONDITIONS
            if st["in_short"] and not st["short_pending"]:
                # Update cumulative movement based on tick-to-tick changes
                update_cumulative_movement(st, price, "short")
                
                # Exit when 10% accumulated movement is reached
                if st["short_accumulated_movement"] >= MOVEMENT_THRESHOLD:
                    async with st["order_lock"]:
                        if not st["in_short"] or st["short_pending"]:
                            continue
                        st["short_pending"] = True
                    try:
                        entry_price_for_pnl = st["short_entry_price"]
                        accumulated_for_log = st["short_accumulated_movement"]
                        success = await safe_order_execution(cli, close_short(sym), sym, "SHORT EXIT")
                        if success:
                            st["in_short"] = False
                            # Record exit price
                            st["short_exit_history"].append(price)
                            log_exit_details(sym, "SHORT", entry_price_for_pnl, price,
                                             accumulated_for_log * 100, len(st["short_exit_history"]))
                            # Reset position tracking
                            st["short_entry_price"] = None
                            st["short_last_tick_price"] = None
                            st["short_accumulated_movement"] = 0.0
                        else:
                            logging.error(f"{sym} SHORT EXIT failed")
                    except Exception as e:
                        logging.error(f"{sym} SHORT EXIT error: {e}")
                    finally:
                        st["short_pending"] = False

async def main():
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
    asyncio.run(main())
