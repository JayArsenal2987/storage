#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, math, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Deque, Dict, Any, Union
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# ---- SIMPLIFIED STRATEGY PARAMETERS ----
POSITION_SIZE_ADA   = float(os.getenv("POSITION_SIZE_ADA", "10.0"))   # Single position size
SIGNAL_CONFIRM_SEC  = float(os.getenv("SIGNAL_CONFIRM_SEC", "3.0"))   # Unified confirmation time (WMA only)
TRADE_COOLDOWN_SEC  = float(os.getenv("TRADE_COOLDOWN_SEC", "10.0"))  # Minimum time between trades
PRICE_GLITCH_PCT    = float(os.getenv("PRICE_GLITCH_PCT", "0.03"))    # Reduced to 3%
CALLBACK_RATE_PCT   = float(os.getenv("CALLBACK_RATE_PCT", "0.005"))  # 0.5% trailing callback gate (exits & flips)

# ---- PRICE SOURCE ----
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "mark").lower()

def _ws_host() -> str:
    return "fstream.binance.com" if PRICE_SOURCE in ("futures", "mark") else "stream.binance.com:9443"

def _stream_name(sym: str) -> str:
    return f"{sym.lower()}@markPrice@1s" if PRICE_SOURCE == "mark" else f"{sym.lower()}@trade"

# ---- SYMBOLS (ADA only) ----
SYMBOLS = { "ADAUSDT": 10.0 }

# ---- WMA config ----
TICK_WMA_PERIOD = 5 * 60 * 60        # 5 hours in seconds
WMA_ACTIVATION_THRESHOLD = 0.001     # 0.1% near WMA before activation
WMA_EQUAL_TOL = float(os.getenv("WMA_EQUAL_TOL", "0.002"))  # 0.2% tolerance for "price ≈ WMA"

# ---- Bounce-from-extremes config (stateful, multi-window) ----
BOUNCE_ENABLED       = True
# Full set: 1m, 5m, 15m, 1h, 4h, 24h
BOUNCE_WINDOWS_SEC   = [60, 300, 900, 3600, 4*3600, 24*3600]
BOUNCE_THRESHOLD_PCT = float(os.getenv("BOUNCE_THRESHOLD_PCT", "2.0")) / 100.0  # 2% (low*1.02 LONG, high*0.98 SHORT)
BOUNCE_COOLDOWN_SEC  = float(os.getenv("BOUNCE_COOLDOWN_SEC", "30.0"))
# Immediate entry + stickiness/hysteresis
BOUNCE_CONFIRM_SEC   = float(os.getenv("BOUNCE_CONFIRM_SEC", "0.0"))    # 0s = immediate entry
BOUNCE_MIN_HOLD_SEC  = float(os.getenv("BOUNCE_MIN_HOLD_SEC", "30.0"))  # min hold after entry
BOUNCE_EXIT_HYST_PCT = float(os.getenv("BOUNCE_EXIT_HYST_PCT", "0.006"))# 0.6% hysteresis for exits

# Risk Management
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_BASE_SEC = 300  # 5 minutes base cooldown
PRICE_STALENESS_SEC = 5.0       # Reject prices older than 5s

# Logging
PNL_SUMMARY_SEC       = 1800.0
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "45.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# ========================= UTILITY CLASSES =========================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
    def log_message(self, *_):
        pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

class TickWMA:
    def __init__(self, period_seconds: int):
        self.period_seconds = period_seconds
        self.alpha = 1.0 / period_seconds
        self.value: Optional[float] = None
        self.last_update: Optional[float] = None
        self.tick_buffer = deque(maxlen=1000)  # Reduced from 10000 per your change
        self.initialized = False

    def update(self, price: float):
        current_time = time.time()
        self.tick_buffer.append((current_time, price))
        if self.value is None:
            self.value = price
            self.last_update = current_time
            return
        time_diff = current_time - self.last_update
        effective_alpha = min(self.alpha * time_diff, 1.0)
        self.value = price * effective_alpha + self.value * (1.0 - effective_alpha)
        self.last_update = current_time
        self.initialized = True

