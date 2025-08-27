#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, math, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28  # safe precision

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

SYMBOLS = {
    "ADAUSDT": 10,  "XRPUSDT": 10,
}

# >>> per-symbol safe ASCII tags <<<
SYMBOL_TAG = {"ADAUSDT":"ADA","BNBUSDT":"BNB","SOLUSDT":"SOL","XRPUSDT":"XRP"}

# WMA periods in seconds
SHORT_WMA_PERIOD = 4 * 60 * 60      # 4h (SHORT side)
LONG_WMA_PERIOD  = 24 * 60 * 60     # 24h (LONG side)

# Legacy log-only threshold (kept for logs; not used for exits now)
MOVEMENT_THRESHOLD = 0.10

# Trailing-stop config (Binance literal percent: 1.0 == 1%)
ACTIVATION_OFFSET   = float(os.getenv("ACTIVATION_OFFSET", "0.001"))
POS_CHECK_INTERVAL  = float(os.getenv("POS_CHECK_INTERVAL", "10.0"))

# Anti-duplicate controls
ENTRY_COOLDOWN_SEC    = float(os.getenv("ENTRY_COOLDOWN_SEC", "2.0"))
TRAILING_COOLDOWN_SEC = float(os.getenv("TRAILING_COOLDOWN_SEC", "2.0"))
ENTRY_GUARD_TTL_SEC   = float(os.getenv("ENTRY_GUARD_TTL_SEC", "5.0"))

# REST backoff / position cache
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "45.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# ---- Special ID tag for callback activation ----
TRAIL_GUARD_TAG = os.getenv("TRAIL_GUARD_TAG", "CALLBK")

# Exit history cap
EXIT_HISTORY_MAX = int(os.getenv("EXIT_HISTORY_MAX", "5"))

# --------- Side-safe signal tags (for readability) ---------
SIGNAL_LONG_TAG  = "L|WMA24+RSI288>50"
SIGNAL_SHORT_TAG = "S|WMA4+RSI48<50"

# Profit-lock thresholds (ROE %)
PROFIT_LOCK_ARM_ROE  = float(os.getenv("PROFIT_LOCK_ARM_ROE",  "40.0"))  # arm when ROE >= +40%
PROFIT_LOCK_STOP_ROE = float(os.getenv("PROFIT_LOCK_STOP_ROE", "25.0"))  # lock at +25% ROE

# ---- Logging quiet window (seconds). Only market order execs + errors will show.
QUIET_FOR_SEC = int(os.getenv("QUIET_FOR_SEC", "3600"))  # 1 hour

# ========================= QUIET /ping =========================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
    def log_message(self, *_):
        pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

# ========================= TICK-BASED WMA ======================
class TickWMA:
    def __init__(self, period_seconds: int):
        self.period_seconds = period_seconds
        self.alpha = 1.0 / period_seconds
        self.value: Optional[float] = None
        self.last_update: Optional[float] = None
        self.tick_buffer = deque(maxlen=10000)

    def update(self, price: float):
        now = time.time()
        self.tick_buffer.append((now, price))
        if self.value is None:
            self.value = price
            self.last_update = now
            return
        dt = now - (self.last_update or now)
        a = min(self.alpha * dt, 1.0)
        self.value = price * a + self.value * (1.0 - a)
        self.last_update = now

# ========================= ORDER HELPERS =======================
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

# per-symbol client IDs using safe ASCII tags
def profit_lock_client_id(symbol: str, side: str) -> str:
    tag = SYMBOL_TAG.get(symbol, symbol)
    return f"LOCK-{tag}-{side}-{int(time.time())}"

# ========================= RUNTIME STATE =======================
state = {
    s: {
        # WMAs
        "wma_short": TickWMA(SHORT_WMA_PERIOD),
        "wma_long":  TickWMA(LONG_WMA_PERIOD),

        # WMA regime flags (persist until flipped)
        "wma24_above": False,  # price > 24h WMA
        "wma4_below":  False,  # price < 4h  WMA
        "wma24_cross_ts": 0.0,
        "wma4_cross_ts":  0.0,

        # long side
        "in_long": False, "long_pending": False,
        "long_entry_price": None, "long_peak": None,
        "long_exit_history": [], "long_accumulated_movement": 0.0,

        # short side
        "in_short": False, "short_pending": False,
        "short_entry_price": None, "short_trough": None,
        "short_exit_history": [], "short_accumulated_movement": 0.0,

        # trailing state
        "long_trailing":  {"order_id": None, "callback": None, "activation": None},
        "short_trailing": {"order_id": None, "callback": None, "activation": None},

        # profit-lock stop (STOP_MARKET) state
        "long_profit_lock":  {"order_id": None, "stop_price": None},
        "short_profit_lock": {"order_id": None, "stop_price": None},

        # RSI(5m) state (persistent, updated on 5m close)
        "rsi_last_close": None,
        "rsi48": None, "rsi48_prev": None,
        "rsi48_avg_gain": None, "rsi48_avg_loss": None, "rsi48_seeded": False,
        "rsi288": None, "rsi288_prev": None,
        "rsi288_avg_gain": None, "rsi288_avg_loss": None, "rsi288_seeded": False,
        "rsi48_cross_below_50_ts": 0.0,
        "rsi48_cross_above_50_ts": 0.0,
        "rsi288_cross_above_50_ts": 0.0,
        "rsi288_cross_below_50_ts": 0.0,

        "last_price": None,
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "last_pos_check": 0.0,

        # anti-duplicate timestamps
        "last_long_entry_ts": 0.0, "last_short_entry_ts": 0.0,
        "last_long_trailing_ts": 0.0, "last_short_trailing_ts": 0.0,

        # explicit entry guard tokens
        "long_entry_guard": None,  "long_entry_guard_ts": 0.0,
        "short_entry_guard": None, "short_entry_guard_ts": 0.0,

        # special client IDs for trailing orders
        "long_trailing_guard_id": None,
        "short_trailing_guard_id": None,

        # locks (ensure present even if state gets reloaded)
        "long_trailing_lock": asyncio.Lock(),
        "short_trailing_lock": asyncio.Lock(),
    }
    for s in SYMBOLS
}

