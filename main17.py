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

# ---- Core parameters ----
POSITION_SIZE_ADA   = float(os.getenv("POSITION_SIZE_ADA", "10.0"))
SIGNAL_CONFIRM_SEC  = float(os.getenv("SIGNAL_CONFIRM_SEC", "3.0"))     # confirm new signal
TRADE_COOLDOWN_SEC  = float(os.getenv("TRADE_COOLDOWN_SEC", "10.0"))    # gap between trades
PRICE_GLITCH_PCT    = float(os.getenv("PRICE_GLITCH_PCT", "0.03"))      # drop bad ticks

# Exit/flip protection (anti-churn) - OPTION A: DISABLED
CALLBACK_RATE_PCT   = 0.0  # DISABLED - using exchange trailing only
MIN_HOLD_SEC        = float(os.getenv("MIN_HOLD_SEC", "10.0"))          # hold before exit/flip

# ---- PRICE SOURCE ----
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "mark").lower()
def _ws_host() -> str:
    return "fstream.binance.com" if PRICE_SOURCE in ("futures", "mark") else "stream.binance.com:9443"
def _stream_name(sym: str) -> str:
    return f"{sym.lower()}@markPrice@1s" if PRICE_SOURCE == "mark" else f"{sym.lower()}@trade"

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

# ---- Adaptive window helpers (env-configurable; defaults keep 20h behavior) ----
def _parse_hours_env(key: str, default_csv: str) -> List[int]:
    raw = os.getenv(key, default_csv).strip()
    hours = []
    for part in raw.split(","):
        part = part.strip()
        if not part: continue
        try:
            h = int(float(part))
            if h > 0: hours.append(h)
        except Exception:
            continue
    # dedupe & sort
    return sorted(set(hours))

# Bounce windows (hours) — default: 20h only (backward compatible)
BOUNCE_WINDOWS_HOURS = _parse_hours_env("BOUNCE_WINDOWS_HOURS", "20")
BOUNCE_WINDOWS_SEC   = [h * 3600 for h in BOUNCE_WINDOWS_HOURS]

# Breakout windows (hours) — default: 20h only (backward compatible)
BREAKOUT_WINDOWS_HOURS = _parse_hours_env("BREAKOUT_WINDOWS_HOURS", "20")
BREAKOUT_WINDOWS_SEC   = [h * 3600 for h in BREAKOUT_WINDOWS_HOURS]

# ---- Bounce (Donchian, now strict candle High/Low) ----
BOUNCE_ENABLED       = True
BOUNCE_THRESHOLD_PCT = float(os.getenv("BOUNCE_THRESHOLD_PCT", "0.7")) / 100.0  # 0.7%
BOUNCE_COOLDOWN_SEC  = float(os.getenv("BOUNCE_COOLDOWN_SEC", "10.0"))  # between separate bounces

# ---- Breakout/Momentum (Donchian breakout on strict High/Low) ----
BREAKOUT_ENABLED      = True
BREAKOUT_COOLDOWN_SEC = float(os.getenv("BREAKOUT_COOLDOWN_SEC", "10.0"))  # between separate breakouts

# ---- Exchange-level trailing stop right after entry ----
EXCHANGE_TRAIL_ENABLED       = True
EXCHANGE_TRAIL_CALLBACK_RATE = float(os.getenv("EXCHANGE_TRAIL_CALLBACK_RATE", "0.5"))  # 0.5%

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

# >>> Diagnostic cadence (30 minutes)
DIAG_SUMMARY_SEC = 1800.0  # 30 minutes

# ========================= UTILITY CLASSES =========================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200); self.end_headers()
    def log_message(self, *_): pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

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
                return {"success": False, "error": f"Order {status.lower()}", "order_status": order_status}
            if attempt < MAX_FILL_CHECK_ATTEMPTS - 1:
                await asyncio.sleep(ORDER_FILL_TIMEOUT_SEC / MAX_FILL_CHECK_ATTEMPTS)
        except Exception as e:
            logging.warning(f"{symbol} Order status check failed (attempt {attempt + 1}): {e}")
            if attempt < MAX_FILL_CHECK_ATTEMPTS - 1:
                await asyncio.sleep(1.0)
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

