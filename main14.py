#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Dict, Any, List
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# ---- AGGRESSIVE TIMING (NO COOLDOWNS) ----
SIGNAL_CONFIRM_SEC  = 0.0   # No confirmation delay
TRADE_COOLDOWN_SEC  = 0.0   # No trade cooldown  
MIN_HOLD_SEC        = 0.0   # No minimum hold time

# ---- SYMBOLS ----
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

# ---- STRATEGY PARAMETERS ----
DONCHIAN_HOURS = 20
BOUNCE_THRESHOLD_PCT = 0.005  # 0.5%

# ---- PRICE SOURCE ----
PRICE_SOURCE = "mark"
def _ws_host() -> str:
    return "fstream.binance.com"
def _stream_name(sym: str) -> str:
    return f"{sym.lower()}@markPrice@1s"

# ---- RISK MANAGEMENT ----
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_BASE_SEC = 300
PRICE_STALENESS_SEC = 5.0
RATE_LIMIT_BASE_SLEEP = 2.0
RATE_LIMIT_MAX_SLEEP = 60.0

# ---- EPSILON for float boundary checks (ADDED) ----
EPS = 1e-8

class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200); self.end_headers()
    def log_message(self, *_): pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

# ========================= BINANCE HELPERS =========================
QUANTITY_STEP: Dict[str, Decimal] = {}

def quantize_qty(symbol: str, qty: float) -> float:
    step = QUANTITY_STEP.get(symbol)
    if step:
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        return float(q)
    return round(qty, 3)

_last_rest_ts = 0.0
async def call_binance(fn, *args, **kwargs):
    global _last_rest_ts
    now = time.time()
    if now - _last_rest_ts < 0.2:
        await asyncio.sleep(0.2 - (now - _last_rest_ts))
    _last_rest_ts = time.time()
    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) or "-1003" in str(e):
                await asyncio.sleep(min(delay, RATE_LIMIT_MAX_SLEEP))
                delay = min(delay * 2.0, RATE_LIMIT_MAX_SLEEP)
                continue
            raise

# ========================= POSITION STATE SYNC =========================
async def sync_position_state(cli: AsyncClient):
    """Sync bot's internal state with actual Binance positions on startup"""
    logging.info("=== SYNCING POSITION STATE ===")
    
    for symbol in SYMBOLS:
        try:
            long_size, short_size = await get_position_sizes(cli, symbol)
            st = state[symbol]
            
            # Determine actual position state
            if long_size > 0.1:  # Has LONG position
                st["current_signal"] = "LONG"
                st["last_position_entry_ts"] = time.time()
                logging.info(f"{symbol}: Found existing LONG position ({long_size}) - State synced")
            elif short_size > 0.1:  # Has SHORT position  
                st["current_signal"] = "SHORT"
                st["last_position_entry_ts"] = time.time()
                logging.info(f"{symbol}: Found existing SHORT position ({short_size}) - State synced")
            else:  # No position
                st["current_signal"] = None
                logging.info(f"{symbol}: No existing position - Starting fresh")
                
        except Exception as e:
            logging.error(f"{symbol}: Failed to sync position state: {e}")
            st["current_signal"] = None
DUAL_SIDE = False

async def get_dual_side(cli: AsyncClient) -> bool:
    try:
        res = await call_binance(cli.futures_get_position_mode)
        return bool(res.get("dualSidePosition", False))
    except Exception:
        return False

async def get_position_sizes(cli: AsyncClient, symbol: str) -> Tuple[float, float]:
    try:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
        long_size = short_size = 0.0
        for pos in positions:
            side = pos.get("positionSide")
            size = abs(float(pos.get("positionAmt", 0.0)))
            if side == "LONG":
                long_size = size
            elif side == "SHORT":
                short_size = size
        return long_size, short_size
    except Exception as e:
        logging.error(f"{symbol} get_position_sizes failed: {e}")
        return 0.0, 0.0

