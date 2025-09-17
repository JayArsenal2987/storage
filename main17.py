#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Dict, Any
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# ---- Core parameters ----
POSITION_SIZE_ADA   = float(os.getenv("POSITION_SIZE_ADA", "10.0"))
SIGNAL_CONFIRM_SEC  = float(os.getenv("SIGNAL_CONFIRM_SEC", "3.0"))     # confirm new signal
TRADE_COOLDOWN_SEC  = float(os.getenv("TRADE_COOLDOWN_SEC", "10.0"))    # gap between trades
PRICE_GLITCH_PCT    = float(os.getenv("PRICE_GLITCH_PCT", "0.03"))      # drop bad ticks

# Exit/flip protection (anti-churn)
CALLBACK_RATE_PCT   = float(os.getenv("CALLBACK_RATE_PCT", "0.005"))    # 0.5% retrace gate
MIN_HOLD_SEC        = float(os.getenv("MIN_HOLD_SEC", "30.0"))          # hold before exit/flip

# >>> ADDED: Profit-priority knobs
PROFIT_MIN_PCT      = float(os.getenv("PROFIT_MIN_PCT", "0.5")) / 100.0   # 0.5% profit min for profit-protect exits
LOSS_CUT_PCT        = float(os.getenv("LOSS_CUT_PCT", "5.0")) / 100.0     # 5% emergency loss override

# ---- PRICE SOURCE ----
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "mark").lower()
def _ws_host() -> str:
    return "fstream.binance.com" if PRICE_SOURCE in ("futures", "mark") else "stream.binance.com:9443"
def _stream_name(sym: str) -> str:
    return f"{sym.lower()}@markPrice@1s" if PRICE_SOURCE == "mark" else f"{sym.lower()}@trade"

# ---- SYMBOLS ----
# NOTE: symbols with exact sizes.
SYMBOLS = {
    "BTCUSDT": 0.001,
    "ETHUSDT": 0.01,
    "BNBUSDT": 0.03,
    "XRPUSDT": 10.0,
    "SOLUSDT": 0.1,
    "ADAUSDT": 10.0,
}

# ---- 5-hour WMA ----
TICK_WMA_PERIOD = 5 * 60 * 60
WMA_EQUAL_TOL   = float(os.getenv("WMA_EQUAL_TOL", "0.001"))            # ±0.1% ~ FLAT band

# ---- Bounce (24h latest extreme) ----
BOUNCE_ENABLED       = True
BOUNCE_WINDOWS_SEC   = [24*3600]
# CHANGED (your request): default 2.0% -> 1.5%
BOUNCE_THRESHOLD_PCT = float(os.getenv("BOUNCE_THRESHOLD_PCT", "1.5")) / 100.0  # 1.5%
BOUNCE_COOLDOWN_SEC  = float(os.getenv("BOUNCE_COOLDOWN_SEC", "30.0"))  # between separate bounces

# Risk / misc
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_BASE_SEC = 300
PRICE_STALENESS_SEC = 5.0
PNL_SUMMARY_SEC       = 1800.0
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# Order validation parameters
ORDER_FILL_TIMEOUT_SEC = 10.0
MAX_FILL_CHECK_ATTEMPTS = 5

# ========================= UTILITY CLASSES =========================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200); self.end_headers()
    def log_message(self, *_): pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

class TickWMA:
    def __init__(self, period_seconds: int):
        self.period_seconds = period_seconds
        self.alpha = 1.0 / period_seconds
        self.value: Optional[float] = None
        self.last_update: Optional[float] = None
        self.initialized = False
        self.tick_buffer = deque(maxlen=1000)
    def update(self, price: float):
        now = time.time()
        self.tick_buffer.append((now, price))
        if self.value is None:
            self.value = price; self.last_update = now; return
        dt = now - self.last_update
        a = min(self.alpha * dt, 1.0)
        self.value = price * a + self.value * (1.0 - a)
        self.last_update = now; self.initialized = True