# ========================= SAFE ORDER EXECUTION =================
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
        "price_buffer": deque(maxlen=5000),
        "ext_price_buffer": deque(maxlen=100000),  # (ts, last price) — kept for compatibility/logs
        # NEW: strict candle Hi/Lo buffer (1-min bars), plus rolling live minute Hi/Lo
        "kline_hl_buffer": deque(maxlen=2000),     # (ts_open_minute, high, low)
        "curr_minute_ts": None,
        "curr_minute_high": None,
        "curr_minute_low": None,

        "ws_last_heartbeat": 0.0,
        "bot_active": True, "manual_override": False,
        "current_signal": None, "signal_since": None, "last_trade_ts": 0.0, "active_signal_source": None,
        "trail_peak": None, "trail_trough": None,
        "last_bounce_ts": 0.0,
        "consecutive_failures": 0, "circuit_breaker_until": 0.0, "position_state": "IDLE",
        "order_lock": asyncio.Lock(), "last_position_entry_ts": 0.0,
        "pending_signal": None,
        # TRUE BOUNCE STATE TRACKING
        "bounce_state": {
            "breached_low": False,
            "breached_high": False,
            "low_breach_price": None,
            "high_breach_price": None,
            "low_threshold": None,
            "high_threshold": None,
        },
        # BREAKOUT STATE TRACKING
        "breakout_state": {
            "last_breakout_ts": 0.0,
            "last_20h_high": None,
            "last_20h_low": None,
            "last_debug_log_ts": 0.0,
        }
    } for s in SYMBOLS
}