# ========================= STATE ===============================
state: Dict[str, Dict[str, Any]] = {
    s: {
        # Price data
        "last_price": None,
        "price_timestamp": None,
        "klines": deque(maxlen=1440),  # 24h of 1-min candles
        
        # WebSocket health
        "ws_last_heartbeat": 0.0,
        
        # Current state
        "current_signal": None,
        "signal_since": None,
        "last_trade_ts": 0.0,
        "last_position_entry_ts": 0.0,
        
        # Risk management
        "consecutive_failures": 0,
        "circuit_breaker_until": 0.0,
        "order_lock": asyncio.Lock(),
        "position_state": "IDLE",
        
        # Signal tracking
        "pending_signal": None,
        "last_breakout_ts": 0.0,
        
        # Bounce state tracking for ALL 4 SCENARIOS
        "bounce_breach_state": "NONE",  # NONE, BREACHED_HIGH, BREACHED_LOW
        "bounce_breach_ts": 0.0,
        "bounce_reverse_confirmed": False,
        
    } for s in SYMBOLS
}

def symbol_target_qty(sym: str) -> float:
    return float(SYMBOLS[sym])

# ========================= CORE CALCULATIONS =========================
def get_donchian_levels(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    st = state[symbol]
    klines = st["klines"]
    
    if len(klines) < 100:
        return None, None
    
    now = time.time()
    cutoff_time = now - (20 * 3600)
    current_minute = _minute_bucket(now)  # Add this: Get the current minute bucket
    
    # Filter to last 20h, EXCLUDING the current (forming) minute
    valid_klines = [k for k in klines if k["time"] >= cutoff_time and k["minute"] < current_minute]
    
    if len(valid_klines) < 50:
        return None, None
    
    minute_highs = [k["high"] for k in valid_klines]
    minute_lows = [k["low"] for k in valid_klines]
    
    donchian_low = min(minute_lows)
    donchian_high = max(minute_highs)
    
    return donchian_low, donchian_high

def get_bounce_levels(donchian_low: float, donchian_high: float) -> Tuple[float, float]:
    """Calculate bounce thresholds"""
    channel = donchian_high - donchian_low
    bounce_offset = channel * BOUNCE_THRESHOLD_PCT
    bounce_low = donchian_low + bounce_offset
    bounce_high = donchian_high - bounce_offset
    return bounce_low, bounce_high

# ========================= 4 SCENARIO SIGNAL DETECTION - NON BIASED =========================
def detect_breakout_signal(symbol: str) -> Optional[str]:
    """SCENARIO 4: Pure Donchian Breakout - Price > High = LONG, Price < Low = SHORT"""
    st = state[symbol]
    current_price = st.get("last_price")
    
    if not current_price:
        return None
    
    donchian_low, donchian_high = get_donchian_levels(symbol)
    
    if not donchian_low or not donchian_high:
        return None
    
    # PURE BREAKOUT LOGIC (with epsilon + more precision logs)  (MODIFIED)
    if current_price >= donchian_high - EPS:
        logging.info(f"{symbol}: SCENARIO 4 - BREAKOUT LONG | price={current_price:.10f} >= donchian_high={donchian_high:.10f}")
        return "LONG"
    elif current_price <= donchian_low + EPS:
        logging.info(f"{symbol}: SCENARIO 4 - BREAKOUT SHORT | price={current_price:.10f} <= donchian_low={donchian_low:.10f}")
        return "SHORT"
    
    return None

def detect_bounce_signal(symbol: str) -> Optional[str]:
    """SCENARIO 3: Bounce signals - true reversals from extremes"""
    st = state[symbol]
    current_price = st.get("last_price")
    
    if not current_price:
        return None
    
    donchian_low, donchian_high = get_donchian_levels(symbol)
    
    if not donchian_low or not donchian_high:
        return None
    
    # Calculate bounce thresholds
    channel_size = donchian_high - donchian_low
    bounce_offset = channel_size * BOUNCE_THRESHOLD_PCT
    long_bounce_threshold = donchian_low + bounce_offset   # Bounce UP from low
    short_bounce_threshold = donchian_high - bounce_offset # Bounce DOWN from high
    
    current_breach = st.get("bounce_breach_state", "NONE")
    now = time.time()
    
    # Phase 1: Detect breach of extreme levels (precision logs)  (MODIFIED: logging format only)
    if current_price <= donchian_low and current_breach != "BREACHED_LOW":
        st["bounce_breach_state"] = "BREACHED_LOW"
        st["bounce_breach_ts"] = now
        st["bounce_reverse_confirmed"] = False
        logging.info(f"{symbol}: BOUNCE - Breached LOW at {current_price:.10f} (donchian_low: {donchian_low:.10f})")
        return None
        
    elif current_price >= donchian_high and current_breach != "BREACHED_HIGH":
        st["bounce_breach_state"] = "BREACHED_HIGH"
        st["bounce_breach_ts"] = now
        st["bounce_reverse_confirmed"] = False
        logging.info(f"{symbol}: BOUNCE - Breached HIGH at {current_price:.10f} (donchian_high: {donchian_high:.10f})")
        return None
    
    # Phase 2: Check for reversal and signal confirmation (precision logs)  (MODIFIED: logging format only)
    if current_breach == "BREACHED_LOW" and not st["bounce_reverse_confirmed"]:
        if current_price >= long_bounce_threshold:
            st["bounce_reverse_confirmed"] = True
            logging.info(f"{symbol}: SCENARIO 3 - BOUNCE LONG confirmed | price={current_price:.10f} >= threshold={long_bounce_threshold:.10f}")
            return "LONG"
            
    elif current_breach == "BREACHED_HIGH" and not st["bounce_reverse_confirmed"]:
        if current_price <= short_bounce_threshold:
            st["bounce_reverse_confirmed"] = True
            logging.info(f"{symbol}: SCENARIO 3 - BOUNCE SHORT confirmed | price={current_price:.10f} <= threshold={short_bounce_threshold:.10f}")
            return "SHORT"
    
    return None

def detect_signals(symbol: str) -> tuple:
    """
    Detect ALL 4 SCENARIOS with NO BIAS:
    SCENARIO 1: Breakout → Bounce Flips
    SCENARIO 2: Bounce → Breakout Flips  
    SCENARIO 3: Bounce → Bounce Flips
    SCENARIO 4: Pure Donchian Breakout (Price > High = LONG, Price < Low = SHORT)
    
    Returns (signal, source) where source is "BREAKOUT" or "BOUNCE"
    """
    st = state[symbol]
    current_signal = st.get("current_signal", "NONE")
    
    # Check BOTH signals simultaneously - NO priority order
    breakout_signal = detect_breakout_signal(symbol)
    bounce_signal = detect_bounce_signal(symbol)
    
    # SCENARIO 4: Pure Donchian Breakout (can flip positions)
    if breakout_signal and breakout_signal != current_signal:
        # Reset bounce state when breakout fires (prevents conflicts)
        st["bounce_breach_state"] = "NONE"
        st["bounce_reverse_confirmed"] = False
        return breakout_signal, "BREAKOUT"
    
    # SCENARIO 1: Breakout → Bounce Flips
    if current_signal == "LONG" and breakout_signal == "LONG" and bounce_signal == "SHORT":
        logging.info(f"{symbol}: SCENARIO 1 - Breakout LONG → Bounce SHORT flip")
        return "SHORT", "BOUNCE"
    elif current_signal == "SHORT" and breakout_signal == "SHORT" and bounce_signal == "LONG":
        logging.info(f"{symbol}: SCENARIO 1 - Breakout SHORT → Bounce LONG flip")
        return "LONG", "BOUNCE"
    
    # SCENARIO 2: Bounce → Breakout Flips  
    elif current_signal == "LONG" and bounce_signal == "LONG" and breakout_signal == "SHORT":
        logging.info(f"{symbol}: SCENARIO 2 - Bounce LONG → Breakout SHORT flip")
        return "SHORT", "BREAKOUT"
    elif current_signal == "SHORT" and bounce_signal == "SHORT" and breakout_signal == "LONG":
        logging.info(f"{symbol}: SCENARIO 2 - Bounce SHORT → Breakout LONG flip")
        return "LONG", "BREAKOUT"
    
    # SCENARIO 3: Bounce → Bounce Flips
    elif current_signal == "LONG" and bounce_signal == "SHORT":
        logging.info(f"{symbol}: SCENARIO 3 - Bounce LONG → Bounce SHORT flip")
        return "SHORT", "BOUNCE"
    elif current_signal == "SHORT" and bounce_signal == "LONG":
        logging.info(f"{symbol}: SCENARIO 3 - Bounce SHORT → Bounce LONG flip")
        return "LONG", "BOUNCE"
    
    # NEW POSITION ENTRY (no current position)
    elif current_signal == "NONE":
        if breakout_signal:
            logging.info(f"{symbol}: NEW ENTRY - Breakout {breakout_signal}")
            return breakout_signal, "BREAKOUT"
        elif bounce_signal:
            logging.info(f"{symbol}: NEW ENTRY - Bounce {bounce_signal}")
            return bounce_signal, "BOUNCE"
    
    return None, "NONE"

# ========================= ORDER EXECUTION =========================
def _maybe_pos_side(params: dict, side: str) -> dict:
    if DUAL_SIDE:
        params["positionSide"] = side
    else:
        params.pop("positionSide", None)
    return params

def open_long(sym: str, qty: float):
    return _maybe_pos_side(dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty)), "LONG")