# ========================= PRICE & QTY FILTERS =================
PRICE_TICK: Dict[str, Decimal] = {}
PRICE_DECIMALS: Dict[str, int] = {}
QUANTITY_STEP: Dict[str, Decimal] = {}
QUANTITY_DECIMALS: Dict[str, int] = {}
def _dec(x) -> Decimal: return x if isinstance(x, Decimal) else Decimal(str(x))
def _decimals_from_tick(tick: str) -> int:
    if "." not in tick: return 0
    return len(tick.split(".")[1].rstrip("0"))
def quantize_price(symbol: str, price: float) -> str:
    tick = PRICE_TICK.get(symbol)
    if not tick: return f"{price:.8f}"
    q = (_dec(price) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return f"{q:.{PRICE_DECIMALS.get(symbol, 8)}f}"
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
    min_gap = 0.2
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
position_cache = { s: {"LONG": {"size": 0.0, "ts": 0.0}, "SHORT": {"size": 0.0, "ts": 0.0}} for s in SYMBOLS }
def invalidate_position_cache(symbol: str):
    position_cache[symbol]["LONG"]["ts"] = 0.0; position_cache[symbol]["SHORT"]["ts"] = 0.0
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
            if side in sizes: sizes[side] = abs(float(pos.get("positionAmt", 0.0)))
        now = time.time()
        for side in ("LONG","SHORT"):
            position_cache[symbol][side]["size"] = sizes[side]
            position_cache[symbol][side]["ts"] = now
    except Exception as e:
        logging.warning(f"{symbol} refresh_position_cache failed: {e}")
async def get_position_size_fresh(cli: AsyncClient, symbol: str, side: str) -> float:
    await refresh_position_cache(cli, symbol); return position_cache[symbol][side]["size"]
async def get_positions_snapshot(cli: AsyncClient, symbol: str) -> Dict[str, Dict[str, float]]:
    snap = {"LONG": {"size": 0.0, "entry": 0.0, "uPnL": 0.0}, "SHORT": {"size": 0.0, "entry": 0.0, "uPnL": 0.0}}
    try:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
        for pos in positions:
            side = pos.get("positionSide")
            if side in ("LONG","SHORT"):
                snap[side]["size"]  = abs(float(pos.get("positionAmt", 0.0)))
                snap[side]["entry"] = float(pos.get("entryPrice", 0.0) or 0.0)
                snap[side]["uPnL"]  = float(pos.get("unRealizedProfit", pos.get("unrealizedProfit", 0.0)) or 0.0)
    except Exception as e:
        logging.warning(f"{symbol} positions snapshot failed: {e}")
    return snap

# ========================= ORDER BUILDERS ======================
DUAL_SIDE = False
def _maybe_pos_side(params: dict, side: str) -> dict:
    if DUAL_SIDE: params["positionSide"] = side
    else: params.pop("positionSide", None)
    return params
def open_long(sym: str, qty: float):  return _maybe_pos_side(dict(symbol=sym, side="BUY",  type="MARKET", quantity=quantize_qty(sym, qty)), "LONG")
def open_short(sym: str, qty: float): return _maybe_pos_side(dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty)), "SHORT")
def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE: p["reduceOnly"] = True
    return _maybe_pos_side(p, "LONG")
def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE: p["reduceOnly"] = True
    return _maybe_pos_side(p, "SHORT")

# ========================= IMPROVED ORDER VALIDATION ===================
async def wait_for_order_fill(cli: AsyncClient, symbol: str, order_id: str) -> Dict[str, Any]:
    """Wait for order to fill and return order status"""
    for attempt in range(MAX_FILL_CHECK_ATTEMPTS):
        try:
            order_status = await call_binance(cli, cli.futures_get_order, symbol=symbol, orderId=order_id)
        except TypeError:
            # backward compat: call_binance expects (fn, *args, **kwargs)
            order_status = await call_binance(cli.futures_get_order, symbol=symbol, orderId=order_id)
        status = order_status.get("status")
        if status == "FILLED":
            return {
                "success": True,
                "filled_qty": float(order_status.get("executedQty", 0)),
                "avg_price": float(order_status.get("avgPrice", 0)),
                "order_status": order_status
            }
        elif status in ["CANCELED", "REJECTED", "EXPIRED"]:
            return {
                "success": False,
                "error": f"Order {status.lower()}",
                "order_status": order_status
            }
        if attempt < MAX_FILL_CHECK_ATTEMPTS - 1:
            await asyncio.sleep(ORDER_FILL_TIMEOUT_SEC / MAX_FILL_CHECK_ATTEMPTS)
    return {"success": False, "error": "Fill verification timeout"}

