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
    "BNBUSDT": 0.03, "SOLUSDT": 0.10,
    "XRPUSDT": 10,   "ADAUSDT": 10,
}

# WMA periods in seconds
SHORT_WMA_PERIOD = 4 * 60 * 60      # 4h (SHORT side)
LONG_WMA_PERIOD  = 24 * 60 * 60     # 24h (LONG side)

# Legacy log-only threshold (kept for logs; not used for exits now)
MOVEMENT_THRESHOLD = 0.10

# Activation gate: price must be close to the side's WMA
WMA_ACTIVATION_THRESHOLD = 0.001    # 0.1%

# Trailing-stop config (Binance literal percent: 1.0 == 1%)
ACTIVATION_OFFSET   = float(os.getenv("ACTIVATION_OFFSET", "0.001"))   # 0.1% away from market
POS_CHECK_INTERVAL  = float(os.getenv("POS_CHECK_INTERVAL", "3.0"))

# Anti-duplicate controls
ENTRY_COOLDOWN_SEC    = float(os.getenv("ENTRY_COOLDOWN_SEC", "2.0"))
TRAILING_COOLDOWN_SEC = float(os.getenv("TRAILING_COOLDOWN_SEC", "2.0"))
ENTRY_GUARD_TTL_SEC   = float(os.getenv("ENTRY_GUARD_TTL_SEC", "5.0"))

# REST backoff / position cache
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "15.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# ---- Special ID tag for callback activation (your request) ----
TRAIL_GUARD_TAG = os.getenv("TRAIL_GUARD_TAG", "CALLBK")

# ---- NEW: Enable/disable the tightening scheduler (defaults ON) ----
USE_EXCHANGE_TRAILING = os.getenv("USE_EXCHANGE_TRAILING", "1") != "0"

# ---- NEW: Dynamic trailing mapping (PnL% → callback %) ----
# Tighten as profit grows
DYNAMIC_TRAIL_RULES = [
    (50.0,  2.0),
    (100.0, 1.5),
    (150.0, 1.0),
    (200.0, 0.5),
]
# Tighten as loss deepens (negative ROE thresholds)
DYNAMIC_TRAIL_RULES_LOSS = [
    (-50.0,  1.5),
    (-100.0, 1.0),
    (-150.0, 0.5),
]

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

# ========================= RUNTIME STATE =======================
state = {
    s: {
        # WMAs
        "wma_short": TickWMA(SHORT_WMA_PERIOD),
        "wma_long":  TickWMA(LONG_WMA_PERIOD),

        # activation flags (per side)
        "long_active": False,
        "short_active": False,

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

        # serialize trailing updates per side
        "long_trailing_lock": asyncio.Lock(),
        "short_trailing_lock": asyncio.Lock(),

        # anti-duplicate timestamps
        "last_long_entry_ts": 0.0, "last_short_entry_ts": 0.0,
        "last_long_trailing_ts": 0.0, "last_short_trailing_ts": 0.0,

        # explicit entry guard tokens
        "long_entry_guard": None,  "long_entry_guard_ts": 0.0,
        "short_entry_guard": None, "short_entry_guard_ts": 0.0,

        # record the special client IDs used for trailing orders
        "long_trailing_guard_id": None,
        "short_trailing_guard_id": None,

        "last_price": None,
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "last_pos_check": 0.0,
    }
    for s in SYMBOLS
}

# ========================= PRICE FILTER CACHE ==================
PRICE_TICK = {}        # e.g., {"XRPUSDT": Decimal("0.0001")}
PRICE_DECIMALS = {}    # e.g., {"XRPUSDT": 4}

def _dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))

def _decimals_from_tick(tick: str) -> int:
    if "." not in tick: return 0
    return len(tick.split(".")[1].rstrip("0"))