# ========================= PRICE & QTY FILTERS =================
PRICE_TICK: Dict[str, Decimal] = {}
PRICE_DECIMALS: Dict[str, int] = {}
QUANTITY_STEP: Dict[str, Decimal] = {}
QUANTITY_DECIMALS: Dict[str, int] = {}

def _dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))

def _decimals_from_tick(tick: str) -> int:
    if "." not in tick: return 0
    return len(tick.split(".")[1].rstrip("0"))

def quantize_price(symbol: str, price: float) -> str:
    tick = PRICE_TICK.get(symbol)
    if not tick:
        return f"{price:.8f}"
    p = _dec(price)
    q = (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    decs = PRICE_DECIMALS.get(symbol, 8)
    return f"{q:.{decs}f}"

def quantize_qty(symbol: str, qty: float) -> float:
    step = QUANTITY_STEP.get(symbol)
    if step:
        q = (_dec(qty) / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        return float(q)
    return round(qty, QUANTITY_DECIMALS.get(symbol, 3))

# ========================= SAFE BINANCE CALL ===================
_last_rest_ts = 0.0
async def call_binance(fn, *args, **kwargs):
    global _last_rest_ts
    now = time.time()
    min_gap = 0.2  # Slightly increased gap for safety
    if now - _last_rest_ts < min_gap:
        await asyncio.sleep(min_gap - (now - _last_rest_ts) + random.uniform(0.01, 0.05))
    _last_rest_ts = time.time()
    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "-1003" in msg or "Too many requests" in msg or "IP banned" in msg:
                sleep_for = min(delay, RATE_LIMIT_MAX_SLEEP)
                logging.warning(f"Rate limit: sleeping {sleep_for:.1f}s")
                await asyncio.sleep(sleep_for + random.uniform(0.0, 0.5))
                delay = min(delay * 2.0, RATE_LIMIT_MAX_SLEEP)
                continue
            raise

# ========================= POSITION HELPERS ====================
position_cache = {
    s: {"LONG": {"size": 0.0, "ts": 0.0}, "SHORT": {"size": 0.0, "ts": 0.0}}
    for s in SYMBOLS
}

def invalidate_position_cache(symbol: str):
    position_cache[symbol]["LONG"]["ts"] = 0.0
    position_cache[symbol]["SHORT"]["ts"] = 0.0

async def get_dual_side(cli: AsyncClient) -> bool:
    try:
        res = await call_binance(cli.futures_get_position_mode)
        return bool(res.get("dualSidePosition", False))
    except Exception:
        return False

async def refresh_position_cache(cli: AsyncClient, symbol: str):
    try:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
        sizes = {"LONG": 0.0, "SHORT": 0.0}
        for pos in positions:
            side = pos.get("positionSide")
            if side in sizes:
                sizes[side] = abs(float(pos.get("positionAmt", 0.0)))
        now = time.time()
        for side in ("LONG", "SHORT"):
            position_cache[symbol][side]["size"] = sizes[side]
            position_cache[symbol][side]["ts"] = now
    except Exception as e:
        logging.warning(f"{symbol} refresh_position_cache failed: {e}")

async def get_position_size_fresh(cli: AsyncClient, symbol: str, side: str) -> float:
    await refresh_position_cache(cli, symbol)
    return position_cache[symbol][side]["size"]

async def get_positions_snapshot(cli: AsyncClient, symbol: str) -> Dict[str, Dict[str, float]]:
    snap = {"LONG": {"size": 0.0, "entry": 0.0, "uPnL": 0.0},
            "SHORT": {"size": 0.0, "entry": 0.0, "uPnL": 0.0}}
    try:
        positions = await call_binance(cli, cli.futures_position_information, symbol=symbol)
    except TypeError:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
    try:
        for pos in positions:
            side = pos.get("positionSide")
            if side in ("LONG", "SHORT"):
                snap[side]["size"]  = abs(float(pos.get("positionAmt", 0.0)))
                snap[side]["entry"] = float(pos.get("entryPrice", 0.0) or 0.0)
                snap[side]["uPnL"]  = float(pos.get("unRealizedProfit", pos.get("unrealizedProfit", 0.0)) or 0.0)
    except Exception as e:
        logging.warning(f"{symbol} positions snapshot failed: {e}")
    return snap

# ========================= ORDER BUILDERS ======================
DUAL_SIDE = False  # Will be set True only if Hedge Mode is confirmed

def _maybe_pos_side(params: dict, side: str) -> dict:
    if DUAL_SIDE:
        params["positionSide"] = side
    else:
        params.pop("positionSide", None)
    return params

def open_long(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty))
    return _maybe_pos_side(p, "LONG")

def open_short(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty))
    return _maybe_pos_side(p, "SHORT")