# ========================= PRICE FILTER CACHE ==================
PRICE_TICK = {}
PRICE_DECIMALS = {}

def _dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))

def _decimals_from_tick(tick: str) -> int:
    if "." not in tick: return 0
    return len(tick.split(".")[1].rstrip("0"))

def quantize_price(symbol: str, price: float) -> str:
    tick = PRICE_TICK.get(symbol)
    if not tick:
        return f"{price:.8f}"  # fallback
    p = _dec(price)
    q = (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    decs = PRICE_DECIMALS.get(symbol, 8)
    return f"{q:.{decs}f}"

# ========================= SAFE BINANCE CALL ===================
_last_rest_ts = 0.0

async def call_binance(fn, *args, **kwargs):
    global _last_rest_ts
    now = time.time()
    min_gap = 0.08
    if now - _last_rest_ts < min_gap:
        await asyncio.sleep(min_gap - (now - _last_rest_ts) + random.uniform(0.01, 0.03))
    _last_rest_ts = time.time()
    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "-1003" in msg or "Too many requests" in msg or "IP banned" in msg:
                sleep_for = min(delay, RATE_LIMIT_MAX_SLEEP)
                logging.warning(f"Rate limit/backoff: sleeping {sleep_for:.1f}s ({msg})")
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
        logging.error(f"{symbol} refresh_position_cache failed: {e}")

async def get_position_size(cli: AsyncClient, symbol: str, side: str) -> float:
    now = time.time()
    pc = position_cache[symbol][side]
    if now - pc["ts"] <= POSITION_CACHE_TTL:
        return pc["size"]
    await refresh_position_cache(cli, symbol)
    return position_cache[symbol][side]["size"]

async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    try:
        if action.startswith("CLOSE") or "EXIT" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size(cli, symbol, side)
            required_qty = float(order_params["quantity"])
            if current_pos < required_qty * 0.99:
                logging.warning(f"{symbol} {action}: Insufficient position {current_pos} < {required_qty}")
                return False
        result = await call_binance(cli.futures_create_order, **order_params)
        state[symbol]["last_order_id"] = result.get("orderId")
        invalidate_position_cache(symbol)
        # elevate to WARNING so it appears during quiet window
        logging.warning(f"{symbol} {action} executed - OrderID: {state[symbol]['last_order_id']}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} failed: {e}")
        return False

# ========================= LOGGING HELPERS =====================
def log_entry_details(symbol: str, entry_type: str, price: float, exit_history: list, direction: str):
    history_str = f"[{', '.join([f'${p:.4f}' for p in exit_history])}]" if exit_history else "[]"
    logging.info(f"{symbol} {entry_type} @ ${price:.4f}")
    logging.info(f"Exit History: {history_str} ({len(exit_history)} exits)")

def log_exit_details(symbol: str, direction: str, entry_price: float, exit_price: float, accumulated_movement: float, exit_number: int):
    if direction == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
    pnl_sign = "+" if pnl_pct >= 0 else ""
    logging.info(f"{symbol} {direction} EXIT @ ${exit_price:.4f}")
    logging.info(f"Accumulated Movement: {accumulated_movement:.2f}% (threshold: {MOVEMENT_THRESHOLD*100:.1f}%)")
    logging.info(f"Trade PNL: {pnl_sign}{pnl_pct:.2f}% [Entry: ${entry_price:.4f} -> Exit: ${exit_price:.4f}]")
    logging.info("Exit recorded" if exit_number > 0 else "Exit NOT recorded")

# ========================= TRAILING/ROE HELPERS ====================
def pnl_percent(entry_price: Optional[float], current_price: float, side: str, leverage: float = LEVERAGE) -> float:
    if not entry_price:
        return 0.0
    if side.upper() == "LONG":
        base = (current_price - entry_price) / entry_price * 100.0
    else:
        base = (entry_price - current_price) / entry_price * 100.0
    return base * leverage  # ROE %

async def cancel_trailing(cli: AsyncClient, symbol: str, order_id: Optional[int]):
    if not order_id:
        return
    try:
        await call_binance(cli.futures_cancel_order, symbol=symbol, orderId=order_id)
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} cancelled trailing order {order_id}")
    except Exception as e:
        logging.warning(f"{symbol} failed to cancel trailing order {order_id}: {e}")

def make_client_id(prefix: str, symbol: str, side: str, bucket_sec: float = 2.0) -> str:
    tag = SYMBOL_TAG.get(symbol, symbol)
    bucket = int(time.time() / bucket_sec)
    return f"{prefix}-{tag}-{side}-{bucket}"

def trailing_client_id(symbol: str, side: str) -> str:
    tag = SYMBOL_TAG.get(symbol, symbol)
    return f"{TRAIL_GUARD_TAG}-{tag}-{side}-{int(time.time())}"

def clamp_callback(cb: float) -> float:
    return max(0.1, min(5.0, cb))

def quant_activation(symbol: str, side: str, latest_price: float) -> str:
    if side.upper() == "LONG":
        raw_activation = latest_price * (1.0 + ACTIVATION_OFFSET)
    else:
        raw_activation = latest_price * (1.0 - ACTIVATION_OFFSET)
    return quantize_price(symbol, raw_activation)