async def verify_position_after_trade(cli: AsyncClient, symbol: str, expected_long: float, expected_short: float) -> bool:
    """Verify actual positions match expected after trade"""
    try:
        await refresh_position_cache(cli, symbol)
        actual_long = position_cache[symbol]["LONG"]["size"]
        actual_short = position_cache[symbol]["SHORT"]["size"]
        tolerance = max(0.1, max(expected_long, expected_short) * 0.001)
        long_match = abs(actual_long - expected_long) <= tolerance
        short_match = abs(actual_short - expected_short) <= tolerance
        if not (long_match and short_match):
            logging.warning(f"{symbol} Position mismatch - Expected L:{expected_long:.3f} S:{expected_short:.3f}, "
                          f"Actual L:{actual_long:.3f} S:{actual_short:.3f}, Tolerance:{tolerance:.3f}")
            return False
        return True
    except Exception as e:
        logging.error(f"{symbol} Position verification failed: {e}")
        return False

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
            logging.info(f"{symbol} {action} submitted - OrderID: {oid}")
            # Wait for order fill and validate
            fill_result = await wait_for_order_fill(cli, symbol, str(oid))
            if fill_result["success"]:
                filled_qty = fill_result["filled_qty"]
                avg_price = fill_result["avg_price"]
                logging.info(f"{symbol} {action} FILLED - Qty: {filled_qty:.3f} @ {avg_price:.6f}")
                invalidate_position_cache(symbol)
                return True
            else:
                logging.error(f"{symbol} {action} FAILED - {fill_result.get('error', 'Unknown error')}")
                return False
        except Exception as e:
            if attempt < max_retries:
                logging.warning(f"{symbol} {action} attempt {attempt + 1} failed: {e}, retrying...")
                await asyncio.sleep(1.0 + random.uniform(0.0, 0.5))
                continue
            logging.error(f"{symbol} {action} failed after {max_retries + 1} attempts: {e}")
            return False

# ========================= STATE ===============================
state: Dict[str, Dict[str, Any]] = {
    s: {
        "last_price": None, "last_good_price": None, "price_timestamp": None,
        "price_buffer": deque(maxlen=5000), "ext_price_buffer": deque(maxlen=100000),
        "ws_last_heartbeat": 0.0,
        "wma": TickWMA(TICK_WMA_PERIOD), "bot_active": False, "manual_override": False,
        "current_signal": None, "signal_since": None, "last_trade_ts": 0.0, "active_signal_source": None,
        # trailing extremes for callback exit/flip
        "trail_peak": None, "trail_trough": None,
        # >>> ADDED: entry refs for profit checks
        "entry_ref_long": None,
        "entry_ref_short": None,
        # bounce state
        "last_bounce_ts": 0.0,
        # risk
        "consecutive_failures": 0, "circuit_breaker_until": 0.0, "position_state": "IDLE",
        # misc
        "order_lock": asyncio.Lock(), "last_position_entry_ts": 0.0,
    } for s in SYMBOLS
}

# ========================= SYMBOL TARGET SIZE ==================
def symbol_target_qty(sym: str) -> float:
    return float(SYMBOLS.get(sym, POSITION_SIZE_ADA))

# ========================= SYMBOL FILTERS ======================
async def seed_symbol_filters(cli: AsyncClient):
    try:
        info = await call_binance(cli.futures_exchange_info)
        symbols_info = {s["symbol"]: s for s in info.get("symbols", [])}
        for sym in SYMBOLS:
            si = symbols_info.get(sym)
            if not si: continue
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
    if p_str is None: return None
    try: return float(p_str)
    except Exception: return None