def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE: p["reduceOnly"] = True
    return _maybe_pos_side(p, "LONG")

def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE: p["reduceOnly"] = True
    return _maybe_pos_side(p, "SHORT")

# ========================= EXECUTION WRAPPER ===================
async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            try:
                res = await call_binance(cli.futures_create_order, **order_params)
            except Exception as e:
                msg = str(e)
                if "-1106" in msg and "reduceonly" in msg.lower() and ("reduceOnly" in order_params):
                    params_wo = {k: v for k, v in order_params.items() if k != "reduceOnly"}
                    logging.warning(f"{symbol} {action}: retrying without reduceOnly")
                    res = await call_binance(cli.futures_create_order, **params_wo)
                else:
                    raise

            oid = res.get("orderId")
            invalidate_position_cache(symbol)
            logging.info(f"{symbol} {action} executed - OrderID: {oid}")
            return True

        except Exception as e:
            if attempt < max_retries:
                logging.warning(f"{symbol} {action} attempt {attempt + 1} failed: {e}, retrying...")
                await asyncio.sleep(1.0 + random.uniform(0.0, 0.5))
                continue
            else:
                logging.error(f"{symbol} {action} failed after {max_retries + 1} attempts: {e}")
                return False

# ========================= STATE ===============================
state: Dict[str, Dict[str, Any]] = {
    s: {
        # price feed
        "last_price": None,
        "last_good_price": None,
        "price_timestamp": None,  # Track when price was received
        "price_buffer": deque(maxlen=5000),
        "ext_price_buffer": deque(maxlen=100000),  # needs ~24h of 1s ticks
        "ws_last_heartbeat": 0.0,

        # WMA + trading flags
        "wma": TickWMA(TICK_WMA_PERIOD),
        "bot_active": False,
        "manual_override": False,  # Allow manual activation

        # Signal/flow tracking
        "current_signal": None,         # "LONG", "SHORT", or "FLAT"
        "signal_since": None,           # ts when current signal started
        "last_trade_ts": 0.0,           # cooldown tracking
        "active_signal_source": None,   # "BOUNCE" or "WMA"

        # Bounce state (stateful)
        "last_bounce_ts": 0.0,
        "bounce_signal_ts": None,
        "bounce_active": None,       # "LONG"/"SHORT"/None
        "bounce_baseline": None,     # low for LONG, high for SHORT (per window)
        "bounce_hold_until": 0.0,
        "bounce_candidate_side": None,
        "bounce_candidate_base": None,
        "bounce_candidate_ts": None,

        # Callback trailing extremes (for exits & flips gate)
        "trail_peak": None,             # highest since LONG entry
        "trail_trough": None,           # lowest since SHORT entry

        # Risk management
        "consecutive_failures": 0,
        "circuit_breaker_until": 0.0,
        "position_state": "IDLE",       # Track order state to prevent conflicts

        # misc
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "last_position_entry_ts": 0.0,
    } for s in SYMBOLS
}

