#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, math, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Deque, Dict, Any, Union
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28  # safe precision

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# ---- STRATEGY PARAMETERS (kept; not used by WMA strategy except order sizing) ----
BASE_QTY_PER_SIDE   = float(os.getenv("BASE_QTY_PER_SIDE", "100.0"))
STEP_QTY            = float(os.getenv("STEP_QTY", "10.0"))
STEP_PCT            = float(os.getenv("STEP_PCT", "0.5")) / 100.0
MAX_STEPS_PER_BURST = int(os.getenv("MAX_STEPS_PER_BURST", "5"))
BURST_COOLDOWN_SEC  = float(os.getenv("BURST_COOLDOWN_SEC", "2.0"))
CROSS_CONFIRM_SEC   = float(os.getenv("CROSS_CONFIRM_SEC", "0.75"))
PRICE_GLITCH_PCT    = float(os.getenv("PRICE_GLITCH_PCT", "0.20"))

# ---- PRICE SOURCE ----
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "mark").lower()  # "mark" (recommended) or "futures"

def _ws_host() -> str:
    return "fstream.binance.com" if PRICE_SOURCE in ("futures", "mark") else "stream.binance.com:9443"

def _stream_name(sym: str) -> str:
    return f"{sym.lower()}@markPrice@1s" if PRICE_SOURCE == "mark" else f"{sym.lower()}@trade"

# ---- SYMBOLS & order sizes (requested) ----
SYMBOLS = {
    "ETHUSDT": 0.01,
    "BNBUSDT": 0.03,
    "SOLUSDT": 0.10,
    "XRPUSDT": 10.0,
    "ADAUSDT": 10.0,
}
CAP_VALUES: Dict[str, float] = { s: 0.0 for s in SYMBOLS }  # unused here

# ---- WMA Strategy config (requested) ----
TICK_WMA_PERIOD = 4 * 60 * 60        # 4 hours in seconds
WMA_ACTIVATION_THRESHOLD = 0.0005    # 0.05% distance to WMA to activate
MOVEMENT_THRESHOLD = 0.05            # 5% adverse-only (log) cumulative movement to exit

# Quiet logging summary cadence (requested 30 minutes)
PNL_SUMMARY_SEC = 1800.0

# REST backoff / position cache
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "45.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# ====== MAKER-FIRST FLAGS (left intact but not used for this WMA strategy) ======
MAKER_FIRST          = True
MAKER_WAIT_SEC       = float(os.getenv("MAKER_WAIT_SEC", "2.0"))
MAKER_RETRY_TICKS    = int(os.getenv("MAKER_RETRY_TICKS", "1"))
MAKER_MIN_FILL_RATIO = float(os.getenv("MAKER_MIN_FILL_RATIO", "0.995"))

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

def calculate_order_quantity(symbol: str, _: float) -> float:
    return float(SYMBOLS[symbol])  # requested fixed market qty per entry

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
        logging.warning(f"{symbol} refresh_position_cache failed: {e}")

async def get_position_size(cli: AsyncClient, symbol: str, side: str) -> float:
    now = time.time()
    pc = position_cache[symbol][side]
    if now - pc["ts"] <= POSITION_CACHE_TTL:
        return pc["size"]
    await refresh_position_cache(cli, symbol)
    return position_cache[symbol][side]["size"]

async def get_position_size_fresh(cli: AsyncClient, symbol: str, side: str) -> float:
    await refresh_position_cache(cli, symbol)
    return position_cache[symbol][side]["size"]

async def get_positions_snapshot(cli: AsyncClient, symbol: str) -> Dict[str, Dict[str, float]]:
    snap = {"LONG": {"size": 0.0, "entry": 0.0, "uPnL": 0.0},
            "SHORT": {"size": 0.0, "entry": 0.0, "uPnL": 0.0}}
    try:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
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
DUAL_SIDE = False  # set in main()

def _maybe_pos_side(params: dict, side: str) -> dict:
    if DUAL_SIDE:
        params["positionSide"] = side
    else:
        params.pop("positionSide", None)
    return params