def quantize_price(symbol: str, price: float) -> str:
    """Round price to the symbol's tick size, return as string for API."""
    tick = PRICE_TICK.get(symbol)
    if not tick:
        return f"{price:.8f}"  # fallback
    p = _dec(price)
    q = (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    decs = PRICE_DECIMALS.get(symbol, 8)
    return f"{q:.{decs}f}"

# ========================= SAFE BINANCE CALL ===================
async def call_binance(fn, *args, **kwargs):
    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "-1003" in msg or "Too many requests" in msg or "IP banned" in msg:
                sleep_for = min(delay, RATE_LIMIT_MAX_SLEEP)
                logging.warning(f"Rate limit/backoff: sleeping {sleep_for:.1f}s ({msg})")
                await asyncio.sleep(sleep_for)
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
        logging.info(f"{symbol} {action} executed - OrderID: {state[symbol]['last_order_id']}")
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

# ========================= TRAILING HELPERS ====================
def pnl_percent(entry_price: Optional[float], current_price: float, side: str, leverage: float = LEVERAGE) -> float:
    if not entry_price:
        return 0.0
    if side.upper() == "LONG":
        base = (current_price - entry_price) / entry_price * 100.0
    else:
        base = (entry_price - current_price) / entry_price * 100.0
    return base * leverage  # ROE %

# ---- UPDATED: mapping now uses your dynamic rules tables ----
def _target_callback_rate(pnl_pct_roe: float) -> Optional[float]:
    """Return desired callback rate (%) from dynamic PnL rules. None = keep/remove."""
    if pnl_pct_roe >= 0:
        best = None
        for th, cb in DYNAMIC_TRAIL_RULES:
            if pnl_pct_roe >= th:
                best = cb
        return best
    else:
        best = None
        for th, cb in DYNAMIC_TRAIL_RULES_LOSS:
            if pnl_pct_roe <= th:
                best = cb
        return best

def callback_from_tiers(pnl_pct_roe: float) -> Optional[float]:
    # preserved function name; now delegates to the rules above
    return _target_callback_rate(pnl_pct_roe)

def clamp_callback(cb: float) -> float:
    return max(0.1, min(5.0, cb))

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
    bucket = int(time.time() / bucket_sec)
    return f"{prefix}-{symbol}-{side}-{bucket}"

def trailing_client_id(symbol: str, side: str) -> str:
    return f"{TRAIL_GUARD_TAG}-{symbol}-{side}-{int(time.time())}"

async def place_trailing(cli: AsyncClient, symbol: str, side: str, latest_price: float,
                         callback_rate_pct: float, reduce_only_allowed: bool) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    if latest_price is None or not math.isfinite(latest_price) or latest_price <= 0.0:
        logging.warning(f"{symbol} {side} trailing skipped: invalid latest_price={latest_price}")
        return None, None, None

    # Compute activation above/below and round to tick
    if side.upper() == "LONG":
        order_side = "SELL";  position_side = "LONG"
        raw_activation = latest_price * (1.0 + ACTIVATION_OFFSET)
    else:
        order_side = "BUY";   position_side = "SHORT"
        raw_activation = latest_price * (1.0 - ACTIVATION_OFFSET)

    activation_str = quantize_price(symbol, raw_activation)   # tick-quantized string
    cb = clamp_callback(float(callback_rate_pct))
    cid = trailing_client_id(symbol, side)

    params = dict(
        symbol=symbol,
        side=order_side,
        type="TRAILING_STOP_MARKET",
        quantity=SYMBOLS[symbol],
        positionSide=position_side,
        callbackRate=cb,                    # literal %
        activationPrice=activation_str,     # tick-aligned string
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
        # If activation would immediately trigger or still invalid, widen once & re-quantize
        if "-2021" in msg or "-1102" in msg:
            bump = ACTIVATION_OFFSET * 2.0
            if side.upper() == "LONG":
                raw_activation = latest_price * (1.0 + bump)
            else:
                raw_activation = latest_price * (1.0 - bump)
            activation_str = quantize_price(symbol, raw_activation)
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

async def ensure_trailing(cli: AsyncClient, sym: str, st: dict, latest_price: float,
                          side: str, reduce_only_allowed: bool):
    # Serialize per side to prevent stacked cancels/places
    lock = st["long_trailing_lock"] if side == "LONG" else st["short_trailing_lock"]
    async with lock:
        now = time.time()
        if side == "LONG":
            if now - st["last_long_trailing_ts"] < TRAILING_COOLDOWN_SEC:
                return
        else:
            if now - st["last_short_trailing_ts"] < TRAILING_COOLDOWN_SEC:
                return

        if side == "LONG":
            entry = st["long_entry_price"]
            roe = pnl_percent(entry, latest_price, "LONG")
            want_cb = callback_from_tiers(roe)
            have = st["long_trailing"]
        else:
            entry = st["short_entry_price"]
            roe = pnl_percent(entry, latest_price, "SHORT")
            want_cb = callback_from_tiers(roe)
            have = st["short_trailing"]

        if want_cb is None:
            if have["order_id"]:
                logging.info(f"{sym} {side} trailing remove (ROE={roe:.2f}%)")
                await cancel_trailing(cli, sym, have["order_id"])
                have.update({"order_id": None, "callback": None, "activation": None})
            return

        want_cb = clamp_callback(want_cb)

        # place or tighten
        if (have["order_id"] is None) or (have["callback"] is None) or (want_cb < have["callback"] - 1e-9):
            if have["order_id"]:
                logging.info(f"{sym} {side} trailing tighten {have['callback']}% -> {want_cb}% (ROE={roe:.2f}%)")
                await cancel_trailing(cli, sym, have["order_id"])
                await asyncio.sleep(0.05 + random.random() * 0.10)  # tiny jitter

            oid, act, cid = await place_trailing(cli, sym, side, latest_price, want_cb, reduce_only_allowed)
            if oid:
                have.update({"order_id": oid, "callback": want_cb, "activation": act})
                if side == "LONG":
                    st["long_trailing_guard_id"] = cid
                    st["last_long_trailing_ts"] = time.time()
                else:
                    st["short_trailing_guard_id"] = cid
                    st["last_short_trailing_ts"] = time.time()

# ---- NEW: separate tightening scheduler (never loosens) ----
async def dynamic_trailing_scheduler(cli: AsyncClient, reduce_only_allowed: bool):
    """
    Periodically tighten the exchange-native trailing stop based on unrealized ROE%.
    Only tightens if target callback is smaller than current; never loosens or places anew.
    """
    while True:
        try:
            if not USE_EXCHANGE_TRAILING:
                await asyncio.sleep(1.0)
                continue

            for symbol in SYMBOLS:
                st = state[symbol]
                price = st.get("last_price")
                if price is None:
                    continue

                # LONG side tighten
                if st.get("in_long") and st["long_trailing"].get("order_id"):
                    entry = st.get("long_entry_price")
                    pnl = pnl_percent(entry, price, "LONG")
                    target_cb = _target_callback_rate(pnl)
                    have_cb = st["long_trailing"].get("callback")
                    if target_cb is not None:
                        if have_cb is None or target_cb < have_cb - 1e-9:
                            async with st["long_trailing_lock"]:
                                # re-check inside lock
                                have_cb = st["long_trailing"].get("callback")
                                if st["long_trailing"].get("order_id") and (have_cb is None or target_cb < have_cb - 1e-9):
                                    await cancel_trailing(cli, symbol, st["long_trailing"]["order_id"])
                                    oid, act, cid = await place_trailing(cli, symbol, "LONG", price, target_cb, reduce_only_allowed)
                                    if oid:
                                        st["long_trailing"].update({"order_id": oid, "callback": target_cb, "activation": act})
                                        st["long_trailing_guard_id"] = cid
                                        st["last_long_trailing_ts"] = time.time()
                                        logging.info(f"{symbol} LONG tightened trailing: {have_cb}% → {target_cb}% (ROE {pnl:.2f}%)")

                # SHORT side tighten
                if st.get("in_short") and st["short_trailing"].get("order_id"):
                    entry = st.get("short_entry_price")
                    pnl = pnl_percent(entry, price, "SHORT")
                    target_cb = _target_callback_rate(pnl)
                    have_cb = st["short_trailing"].get("callback")
                    if target_cb is not None:
                        if have_cb is None or target_cb < have_cb - 1e-9:
                            async with st["short_trailing_lock"]:
                                have_cb = st["short_trailing"].get("callback")
                                if st["short_trailing"].get("order_id") and (have_cb is None or target_cb < have_cb - 1e-9):
                                    await cancel_trailing(cli, symbol, st["short_trailing"]["order_id"])
                                    oid, act, cid = await place_trailing(cli, symbol, "SHORT", price, target_cb, reduce_only_allowed)
                                    if oid:
                                        st["short_trailing"].update({"order_id": oid, "callback": target_cb, "activation": act})
                                        st["short_trailing_guard_id"] = cid
                                        st["last_short_trailing_ts"] = time.time()
                                        logging.info(f"{symbol} SHORT tightened trailing: {have_cb}% → {target_cb}% (ROE {pnl:.2f}%)")

        except Exception as e:
            logging.error(f"dynamic_trailing_scheduler error: {e}")
        await asyncio.sleep(1.0)

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

# ========================= SEED SYMBOL FILTERS =================
async def seed_symbol_filters(cli: AsyncClient):
    """Fill PRICE_TICK/PRICE_DECIMALS from exchange info."""
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
        # 24h = 1440; 4h = 240 one-minute candles
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

    await seed_symbol_filters(cli)
    await seed_wmas(cli)
    await adopt_positions(cli)

    # NEW: start tightening scheduler (non-blocking)
    asyncio.create_task(dynamic_trailing_scheduler(cli, reduce_only_allowed))

    streams = [f"{s.lower()}@trade" for s in SYMBOLS]
    url     = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    # Reconnect guard: keep websockets; auto-retry on drop
    while True:
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    m     = json.loads(raw)
                    stype = m["stream"]; d = m["data"]
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

                    # Activation gates
                    if not st["long_active"]:
                        if abs(price - wma_long) / wma_long <= WMA_ACTIVATION_THRESHOLD:
                            st["long_active"] = True
                            logging.info(f"{sym} LONG SIDE ACTIVATED: price {price:.4f} ~ 24h WMA {wma_long:.4f}")
                    if not st["short_active"]:
                        if abs(price - wma_short) / wma_short <= WMA_ACTIVATION_THRESHOLD:
                            st["short_active"] = True
                            logging.info(f"{sym} SHORT SIDE ACTIVATED: price {price:.4f} ~ 4h WMA {wma_short:.4f}")

                    # ---------------- LONG ENTRY / RE-ENTRY (24h WMA) ----------------
                    if st["long_active"] and (not st["in_long"]) and (not st["long_pending"]):
                        entry_triggered = False; entry_type = ""
                        if len(st["long_exit_history"]) > 0 and price <= wma_long:
                            st["long_exit_history"] = []
                            logging.info(f"{sym} LONG reset: price {price:.4f} <= 24h WMA {wma_long:.4f}")
                        if len(st["long_exit_history"]) == 0:
                            if price > wma_long:
                                entry_triggered = True; entry_type = "LONG FIRST ENTRY (price > 24h WMA)"
                        else:
                            H = max(st["long_exit_history"]) if st["long_exit_history"] else None
                            if H and price > H > wma_long:
                                entry_triggered = True
                                entry_type = f"LONG RE-ENTRY (price {price:.4f} > exit {H:.4f} > 24h WMA {wma_long:.4f})"
                        if entry_triggered:
                            async with st["order_lock"]:
                                if st["in_long"] or st["long_pending"]:
                                    continue
                                now = time.time()
                                if st["long_entry_guard"] and (now - st["long_entry_guard_ts"] <= ENTRY_GUARD_TTL_SEC):
                                    logging.info(f"{sym} LONG entry skipped: guard active token={st['long_entry_guard']}")
                                    continue
                                if st["long_entry_guard"] and (now - st["long_entry_guard_ts"] > ENTRY_GUARD_TTL_SEC):
                                    st["long_entry_guard"] = None
                                if now - st["last_long_entry_ts"] < ENTRY_COOLDOWN_SEC:
                                    logging.info(f"{sym} LONG entry skipped: cooldown"); continue
                                existing = await get_position_size(cli, sym, "LONG")
                                if existing >= SYMBOLS[sym] * 0.5:
                                    logging.info(f"{sym} LONG entry skipped: size already {existing}"); continue
                                token = make_client_id("ENTRYGUARD", sym, "LONG", ENTRY_GUARD_TTL_SEC)
                                st["long_entry_guard"] = token; st["long_entry_guard_ts"] = now
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

                    # ---------------- SHORT ENTRY / RE-ENTRY (4h WMA) -----------------
                    if st["short_active"] and (not st["in_short"]) and (not st["short_pending"]):
                        entry_triggered = False; entry_type = ""
                        if len(st["short_exit_history"]) > 0 and price >= wma_short:
                            st["short_exit_history"] = []
                            logging.info(f"{sym} SHORT reset: price {price:.4f} >= 4h WMA {wma_short:.4f}")
                        if len(st["short_exit_history"]) == 0:
                            if price < wma_short:
                                entry_triggered = True; entry_type = "SHORT FIRST ENTRY (price < 4h WMA)"
                        else:
                            L = min(st["short_exit_history"]) if st["short_exit_history"] else None
                            if L and price < L < wma_short:
                                entry_triggered = True
                                entry_type = f"SHORT RE-ENTRY (price {price:.4f} < exit {L:.4f} < 4h WMA {wma_short:.4f})"
                        if entry_triggered:
                            async with st["order_lock"]:
                                if st["in_short"] or st["short_pending"]:
                                    continue
                                now = time.time()
                                if st["short_entry_guard"] and (now - st["short_entry_guard_ts"] <= ENTRY_GUARD_TTL_SEC):
                                    logging.info(f"{sym} SHORT entry skipped: guard active token={st['short_entry_guard']}"); continue
                                if st["short_entry_guard"] and (now - st["short_entry_guard_ts"] > ENTRY_GUARD_TTL_SEC):
                                    st["short_entry_guard"] = None
                                if now - st["last_short_entry_ts"] < ENTRY_COOLDOWN_SEC:
                                    logging.info(f"{sym} SHORT entry skipped: cooldown"); continue
                                existing = await get_position_size(cli, sym, "SHORT")
                                if existing >= SYMBOLS[sym] * 0.5:
                                    logging.info(f"{sym} SHORT entry skipped: size already {existing}"); continue
                                token = make_client_id("ENTRYGUARD", sym, "SHORT", ENTRY_GUARD_TTL_SEC)
                                st["short_entry_guard"] = token; st["short_entry_guard_ts"] = now
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

                    # ---------------- TRAILING MANAGEMENT -----------------------------
                    if st["in_long"]:
                        await ensure_trailing(cli, sym, st, price, side="LONG", reduce_only_allowed=reduce_only_allowed)
                    if st["in_short"]:
                        await ensure_trailing(cli, sym, st, price, side="SHORT", reduce_only_allowed=reduce_only_allowed)

                    # Detect auto-closure by trailing
                    now = time.time()
                    if now - st["last_pos_check"] >= POS_CHECK_INTERVAL:
                        st["last_pos_check"] = now
                        if st["in_long"]:
                            size = await get_position_size(cli, sym, "LONG")
                            if size <= 1e-12:
                                st["in_long"] = False
                                st["long_entry_price"] = None; st["long_peak"] = None
                                st["long_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                st["long_trailing_guard_id"] = None
                                logging.info(f"{sym} LONG closed (trailing likely filled)")
                        if st["in_short"]:
                            size = await get_position_size(cli, sym, "SHORT")
                            if size <= 1e-12:
                                st["in_short"] = False
                                st["short_entry_price"] = None; st["short_trough"] = None
                                st["short_trailing"] = {"order_id": None, "callback": None, "activation": None}
                                st["short_trailing_guard_id"] = None
                                logging.info(f"{sym} SHORT closed (trailing likely filled)")
        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting shortly...")
            await asyncio.sleep(2.0)
            continue

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
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%b %d %H:%M:%S"
    )
    asyncio.run(main())