# ========================= SYMBOL FILTERS ======================
async def seed_symbol_filters(cli: AsyncClient):
    try:
        info = await call_binance(cli.futures_exchange_info)
        symbols_info = {s["symbol"]: s for s in info.get("symbols", [])}
        for sym in SYMBOLS:
            si = symbols_info.get(sym)
            if not si:
                continue
            filters = si.get("filters", [])
            pf = next((f for f in filters if f.get("filterType") == "PRICE_FILTER"), None)
            if pf:
                tick = pf.get("tickSize")
                if tick:
                    PRICE_TICK[sym] = _dec(tick)
                    PRICE_DECIMALS[sym] = _decimals_from_tick(tick)
            qf = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), None)
            if qf:
                step = qf.get("stepSize")
                if step:
                    QUANTITY_STEP[sym] = _dec(step)
                    QUANTITY_DECIMALS[sym] = _decimals_from_tick(step)
    except Exception as e:
        logging.warning(f"seed_symbol_filters failed: {e}")

# ========================= PRICE FEED ===========================
def _parse_price_from_event(d: dict) -> Optional[float]:
    p_str = d.get("p") or d.get("c") or d.get("ap") or d.get("a")
    if p_str is None:
        return None
    try:
        return float(p_str)
    except Exception:
        return None

async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"
    reconnect_delay = 1.0
    max_reconnect_delay = 60.0

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5, max_queue=1000) as ws:
                reconnect_delay = 1.0
                logging.info("WebSocket price feed connected")

                async for raw in ws:
                    now = time.time()
                    for sym in SYMBOLS:
                        state[sym]["ws_last_heartbeat"] = now

                    m = json.loads(raw)
                    d = m.get("data", {})
                    sym = d.get("s")
                    if not sym or sym not in state:
                        continue
                    
                    p = _parse_price_from_event(d)
                    if p is None or p <= 0.0:
                        continue

                    st = state[sym]
                    prev = st["last_price"]
                    if prev is not None and abs(p / prev - 1.0) > PRICE_GLITCH_PCT:
                        logging.warning(f"{sym} Price glitch filtered: {prev:.6f} -> {p:.6f}")
                        continue

                    st["last_price"] = p
                    st["last_good_price"] = p
                    st["price_timestamp"] = now
                    st["price_buffer"].append((now, p))
                    st["ext_price_buffer"].append((now, p))
                    st["wma"].update(p)

        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting in {reconnect_delay:.1f}s...")
            await asyncio.sleep(reconnect_delay + random.uniform(0.0, 0.8))
            reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)