def open_long(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY",  type="MARKET", quantity=quantize_qty(sym, qty))
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
    try:
        if action.startswith("CLOSE") or "EXIT" in action or "FLATTEN" in action or "TRIM" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size_fresh(cli, symbol, side)
            required_qty = float(order_params["quantity"])
            if current_pos < required_qty * 0.995:
                logging.warning(f"{symbol} {action}: Insufficient position {current_pos} < {required_qty}")
                return False

        try:
            res = await call_binance(cli.futures_create_order, **order_params)
        except Exception as e:
            msg = str(e)
            if "-1106" in msg and "reduceonly" in msg.lower() and ("reduceOnly" in order_params):
                params_wo = {k: v for k, v in order_params.items() if k != "reduceOnly"}
                logging.warning(f"{symbol} {action}: retrying without reduceOnly due to -1106")
                res = await call_binance(cli.futures_create_order, **params_wo)
            else:
                raise

        oid = res.get("orderId")
        state[symbol]["last_order_id"] = oid
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} {action} executed - OrderID: {oid}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} failed: {e}")
        return False

# ========================= TICK-BASED WMA ======================
class TickWMA:
    def __init__(self, period_seconds: int):
        self.period_seconds = period_seconds
        self.alpha = 1.0 / period_seconds
        self.value: Optional[float] = None
        self.last_update: Optional[float] = None
        self.tick_buffer = deque(maxlen=10000)

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