async def place_trailing(cli: AsyncClient, symbol: str, side: str,
                         latest_price: float, callback_rate_pct: float,
                         reduce_only_allowed: bool) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    """Place a TRIGGERING trailing stop with fixed callback (activation at current price)."""
    if latest_price is None or not math.isfinite(latest_price) or latest_price <= 0.0:
        logging.warning(f"{symbol} {side} trailing skipped: invalid latest_price={latest_price}")
        return None, None, None

    order_side = "SELL" if side.upper() == "LONG" else "BUY"
    position_side = side.upper()
    activation_str = quantize_price(symbol, latest_price)  # avoid dead-zone
    cb = clamp_callback(float(callback_rate_pct))
    cid = trailing_client_id(symbol, side)

    params = dict(
        symbol=symbol,
        side=order_side,
        type="TRAILING_STOP_MARKET",
        quantity=SYMBOLS[symbol],
        positionSide=position_side,
        callbackRate=cb,
        activationPrice=activation_str,
        workingType="CONTRACT_PRICE",
        newOrderRespType="ACK",
        newClientOrderId=cid,
    )
    if reduce_only_allowed:
        params["reduceOnly"] = True

    logging.info(f"{symbol} placing trailing {side}: cb={cb:.3f}% act={activation_str} cid={cid}")
    try:
        res = await call_binance(cli.futures_create_order, **params)
        oid = res.get("orderId")
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} placed trailing to close {side}: oid={oid} cid={cid}")
        return oid, float(activation_str), cid
    except Exception as e:
        msg = str(e)
        if "-2021" in msg or "-1102" in msg:
            bump = ACTIVATION_OFFSET * 2.0
            activation_str = quantize_price(
                symbol,
                (latest_price * (1.0 + bump)) if side.upper() == "LONG" else (latest_price * (1.0 - bump))
            )
            params["activationPrice"] = activation_str
            try:
                res = await call_binance(cli.futures_create_order, **params)
                oid = res.get("orderId")
                invalidate_position_cache(symbol)
                logging.info(f"{symbol} retried trailing {side}: oid={oid} act={activation_str} cid={cid}")
                return oid, float(activation_str), cid
            except Exception as e2:
                logging.error(f"{symbol} trailing retry failed: {e2} cid={cid}")
                return None, None, cid
        logging.error(f"{symbol} trailing failed: {e} cid={cid}")
        return None, None, cid

# ---- Profit-lock helpers ----
def roe_stop_price(entry: Optional[float], side: str, target_roe_pct: float) -> Optional[float]:
    """
    Convert a target ROE% into a stop price relative to the entry.
    Example (LONG, L=50, target=25%): +25% ROE ≈ +0.5% price -> stop = entry * 1.005
    """
    if not entry or entry <= 0:
        return None
    frac_change = (target_roe_pct / LEVERAGE) / 100.0  # ROE% -> raw price % change
    if side.upper() == "LONG":
        return entry * (1.0 + frac_change)
    else:  # SHORT
        return entry * (1.0 - frac_change)

# NEW: place/cancel profit-lock STOP-MARKET orders
async def place_profit_lock(cli: AsyncClient, symbol: str, side: str,
                            stop_price_float: float, reduce_only_allowed: bool) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    size = await get_position_size(cli, symbol, side.upper())
    if size <= 1e-12:
        logging.info(f"{symbol} {side} profit-lock skipped: no size")
        return None, None, None

    order_side = "SELL" if side.upper() == "LONG" else "BUY"
    position_side = side.upper()
    stop_str = quantize_price(symbol, stop_price_float)
    cid = profit_lock_client_id(symbol, side)

    params = dict(
        symbol=symbol,
        side=order_side,
        type="STOP_MARKET",
        positionSide=position_side,
        stopPrice=stop_str,
        workingType="MARK_PRICE",
        quantity=size,
        newOrderRespType="ACK",
        newClientOrderId=cid,
        reduceOnly=True if reduce_only_allowed else None,
    )
    params = {k: v for k, v in params.items() if v is not None}

    logging.info(f"{symbol} placing PROFIT-LOCK {side}: stop={stop_str} qty={size} cid={cid}")
    try:
        res = await call_binance(cli.futures_create_order, **params)
        oid = res.get("orderId")
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} placed PROFIT-LOCK {side}: oid={oid} stop={stop_str} cid={cid}")
        return oid, float(stop_str), cid
    except Exception as e:
        logging.error(f"{symbol} profit-lock place failed ({side}): {e}")
        return None, None, cid

async def cancel_profit_lock(cli: AsyncClient, symbol: str, order_id: Optional[int], side_label: str):
    if not order_id:
        return
    try:
        await call_binance(cli.futures_cancel_order, symbol=symbol, orderId=order_id)
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} cancelled PROFIT-LOCK {side_label} order {order_id}")
    except Exception as e:
        logging.warning(f"{symbol} failed to cancel PROFIT-LOCK {side_label} order {order_id}: {e}")

# ========================= SIMPLE TRAILING LOGIC ==================
def _ensure_lock(st: dict, key: str) -> asyncio.Lock:
    """Guarantee presence of a lock in state (fixes KeyError 'long_trailing_lock')."""
    lock = st.get(key)
    if lock is None or not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        st[key] = lock
    return lock

async def ensure_trailing(cli: AsyncClient, sym: str, st: dict, latest_price: float,
                          side: str, reduce_only_allowed: bool):
    """
    Place a single trailing stop exactly when ROE first reaches +60%,
    callback=0.5%, then keep it until position closes.
    """
    lock = _ensure_lock(st, "long_trailing_lock" if side == "LONG" else "short_trailing_lock")
    async with lock:
        now = time.time()
        if side == "LONG":
            if now - st["last_long_trailing_ts"] < TRAILING_COOLDOWN_SEC:
                return
            entry = st["long_entry_price"]
            have = st["long_trailing"]
            roe = pnl_percent(entry, latest_price, "LONG")
        else:
            if now - st["last_short_trailing_ts"] < TRAILING_COOLDOWN_SEC:
                return
            entry = st["short_entry_price"]
            have = st["short_trailing"]
            roe = pnl_percent(entry, latest_price, "SHORT")

        if have["order_id"]:
            return

        if roe >= 60.0:
            oid, act, cid = await place_trailing(cli, sym, side, latest_price, callback_rate_pct=0.5,
                                                 reduce_only_allowed=reduce_only_allowed)
            if oid:
                have.update({"order_id": oid, "callback": 0.5, "activation": act})
                if side == "LONG":
                    st["long_trailing_guard_id"] = cid
                    st["last_long_trailing_ts"] = time.time()
                else:
                    st["short_trailing_guard_id"] = cid
                    st["last_short_trailing_ts"] = time.time()