# Track trailing stop order ids per side
trailing_orders: Dict[str, Dict[str, Optional[int]]] = {
    s: {"LONG": None, "SHORT": None} for s in SYMBOLS
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

# ========================= FIXED PRICE FEED (Compatible) ===========================
def _parse_price_from_event(d: dict) -> Optional[float]:
    p_str = d.get("p") or d.get("c") or d.get("ap") or d.get("a")
    if p_str is None: return None
    try: return float(p_str)
    except Exception: return None

def _minute_bucket(ts: float) -> int:
    return int(ts // 60)  # minute-open epoch bucket

def _append_finalized_minute(st: dict):
    """When a minute rolls over, push the finished minute's high/low into the kline_hl_buffer."""
    mts = st["curr_minute_ts"]
    hi  = st["curr_minute_high"]
    lo  = st["curr_minute_low"]
    if mts is not None and hi is not None and lo is not None:
        st["kline_hl_buffer"].append((mts, hi, lo))
    # reset for the next minute
    st["curr_minute_ts"] = None
    st["curr_minute_high"] = None
    st["curr_minute_low"] = None

async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"
    reconnect_delay = 1.0; max_reconnect_delay = 60.0
    connection_attempts = 0
    
    while True:
        connection_attempts += 1
        try:
            logging.info(f"WebSocket connecting (attempt {connection_attempts}) to: {url}")
            async with websockets.connect(
                url, 
                ping_interval=20, 
                ping_timeout=10, 
                close_timeout=5, 
                max_queue=2000
            ) as ws:
                reconnect_delay = 1.0
                connection_attempts = 0
                logging.info("WebSocket price feed connected successfully")
                message_count = 0
                
                async for raw in ws:
                    try:
                        now = time.time()
                        message_count += 1
                        for sym in SYMBOLS: 
                            state[sym]["ws_last_heartbeat"] = now
                        try:
                            m = json.loads(raw)
                        except json.JSONDecodeError as e:
                            logging.warning(f"WebSocket JSON decode error: {e}")
                            continue
                        d = m.get("data", {})
                        if not d:
                            continue
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
                        # Update price/tick buffers
                        st["last_price"] = p
                        st["last_good_price"] = p
                        st["price_timestamp"] = now
                        st["price_buffer"].append((now, p))
                        st["ext_price_buffer"].append((now, p))  # still kept for logs/compat

                        # ---- NEW: maintain a rolling live 1-min high/low (strict candle Hi/Lo) ----
                        curr_bucket = _minute_bucket(now)
                        if st["curr_minute_ts"] is None:
                            st["curr_minute_ts"] = curr_bucket
                            st["curr_minute_high"] = p
                            st["curr_minute_low"]  = p
                        elif curr_bucket != st["curr_minute_ts"]:
                            # minute rolled — finalize previous minute into kline buffer
                            _append_finalized_minute(st)
                            st["curr_minute_ts"]   = curr_bucket
                            st["curr_minute_high"] = p
                            st["curr_minute_low"]  = p
                        else:
                            # same minute → update hi/lo
                            st["curr_minute_high"] = p if st["curr_minute_high"] is None else max(st["curr_minute_high"], p)
                            st["curr_minute_low"]  = p if st["curr_minute_low"]  is None else min(st["curr_minute_low"],  p)

                        if prev is None:
                            logging.info(f"First price received for {sym}: {p:.6f}")
                        if message_count % 500 == 0:
                            active_symbols = sum(1 for s in SYMBOLS if state[s]["last_price"] is not None)
                            logging.info(f"WebSocket health: {message_count} messages, {active_symbols}/{len(SYMBOLS)} symbols active")
                    except Exception as msg_error:
                        logging.warning(f"WebSocket message processing error: {msg_error}")
                        continue
                        
        except websockets.exceptions.ConnectionClosed as e:
            logging.warning(f"WebSocket connection closed: {e}. Reconnecting in {reconnect_delay:.1f}s...")
        except websockets.exceptions.WebSocketException as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting in {reconnect_delay:.1f}s...")
        except OSError as e:
            logging.warning(f"Network error: {e}. Reconnecting in {reconnect_delay:.1f}s...")
        except Exception as e:
            logging.error(f"Unexpected WebSocket error: {e}. Reconnecting in {reconnect_delay:.1f}s...")
        
        # On reconnect, finalize any in-progress minute to avoid gaps
        for sym in SYMBOLS:
            st = state[sym]
            _append_finalized_minute(st)
            if st.get("last_price") is not None:
                logging.info(f"Resetting stale price data for {sym}")
                st["last_price"] = None
                st["price_timestamp"] = None
        await asyncio.sleep(reconnect_delay + random.uniform(0.0, 1.0))
        reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)

# ========================= INITIALIZATION ===================
async def seed_extremes_buffer(cli: AsyncClient):
    """Seed both: (1) price close buffer (compat) and (2) strict 1m candle high/low buffer."""
    for s in SYMBOLS:
        try:
            kl = await call_binance(cli.futures_klines, symbol=s, interval="1m", limit=1440)
            pbuf = state[s]["ext_price_buffer"]
            hbuf = state[s]["kline_hl_buffer"]
            for k in kl:
                ts_open = float(k[0]) / 1000.0
                hi = float(k[2]); lo = float(k[3])
                c  = float(k[4])
                pbuf.append((ts_open, c))
                hbuf.append((int(ts_open // 60), hi, lo))
            if hbuf: logging.info(f"{s} seeded: {len(hbuf)} x 1m Hi/Lo candles (strict)")
        except Exception as e:
            logging.warning(f"{s} extremes buffer seed failed: {e}")

# ========================= DUAL STRATEGY SIGNALS ============================
def _rolling_candle_extremes_with_ts(st: dict, window_sec: int) -> Tuple[Optional[float], Optional[int], Optional[float], Optional[int]]:
    """
    Strict Donchian over 1-minute candles (High/Low).
    Scans kline_hl_buffer (historical finalized minutes) plus the current live minute.
    Returns: low, low_bucket_ts, high, high_bucket_ts (bucket is minute epoch int)
    """
    now = time.time()
    start_bucket = _minute_bucket(now - window_sec)
    hbuf: deque = st["kline_hl_buffer"]

    low = None; high = None
    low_ts = None; high_ts = None

    # historical finalized minutes
    for mts, hi, lo in reversed(hbuf):
        if mts < start_bucket: break
        if low is None or lo < low or (lo == low and (low_ts is None or mts > low_ts)):
            low, low_ts = lo, mts
        if high is None or hi > high or (hi == high and (high_ts is None or mts > high_ts)):
            high, high_ts = hi, mts

    # include current (still forming) minute
    if st["curr_minute_ts"] is not None and st["curr_minute_ts"] >= start_bucket:
        c_hi = st["curr_minute_high"]
        c_lo = st["curr_minute_low"]
        mts = st["curr_minute_ts"]
        if c_lo is not None and (low is None or c_lo < low or (c_lo == low and (low_ts is None or mts > low_ts))):
            low, low_ts = c_lo, mts
        if c_hi is not None and (high is None or c_hi > high or (c_hi == high and (high_ts is None or mts > high_ts))):
            high, high_ts = c_hi, mts

    return low, low_ts, high, high_ts

# Legacy name kept for compatibility (now strict candle-based)
def _rolling_extremes_with_ts(st: dict, window_sec: int) -> Tuple[Optional[float], Optional[int], Optional[float], Optional[int]]:
    return _rolling_candle_extremes_with_ts(st, window_sec)

# ========================= BOUNCE STRATEGY (strict Donchian Hi/Lo) ===================
def update_bounce_thresholds(st: dict):
    """
    Update bounce thresholds from strict Donchian over the (largest) configured bounce window.
    (Backward compatible: single threshold pair stored, using the widest window.)
    """
    if not BOUNCE_WINDOWS_SEC:
        wsec = 20 * 3600
    else:
        wsec = max(BOUNCE_WINDOWS_SEC)  # use widest to remain stable vs noise
    low_w, _, high_w, _ = _rolling_extremes_with_ts(st, wsec)
    bs = st["bounce_state"]

    new_low_threshold  = (float(low_w)  * (1.0 + BOUNCE_THRESHOLD_PCT)) if low_w  is not None else None
    new_high_threshold = (float(high_w) * (1.0 - BOUNCE_THRESHOLD_PCT)) if high_w is not None else None

    def moved_materially(old, new) -> bool:
        if old is None or new is None: return True
        if old == 0: return True
        return abs(new - old) / abs(old) > 0.001  # >0.1% move

    if moved_materially(bs["low_threshold"], new_low_threshold):
        bs["breached_low"] = False
        bs["low_breach_price"] = None
    if moved_materially(bs["high_threshold"], new_high_threshold):
        bs["breached_high"] = False
        bs["high_breach_price"] = None

    bs["low_threshold"]  = new_low_threshold
    bs["high_threshold"] = new_high_threshold

def true_bounce_signal(st: dict) -> Optional[str]:
    """
    SIMPLIFIED bounce logic (unchanged): breach → cross back through threshold → signal.
    Thresholds now come from strict candle Hi/Lo Donchian (widest configured window).
    """
    if not BOUNCE_ENABLED:
        return None

    price = st.get("last_price")
    if price is None:
        return None

    now = time.time()
    if now - st.get("last_bounce_ts", 0.0) < BOUNCE_COOLDOWN_SEC:
        return None

    update_bounce_thresholds(st)
    bs = st["bounce_state"]
    low_threshold  = bs["low_threshold"]
    high_threshold = bs["high_threshold"]
    if low_threshold is None and high_threshold is None:
        return None

    # Step 1: track breaches
    if low_threshold is not None and price < low_threshold:
        if not bs["breached_low"]:
            bs["breached_low"] = True
            bs["low_breach_price"] = price
            logging.info(f"BOUNCE: Breached LOW threshold {low_threshold:.6f} at {price:.6f}")
        else:
            bs["low_breach_price"] = min(bs["low_breach_price"] or price, price)

    if high_threshold is not None and price > high_threshold:
        if not bs["breached_high"]:
            bs["breached_high"] = True
            bs["high_breach_price"] = price
            logging.info(f"BOUNCE: Breached HIGH threshold {high_threshold:.6f} at {price:.6f}")
        else:
            bs["high_breach_price"] = max(bs["high_breach_price"] or price, price)

    # Step 2: cross-back confirmation
    min_depth_ratio = BOUNCE_THRESHOLD_PCT * 0.01

    if (bs["breached_low"] and low_threshold is not None and 
        bs["low_breach_price"] is not None and price >= low_threshold):
        breach_depth = (low_threshold - bs["low_breach_price"]) / low_threshold
        if breach_depth >= min_depth_ratio:
            bs["breached_low"] = False
            bs["low_breach_price"] = None
            st["last_bounce_ts"] = now
            logging.info(f"BOUNCE LONG: Price {price:.6f} bounced UP through {low_threshold:.6f} (depth {breach_depth*100:.3f}%)")
            return "LONG"
    
    if (bs["breached_high"] and high_threshold is not None and 
        bs["high_breach_price"] is not None and price <= high_threshold):
        breach_depth = (bs["high_breach_price"] - high_threshold) / high_threshold
        if breach_depth >= min_depth_ratio:
            bs["breached_high"] = False
            bs["high_breach_price"] = None
            st["last_bounce_ts"] = now
            logging.info(f"BOUNCE SHORT: Price {price:.6f} bounced DOWN through {high_threshold:.6f} (depth {breach_depth*100:.3f}%)")
            return "SHORT"

    return None

# ========================= BREAKOUT STRATEGY (strict Donchian Hi/Lo) =======================
def breakout_signal(st: dict) -> Optional[str]:
    """
    Breakout/Momentum strategy (priority):
    - LONG if price > Donchian High of ANY configured breakout window
    - SHORT if price < Donchian Low  of ANY configured breakout window
    Uses strict candle High/Low.
    """
    if not BREAKOUT_ENABLED:
        return None

    price = st.get("last_price")
    if price is None:
        return None

    now = time.time()
    bos = st["breakout_state"]
    if now - bos["last_breakout_ts"] < BREAKOUT_COOLDOWN_SEC:
        return None

    # If no windows configured (shouldn't happen), fallback to 20h
    windows = BREAKOUT_WINDOWS_SEC or [20 * 3600]

    fired = None
    high_any = None
    low_any  = None

    # Compute and test each window; fire if ANY window is broken
    for wsec in windows:
        low_w, _, high_w, _ = _rolling_extremes_with_ts(st, wsec)
        if low_w is None or high_w is None:
            continue
        high_any = high_w if high_any is None else max(high_any, high_w)
        low_any  = low_w  if low_any  is None else min(low_any,  low_w)

        if price > high_w:
            fired = "LONG"; break
        if price < low_w:
            fired = "SHORT"; break

    # Periodic debug (every 5 min) using the widest window for display
    if now - bos.get("last_debug_log_ts", 0.0) >= 300.0:
        # find symbol name
        symbol = None
        for sym, symbol_state in state.items():
            if symbol_state is st:
                symbol = sym
                break
        if symbol:
            # show widest window ref for clarity
            wdisp = max(windows)
            lw, _, hw, _ = _rolling_extremes_with_ts(st, wdisp)
            if lw is not None and hw is not None:
                logging.info(f"BREAKOUT CHECK {symbol}: price={price:.6f} | Donchian({int(wdisp/3600)}h)_low={lw:.6f} | _high={hw:.6f}")
        bos["last_debug_log_ts"] = now

    if fired:
        bos["last_breakout_ts"] = now
        if fired == "LONG":
            logging.info(f"BREAKOUT LONG: Price {price:.6f} broke above Donchian high")
        else:
            logging.info(f"BREAKOUT SHORT: Price {price:.6f} broke below Donchian low")
        return fired

    return None

# ========================= COMBINED SIGNAL LOGIC (OPTION B PRIORITY) ===============
def get_target_signal(st: dict) -> Tuple[Optional[str], str]:
    # Priority 1: Breakout signals
    breakout = breakout_signal(st)
    if breakout in ("LONG", "SHORT"):
        return breakout, "BREAKOUT"
    # Priority 2: Bounce signals
    bounce = true_bounce_signal(st)
    if bounce in ("LONG", "SHORT"):
        return bounce, "BOUNCE"
    return None, "NONE"

# ========================= EXCHANGE TRAILING STOP (FIXED -2021) =========
async def place_exchange_trailing_stop(cli: AsyncClient, symbol: str, side_pos: str, qty: float, last_price: float) -> Optional[int]:
    if not EXCHANGE_TRAIL_ENABLED or qty <= 0 or last_price <= 0:
        return None
    working_type = "MARK_PRICE" if PRICE_SOURCE == "mark" else "CONTRACT_PRICE"
    side = "SELL" if side_pos == "LONG" else "BUY"
    base_params = {
        "symbol": symbol,
        "side": side,
        "type": "TRAILING_STOP_MARKET",
        "quantity": quantize_qty(symbol, qty),
        "callbackRate": EXCHANGE_TRAIL_CALLBACK_RATE,
        "workingType": working_type,
        "reduceOnly": True,
    }
    if DUAL_SIDE:
        base_params["positionSide"] = side_pos
    params = dict(base_params)
    try:
        res = await call_binance(cli.futures_create_order, **params)
    except Exception as e:
        msg = str(e)
        if "-1106" in msg and "reduceonly" in msg.lower() and ("reduceOnly" in params):
            params_wo = {k: v for k, v in params.items() if k != "reduceOnly"}
            logging.warning(f"{symbol} TRAIL {side_pos}: retrying without reduceOnly")
            res = await call_binance(cli.futures_create_order, **params_wo)
        else:
            logging.warning(f"{symbol} TRAIL {side_pos}: create failed: {e}")
            return None
    oid = res.get("orderId")
    if oid is not None:
        trailing_orders[symbol][side_pos] = int(oid)
        logging.info(f"{symbol} TRAIL {side_pos}: set {EXCHANGE_TRAIL_CALLBACK_RATE:.3f}% (orderId={oid}) act=immediate")
    return trailing_orders[symbol][side_pos]

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

            if short_size > tolerance and target_signal != "SHORT":
                ok_all &= await safe_order_execution(cli, exit_short(symbol, short_size), symbol, "CLOSE SHORT")
            if long_size > tolerance and target_signal != "LONG":
                ok_all &= await safe_order_execution(cli, exit_long(symbol, long_size), symbol, "CLOSE LONG")

            if target_signal == "LONG" and long_size < target_long - tolerance:
                delta = target_long - long_size
                ok = await safe_order_execution(cli, open_long(symbol, delta), symbol, "OPEN LONG")
                if ok:
                    st["last_position_entry_ts"] = time.time()
                    st["trail_peak"] = st.get("last_price")
                    st["trail_trough"] = None
                    actual_long = await get_position_size_fresh(cli, symbol, "LONG")
                    await place_exchange_trailing_stop(cli, symbol, "LONG", actual_long, st.get("last_price") or 0.0)
                ok_all &= ok
            elif target_signal == "SHORT" and short_size < target_short - tolerance:
                delta = target_short - short_size
                ok = await safe_order_execution(cli, open_short(symbol, delta), symbol, "OPEN SHORT")
                if ok:
                    st["last_position_entry_ts"] = time.time()
                    st["trail_trough"] = st.get("last_price")
                    st["trail_peak"] = None
                    actual_short = await get_position_size_fresh(cli, symbol, "SHORT")
                    await place_exchange_trailing_stop(cli, symbol, "SHORT", actual_short, st.get("last_price") or 0.0)
                ok_all &= ok
            elif target_signal == "FLAT":
                st["trail_peak"] = None
                st["trail_trough"] = None

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
    # OPTION A: DISABLED - Always return True (no internal callback logic)
    return True

async def simplified_trading_loop(cli: AsyncClient):
    POLL_SEC = 0.25
    while True:
        await asyncio.sleep(POLL_SEC)
        for sym in SYMBOLS:
            st = state[sym]
            price = st["last_price"]; ts = st.get("price_timestamp", 0); now = time.time()
            if price is None or ts is None or (now - ts) > PRICE_STALENESS_SEC: continue

            # Circuit breaker
            if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                if now < st["circuit_breaker_until"]: continue
                st["consecutive_failures"] = 0; logging.info(f"{sym} Circuit breaker reset")

            if st["manual_override"]:
                continue

            # Maintain trailing markers for info only
            if st["current_signal"] == "LONG":
                st["trail_peak"] = price if st["trail_peak"] is None else max(st["trail_peak"], price); st["trail_trough"] = None
            elif st["current_signal"] == "SHORT":
                st["trail_trough"] = price if st["trail_trough"] is None else min(st["trail_trough"], price); st["trail_peak"] = None
            else:
                st["trail_peak"] = None; st["trail_trough"] = None

            # LATCH fresh signal
            fresh_signal, signal_source = get_target_signal(st)
            if fresh_signal in ("LONG","SHORT"):
                st["pending_signal"] = fresh_signal
                st["pending_signal_source"] = signal_source

            pending = st.get("pending_signal")
            if pending is None:
                continue

            target = pending
            current = st["current_signal"]
            source = st.get("pending_signal_source", "UNKNOWN")

            if target != current and current in ("LONG","SHORT") and st.get("last_position_entry_ts", 0) > 0:
                if now - st["last_position_entry_ts"] < MIN_HOLD_SEC:
                    continue
                if not _callback_retrace_hit(st, price, current):
                    continue

            if st["signal_since"] is None or target != current:
                st["current_signal"] = target
                st["signal_since"] = now
                st["active_signal_source"] = source
                logging.info(f"{sym} Signal change: {current} -> {target} (source={source})")

            if (now - st["signal_since"]) < SIGNAL_CONFIRM_SEC:
                continue
            if now - st["last_trade_ts"] < TRADE_COOLDOWN_SEC:
                continue

            symbol_delay = (hash(sym) % len(SYMBOLS)) * 0.5  # 0-2.5s stagger
            await asyncio.sleep(symbol_delay)

            try:
                success = await set_target_position(cli, sym, target)
                if success:
                    st["last_trade_ts"] = now
                    st["last_position_entry_ts"] = now
                    if target == "LONG":
                        st["trail_peak"] = price; st["trail_trough"] = None
                    else:
                        st["trail_trough"] = price; st["trail_peak"] = None
                    st["pending_signal"] = None
                    st["pending_signal_source"] = None
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
            price = st["last_price"] or 0.0
            L = snap["LONG"]; S = snap["SHORT"]; upnl_sym = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0); total_upnl += upnl_sym
            ws_health = "OK" if (time.time() - st["ws_last_heartbeat"]) < 60.0 else "STALE"
            circuit = f"CB:{st['consecutive_failures']}" if st["consecutive_failures"]>0 else "OK"
            logging.info(
                f"[SUMMARY] {sym} active={st['bot_active']} | price={price:.6f} | WS={ws_health} | "
                f"source={st.get('active_signal_source','NONE')} signal={st.get('current_signal')} | risk={circuit} | "
                f"LONG={L['size']:.1f}({L['uPnL']:.2f}) SHORT={S['size']:.1f}({S['uPnL']:.2f})"
            )
        logging.info(f"[SUMMARY] TOTAL uPnL: {total_upnl:.2f} USDT")
        await asyncio.sleep(PNL_SUMMARY_SEC)

# >>> Diagnostics (entrypoint snapshot + 30-minute reasons-why-no-order)
def _compute_threshold_preview(st: dict) -> Tuple[Optional[float], Optional[float]]:
    """Preview thresholds from strict Donchian over the widest bounce window (no side effects)."""
    wsec = max(BOUNCE_WINDOWS_SEC) if BOUNCE_WINDOWS_SEC else 20*3600
    low_w, _, high_w, _ = _rolling_extremes_with_ts(st, wsec)
    low_thr  = (float(low_w)  * (1.0 + BOUNCE_THRESHOLD_PCT)) if low_w  is not None else None
    high_thr = (float(high_w) * (1.0 - BOUNCE_THRESHOLD_PCT)) if high_w is not None else None
    return low_thr, high_thr

def _fmt(o):
    if o is None: return "n/a"
    try:
        return f"{float(o):.6f}"
    except Exception:
        return str(o)

async def entrypoint_diagnostic_once():
    await asyncio.sleep(3.0)
    for sym in SYMBOLS:
        st = state[sym]
        price = st.get("last_price")
        low_thr, high_thr = _compute_threshold_preview(st)
        bs = st["bounce_state"]
        bos = st["breakout_state"]
        cool_left = max(0.0, (st.get("last_bounce_ts", 0.0) + BOUNCE_COOLDOWN_SEC) - time.time())
        breakout_cool_left = max(0.0, (bos["last_breakout_ts"] + BREAKOUT_COOLDOWN_SEC) - time.time())
        logging.info(
            f"[DIAG-ENTRY] {sym} price={_fmt(price)} | bounce_thr_low={_fmt(low_thr)} bounce_thr_high={_fmt(high_thr)} | "
            f"breakout_last_20h_low={_fmt(bos['last_20h_low'])} breakout_last_20h_high={_fmt(bos['last_20h_high'])} | "
            f"bounce_breached_low={bs['breached_low']} bounce_breached_high={bs['breached_high']} | "
            f"bounce_cooldown={cool_left:.1f}s breakout_cooldown={breakout_cool_left:.1f}s"
        )

async def diagnostic_summary_loop():
    while True:
        await asyncio.sleep(DIAG_SUMMARY_SEC)
        for sym in SYMBOLS:
            st = state[sym]
            bs = st["bounce_state"]
            bos = st["breakout_state"]
            price = st.get("last_price")
            low_thr, high_thr = _compute_threshold_preview(st)

            # For display, use the widest breakout window
            wsec = max(BREAKOUT_WINDOWS_SEC) if BREAKOUT_WINDOWS_SEC else 20*3600
            low_20h, _, high_20h, _ = _rolling_extremes_with_ts(st, wsec)
            
            bounce_cool_left = max(0.0, (st.get("last_bounce_ts", 0.0) + BOUNCE_COOLDOWN_SEC) - time.time())
            breakout_cool_left = max(0.0, (bos["last_breakout_ts"] + BREAKOUT_COOLDOWN_SEC) - time.time())
            
            reason = []
            if bounce_cool_left > 0:
                reason.append(f"bounce_cooldown {bounce_cool_left:.0f}s")
            if breakout_cool_left > 0:
                reason.append(f"breakout_cooldown {breakout_cool_left:.0f}s")
            if price is None:
                reason.append("no price yet")
            else:
                if high_20h is not None and price <= high_20h:
                    reason.append(f"price {price:.6f} ≤ Donchian({int(wsec/3600)}h)_high {high_20h:.6f} (no breakout up)")
                if low_20h is not None and price >= low_20h:
                    reason.append(f"price {price:.6f} ≥ Donchian({int(wsec/3600)}h)_low {low_20h:.6f} (no breakout down)")
                if low_thr is not None and price < low_thr and not bs["breached_low"]:
                    reason.append("await breach below bounce_low_thr")
                if high_thr is not None and price > high_thr and not bs["breached_high"]:
                    reason.append("await breach above bounce_high_thr")
                if bs["breached_low"] and (low_thr is not None) and price < low_thr:
                    depth = (low_thr - (bs["low_breach_price"] or low_thr)) / low_thr if low_thr else 0.0
                    reason.append(f"bounce_breached_low depth={depth*100:.2f}% waiting cross↑")
                if bs["breached_high"] and (high_thr is not None) and price > high_thr:
                    depth = ((bs["high_breach_price"] or high_thr) - high_thr) / high_thr if high_thr else 0.0
                    reason.append(f"bounce_breached_high depth={depth*100:.2f}% waiting cross↓")
            reason_str = "; ".join(reason) if reason else "ready→pending confirm/cooldown"
            logging.info(
                f"[DIAG] {sym} price={_fmt(price)} | BREAKOUT Donchian({int(wsec/3600)}h)_low={_fmt(low_20h)} _high={_fmt(high_20h)} | "
                f"BOUNCE thr_low={_fmt(low_thr)} thr_high={_fmt(high_thr)} | "
                f"bounce_breached_low={bs['breached_low']} bounce_breached_high={bs['breached_high']} | "
                f"why_not_order= {reason_str}"
            )

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
        await seed_symbol_filters(cli)
        await seed_extremes_buffer(cli)
        feed_task = asyncio.create_task(price_feed_loop(cli))
        health_task = asyncio.create_task(websocket_health_monitor())
        trading_task = asyncio.create_task(simplified_trading_loop(cli))
        summary_task = asyncio.create_task(pnl_summary_loop(cli))

        entry_diag = asyncio.create_task(entrypoint_diagnostic_once())
        diag_task  = asyncio.create_task(diagnostic_summary_loop())

        await asyncio.gather(feed_task, trading_task, summary_task, health_task, entry_diag, diag_task)
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
            if "[DIAG" in msg: return True
            if record.levelno >= logging.WARNING: return True
            if any(x in msg for x in [
                "submitted - OrderID",
                "executed - OrderID",
                "Signal change",
                "BOT ACTIVATED",
                "Circuit breaker",
                "FILLED",
                "Bounce conflict resolved",
                "TRAIL",
                "BOUNCE:",
                "BOUNCE LONG:",
                "BOUNCE SHORT:",
                "BREAKOUT LONG:",
                "BREAKOUT SHORT:",
                "BREAKOUT CHECK"
            ]): return True
            return False
    logging.getLogger().addFilter(CleanLogFilter())

    logging.info("=== DUAL STRATEGY TRADING BOT (Breakout + Bounce with Priority System | STRICT Donchian Hi/Lo + Adaptive Windows) ===")
    logging.info(f"Symbols & target sizes: {SYMBOLS}")
    logging.info(f"Leverage: {LEVERAGE}x | Bounce threshold={BOUNCE_THRESHOLD_PCT*100:.1f}% | Min hold={MIN_HOLD_SEC}s")
    logging.info(f"STRATEGY PRIORITY: 1st=BREAKOUT (strict Donchian), 2nd=BOUNCE (strict Donchian thresholds)")
    logging.info(f"Exchange trailing: {EXCHANGE_TRAIL_CALLBACK_RATE:.3f}% (internal callback disabled)")
    logging.info(f"Bounce windows (h): {BOUNCE_WINDOWS_HOURS} | Breakout windows (h): {BREAKOUT_WINDOWS_HOURS}")
    logging.info(f"Order validation: timeout={ORDER_FILL_TIMEOUT_SEC}s, attempts={MAX_FILL_CHECK_ATTEMPTS}")
    asyncio.run(main())