# ========================= STATE ===============================
state: Dict[str, Dict[str, Any]] = {
    s: {
        # price feed
        "last_price": None,
        "last_good_price": None,
        "price_buffer": deque(maxlen=5000),

        # WMA + trading flags (requested)
        "wma": TickWMA(TICK_WMA_PERIOD),
        "bot_active": False,

        "in_long": False,  "long_pending": False,
        "long_entry_price": None,
        "long_last_tick_price": None,
        "long_exit_history": [],
        "long_accumulated_movement": 0.0,

        "in_short": False, "short_pending": False,
        "short_entry_price": None,
        "short_last_tick_price": None,
        "short_exit_history": [],
        "short_accumulated_movement": 0.0,

        # misc
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
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
                    logging.info(f"{sym} tickSize={tick} decimals={PRICE_DECIMALS[sym]}")
            qf = next((f for f in filters if f.get("filterType") == "LOT_SIZE"), None)
            if qf:
                step = qf.get("stepSize")
                if step:
                    QUANTITY_STEP[sym] = _dec(step)
                    QUANTITY_DECIMALS[sym] = _decimals_from_tick(step)
                    logging.info(f"{sym} stepSize={step} qty_decimals={QUANTITY_DECIMALS[sym]}")
    except Exception as e:
        logging.warning(f"seed_symbol_filters failed: {e}")

# ========================= PRICE FEED (mark/trade) =============
def _parse_price_from_event(d: dict) -> Optional[float]:
    p_str = d.get("p") or d.get("c") or d.get("ap") or d.get("a")
    if p_str is None:
        return None
    try:
        p = float(p_str)
    except Exception:
        return None
    return p

async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"
    suffix_trade = "@trade"
    suffix_mark  = "@markPrice@1s"

    logging.info(f"Price feed initialized using: {'Mark Price (1s)' if PRICE_SOURCE=='mark' else 'Trade Stream'}")

    while True:
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=1000
            ) as ws:
                async for raw in ws:
                    m = json.loads(raw)
                    stream = m.get("stream", "")
                    if not (stream.endswith(suffix_trade) or stream.endswith(suffix_mark)):
                        continue
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
                        # obvious glitch; skip
                        continue

                    # Update price + WMA on every good tick
                    st["last_price"] = p
                    st["last_good_price"] = p
                    st["price_buffer"].append((time.time(), p))
                    st["wma"].update(p)

        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting shortly...")
            await asyncio.sleep(2.0 + random.uniform(0.0, 0.8))
            continue

# ========================= WMA SEED (1m klines) =================
async def seed_tick_wma(cli: AsyncClient):
    """Initialize WMA per symbol with recent 1-minute closes (4h)."""
    for s in SYMBOLS:
        closes = []
        try:
            kl = await call_binance(cli.futures_klines, symbol=s, interval="1m", limit=240)
        except Exception:
            kl = await call_binance(cli.get_klines, symbol=s, interval="1m", limit=240)
        try:
            closes = [float(k[4]) for k in kl]
        except Exception:
            closes = []
        if not closes:
            logging.warning(f"{s} WMA seed: no klines")
            continue
        wma = state[s]["wma"]
        for c in closes:
            wma.update(c)
        if wma.value is not None:
            logging.info(f"{s} Tick WMA initialized: {wma.value:.6f} (4h)")

# ========================= WMA LOGIC HELPERS ===================
def get_target_exit_price(exit_history: list, is_long: bool) -> Optional[float]:
    if not exit_history:
        return None
    return max(exit_history) if is_long else min(exit_history)

def log_entry_details(symbol: str, entry_type: str, price: float, exit_history: list, direction: str):
    history_str = f"[{', '.join([f'${p:.4f}' for p in exit_history])}]" if exit_history else "[]"
    logging.info(f"{symbol} {entry_type} @ ${price:.4f}")
    logging.info(f"{symbol} {direction} Exit History: {history_str}")

def log_exit_details(symbol: str, direction: str, entry_price: float, exit_price: float, accumulated_movement: float, exit_number: int):
    if direction == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
    pnl_sign = "+" if pnl_pct >= 0 else ""
    logging.info(f"{symbol} {direction} EXIT @ ${exit_price:.4f}")
    logging.info(f"{symbol} Accumulated Movement: {accumulated_movement:.2f}% (threshold: {MOVEMENT_THRESHOLD*100:.1f}%)")
    logging.info(f"{symbol} Trade PNL: {pnl_sign}{pnl_pct:.2f}% [Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f}]")
    if exit_number > 0:
        logging.info(f"{symbol} Exit recorded (#{exit_number})")

def update_cumulative_movement(st: dict, current_price: float, position_type: str):
    """
    Adverse-only log-return accumulation:
      - LONG: only count down ticks (current < prev)
      - SHORT: only count up ticks (current > prev)
    Adds abs(log(current/prev)) to the side's accumulator.
    """
    if position_type == "long":
        last_tick_key = "long_last_tick_price"
        accum_key     = "long_accumulated_movement"
    else:
        last_tick_key = "short_last_tick_price"
        accum_key     = "short_accumulated_movement"

    prev = st[last_tick_key]
    if prev is not None and prev > 0:
        delta_log = math.log(current_price / prev)
        is_adverse = (delta_log < 0.0) if (position_type == "long") else (delta_log > 0.0)
        if is_adverse:
            st[accum_key] += abs(delta_log)
    st[last_tick_key] = current_price

# ========================= WMA TRADING DRIVER ==================
async def wma_driver(cli: AsyncClient):
    POLL_SEC = 0.10  # lightweight poll; price+WMA updated by feed

    while True:
        await asyncio.sleep(POLL_SEC)
        now = time.time()

        for sym in SYMBOLS:
            st = state[sym]
            price = st["last_price"]
            wma   = st["wma"].value

            if price is None or wma is None or wma <= 0:
                continue

            # ── One-time activation when price is near WMA ──
            if not st["bot_active"]:
                wma_diff_pct = abs(price - wma) / wma
                if wma_diff_pct <= WMA_ACTIVATION_THRESHOLD:
                    st["bot_active"] = True
                    logging.info(f"{sym} BOT ACTIVATED - price ${price:.4f} within {WMA_ACTIVATION_THRESHOLD*100:.2f}% of WMA ${wma:.4f}")
                else:
                    continue  # wait until activation

            # ───────────────── LONG ENTRY ─────────────────
            if not st["in_long"] and not st["long_pending"]:
                entry_triggered = False
                entry_type = ""
                # Smart reset if price back to/below WMA while waiting for re-entry
                if st["long_exit_history"] and price <= wma:
                    st["long_exit_history"].clear()
                    logging.info(f"{sym} LONG: Smart Reset (price {price:.4f} <= WMA {wma:.4f})")

                if not st["long_exit_history"]:
                    if price > wma:
                        entry_triggered = True
                        entry_type = "LONG FIRST ENTRY (price > WMA)"
                else:
                    highest_exit = get_target_exit_price(st["long_exit_history"], is_long=True)
                    if highest_exit and highest_exit > wma and price > highest_exit:
                        entry_triggered = True
                        entry_type = f"LONG RE-ENTRY (price {price:.4f} > highest exit {highest_exit:.4f} > WMA {wma:.4f})"

                if entry_triggered:
                    async with st["order_lock"]:
                        if st["in_long"] or st["long_pending"]:
                            continue
                        st["long_pending"] = True
                    try:
                        qty = calculate_order_quantity(sym, price)
                        if await safe_order_execution(cli, open_long(sym, qty), sym, entry_type):
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

            # ───────────────── LONG EXIT ─────────────────
            if st["in_long"] and not st["long_pending"]:
                update_cumulative_movement(st, price, "long")
                if st["long_accumulated_movement"] >= MOVEMENT_THRESHOLD:
                    async with st["order_lock"]:
                        if not st["in_long"] or st["long_pending"]:
                            continue
                        st["long_pending"] = True
                    try:
                        entry_px = st["long_entry_price"]
                        total_mv = st["long_accumulated_movement"] * 100.0
                        # Close full current long position
                        cur_sz = await get_position_size_fresh(cli, sym, "LONG")
                        if cur_sz > 0:
                            if await safe_order_execution(cli, exit_long(sym, cur_sz), sym, "LONG EXIT"):
                                st["in_long"] = False
                                st["long_exit_history"].append(price)
                                log_exit_details(sym, "LONG", entry_px, price, total_mv, len(st["long_exit_history"]))
                        # reset side state
                        st["long_entry_price"] = None
                        st["long_last_tick_price"] = None
                        st["long_accumulated_movement"] = 0.0
                    except Exception as e:
                        logging.error(f"{sym} LONG EXIT error: {e}")
                    finally:
                        st["long_pending"] = False

            # ───────────────── SHORT ENTRY ─────────────────
            if not st["in_short"] and not st["short_pending"]:
                entry_triggered = False
                entry_type = ""
                if st["short_exit_history"] and price >= wma:
                    st["short_exit_history"].clear()
                    logging.info(f"{sym} SHORT: Smart Reset (price {price:.4f} >= WMA {wma:.4f})")

                if not st["short_exit_history"]:
                    if price < wma:
                        entry_triggered = True
                        entry_type = "SHORT FIRST ENTRY (price < WMA)"
                else:
                    lowest_exit = get_target_exit_price(st["short_exit_history"], is_long=False)
                    if lowest_exit and lowest_exit < wma and price < lowest_exit:
                        entry_triggered = True
                        entry_type = f"SHORT RE-ENTRY (price {price:.4f} < lowest exit {lowest_exit:.4f} < WMA {wma:.4f})"

                if entry_triggered:
                    async with st["order_lock"]:
                        if st["in_short"] or st["short_pending"]:
                            continue
                        st["short_pending"] = True
                    try:
                        qty = calculate_order_quantity(sym, price)
                        if await safe_order_execution(cli, open_short(sym, qty), sym, entry_type):
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

            # ───────────────── SHORT EXIT ─────────────────
            if st["in_short"] and not st["short_pending"]:
                update_cumulative_movement(st, price, "short")
                if st["short_accumulated_movement"] >= MOVEMENT_THRESHOLD:
                    async with st["order_lock"]:
                        if not st["in_short"] or st["short_pending"]:
                            continue
                        st["short_pending"] = True
                    try:
                        entry_px = st["short_entry_price"]
                        total_mv = st["short_accumulated_movement"] * 100.0
                        cur_sz = await get_position_size_fresh(cli, sym, "SHORT")
                        if cur_sz > 0:
                            if await safe_order_execution(cli, exit_short(sym, cur_sz), sym, "SHORT EXIT"):
                                st["in_short"] = False
                                st["short_exit_history"].append(price)
                                log_exit_details(sym, "SHORT", entry_px, price, total_mv, len(st["short_exit_history"]))
                        st["short_entry_price"] = None
                        st["short_last_tick_price"] = None
                        st["short_accumulated_movement"] = 0.0
                    except Exception as e:
                        logging.error(f"{sym} SHORT EXIT error: {e}")
                    finally:
                        st["short_pending"] = False

# ========================= SUMMARY (WMA & movement) ============
async def pnl_summary_loop(cli: AsyncClient):
    while True:
        total_upnl = 0.0
        lines = []
        for sym in SYMBOLS:
            snap = await get_positions_snapshot(cli, sym)
            st = state[sym]
            lp = st["last_price"] or 0.0
            wma = st["wma"].value
            wma_str = f"{wma:.6f}" if wma else "n/a"
            L = snap["LONG"]; S = snap["SHORT"]
            upnl_sym = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0)
            total_upnl += upnl_sym

            highest_exit = get_target_exit_price(st["long_exit_history"], True)
            lowest_exit  = get_target_exit_price(st["short_exit_history"], False)

            lines.append(
                f"[SUMMARY30] {sym} "
                f"bot_active={st['bot_active']} | last={lp:.6f} | WMA={wma_str} | "
                f"inL={st['in_long']} movL={st['long_accumulated_movement']*100:.2f}% exitsL={len(st['long_exit_history'])} targetL={highest_exit if highest_exit else 'n/a'} | "
                f"inS={st['in_short']} movS={st['short_accumulated_movement']*100:.2f}% exitsS={len(st['short_exit_history'])} targetS={lowest_exit if lowest_exit else 'n/a'} | "
                f"uPnL L={L['uPnL']:.2f} S={S['uPnL']:.2f} (sum={upnl_sym:.2f})"
            )
        for line in lines:
            logging.info(line)
        logging.info(f"[SUMMARY30] TOTAL uPnL across {len(SYMBOLS)} syms: {total_upnl:.2f} USDT")
        await asyncio.sleep(PNL_SUMMARY_SEC)