async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"
    reconnect_delay = 1.0; max_reconnect_delay = 60.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5, max_queue=1000) as ws:
                reconnect_delay = 1.0
                logging.info("WebSocket price feed connected")
                async for raw in ws:
                    now = time.time()
                    for sym in SYMBOLS: state[sym]["ws_last_heartbeat"] = now
                    m = json.loads(raw); d = m.get("data", {}); sym = d.get("s")
                    if not sym or sym not in state: continue
                    p = _parse_price_from_event(d)
                    if p is None or p <= 0.0: continue
                    st = state[sym]; prev = st["last_price"]
                    if prev is not None and abs(p / prev - 1.0) > PRICE_GLITCH_PCT:
                        logging.warning(f"{sym} Price glitch filtered: {prev:.6f} -> {p:.6f}"); continue
                    st["last_price"] = p; st["last_good_price"] = p; st["price_timestamp"] = now
                    st["price_buffer"].append((now, p)); st["ext_price_buffer"].append((now, p))
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
            for c in closes: wma.update(c)
            if wma.value is not None: logging.info(f"{s} WMA initialized: {wma.value:.6f}")
        except Exception as e:
            logging.warning(f"{s} WMA seed failed: {e}")

async def seed_extremes_buffer(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            kl = await call_binance(cli.futures_klines, symbol=s, interval="1m", limit=1440)
            buf = state[s]["ext_price_buffer"]
            for k in kl:
                ts = float(k[0]) / 1000.0; c = float(k[4])
                buf.append((ts, c))
            if buf: logging.info(f"{s} extremes buffer seeded with {len(buf)} points")
        except Exception as e:
            logging.warning(f"{s} extremes buffer seed failed: {e}")

# ========================= IMPROVED SIGNALS ============================
def _rolling_extremes_with_ts(st: dict, window_sec: int) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    now = time.time(); buf = st["ext_price_buffer"]
    if not buf: return None, None, None, None
    low = high = None; low_ts = high_ts = None
    for ts, p in reversed(buf):
        if now - ts > window_sec: break
        if low is None or p < low or (p == low and (low_ts is None or ts > low_ts)): low, low_ts = p, ts
        if high is None or p > high or (p == high and (high_ts is None or ts > high_ts)): high, high_ts = p, ts
    return low, low_ts, high, high_ts

def bounce_signal(st: dict) -> Optional[str]:
    if not BOUNCE_ENABLED:
        return None
    price = st.get("last_price")
    if price is None:
        return None
    now = time.time()
    if now - st.get("last_bounce_ts", 0.0) < BOUNCE_COOLDOWN_SEC:
        return None
    low_24, low_ts_24, high_24, high_ts_24 = _rolling_extremes_with_ts(st, 24 * 3600)
    if low_ts_24 is None and high_ts_24 is None:
        return None
    long_trigger = float(low_24) * (1.0 + BOUNCE_THRESHOLD_PCT) if low_ts_24 is not None else None
    short_trigger = float(high_24) * (1.0 - BOUNCE_THRESHOLD_PCT) if high_ts_24 is not None else None
    long_ok = (long_trigger is not None) and (price >= long_trigger)
    short_ok = (short_trigger is not None) and (price <= short_trigger)
    if long_ok and short_ok:
        distance_to_long_trigger = abs(price - long_trigger)
        distance_to_short_trigger = abs(price - short_trigger)
        pick = "LONG" if distance_to_long_trigger < distance_to_short_trigger else "SHORT"
        st["last_bounce_ts"] = now
        logging.info(f"Bounce conflict resolved: price={price:.6f}, long_trigger={long_trigger:.6f}(dist:{distance_to_long_trigger:.6f}), "
                    f"short_trigger={short_trigger:.6f}(dist:{distance_to_short_trigger:.6f}) -> {pick}")
        return pick
    elif long_ok:
        st["last_bounce_ts"] = now
        return "LONG"
    elif short_ok:
        st["last_bounce_ts"] = now
        return "SHORT"
    return None

def wma_signal(st: dict) -> Optional[str]:
    price = st.get("last_price"); wma = st["wma"].value if st["wma"] else None
    if price is None or wma is None or not st["wma"].initialized: return None
    if abs(price - wma) / wma <= WMA_EQUAL_TOL: return "FLAT"
    return "LONG" if price > wma else "SHORT"

def get_target_signal(st: dict) -> Tuple[Optional[str], str]:
    b = bounce_signal(st)
    if b in ("LONG","SHORT"): return b, "BOUNCE"
    w = wma_signal(st)
    if w is not None: return w, "WMA"
    return None, "NONE"

# ========================= IMPROVED POSITION MGMT =======================
async def set_target_position(cli: AsyncClient, symbol: str, target_signal: str):
    st = state[symbol]
    async with st["order_lock"]:
        if st["position_state"] != "IDLE": return False
        st["position_state"] = "PROCESSING"
        try:
            long_size = await get_position_size_fresh(cli, symbol, "LONG")
            short_size = await get_position_size_fresh(cli, symbol, "SHORT")
            target_qty = symbol_target_qty(symbol)
            target_long = target_qty if target_signal == "LONG" else 0.0
            target_short = target_qty if target_signal == "SHORT" else 0.0
            tolerance = max(0.1, max(target_long, target_short, long_size, short_size) * 0.001)
            ok_all = True

            # Close unwanted positions first
            if short_size > tolerance and target_signal != "SHORT":
                ok_all &= await safe_order_execution(cli, exit_short(symbol, short_size), symbol, "CLOSE SHORT")
            if long_size > tolerance and target_signal != "LONG":
                ok_all &= await safe_order_execution(cli, exit_long(symbol, long_size), symbol, "CLOSE LONG")

            # TRIM same-side oversize positions to target (prevents mismatch spam)
            if target_signal == "LONG" and long_size > target_long + tolerance:
                delta = long_size - target_long
                ok_all &= await safe_order_execution(cli, exit_long(symbol, delta), symbol, "TRIM LONG")

            if target_signal == "SHORT" and short_size > target_short + tolerance:
                delta = short_size - target_short
                ok_all &= await safe_order_execution(cli, exit_short(symbol, delta), symbol, "TRIM SHORT")

            # Open new positions
            if target_signal == "LONG" and long_size < target_long - tolerance:
                delta = target_long - long_size
                ok = await safe_order_execution(cli, open_long(symbol, delta), symbol, "OPEN LONG")
                if ok:
                    st["last_position_entry_ts"] = time.time()
                    st["trail_peak"] = st.get("last_price")
                    st["trail_trough"] = None
                    # >>> ADDED: set entry reference on fill
                    st["entry_ref_long"] = st.get("last_price")
                ok_all &= ok
            elif target_signal == "SHORT" and short_size < target_short - tolerance:
                delta = target_short - short_size
                ok = await safe_order_execution(cli, open_short(symbol, delta), symbol, "OPEN SHORT")
                if ok:
                    st["last_position_entry_ts"] = time.time()
                    st["trail_trough"] = st.get("last_price")
                    st["trail_peak"] = None
                    # >>> ADDED: set entry reference on fill
                    st["entry_ref_short"] = st.get("last_price")
                ok_all &= ok
            elif target_signal == "FLAT":
                st["trail_peak"] = None
                st["trail_trough"] = None

            # Verify final positions match expectations
            if ok_all:
                position_verified = await verify_position_after_trade(cli, symbol, target_long, target_short)
                if not position_verified:
                    logging.warning(f"{symbol} Position verification failed after successful orders")
                st["consecutive_failures"] = 0
            else:
                st["consecutive_failures"] += 1
                st["circuit_breaker_until"] = time.time() + (CIRCUIT_BREAKER_BASE_SEC * st["consecutive_failures"])
            return ok_all
        finally:
            st["position_state"] = "IDLE"

# ========================= MAIN LOOP ===========================
def _callback_retrace_hit(st: dict, price: float, side: str) -> bool:
    if CALLBACK_RATE_PCT <= 0.0: return True
    if side == "LONG":
        peak = st.get("trail_peak") or price
        return ((peak - price) / peak) >= CALLBACK_RATE_PCT
    if side == "SHORT":
        trough = st.get("trail_trough") or price
        return ((price - trough) / trough) >= CALLBACK_RATE_PCT
    return True

async def simplified_trading_loop(cli: AsyncClient):
    POLL_SEC = 0.25
    while True:
        await asyncio.sleep(POLL_SEC)
        for sym in SYMBOLS:
            st = state[sym]
            price = st["last_price"]; wma = st["wma"].value; ts = st.get("price_timestamp", 0); now = time.time()
            if price is None or ts is None or (now - ts) > PRICE_STALENESS_SEC: continue
            if wma is None or wma <= 0 or not st["wma"].initialized: continue

            # Circuit breaker
            if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                if now < st["circuit_breaker_until"]: continue
                st["consecutive_failures"] = 0; logging.info(f"{sym} Circuit breaker reset")

            # Activation
            if not st["bot_active"] and not st["manual_override"]:
                b = bounce_signal(st)
                w = wma_signal(st)
                if (b in ("LONG","SHORT")) or (w in ("LONG","SHORT")):
                    st["bot_active"] = True
                    logging.info(f"{sym} BOT ACTIVATED")
                else:
                    continue

            # Update trailing extremes
            if st["current_signal"] == "LONG":
                st["trail_peak"] = price if st["trail_peak"] is None else max(st["trail_peak"], price); st["trail_trough"] = None
            elif st["current_signal"] == "SHORT":
                st["trail_trough"] = price if st["trail_trough"] is None else min(st["trail_trough"], price); st["trail_peak"] = None
            else:
                st["trail_peak"] = None; st["trail_trough"] = None

            # Decide target
            target, source = get_target_signal(st)
            if target is None: continue
            current = st["current_signal"]

            # Anti-churn gates
            if target != current and current in ("LONG","SHORT") and st.get("last_position_entry_ts",0) > 0:
                if now - st["last_position_entry_ts"] < MIN_HOLD_SEC:
                    continue
                if not _callback_retrace_hit(st, price, current):
                    continue

            # Apply signal change / confirmation / cooldown
            if target != current:
                st["current_signal"] = target
                st["signal_since"] = now
                st["active_signal_source"] = source
                logging.info(f"{sym} Signal change: {current} -> {target} (source={source})")

            if st["signal_since"] is None: continue
            if (now - st["signal_since"]) < SIGNAL_CONFIRM_SEC: continue
            if now - st["last_trade_ts"] < TRADE_COOLDOWN_SEC: continue

            # >>> ADDED: Profit-priority + trailing profit protection gate
            # Only enforce "profit-only" for BOO NCE exits; always allow big-loss override
            if target != current and current in ("LONG","SHORT"):
                # compute pnl% vs entry_ref
                entry_ref = st["entry_ref_long"] if current == "LONG" else st["entry_ref_short"]
                if entry_ref:
                    pnl = (price / entry_ref - 1.0) if current == "LONG" else (entry_ref / price - 1.0)
                    # Loss override
                    if pnl <= -LOSS_CUT_PCT:
                        pass  # allow exit/flip immediately
                    else:
                        # Profit-only for bounce exits
                        if source == "BOUNCE" and pnl < PROFIT_MIN_PCT:
                            continue  # skip this cycle until at least small profit
                # If no entry_ref recorded, do not block (fallback to normal behavior)

            # --- Staggered Execution ---
            symbol_delay = (hash(sym) % len(SYMBOLS)) * 0.5  # 0-2.5s stagger
            await asyncio.sleep(symbol_delay)

            try:
                success = await set_target_position(cli, sym, target)
                if success:
                    st["last_trade_ts"] = now
                    if target in ("LONG","SHORT"):
                        st["last_position_entry_ts"] = now
                        if target == "LONG": st["trail_peak"] = price; st["trail_trough"] = None
                        else: st["trail_trough"] = price; st["trail_peak"] = None
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
            st = state[sym]; hb = st["ws_last_heartbeat"]
            if hb > 0 and (now - hb) > 60.0:
                logging.error(f"{sym} WebSocket stale! {now - hb:.1f}s"); st["bot_active"] = False

async def pnl_summary_loop(cli: AsyncClient):
    while True:
        total_upnl = 0.0
        for sym in SYMBOLS:
            snap = await get_positions_snapshot(cli, sym); st = state[sym]
            price = st["last_price"] or 0.0; wma = st["wma"].value; wma_str = f"{wma:.6f}" if wma else "n/a"
            L = snap["LONG"]; S = snap["SHORT"]; upnl_sym = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0); total_upnl += upnl_sym
            ws_health = "OK" if (time.time() - st["ws_last_heartbeat"]) < 60.0 else "STALE"
            wma_init = "✓" if st["wma"].initialized else "✗"; circuit = f"CB:{st['consecutive_failures']}" if st["consecutive_failures"]>0 else "OK"
            logging.info(f"[SUMMARY] {sym} active={st['bot_active']} | price={price:.6f} WMA={wma_str}({wma_init}) | WS={ws_health} | "
                         f"source={st.get('active_signal_source','NONE')} signal={st.get('current_signal')} | risk={circuit} | "
                         f"LONG={L['size']:.1f}({L['uPnL']:.2f}) SHORT={S['size']:.1f}({S['uPnL']:.2f})")
        logging.info(f"[SUMMARY] TOTAL uPnL: {total_upnl:.2f} USDT")
        await asyncio.sleep(PNL_SUMMARY_SEC)