# ========================= INITIALIZATION ===================
async def seed_tick_wma(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            kl = await call_binance(cli.futures_klines, symbol=s, interval="1m", limit=300)
            closes = [float(k[4]) for k in kl]
            wma = state[s]["wma"]
            for c in closes:
                wma.update(c)
            if wma.value is not None:
                logging.info(f"{s} WMA initialized: {wma.value:.6f}")
        except Exception as e:
            logging.warning(f"{s} WMA seed failed: {e}")

async def seed_extremes_buffer(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            kl = await call_binance(cli.futures_klines, symbol=s, interval="1m", limit=1440)
            buf = state[s]["ext_price_buffer"]
            for k in kl:
                ts = float(k[0]) / 1000.0
                c = float(k[4])
                buf.append((ts, c))
            if buf:
                logging.info(f"{s} extremes buffer seeded with {len(buf)} points")
        except Exception as e:
            logging.warning(f"{s} extremes buffer seed failed: {e}")

# ========================= SIGNAL DETECTION =====================
def _rolling_low_high(st: dict, window_sec: int) -> Tuple[Optional[float], Optional[float]]:
    now = time.time()
    low = high = None
    buf = st["ext_price_buffer"]
    if not buf:
        return None, None
    for ts, p in reversed(buf):
        if now - ts > window_sec:
            break
        if low is None or p < low: low = p
        if high is None or p > high: high = p
    return low, high

def bounce_signal(st: dict) -> Optional[str]:
    """
    Stateful bounce with Schmitt-trigger hysteresis.
    LONG:
      entry_line = low * (1 + BOUNCE_THRESHOLD_PCT)
      exit_line  = entry_line * (1 - BOUNCE_EXIT_HYST_PCT)  # stay LONG while price >= exit_line
    SHORT:
      entry_line = high * (1 - BOUNCE_THRESHOLD_PCT)
      exit_line  = entry_line * (1 + BOUNCE_EXIT_HYST_PCT)  # stay SHORT while price <= exit_line
    """
    if not BOUNCE_ENABLED:
        return None
    price = st["last_price"]
    if price is None:
        return None
    now = time.time()

    # If a bounce is active, enforce min-hold and Schmitt exit
    if st.get("bounce_active"):
        side = st["bounce_active"]
        base = st["bounce_baseline"]  # low for LONG, high for SHORT

        # Minimum hold time after entry
        if now < st.get("bounce_hold_until", 0.0):
            return side

        if side == "LONG":
            entry_line = base * (1.0 + BOUNCE_THRESHOLD_PCT)
            exit_line  = entry_line * (1.0 - BOUNCE_EXIT_HYST_PCT)   # below entry_line
            return "LONG" if price >= exit_line else None
        else:  # SHORT
            entry_line = base * (1.0 - BOUNCE_THRESHOLD_PCT)
            exit_line  = entry_line * (1.0 + BOUNCE_EXIT_HYST_PCT)   # above entry_line
            return "SHORT" if price <= exit_line else None

    # Cooldown before arming a new bounce
    if now - st.get("last_bounce_ts", 0.0) < BOUNCE_COOLDOWN_SEC:
        st["bounce_candidate_side"] = None
        st["bounce_candidate_base"] = None
        st["bounce_candidate_ts"] = None
        return None

    # Scan all configured windows; trigger immediately if BOUNCE_CONFIRM_SEC == 0
    for w in BOUNCE_WINDOWS_SEC:
        low, high = _rolling_low_high(st, w)

        # LONG candidate: price >= low * (1 + TH)
        if low and price >= low * (1.0 + BOUNCE_THRESHOLD_PCT):
            if (st.get("bounce_candidate_side") != "LONG") or (st.get("bounce_candidate_base") != low):
                st["bounce_candidate_side"] = "LONG"
                st["bounce_candidate_base"] = low
                st["bounce_candidate_ts"] = now
            if (now - st["bounce_candidate_ts"] >= BOUNCE_CONFIRM_SEC) or (BOUNCE_CONFIRM_SEC <= 0.0):
                st["bounce_active"] = "LONG"
                st["bounce_baseline"] = low
                st["bounce_hold_until"] = now + BOUNCE_MIN_HOLD_SEC
                st["last_bounce_ts"] = now
                st["bounce_signal_ts"] = now
                st["bounce_candidate_side"] = None
                return "LONG"
            return None  # wait for confirm if > 0

        # SHORT candidate: price <= high * (1 - TH)
        if high and price <= high * (1.0 - BOUNCE_THRESHOLD_PCT):
            if (st.get("bounce_candidate_side") != "SHORT") or (st.get("bounce_candidate_base") != high):
                st["bounce_candidate_side"] = "SHORT"
                st["bounce_candidate_base"] = high
                st["bounce_candidate_ts"] = now
            if (now - st["bounce_candidate_ts"] >= BOUNCE_CONFIRM_SEC) or (BOUNCE_CONFIRM_SEC <= 0.0):
                st["bounce_active"] = "SHORT"
                st["bounce_baseline"] = high
                st["bounce_hold_until"] = now + BOUNCE_MIN_HOLD_SEC
                st["last_bounce_ts"] = now
                st["bounce_signal_ts"] = now
                st["bounce_candidate_side"] = None
                return "SHORT"
            return None  # wait for confirm if > 0

    # No candidate this tick
    st["bounce_candidate_side"] = None
    st["bounce_candidate_base"] = None
    st["bounce_candidate_ts"] = None
    return None

def wma_signal(st: dict) -> Optional[str]:
    """WMA signal with proper initialization check"""
    price = st["last_price"]
    wma = st["wma"].value
    if price is None or wma is None or wma <= 0 or not st["wma"].initialized:
        return None
    
    # ≈WMA band
    if abs(price - wma) / wma <= WMA_EQUAL_TOL:
        return "FLAT"
    
    return "LONG" if price > wma else "SHORT"

def get_target_signal(st: dict) -> Tuple[Optional[str], str]:
    """Returns (signal, source) tuple"""
    # Priority 1: Bounce signal
    b = bounce_signal(st)
    if b:
        return b, "BOUNCE"

    # If a previously active bounce just ended, clear flags
    if st["bounce_active"] and b is None:
        st["bounce_active"] = None
        st["bounce_baseline"] = None

    # Priority 2: WMA signal
    w = wma_signal(st)
    if w:
        return w, "WMA"
    
    return None, "NONE"

# ========================= POSITION MANAGEMENT ==================
async def set_target_position(cli: AsyncClient, symbol: str, target_signal: str):
    """Set position to match target signal with improved state tracking"""
    st = state[symbol]
    tol = 0.1  # 0.1 ADA tolerance
    
    async with st["order_lock"]:
        # Check if we're already processing an order
        if st["position_state"] != "IDLE":
            return False
            
        st["position_state"] = "PROCESSING"
        
        try:
            # Get current positions
            long_size = await get_position_size_fresh(cli, symbol, "LONG")
            short_size = await get_position_size_fresh(cli, symbol, "SHORT")
            
            target_long = POSITION_SIZE_ADA if target_signal == "LONG" else 0.0
            target_short = POSITION_SIZE_ADA if target_signal == "SHORT" else 0.0
            
            success = True
            
            # Close unwanted positions first
            if short_size > tol and target_signal != "SHORT":
                success &= await safe_order_execution(cli, exit_short(symbol, short_size), symbol, "CLOSE SHORT")
            if long_size > tol and target_signal != "LONG":  
                success &= await safe_order_execution(cli, exit_long(symbol, long_size), symbol, "CLOSE LONG")
            
            # Open target position
            if target_signal == "LONG" and long_size < target_long - tol:
                delta = target_long - long_size
                ok = await safe_order_execution(cli, open_long(symbol, delta), symbol, "OPEN LONG")
                if ok:
                    st["last_position_entry_ts"] = time.time()
                    st["trail_peak"] = st.get("last_price")
                    st["trail_trough"] = None
                success &= ok
            
            elif target_signal == "SHORT" and short_size < target_short - tol:
                delta = target_short - short_size  
                ok = await safe_order_execution(cli, open_short(symbol, delta), symbol, "OPEN SHORT")
                if ok:
                    st["last_position_entry_ts"] = time.time()
                    st["trail_trough"] = st.get("last_price")
                    st["trail_peak"] = None
                success &= ok

            elif target_signal == "FLAT":
                st["trail_peak"] = None
                st["trail_trough"] = None

            # Update failure tracking
            if success:
                st["consecutive_failures"] = 0
            else:
                st["consecutive_failures"] += 1
                st["circuit_breaker_until"] = time.time() + (CIRCUIT_BREAKER_BASE_SEC * st["consecutive_failures"])
                logging.warning(f"{symbol} Trade failed, circuit breaker until {st['circuit_breaker_until']}")
                
            return success
            
        finally:
            st["position_state"] = "IDLE"

# ========================= MAIN TRADING LOOP ====================
def _callback_retrace_hit(st: dict, price: float, side: str) -> bool:
    """Return True if the configured retrace has occurred (applies to exits & flips)"""
    if CALLBACK_RATE_PCT <= 0.0:
        return True
    if side == "LONG":
        peak = st.get("trail_peak") or price
        return ((peak - price) / peak) >= CALLBACK_RATE_PCT
    elif side == "SHORT":
        trough = st.get("trail_trough") or price
        return ((price - trough) / trough) >= CALLBACK_RATE_PCT
    return True

async def simplified_trading_loop(cli: AsyncClient):
    POLL_SEC = 0.25
    
    while True:
        await asyncio.sleep(POLL_SEC)
        
        for sym in SYMBOLS:
            st = state[sym]
            price = st["last_price"]
            wma = st["wma"].value
            price_ts = st.get("price_timestamp", 0)
            now = time.time()
            
            # Check for stale price data
            if price is None or price_ts is None or (now - price_ts) > PRICE_STALENESS_SEC:
                continue
                
            if wma is None or wma <= 0 or not st["wma"].initialized:
                continue
            
            # Circuit breaker check
            if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                if now < st["circuit_breaker_until"]:
                    continue
                else:
                    st["consecutive_failures"] = 0  # Reset after cooldown
                    logging.info(f"{sym} Circuit breaker reset")

            # Bot activation check (near WMA or manual)
            if not st["bot_active"] and not st["manual_override"]:
                wma_diff_pct = abs(price - wma) / wma
                if wma_diff_pct <= WMA_ACTIVATION_THRESHOLD:
                    st["bot_active"] = True
                    logging.info(f"{sym} BOT ACTIVATED - price {price:.6f} near WMA {wma:.6f}")
                else:
                    continue

            # Update callback extremes while holding a side
            if st["current_signal"] == "LONG":
                st["trail_peak"] = price if st["trail_peak"] is None else max(st["trail_peak"], price)
                st["trail_trough"] = None
            elif st["current_signal"] == "SHORT":
                st["trail_trough"] = price if st["trail_trough"] is None else min(st["trail_trough"], price)
                st["trail_peak"] = None
            else:
                st["trail_peak"] = None
                st["trail_trough"] = None
            
            # Get unified target signal
            target, source = get_target_signal(st)
            if target is None:
                continue

            current = st["current_signal"]
            
            # Callback gate - apply to ANY change away from the current side (exit or flip)
            if target != current and current in ("LONG", "SHORT") and st.get("last_position_entry_ts", 0) > 0:
                if not _callback_retrace_hit(st, price, current):
                    continue

            # Signal change detection
            if target != current:
                st["current_signal"] = target
                st["signal_since"] = now
                st["active_signal_source"] = source
                logging.info(f"{sym} Signal change: {current} -> {target} (source={source})")
            
            if st["signal_since"] is None:
                continue

            # Confirmation: immediate for bounce, timed for WMA
            required_confirm = 0.0 if source == "BOUNCE" else SIGNAL_CONFIRM_SEC
            if (now - st["signal_since"]) < required_confirm:
                continue
                
            # Cooldown check
            if now - st["last_trade_ts"] < TRADE_COOLDOWN_SEC:
                continue
            
            # Execute position change
            try:
                success = await set_target_position(cli, sym, target)
                if success:
                    st["last_trade_ts"] = now
                    if target in ("LONG", "SHORT"):
                        st["last_position_entry_ts"] = now
                        if target == "LONG":
                            st["trail_peak"] = price; st["trail_trough"] = None
                        else:
                            st["trail_trough"] = price; st["trail_peak"] = None
                    else:
                        st["trail_peak"] = None; st["trail_trough"] = None
            except Exception as e:
                logging.error(f"{sym} Position update failed: {e}")
                st["consecutive_failures"] += 1

# ========================= MONITORING ===========================
async def websocket_health_monitor():
    while True:
        await asyncio.sleep(30.0)
        now = time.time()
        for sym in SYMBOLS:
            st = state[sym]
            last_heartbeat = st["ws_last_heartbeat"]
            if last_heartbeat > 0 and (now - last_heartbeat) > 60.0:
                logging.error(f"{sym} WebSocket stale! {now - last_heartbeat:.1f}s")
                st["bot_active"] = False

async def pnl_summary_loop(cli: AsyncClient):
    while True:
        total_upnl = 0.0
        for sym in SYMBOLS:
            snap = await get_positions_snapshot(cli, sym)
            st = state[sym]
            price = st["last_price"] or 0.0
            wma = st["wma"].value
            wma_str = f"{wma:.6f}" if wma else "n/a"
            
            L = snap["LONG"]; S = snap["SHORT"]
            upnl_sym = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0)
            total_upnl += upnl_sym
            
            ws_health = "OK" if (time.time() - st["ws_last_heartbeat"]) < 60.0 else "STALE"
            wma_init = "✓" if st["wma"].initialized else "✗"
            circuit_status = f"CB:{st['consecutive_failures']}" if st["consecutive_failures"] > 0 else "OK"
            
            logging.info(
                f"[SUMMARY] {sym} active={st['bot_active']} | "
                f"price={price:.6f} WMA={wma_str}({wma_init}) | WS={ws_health} | "
                f"source={st.get('active_signal_source', 'NONE')} signal={st.get('current_signal')} | "
                f"risk={circuit_status} | "
                f"LONG={L['size']:.1f}({L['uPnL']:.2f}) SHORT={S['size']:.1f}({S['uPnL']:.2f})"
            )
            
        logging.info(f"[SUMMARY] TOTAL uPnL: {total_upnl:.2f} USDT")
        await asyncio.sleep(PNL_SUMMARY_SEC)

# ========================= MAIN ================================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API credentials")

    cli = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        global DUAL_SIDE
        DUAL_SIDE = await get_dual_side(cli)
        if not DUAL_SIDE:
            logging.error("Hedge Mode (dual-side) required!")
            raise RuntimeError("Enable Hedge Mode in Binance Futures settings")
        
        logging.info("Hedge Mode confirmed")

        for s in SYMBOLS:
            try:
                await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except Exception as e:
                logging.warning(f"{s} leverage setting failed: {e}")

        await seed_symbol_filters(cli)
        await seed_tick_wma(cli)
        await seed_extremes_buffer(cli)

        feed_task = asyncio.create_task(price_feed_loop(cli))   # websockets stay
        health_task = asyncio.create_task(websocket_health_monitor())
        await asyncio.sleep(5.0)  # Let WMA initialize properly

        trading_task = asyncio.create_task(simplified_trading_loop(cli))
        summary_task = asyncio.create_task(pnl_summary_loop(cli))

        await asyncio.gather(feed_task, trading_task, summary_task, health_task)
        
    finally:
        try:
            await cli.close_connection()
        except Exception:
            pass

# ========================= ENTRYPOINT ===========================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%b %d %H:%M:%S"
    )

    # Clean logging filter
    class CleanLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            # Always show summaries and important events
            if "[SUMMARY]" in msg:
                return True
            if record.levelno >= logging.WARNING:
                return True
            # Show trade executions and signal changes
            if any(x in msg for x in ["executed - OrderID", "Signal change", "BOT ACTIVATED", "Circuit breaker"]):
                return True
            return False

    logging.getLogger().addFilter(CleanLogFilter())

    logging.info("=== IMPROVED TRADING BOT STARTUP (Multi-window bounce + callback) ===")
    logging.info(f"Position Size: {POSITION_SIZE_ADA} ADA | Leverage: {LEVERAGE}x")
    logging.info(f"WMA Band: ±{WMA_EQUAL_TOL*100:.2f}% | Activation: {WMA_ACTIVATION_THRESHOLD*100:.2f}%")
    logging.info(f"Confirmation Time (WMA): {SIGNAL_CONFIRM_SEC}s | Trade Cooldown: {TRADE_COOLDOWN_SEC}s")
    logging.info(f"Bounce Windows: {BOUNCE_WINDOWS_SEC}s | Threshold: {BOUNCE_THRESHOLD_PCT*100:.1f}% | "
                 f"Confirm={BOUNCE_CONFIRM_SEC}s | Hold={BOUNCE_MIN_HOLD_SEC}s | Hyst={BOUNCE_EXIT_HYST_PCT*100:.2f}%")
    logging.info(f"Price Glitch Filter: {PRICE_GLITCH_PCT*100:.1f}% | Staleness: {PRICE_STALENESS_SEC}s")
    logging.info(f"Callback Gate: {CALLBACK_RATE_PCT*100:.2f}% (applies to exits & flips)")
    logging.info(f"Risk: Max {MAX_CONSECUTIVE_FAILURES} failures, {CIRCUIT_BREAKER_BASE_SEC}s base cooldown")
    logging.info("Strategy: Bounce (1m,5m,15m,1h,4h,24h) → WMA (5hr) | Exits/flips gated by callback retrace")
    logging.info("================================================================================")

    asyncio.run(main())