def open_short(sym: str, qty: float):
    return _maybe_pos_side(dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty)), "SHORT")

def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return _maybe_pos_side(p, "LONG")

def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return _maybe_pos_side(p, "SHORT")

async def execute_order(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    try:
        res = await call_binance(cli.futures_create_order, **order_params)
        oid = res.get("orderId")
        logging.info(f"{symbol} {action} EXECUTED - OrderID: {oid}")
        return True
    except Exception as e:
        msg = str(e)
        # Handle reduceOnly errors
        if "-1106" in msg and "reduceonly" in msg.lower() and "reduceOnly" in order_params:
            try:
                params_retry = {k: v for k, v in order_params.items() if k != "reduceOnly"}
                res = await call_binance(cli.futures_create_order, **params_retry)
                oid = res.get("orderId")
                logging.info(f"{symbol} {action} EXECUTED (no reduceOnly) - OrderID: {oid}")
                return True
            except Exception as e2:
                logging.error(f"{symbol} {action} RETRY FAILED: {e2}")
        else:
            logging.error(f"{symbol} {action} FAILED: {e}")
        return False

async def set_position(cli: AsyncClient, symbol: str, target_signal: str):
    """Execute position change immediately"""
    st = state[symbol]
    async with st["order_lock"]:
        if st["position_state"] != "IDLE":
            # ADDED: debug when order is already in progress
            logging.debug(f"{symbol} skip: order in progress (position_state={st['position_state']})")
            return False
        st["position_state"] = "PROCESSING"
        
        try:
            long_size, short_size = await get_position_sizes(cli, symbol)
            target_qty = symbol_target_qty(symbol)
            
            success = True
            
            # Close opposite positions first
            if target_signal == "LONG" and short_size > 0.1:
                success &= await execute_order(cli, exit_short(symbol, short_size), symbol, "CLOSE SHORT")
            elif target_signal == "SHORT" and long_size > 0.1:
                success &= await execute_order(cli, exit_long(symbol, long_size), symbol, "CLOSE LONG")
            
            # Open target position
            if target_signal == "LONG" and long_size < target_qty * 0.9:
                delta = target_qty - long_size
                success &= await execute_order(cli, open_long(symbol, delta), symbol, "OPEN LONG")
            elif target_signal == "SHORT" and short_size < target_qty * 0.9:
                delta = target_qty - short_size
                success &= await execute_order(cli, open_short(symbol, delta), symbol, "OPEN SHORT")
            
            if success:
                st["consecutive_failures"] = 0
                st["last_position_entry_ts"] = time.time()
                logging.info(f"{symbol} POSITION SET TO {target_signal}")
            else:
                st["consecutive_failures"] += 1
                st["circuit_breaker_until"] = time.time() + (CIRCUIT_BREAKER_BASE_SEC * st["consecutive_failures"])
                
            return success
        finally:
            st["position_state"] = "IDLE"

# ========================= PRICE FEED =========================
def _minute_bucket(ts: float) -> int:
    return int(ts // 60)

async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("WebSocket connected")
                async for raw in ws:
                    try:
                        now = time.time()
                        
                        # Update heartbeat for all symbols
                        for sym in SYMBOLS:
                            state[sym]["ws_last_heartbeat"] = now
                            
                        m = json.loads(raw)
                        d = m.get("data", {})
                        if not d:
                            continue
                            
                        sym = d.get("s")
                        if not sym or sym not in state:
                            continue
                            
                        # Parse price
                        p_str = d.get("p") or d.get("c") or d.get("ap") or d.get("a")
                        if not p_str:
                            continue
                            
                        price = float(p_str)
                        st = state[sym]
                        
                        # Update current price
                        st["last_price"] = price
                        st["price_timestamp"] = now
                        
                        # Update kline data
                        current_minute = _minute_bucket(now)
                        klines = st["klines"]
                        
                        if not klines or klines[-1]["minute"] != current_minute:
                            # New minute candle
                            klines.append({
                                "minute": current_minute,
                                "time": now,
                                "high": price,
                                "low": price,
                                "close": price
                            })
                        else:
                            # Update current minute
                            klines[-1]["high"] = max(klines[-1]["high"], price)
                            klines[-1]["low"] = min(klines[-1]["low"], price)
                            klines[-1]["close"] = price
                            
                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
                        continue
                        
        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ========================= MAIN TRADING LOOP =========================
async def trading_loop(cli: AsyncClient):
    while True:
        await asyncio.sleep(0.1)  # Very fast polling
        
        for sym in SYMBOLS:
            st = state[sym]
            now = time.time()
            
            # Skip if no recent price data (ADDED: debug reason)
            if st["last_price"] is None or now - st.get("price_timestamp", 0) > PRICE_STALENESS_SEC:
                if st["last_price"] is not None:
                    logging.debug(f"{sym} skip: stale price ({now - st['price_timestamp']:.2f}s old > {PRICE_STALENESS_SEC}s)")
                continue
            
            # Circuit breaker check (ADDED: debug when active)
            if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                if now < st["circuit_breaker_until"]:
                    logging.debug(f"{sym} skip: circuit breaker active ({st['circuit_breaker_until'] - now:.2f}s remaining)")
                    continue
                st["consecutive_failures"] = 0
                logging.info(f"{sym} Circuit breaker reset")
            
            # Detect signal with NO BIAS
            signal, signal_source = detect_signals(sym)
            if signal:
                st["pending_signal"] = signal
                st["pending_source"] = signal_source
            
            pending = st.get("pending_signal")
            pending_source = st.get("pending_source", "UNKNOWN")
            if not pending:
                # ---- NEW: ensure we reach target even if signal didn't change ----
                if st.get("current_signal") in ("LONG", "SHORT"):
                    long_size, short_size = await get_position_sizes(cli, sym)
                    target_qty = symbol_target_qty(sym)
                    if st["current_signal"] == "LONG" and long_size < target_qty * 0.9:
                        st["pending_signal"] = "LONG"
                        st["pending_source"] = "REACH_TARGET"
                    elif st["current_signal"] == "SHORT" and short_size < target_qty * 0.9:
                        st["pending_signal"] = "SHORT"
                        st["pending_source"] = "REACH_TARGET"
                # re-read pending after top-up evaluation
                pending = st.get("pending_signal")
                pending_source = st.get("pending_source", "UNKNOWN")
                if not pending:
                    continue
                
            current = st["current_signal"]
            
            # Check minimum hold time for position flips (ADDED: debug when skipping)
            if current and current != pending and st.get("last_position_entry_ts", 0) > 0:
                elapsed = now - st["last_position_entry_ts"]
                if elapsed < MIN_HOLD_SEC:
                    logging.debug(f"{sym} skip: hold {elapsed:.3f}s/{MIN_HOLD_SEC}s before flip {current}->{pending}")
                    continue
            
            # Update signal state
            if st["signal_since"] is None or current != pending:
                st["current_signal"] = pending
                st["signal_since"] = now
                logging.info(f"{sym} SIGNAL CHANGE: {current} -> {pending} ({pending_source}) - NO BIAS")
            
            # Signal confirmation delay (ADDED: debug when skipping)
            elapsed = now - st["signal_since"]
            if elapsed < SIGNAL_CONFIRM_SEC:
                logging.debug(f"{sym} skip: confirm {elapsed:.3f}s/{SIGNAL_CONFIRM_SEC}s")
                continue
                
            # Trade cooldown (ADDED: debug when skipping)
            since_last = now - st["last_trade_ts"]
            if since_last < TRADE_COOLDOWN_SEC:
                logging.debug(f"{sym} skip: cooldown {since_last:.3f}s/{TRADE_COOLDOWN_SEC}s")
                continue
            
            # Execute trade
            try:
                success = await set_position(cli, sym, pending)
                if success:
                    st["last_trade_ts"] = now
                    st["pending_signal"] = None
                    st["pending_source"] = None
            except Exception as e:
                logging.error(f"{sym} Trade execution failed: {e}")
                st["consecutive_failures"] += 1

# ========================= MONITORING =========================
async def monitoring_loop(cli: AsyncClient):
    while True:
        await asyncio.sleep(300)  # Every 5 minutes
        
        logging.info("=== STATUS REPORT ===")
        for sym in SYMBOLS:
            st = state[sym]
            price = st.get("last_price")
            donchian_low, donchian_high = get_donchian_levels(sym)
            
            price_str = f"{price:.6f}" if price is not None else "000000.000000"
            status = f"{sym}: price={price_str} | signal={st.get('current_signal', 'None')}"

            # MODIFIED: explicit None checks so zeroes aren't hidden
            if donchian_low is not None and donchian_high is not None:
                status += f" | donchian_low={donchian_low:.6f} | donchian_high={donchian_high:.6f}"
                if price is not None:
                    bounce_low, bounce_high = get_bounce_levels(donchian_low, donchian_high)
                    status += f" | bounce_low={bounce_low:.6f} | bounce_high={bounce_high:.6f}"
            
            logging.info(status)

# ========================= INITIALIZATION =========================
async def init_filters(cli: AsyncClient):
    try:
        info = await call_binance(cli.futures_exchange_info)
        for s in info.get("symbols", []):
            sym = s["symbol"]
            if sym not in SYMBOLS:
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = f.get("stepSize")
                    if step:
                        QUANTITY_STEP[sym] = Decimal(step)
    except Exception as e:
        logging.warning(f"Filter initialization failed: {e}")

async def seed_klines(cli: AsyncClient):
    for sym in SYMBOLS:
        try:
            # Get last 20h of 1m klines (20 hours × 60 minutes = 1200 candles)
            klines = await call_binance(cli.futures_klines, symbol=sym, interval="1m", limit=1200)
            st = state[sym]
            
            for k in klines:
                ts_open = float(k[0]) / 1000.0
                minute = _minute_bucket(ts_open)
                st["klines"].append({
                    "minute": minute,
                    "time": ts_open,
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4])
                })
            
            logging.info(f"{sym} seeded with {len(st['klines'])} candles")
        except Exception as e:
            logging.warning(f"{sym} kline seeding failed: {e}")