# ========================= MAIN ================================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET): raise RuntimeError("Missing Binance API credentials")
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        global DUAL_SIDE
        DUAL_SIDE = await get_dual_side(cli)
        if not DUAL_SIDE:
            logging.error("Hedge Mode (dual-side) required!")
            raise RuntimeError("Enable Hedge Mode in Binance Futures settings")
        logging.info("Hedge Mode confirmed")
        for s in SYMBOLS:
            try: await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except Exception as e: logging.warning(f"{s} leverage setting failed: {e}")
        await seed_symbol_filters(cli); await seed_tick_wma(cli); await seed_extremes_buffer(cli)
        feed_task = asyncio.create_task(price_feed_loop(cli))
        health_task = asyncio.create_task(websocket_health_monitor())
        await asyncio.sleep(5.0)  # let WMA warm up
        trading_task = asyncio.create_task(simplified_trading_loop(cli))
        summary_task = asyncio.create_task(pnl_summary_loop(cli))
        await asyncio.gather(feed_task, trading_task, summary_task, health_task)
    finally:
        try: await cli.close_connection()
        except Exception: pass

# ========================= ENTRYPOINT ===========================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%b %d %H:%M:%S")
    class CleanLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "[SUMMARY]" in msg: return True
            if record.levelno >= logging.WARNING: return True
            if any(x in msg for x in ["executed - OrderID", "Signal change", "BOT ACTIVATED", "Circuit breaker", "FILLED", "Bounce conflict resolved"]): return True
            return False
    logging.getLogger().addFilter(CleanLogFilter())
    logging.info("=== IMPROVED TRADING BOT (Distance-based Bounce + Order Validation) ===")
    logging.info(f"Position Size: {POSITION_SIZE_ADA} ADA | Leverage: {LEVERAGE}x")
    logging.info(f"Bounce: distance-based priority, thr={BOUNCE_THRESHOLD_PCT*100:.1f}% | WMA tol ±{WMA_EQUAL_TOL*100:.2f}%")
    logging.info(f"Exit protection: callback={CALLBACK_RATE_PCT*100:.2f}%, min_hold={MIN_HOLD_SEC}s")
    logging.info(f"Order validation: timeout={ORDER_FILL_TIMEOUT_SEC}s, attempts={MAX_FILL_CHECK_ATTEMPTS}")
    asyncio.run(main())