# ========================= STARTUP =============================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API creds")

    cli = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        global DUAL_SIDE
        DUAL_SIDE = await get_dual_side(cli)
        if not DUAL_SIDE:
            logging.error("This strategy REQUIRES Hedge (dual-side) Mode on Binance Futures.")
            raise RuntimeError("Hedge Mode (dualSidePosition) is OFF. Enable it in Binance Futures settings.")
        logging.info("Hedge Mode detected — long & short sides managed independently.")

        logging.info(f"Price source configured: {PRICE_SOURCE.upper()} ({'Mark Price (smoother)' if PRICE_SOURCE == 'mark' else 'Trade Stream'})")

        for s in SYMBOLS:
            try:
                await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except Exception as e:
                logging.warning(f"{s} set leverage failed: {e}")

        await seed_symbol_filters(cli)
        await seed_tick_wma(cli)

        feed_task = asyncio.create_task(price_feed_loop(cli))
        await asyncio.sleep(3.0)  # let prices/WMA flow

        driver_task = asyncio.create_task(wma_driver(cli))
        summary_task = asyncio.create_task(pnl_summary_loop(cli))
        # NOTE: zero_guard_loop DISABLED per request

        await asyncio.gather(feed_task, driver_task, summary_task)
    finally:
        try:
            await cli.close_connection()
        except Exception:
            pass

# ========================= ENTRYPOINT ==========================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%b %d %H:%M:%S"
    )

    # ---- Summary filter: allow 30-min summaries + WARN/ERROR ----
    class PNLOnlyFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "[SUMMARY30]" in msg:
                return True
            if record.levelno >= logging.WARNING:
                if "executed - OrderID" in msg:
                    return False
                return True
            return False

    logging.getLogger().addFilter(PNLOnlyFilter())

    logging.info("=== TRADING BOT STARTUP (WMA+Adverse-Only @5%) ===")
    logging.info(f"Symbols: {', '.join(SYMBOLS.keys())}")
    logging.info(f"Activation gate: {WMA_ACTIVATION_THRESHOLD*100:.2f}% to WMA | Exit threshold: {MOVEMENT_THRESHOLD*100:.1f}% (adverse-only log)")
    logging.info("==================================================")

    asyncio.run(main())