# PROFIT-LOCK: arm at +40% ROE; stop sits at +25% ROE from entry
async def ensure_profit_lock(cli: AsyncClient, sym: str, st: dict, latest_price: float,
                             side: str, reduce_only_allowed: bool):
    lock = _ensure_lock(st, "long_trailing_lock" if side == "LONG" else "short_trailing_lock")
    async with lock:
        if side == "LONG":
            entry = st["long_entry_price"]; have = st["long_profit_lock"]
            roe = pnl_percent(entry, latest_price, "LONG")
        else:
            entry = st["short_entry_price"]; have = st["short_profit_lock"]
            roe = pnl_percent(entry, latest_price, "SHORT")

        if have["order_id"]:
            return  # already armed

        # Arm only once when ROE first reaches the arm threshold
        if entry and roe >= PROFIT_LOCK_ARM_ROE:
            # Compute stop at the preserved-ROE level (e.g., +25% ROE)
            stop_target = roe_stop_price(entry, side, PROFIT_LOCK_STOP_ROE)
            if stop_target is None:
                return
            oid, stop_px, cid = await place_profit_lock(
                cli, sym, side, stop_target, reduce_only_allowed=reduce_only_allowed
            )
            if oid:
                have.update({"order_id": oid, "stop_price": stop_px})