# ========================= MAIN =========================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API credentials")
    
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    
    try:
        global DUAL_SIDE
        DUAL_SIDE = await get_dual_side(cli)
        if not DUAL_SIDE:
            raise RuntimeError("Enable Hedge Mode in Binance Futures")
        
        logging.info("Hedge Mode confirmed")
        
        # Set leverage
        for s in SYMBOLS:
            try:
                await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except Exception as e:
                logging.warning(f"{s} leverage setting failed: {e}")
        
        await init_filters(cli)
        await seed_klines(cli)
        
        # NEW: Sync bot state with actual positions
        await sync_position_state(cli)
        
        # Start all tasks
        feed_task = asyncio.create_task(price_feed_loop(cli))
        trading_task = asyncio.create_task(trading_loop(cli))
        monitor_task = asyncio.create_task(monitoring_loop(cli))
        
        await asyncio.gather(feed_task, trading_task, monitor_task)
        
    finally:
        try:
            await cli.close_connection()
        except Exception:
            pass

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s %(levelname)s: %(message)s", 
        datefmt="%b %d %H:%M:%S"
    )
    
    logging.info("=== ALL 4 SCENARIOS - NON BIASED ===")
    logging.info(f"SCENARIO 1: Breakout → Bounce Flips")
    logging.info(f"SCENARIO 2: Bounce → Breakout Flips")  
    logging.info(f"SCENARIO 3: Bounce → Bounce Flips")
    logging.info(f"SCENARIO 4: Pure Donchian Breakout (Price > High = LONG, Price < Low = SHORT)")
    logging.info(f"NO BIAS: All scenarios treated equally")
    logging.info(f"Symbols: {list(SYMBOLS.keys())}")
    logging.info(f"Donchian Window: {DONCHIAN_HOURS}h | Bounce Threshold: {BOUNCE_THRESHOLD_PCT*100:.1f}%")
    logging.info(f"ULTRA-FAST: confirm={SIGNAL_CONFIRM_SEC}s, cooldown={TRADE_COOLDOWN_SEC}s, hold={MIN_HOLD_SEC}s")
    
    asyncio.run(main())