# ========================= RSI (5m) HELPERS =====================
def _seed_wilder(period: int, closes: list) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(closes) < period + 1:
        return None, None, None
    window = closes[-(period + 1):]
    gains = []
    losses = []
    for i in range(1, len(window)):
        delta = window[i] - window[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    return avg_gain, avg_loss, window[-1]

def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

async def seed_rsi(cli: AsyncClient):
    """Seed RSI(48) and RSI(288) for each symbol from REST 5m klines (one-time)."""
    for sym in SYMBOLS:
        try:
            kl = await call_binance(cli.get_klines, symbol=sym, interval="5m", limit=300)
            closes = [float(k[4]) for k in kl]
            st = state[sym]

            # Seed 48
            ag48, al48, last_close = _seed_wilder(48, closes)
            if ag48 is not None:
                st["rsi48_avg_gain"] = ag48
                st["rsi48_avg_loss"] = al48
                st["rsi_last_close"] = last_close
                st["rsi48"] = _rsi_from_avgs(ag48, al48)
                st["rsi48_prev"] = st["rsi48"]
                st["rsi48_seeded"] = True

            # Seed 288
            ag288, al288, last_close2 = _seed_wilder(288, closes)
            if ag288 is not None:
                st["rsi288_avg_gain"] = ag288
                st["rsi288_avg_loss"] = al288
                st["rsi_last_close"] = last_close2
                st["rsi288"] = _rsi_from_avgs(ag288, al288)
                st["rsi288_prev"] = st["rsi288"]
                st["rsi288_seeded"] = True

            logging.info(f"{sym} RSI seeded: 48={st['rsi48']}, 288={st['rsi288']}")
        except Exception as e:
            logging.warning(f"{sym} seed_rsi failed: {e}")

def _update_rsi_on_close(sym: str, close_price: float):
    """Update Wilder RSI(48) and RSI(288) for symbol at 5m candle close."""
    st = state[sym]
    prev_close = st["rsi_last_close"]
    if prev_close is None:
        st["rsi_last_close"] = close_price
        return

    delta = close_price - prev_close
    gain = max(delta, 0.0)
    loss = max(-delta, 0.0)

    now_ts = time.time()

    # --- 48 ---
    if st["rsi48_seeded"]:
        ag = st["rsi48_avg_gain"]; al = st["rsi48_avg_loss"]
        ag = (ag * (48 - 1) + gain) / 48.0
        al = (al * (48 - 1) + loss) / 48.0
        rsi_prev = st["rsi48"]
        rsi_new = _rsi_from_avgs(ag, al)
        st["rsi48_avg_gain"], st["rsi48_avg_loss"] = ag, al
        st["rsi48_prev"], st["rsi48"] = rsi_prev, rsi_new
        if rsi_prev is not None and rsi_prev >= 50.0 and rsi_new < 50.0:
            st["rsi48_cross_below_50_ts"] = now_ts
            logging.info(f"{sym} RSI(48) crossed BELOW 50 @ {rsi_prev:.2f}->{rsi_new:.2f}")
        if rsi_prev is not None and rsi_prev <= 50.0 and rsi_new > 50.0:
            st["rsi48_cross_above_50_ts"] = now_ts
            logging.info(f"{sym} RSI(48) crossed ABOVE 50 @ {rsi_prev:.2f}->{rsi_new:.2f}")

    # --- 288 ---
    if st["rsi288_seeded"]:
        ag = st["rsi288_avg_gain"]; al = st["rsi288_avg_loss"]
        ag = (ag * (288 - 1) + gain) / 288.0
        al = (al * (288 - 1) + loss) / 288.0
        rsi_prev = st["rsi288"]
        rsi_new = _rsi_from_avgs(ag, al)
        st["rsi288_avg_gain"], st["rsi288_avg_loss"] = ag, al
        st["rsi288_prev"], st["rsi288"] = rsi_prev, rsi_new
        if rsi_prev is not None and rsi_prev <= 50.0:
            if rsi_new > 50.0:
                st["rsi288_cross_above_50_ts"] = now_ts
        if rsi_prev is not None and rsi_prev >= 50.0:
            if rsi_new < 50.0:
                st["rsi288_cross_below_50_ts"] = now_ts

    st["rsi_last_close"] = close_price

# ========================= SEED SYMBOL FILTERS =================
async def seed_symbol_filters(cli: AsyncClient):
    try:
        info = await call_binance(cli.futures_exchange_info)
        symbols_info = {s["symbol"]: s for s in info.get("symbols", [])}
        for sym in SYMBOLS:
            si = symbols_info.get(sym)
            if not si:
                continue
            pf = next((f for f in si.get("filters", []) if f.get("filterType") == "PRICE_FILTER"), None)
            if not pf:
                continue
            tick = pf.get("tickSize")
            if tick:
                PRICE_TICK[sym] = _dec(tick)
                PRICE_DECIMALS[sym] = _decimals_from_tick(tick)
                logging.info(f"{sym} tickSize={tick} decimals={PRICE_DECIMALS[sym]}")
    except Exception as e:
        logging.warning(f"seed_symbol_filters failed: {e}")

# ========================= SEED WMAs ===========================
async def seed_wmas(cli: AsyncClient):
    for s in SYMBOLS:
        kl = await call_binance(cli.get_klines, symbol=s, interval="1m", limit=1440)
        closes = [float(k[4]) for k in kl]
        closes_4h = closes[-240:] if len(closes) >= 240 else closes
        wma_long  = state[s]["wma_long"]
        wma_short = state[s]["wma_short"]
        for c in closes:
            wma_long.update(c)
        for c in closes_4h:
            wma_short.update(c)
        logging.info(f"{s} 24h WMA initialized: {wma_long.value:.4f}")
        logging.info(f"{s} 4h  WMA initialized: {wma_short.value:.4f}")

# ========================= MAIN LOOP ==========================
async def run(cli: AsyncClient):
    dual_side = await get_dual_side(cli)            # Hedge Mode?
    reduce_only_allowed = not dual_side             # reduceOnly in one-way mode

    for s in SYMBOLS:
        try:
            await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
        except Exception as e:
            logging.warning(f"{s} set leverage failed: {e}")

    # Seed filters/wmas/RSI and adopt existing positions
    try:
        await seed_symbol_filters(cli)
        await seed_wmas(cli)
        await adopt_positions(cli)
        await seed_rsi(cli)
    except Exception as e:
        logging.warning(f"Startup seeding warning: {e}")

    trade_streams = [f"{s.lower()}@trade" for s in SYMBOLS]
    k5_streams    = [f"{s.lower()}@kline_5m" for s in SYMBOLS]
    streams       = trade_streams + k5_streams
    url           = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=1000,
            ) as ws:
                async for raw in ws:
                    m     = json.loads(raw)
                    stype = m["stream"]; d = m["data"]

                    # ---------- 5m kline (RSI updates + RSI-based exits on close) ----------
                    if stype.endswith("@kline_5m"):
                        k = d.get("k", {})
                        if not k.get("x", False):
                            continue
                        sym = d["s"]
                        close_price = float(k["c"])
                        _update_rsi_on_close(sym, close_price)

                        st = state[sym]
                        # LONG: RSI(288) crosses below 50 -> exit
                        if st["in_long"] and (st["rsi288_prev"] is not None and st["rsi288_prev"] >= 50.0 and st["rsi288"] is not None and st["rsi288"] < 50.0):
                            async with st["order_lock"]:
                                if st["in_long"]:
                                    await cancel_trailing(cli, sym, st["long_trailing"]["order_id"])
                                    await cancel_profit_lock(cli, sym, st["long_profit_lock"]["order_id"], "LONG")
                                    size = await get_position_size(cli, sym, "LONG")
                                    if size > 1e-12:
                                        params = dict(symbol=sym, side="SELL", type="MARKET",
                                                      quantity=size, positionSide="LONG")
                                        success = await safe_order_execution(cli, params, sym, "LONG RSI EXIT (RSI288 < 50)")
                                        if success:
                                            st["long_exit_history"].append(close_price)
                                            if len(st["long_exit_history"]) > EXIT_HISTORY_MAX:
                                                st["long_exit_history"] = st["long_exit_history"][-EXIT_HISTORY_MAX:]
                                            st["in_long"] = False
                                            st["long_entry_price"] = None
                                            st["long_peak"] = None
                                            st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                            st["long_trailing_guard_id"] = None
                                            st["long_profit_lock"] = {"order_id": None, "stop_price": None}
                                            logging.info(f"{sym} LONG RSI EXIT executed at close {close_price:.6f}")

                        # SHORT: RSI(48) crosses above 50 -> exit
                        if st["in_short"] and (st["rsi48_prev"] is not None and st["rsi48_prev"] <= 50.0 and st["rsi48"] is not None and st["rsi48"] > 50.0):
                            async with st["order_lock"]:
                                if st["in_short"]:
                                    await cancel_trailing(cli, sym, st["short_trailing"]["order_id"])
                                    await cancel_profit_lock(cli, sym, st["short_profit_lock"]["order_id"], "SHORT")
                                    size = await get_position_size(cli, sym, "SHORT")
                                    if size > 1e-12:
                                        params = dict(symbol=sym, side="BUY", type="MARKET",
                                                      quantity=size, positionSide="SHORT")
                                        success = await safe_order_execution(cli, params, sym, "SHORT RSI EXIT (RSI48 > 50)")
                                        if success:
                                            st["short_exit_history"].append(close_price)
                                            if len(st["short_exit_history"]) > EXIT_HISTORY_MAX:
                                                st["short_exit_history"] = st["short_exit_history"][-EXIT_HISTORY_MAX:]
                                            st["in_short"] = False
                                            st["short_entry_price"] = None
                                            st["short_trough"] = None
                                            st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                            st["short_trailing_guard_id"] = None
                                            st["short_profit_lock"] = {"order_id": None, "stop_price": None}
                                            logging.info(f"{sym} SHORT RSI EXIT executed at close {close_price:.6f}")
                        continue

                    # ---------- trade ticks ----------
                    if not stype.endswith("@trade"):
                        continue

                    sym   = d["s"]
                    price = float(d["p"])
                    st    = state[sym]

                    # Update WMAs per tick
                    st["wma_short"].update(price)
                    st["wma_long"].update(price)
                    wma_short = st["wma_short"].value
                    wma_long  = st["wma_long"].value
                    st["last_price"] = price

                    # Update WMA regimes and cross timestamps (persist until flipped)
                    prev_wma24 = st["wma24_above"]
                    prev_wma4  = st["wma4_below"]
                    st["wma24_above"] = bool(price > wma_long)
                    st["wma4_below"]  = bool(price < wma_short)
                    now_ts = time.time()
                    if st["wma24_above"] != prev_wma24:
                        st["wma24_cross_ts"] = now_ts
                        logging.info(f"{sym} WMA24 regime {'ABOVE' if st['wma24_above'] else 'NOT ABOVE'} flipped at {price:.6f}")
                    if st["wma4_below"] != prev_wma4:
                        st["wma4_cross_ts"] = now_ts
                        logging.info(f"{sym} WMA4 regime {'BELOW' if st['wma4_below'] else 'NOT BELOW'} flipped at {price:.6f}")

                    # ---------------- HARD LOSS EXIT at ROE ≤ -25% ----------------
                    roe_long = pnl_percent(st["long_entry_price"], price, "LONG")
                    if st["in_long"] and (roe_long <= -25.0):
                        async with st["order_lock"]:
                            if st["in_long"]:
                                await cancel_trailing(cli, sym, st["long_trailing"]["order_id"])
                                await cancel_profit_lock(cli, sym, st["long_profit_lock"]["order_id"], "LONG")
                                size = await get_position_size(cli, sym, "LONG")
                                if size > 1e-12:
                                    params = dict(symbol=sym, side="SELL", type="MARKET",
                                                  quantity=size, positionSide="LONG")
                                    success = await safe_order_execution(cli, params, sym, "LONG HARD EXIT (ROE ≤ -25%)")
                                    if success:
                                        exit_px = st["last_price"] or price
                                        st["long_exit_history"].append(exit_px)
                                        if len(st["long_exit_history"]) > EXIT_HISTORY_MAX:
                                            st["long_exit_history"] = st["long_exit_history"][-EXIT_HISTORY_MAX:]
                                        st["in_long"] = False
                                        st["long_entry_price"] = None
                                        st["long_peak"] = None
                                        st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                        st["long_trailing_guard_id"] = None
                                        st["long_profit_lock"] = {"order_id": None, "stop_price": None}
                                        logging.info(f"{sym} LONG HARD EXIT at ROE {roe_long:.2f}%")
                        continue

                    roe_short = pnl_percent(st["short_entry_price"], price, "SHORT")
                    if st["in_short"] and (roe_short <= -25.0):
                        async with st["order_lock"]:
                            if st["in_short"]:
                                await cancel_trailing(cli, sym, st["short_trailing"]["order_id"])
                                await cancel_profit_lock(cli, sym, st["short_profit_lock"]["order_id"], "SHORT")
                                size = await get_position_size(cli, sym, "SHORT")
                                if size > 1e-12:
                                    params = dict(symbol=sym, side="BUY", type="MARKET",
                                                  quantity=size, positionSide="SHORT")
                                    success = await safe_order_execution(cli, params, sym, "SHORT HARD EXIT (ROE ≤ -25%)")
                                    if success:
                                        exit_px = st["last_price"] or price
                                        st["short_exit_history"].append(exit_px)
                                        if len(st["short_exit_history"]) > EXIT_HISTORY_MAX:
                                            st["short_exit_history"] = st["short_exit_history"][-EXIT_HISTORY_MAX:]
                                        st["in_short"] = False
                                        st["short_entry_price"] = None
                                        st["short_trough"] = None
                                        st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                        st["short_trailing_guard_id"] = None
                                        st["short_profit_lock"] = {"order_id": None, "stop_price": None}
                                        logging.info(f"{sym} SHORT HARD EXIT at ROE {roe_short:.2f}%")
                        continue

                    # ---------------- POSITIVE SIDE: PROFIT-LOCK (+40% arm, +25% stop) ----------
                    if st["in_long"]:
                        await ensure_profit_lock(cli, sym, st, price, side="LONG",
                                                 reduce_only_allowed=(not dual_side))
                    if st["in_short"]:
                        await ensure_profit_lock(cli, sym, st, price, side="SHORT",
                                                 reduce_only_allowed=(not dual_side))

                    # ---------------- POSITIVE SIDE: TRAILING at +60% ROE (0.5% cb) ----------
                    if st["in_long"]:
                        await ensure_trailing(cli, sym, st, price, side="LONG",
                                              reduce_only_allowed=(not dual_side))
                    if st["in_short"]:
                        await ensure_trailing(cli, sym, st, price, side="SHORT",
                                              reduce_only_allowed=(not dual_side))

                    # ---------------- LONG ENTRY / RE-ENTRY (24h WMA regime + RSI288>50) ------
                    if (not st["in_long"]) and (not st["long_pending"]):
                        entry_triggered = False; entry_type = ""

                        # PRICE/WMA part (unchanged re-entry rules), but using current WMA regime
                        if len(st["long_exit_history"]) > 0 and not st["wma24_above"]:
                            st["long_exit_history"] = []
                            logging.info(f"{sym} LONG reset: price {price:.4f} <= 24h WMA {wma_long:.4f}")

                        if len(st["long_exit_history"]) == 0:
                            if st["wma24_above"]:  # regime true now (price > 24h WMA)
                                entry_triggered = True; entry_type = "LONG FIRST ENTRY (WMA24 regime above)"
                        else:
                            H = max(st["long_exit_history"]) if st["long_exit_history"] else None
                            if H and (price > H > wma_long):
                                entry_triggered = True
                                entry_type = f"LONG RE-ENTRY (price {price:.4f} > exit {H:.4f} > 24h WMA {wma_long:.4f})"

                        # RSI(288) regime must be > 50 (persistent, last 5m close)
                        if entry_triggered:
                            if not (st["rsi288"] is not None and st["rsi288"] > 50.0):
                                entry_triggered = False
                                # ↓↓↓ CHANGED to DEBUG (hidden at INFO level)
                                logging.debug(f"{sym} LONG gate blocked: need RSI(288) > 50 (current={st['rsi288']})")

                        if entry_triggered:
                            async with st["order_lock"]:
                                if st["in_long"] or st["long_pending"]:
                                    continue
                                now_ts = time.time()
                                if st["long_entry_guard"] and (now_ts - st["long_entry_guard_ts"] <= ENTRY_GUARD_TTL_SEC):
                                    logging.info(f"{sym} LONG entry skipped: guard active token={st['long_entry_guard']}")
                                    continue
                                if st["long_entry_guard"] and (now_ts - st["long_entry_guard_ts"] > ENTRY_GUARD_TTL_SEC):
                                    st["long_entry_guard"] = None
                                if now_ts - st["last_long_entry_ts"] < ENTRY_COOLDOWN_SEC:
                                    logging.info(f"{sym} LONG entry skipped: cooldown"); continue
                                existing = await get_position_size(cli, sym, "LONG")
                                if existing > 1e-12:
                                    logging.info(f"{sym} LONG entry skipped: existing position {existing} > 0 (no stacking)"); continue
                                token = make_client_id("ENTRYGUARD", sym, "LONG", ENTRY_GUARD_TTL_SEC)
                                st["long_entry_guard"] = token; st["long_entry_guard_ts"] = now_ts
                                st["long_pending"] = True
                            try:
                                params = open_long(sym)
                                params["newClientOrderId"] = st["long_entry_guard"]
                                success = await safe_order_execution(cli, params, sym, entry_type)
                                if success:
                                    st["in_long"] = True
                                    st["long_entry_price"] = price
                                    st["long_peak"] = price
                                    st["long_accumulated_movement"] = 0.0
                                    st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                    st["long_profit_lock"] = {"order_id": None, "stop_price": None}
                                    st["last_long_entry_ts"] = time.time()
                                    log_entry_details(sym, entry_type, price, st["long_exit_history"], "LONG")
                                else:
                                    st["long_entry_guard"] = None; st["long_entry_guard_ts"] = 0.0
                                    logging.error(f"{sym} {entry_type} failed")
                            except Exception as e:
                                st["long_entry_guard"] = None; st["long_entry_guard_ts"] = 0.0
                                logging.error(f"{sym} {entry_type} error: {e}")
                            finally:
                                st["long_pending"] = False

                    # ---------------- SHORT ENTRY / RE-ENTRY (4h WMA regime + RSI48<50) -------
                    if (not st["in_short"]) and (not st["short_pending"]):
                        entry_triggered = False; entry_type = ""

                        # PRICE/WMA part (unchanged re-entry rules), but using current WMA regime
                        if len(st["short_exit_history"]) > 0 and not st["wma4_below"]:
                            st["short_exit_history"] = []
                            logging.info(f"{sym} SHORT reset: price {price:.4f} >= 4h WMA {wma_short:.4f}")

                        if len(st["short_exit_history"]) == 0:
                            if st["wma4_below"]:  # regime true now (price < 4h WMA)
                                entry_triggered = True; entry_type = "SHORT FIRST ENTRY (WMA4 regime below)"
                        else:
                            L = min(st["short_exit_history"]) if st["short_exit_history"] else None
                            if L and (price < L < wma_short):
                                entry_triggered = True
                                entry_type = f"SHORT RE-ENTRY (price {price:.4f} < exit {L:.4f} < 4h WMA {wma_short:.4f})"

                        # RSI(48) regime must be < 50 (persistent, last 5m close)
                        if entry_triggered:
                            if not (st["rsi48"] is not None and st["rsi48"] < 50.0):
                                entry_triggered = False
                                # ↓↓↓ CHANGED to DEBUG (hidden at INFO level)
                                logging.debug(f"{sym} SHORT gate blocked: need RSI(48) < 50 (current={st['rsi48']})")

                        if entry_triggered:
                            async with st["order_lock"]:
                                if st["in_short"] or st["short_pending"]:
                                    continue
                                now_ts = time.time()
                                if st["short_entry_guard"] and (now_ts - st["short_entry_guard_ts"] <= ENTRY_GUARD_TTL_SEC):
                                    logging.info(f"{sym} SHORT entry skipped: guard active token={st['short_entry_guard']}"); continue
                                if st["short_entry_guard"] and (now_ts - st["short_entry_guard_ts"] > ENTRY_GUARD_TTL_SEC):
                                    st["short_entry_guard"] = None
                                if now_ts - st["last_short_entry_ts"] < ENTRY_COOLDOWN_SEC:
                                    logging.info(f"{sym} SHORT entry skipped: cooldown"); continue
                                existing = await get_position_size(cli, sym, "SHORT")
                                if existing > 1e-12:
                                    logging.info(f"{sym} SHORT entry skipped: existing position {existing} > 0 (no stacking)"); continue
                                token = make_client_id("ENTRYGUARD", sym, "SHORT", ENTRY_GUARD_TTL_SEC)
                                st["short_entry_guard"] = token; st["short_entry_guard_ts"] = now_ts
                                st["short_pending"] = True
                            try:
                                params = open_short(sym)
                                params["newClientOrderId"] = st["short_entry_guard"]
                                success = await safe_order_execution(cli, params, sym, entry_type)
                                if success:
                                    st["in_short"] = True
                                    st["short_entry_price"] = price
                                    st["short_trough"] = price
                                    st["short_accumulated_movement"] = 0.0
                                    st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                    st["short_profit_lock"] = {"order_id": None, "stop_price": None}
                                    st["last_short_entry_ts"] = time.time()
                                    log_entry_details(sym, entry_type, price, st["short_exit_history"], "SHORT")
                                else:
                                    st["short_entry_guard"] = None; st["short_entry_guard_ts"] = 0.0
                                    logging.error(f"{sym} {entry_type} failed")
                            except Exception as e:
                                st["short_entry_guard"] = None; st["short_entry_guard_ts"] = 0.0
                                logging.error(f"{sym} {entry_type} error: {e}")
                            finally:
                                st["short_pending"] = False

                    # Detect auto-closure by trailing / profit-lock fill
                    now_check = time.time()
                    if now_check - st["last_pos_check"] >= POS_CHECK_INTERVAL:
                        st["last_pos_check"] = now_check
                        if st["in_long"]:
                            size = await get_position_size(cli, sym, "LONG")
                            if size <= 1e-12:
                                exit_px = st["last_price"]
                                if exit_px is not None:
                                    st["long_exit_history"].append(exit_px)
                                    if len(st["long_exit_history"]) > EXIT_HISTORY_MAX:
                                        st["long_exit_history"] = st["long_exit_history"][-EXIT_HISTORY_MAX:]
                                st["in_long"] = False
                                st["long_entry_price"] = None; st["long_peak"] = None
                                st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                st["long_trailing_guard_id"] = None
                                st["long_profit_lock"] = {"order_id": None, "stop_price": None}
                                logging.warning(f"{sym} LONG closed (stop or trailing likely filled)")
                        if st["in_short"]:
                            size = await get_position_size(cli, sym, "SHORT")
                            if size <= 1e-12:
                                exit_px = st["last_price"]
                                if exit_px is not None:
                                    st["short_exit_history"].append(exit_px)
                                    if len(st["short_exit_history"]) > EXIT_HISTORY_MAX:
                                        st["short_exit_history"] = st["short_exit_history"][-EXIT_HISTORY_MAX:]
                                st["in_short"] = False
                                st["short_entry_price"] = None; st["short_trough"] = None
                                st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                st["short_trailing_guard_id"] = None
                                st["short_profit_lock"] = {"order_id": None, "stop_price": None}
                                logging.warning(f"{sym} SHORT closed (stop or trailing likely filled)")
        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting shortly...")
            await asyncio.sleep(2.0 + random.uniform(0.0, 0.8))
            continue

# ========================= STARTUP ADOPTION ====================
async def adopt_positions(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            positions = await call_binance(cli.futures_position_information, symbol=s)
        except Exception as e:
            logging.warning(f"{s} adopt: could not fetch positions: {e}")
            continue
        st = state[s]
        for pos in positions:
            side = pos.get("positionSide", "BOTH")
            amt = float(pos.get("positionAmt", "0") or 0.0)
            entry = float(pos.get("entryPrice", "0") or 0.0)
            if side == "LONG":
                if abs(amt) > 0 and entry > 0:
                    st["in_long"] = True
                    st["long_entry_price"] = entry
                    st["long_peak"] = entry
                    st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
                    logging.info(f"{s} adopt: LONG size {amt} entry {entry}")
                else:
                    st["in_long"] = False
                    st["long_entry_price"] = None
                    st["long_peak"] = None
                    st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
            if side == "SHORT":
                if abs(amt) > 0 and entry > 0:
                    st["in_short"] = True
                    st["short_entry_price"] = entry
                    st["short_trough"] = entry
                    st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
                    logging.info(f"{s} adopt: SHORT size {amt} entry {entry}")
                else:
                    st["in_short"] = False
                    st["short_entry_price"] = None
                    st["short_trough"] = None
                    st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
        await refresh_position_cache(cli, s)

# ========================= BOOTSTRAP ===========================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API creds")
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        await run(cli)
    finally:
        try:
            await cli.close_connection()
        except Exception:
            pass

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%message)s".replace("%message", "%(message)s"),
        datefmt="%b %d %H:%M:%S"
    )

    # --------- Quiet filter: during QUIET_FOR_SEC, only show market order executions and errors ----------
    class QuietOnlyOrders(logging.Filter):
        def __init__(self, quiet_until_ts: float):
            super().__init__()
            self.quiet_until = quiet_until_ts
        def filter(self, record: logging.LogRecord) -> bool:
            # After quiet window: allow everything
            if time.time() > self.quiet_until:
                return True
            # During quiet window: allow only MARKET order executions (from safe_order_execution),
            # and explicit WARNING/ERROR messages (exits via stop/trailing are WARNING).
            msg = record.getMessage()
            if "executed - OrderID" in msg:
                return True
            if record.levelno >= logging.WARNING:
                return True
            return False

    _quiet_until = time.time() + max(QUIET_FOR_SEC, 0)
    _quiet_filter = QuietOnlyOrders(_quiet_until)
    logging.getLogger().addFilter(_quiet_filter)

    def _disable_quiet():
        try:
            logging.getLogger().removeFilter(_quiet_filter)
        except Exception:
            pass
        logging.info("Quiet period ended — normal INFO logs restored.")

    if QUIET_FOR_SEC > 0:
        threading.Timer(QUIET_FOR_SEC, _disable_quiet).start()

    asyncio.run(main())
