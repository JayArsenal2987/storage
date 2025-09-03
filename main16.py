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

# ---- STRATEGY PARAMETERS ----
BASE_QTY_PER_SIDE = float(os.getenv("BASE_QTY_PER_SIDE", "100.0"))   # 100 ADA target for LONG and SHORT
STEP_QTY          = float(os.getenv("STEP_QTY", "10.0"))             # trim 10 ADA per step on indicated side
STEP_PCT          = float(os.getenv("STEP_PCT", "0.5")) / 100.0      # 0.5% move per step
MAX_STEPS_PER_BURST = int(os.getenv("MAX_STEPS_PER_BURST", "5"))     # cap steps executed back-to-back in one check
BURST_COOLDOWN_SEC  = float(os.getenv("BURST_COOLDOWN_SEC", "2.0"))  # small cooldown to avoid thrashing

# ---- FIXED-QUANTITY MODE (legacy; unused by this strategy for entries) ----
USE_NOTIONAL_MODE = False
SYMBOLS = {
    "ADAUSDT": 10.0,   # kept for compatibility; not used for strategy sizing
}
CAP_VALUES: Dict[str, float] = {
    "ADAUSDT": 200.0,  # unused by this strategy
}

# Quiet logging: 15-min summaries + warnings/errors
PNL_SUMMARY_SEC = 900.0

# REST backoff / position cache
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "45.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# ====== MAKER-FIRST ENTRY (entries/repairs only; reductions/resets are market) ======
MAKER_FIRST          = True
MAKER_WAIT_SEC       = float(os.getenv("MAKER_WAIT_SEC", "2.0"))      # short wait for maker fills
MAKER_RETRY_TICKS    = int(os.getenv("MAKER_RETRY_TICKS", "1"))       # small price nudge if GTX rejected
MAKER_MIN_FILL_RATIO = float(os.getenv("MAKER_MIN_FILL_RATIO", "0.995"))

# ====== PRICE FEED SOURCE ======
# PRICE_SOURCE: "futures" (default), "spot", or "mark"
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "futures").lower()

def _ws_host() -> str:
    # USD-M futures default (fstream). Spot uses stream.binance.com:9443
    return "fstream.binance.com" if PRICE_SOURCE in ("futures", "mark") else "stream.binance.com:9443"

def _stream_name(sym: str) -> str:
    # use 1s mark price stream if PRICE_SOURCE == "mark", else trade stream
    return f"{sym.lower()}@markPrice@1s" if PRICE_SOURCE == "mark" else f"{sym.lower()}@trade"

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

# ========================= PRICE & QUANTITY FILTERS =============
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
        return f"{price:.8f}"  # fallback
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
    # legacy interface not used for strategy sizing
    return float(SYMBOLS[symbol])

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
    # side is "LONG" or "SHORT"
    if DUAL_SIDE:
        params["positionSide"] = side
    else:
        params.pop("positionSide", None)  # omit in one-way mode
    return params

def open_long(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY",  type="MARKET",
             quantity=quantize_qty(sym, qty))
    return _maybe_pos_side(p, "LONG")

def open_short(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET",
             quantity=quantize_qty(sym, qty))
    return _maybe_pos_side(p, "SHORT")

def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET",
             quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return _maybe_pos_side(p, "LONG")

def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET",
             quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return _maybe_pos_side(p, "SHORT")

# ========================= EXECUTION WRAPPER ===================
async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    """Executes and logs quietly. On exits, verifies available size.
       If -1106 due to reduceOnly, retry once without reduceOnly."""
    try:
        if action.startswith("CLOSE") or "EXIT" in action or "FLATTEN" in action or "TRIM" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            # fresh size to avoid false 'insufficient position' due to cache
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

# ========================= STATE ===============================
class MicroEntry(dict):
    # (legacy; not used by this strategy)
    pass

def calculate_max_slots(symbol: str) -> int:
    return 1  # not used; keep minimal compatibility

state: Dict[str, Dict[str, Any]] = {
    s: {
        "last_price": None,
        "price_buffer": deque(maxlen=5000),
        "micro_fifo": deque(),               # legacy; unused
        "max_slots": calculate_max_slots(s),
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "fifo_sync_error_count": 0,
        # realtime anchor/cooldown
        "anchor_price": None,     # last processed anchor price (rung base)
        "last_burst_ts": 0.0,     # last time we executed any step(s)
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

# ========================= STRATEGY HELPERS ====================
def _zero_tolerance(symbol: str) -> float:
    step = QUANTITY_STEP.get(symbol, Decimal("0.000001"))
    try:
        return float(step) * 0.51
    except Exception:
        return 1e-8

async def maker_add(cli: AsyncClient, symbol: str, side: str, qty: float) -> float:
    """Open/add exposure using maker-first entry."""
    qty = quantize_qty(symbol, max(0.0, qty))
    if qty <= 0:
        return 0.0
    return await maker_first_entry(cli, symbol, side, qty)

async def ensure_target_side(cli: AsyncClient, symbol: str, side: str, target_qty: float):
    """Adjust a side to exactly target_qty (add via maker-first; trim via market)."""
    current = await get_position_size_fresh(cli, symbol, side)
    tol = _zero_tolerance(symbol)
    target_qty = quantize_qty(symbol, max(0.0, target_qty))
    if current < target_qty - tol:
        await maker_add(cli, symbol, side, target_qty - current)
    elif current > target_qty + tol:
        delta = quantize_qty(symbol, current - target_qty)
        params = exit_long(symbol, delta) if side == "LONG" else exit_short(symbol, delta)
        await safe_order_execution(cli, params, symbol, f"TRIM TO TARGET {side}")

async def ensure_baseline(cli: AsyncClient, symbol: str):
    """Bring both LONG and SHORT to BASE_QTY_PER_SIDE."""
    await ensure_target_side(cli, symbol, "LONG",  BASE_QTY_PER_SIDE)
    await ensure_target_side(cli, symbol, "SHORT", BASE_QTY_PER_SIDE)

async def flatten_all(cli: AsyncClient, symbol: str) -> bool:
    st = state[symbol]
    async with st["order_lock"]:
        # cancel all, then close both sides at market
        try:
            await call_binance(cli.futures_cancel_all_open_orders, symbol=symbol)
        except Exception as e:
            logging.warning(f"{symbol} cancel_all_open failed: {e}")
        long_sz  = await get_position_size_fresh(cli, symbol, "LONG")
        short_sz = await get_position_size_fresh(cli, symbol, "SHORT")
        ok = True
        if long_sz > 0:
            ok &= await safe_order_execution(cli, exit_long(symbol, quantize_qty(symbol, long_sz)), symbol, "FLATTEN LONG")
        if short_sz > 0:
            ok &= await safe_order_execution(cli, exit_short(symbol, quantize_qty(symbol, short_sz)), symbol, "FLATTEN SHORT")
        logging.warning(f"{symbol} FLATTENED ALL")
        return ok

async def flatten_all_and_rebaseline(cli: AsyncClient, symbol: str):
    await flatten_all(cli, symbol)
    await ensure_baseline(cli, symbol)
    logging.warning(f"{symbol} RESET → REBUILT to {BASE_QTY_PER_SIDE} ADA each side")

# ========================= MAKER-FIRST HELPERS =================
async def get_best_bid_ask(cli: AsyncClient, symbol: str) -> Optional[Tuple[float, float]]:
    try:
        ob = await call_binance(cli.futures_order_book, symbol=symbol, limit=5)
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return None
        return float(bids[0][0]), float(asks[0][0])
    except Exception as e:
        logging.warning(f"{symbol} get_best_bid_ask failed: {e}")
        return None

def _tick_float(symbol: str) -> float:
    t = PRICE_TICK.get(symbol)
    return float(t) if t else 0.0001

def maker_limit_price(symbol: str, side: str, bid: float, ask: float) -> str:
    tick = _tick_float(symbol)
    if side == "LONG":  # BUY
        p = min(bid, ask - tick)
        if p <= 0:
            p = max(bid - tick, 0.0)
    else:               # SELL
        p = max(ask, bid + tick)
    q = float(quantize_price(symbol, p))
    if side == "LONG" and q >= ask:
        q = ask - tick
    if side == "SHORT" and q <= bid:
        q = bid + tick
    return quantize_price(symbol, max(q, tick))

async def cancel_order(cli: AsyncClient, symbol: str, order_id: Any):
    try:
        await call_binance(cli.futures_cancel_order, symbol=symbol, orderId=order_id)
    except Exception as e:
        logging.warning(f"{symbol} cancel order {order_id} failed: {e}")

async def maker_first_entry(cli: AsyncClient, symbol: str, side: str, qty: float) -> float:
    """Return actual executed qty (0.0 if failed)."""
    if not MAKER_FIRST:
        params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
        ok = await safe_order_execution(cli, params, symbol, f"ENTRY {side}")
        return qty if ok else 0.0

    pre_sz = await get_position_size_fresh(cli, symbol, side)
    ba = await get_best_bid_ask(cli, symbol)
    if not ba:
        params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
        ok = await safe_order_execution(cli, params, symbol, f"ENTRY {side}")
        return qty if ok else 0.0
    bid, ask = ba
    price = maker_limit_price(symbol, side, bid, ask)
    order = dict(
        symbol=symbol,
        side=("BUY" if side == "LONG" else "SELL"),
        type="LIMIT",
        timeInForce="GTX",
        quantity=quantize_qty(symbol, qty),
        price=price,
        newOrderRespType="ACK",
    )
    if DUAL_SIDE:
        order["positionSide"] = side

    oid = None
    for _ in range(2):
        try:
            res = await call_binance(cli.futures_create_order, **order)
            oid = res.get("orderId")
            break
        except Exception as e:
            msg = str(e)
            if "-5022" in msg or "GTX" in msg.upper():
                tick = _tick_float(symbol) * max(1, MAKER_RETRY_TICKS)
                price = float(price) - tick if side == "LONG" else float(price) + tick
                order["price"] = quantize_price(symbol, price)
                continue
            logging.warning(f"{symbol} GTX place failed ({msg}); falling back to MARKET")
            params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
            ok = await safe_order_execution(cli, params, symbol, f"ENTRY {side}")
            return qty if ok else 0.0

    if not oid:
        params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
        ok = await safe_order_execution(cli, params, symbol, f"ENTRY {side}")
        return qty if ok else 0.0

    await asyncio.sleep(MAKER_WAIT_SEC)

    post_sz = await get_position_size_fresh(cli, symbol, side)
    filled = max(0.0, post_sz - pre_sz)

    if filled >= qty * MAKER_MIN_FILL_RATIO:
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} ENTRY {side} filled as MAKER ~{filled:.6f}/{qty:.6f} at {order['price']}")
        return filled

    # cancel just that order (not all)
    await cancel_order(cli, symbol, oid)

    post_sz2 = await get_position_size_fresh(cli, symbol, side)
    filled2 = max(0.0, post_sz2 - pre_sz)
    remaining = max(0.0, qty - filled2)
    remaining = quantize_qty(symbol, remaining)
    if remaining <= 0:
        invalidate_position_cache(symbol)
        return filled2

    logging.warning(f"{symbol} ENTRY {side} maker underfilled {filled2:.6f}/{qty:.6f}; MARKET remaining {remaining:.6f}")
    params = open_long(symbol, remaining) if side == "LONG" else open_short(symbol, remaining)
    ok = await safe_order_execution(cli, params, symbol, f"ENTRY {side} (market remainder)")
    if not ok:
        return filled2
    post_sz3 = await get_position_size_fresh(cli, symbol, side)
    return max(0.0, post_sz3 - pre_sz)

# ========================= FEED LOOP ===========================
async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"
    suffix_trade = "@trade"
    suffix_mark  = "@markPrice@1s"
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
                    m = json.loads(raw)
                    stream = m.get("stream", "")
                    if not (stream.endswith(suffix_trade) or stream.endswith(suffix_mark)):
                        continue
                    d = m.get("data", {})
                    sym = d.get("s")
                    if not sym:
                        continue
                    price = float(d.get("p"))
                    st = state[sym]
                    st["last_price"] = price
                    st["price_buffer"].append((time.time(), price))
        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting shortly...")
            await asyncio.sleep(2.0 + random.uniform(0.0, 0.8))
            continue

# ========================= REALTIME DRIVER (STRICT 0.5% => 10 ADA) =====================
async def realtime_driver(cli: AsyncClient):
    """Strict step strategy (no aggregation):
       - Keep 100/100 baseline (hedged).
       - Each time price >= anchor*(1+STEP_PCT) -> trim SHORT by exactly STEP_QTY (10 ADA), then move anchor up by one rung.
       - Each time price <= anchor*(1-STEP_PCT) -> trim LONG  by exactly STEP_QTY (10 ADA), then move anchor down by one rung.
       - If price jumps across multiple rungs, execute multiple separate 10-ADA orders back-to-back (capped by MAX_STEPS_PER_BURST).
       - After any steps, apply a short cooldown to avoid thrashing.
       - If any side hits 0 after a step -> reset (cancel, close both) and rebuild to 100/100 immediately.
    """
    POLL_SEC = 0.25  # check trigger frequently

    while True:
        await asyncio.sleep(POLL_SEC)

        for sym in SYMBOLS:
            st = state[sym]
            price = st["last_price"]
            if price is None:
                continue

            # Initialize: ensure baseline and set anchor on first run
            if st["anchor_price"] is None:
                async with st["order_lock"]:
                    price = st["last_price"]
                    if price is None:
                        continue
                    logging.info(f"{sym} initializing baseline & anchor at price={price}")
                    await ensure_baseline(cli, sym)
                    st["anchor_price"] = price
                    st["last_burst_ts"] = 0.0
                continue

            # Cooldown guard (after a burst of one or more steps)
            if time.time() - st["last_burst_ts"] < BURST_COOLDOWN_SEC:
                continue

            async with st["order_lock"]:
                # refresh under lock
                price  = st["last_price"] or price
                anchor = st["anchor_price"]
                if not anchor or anchor <= 0:
                    st["anchor_price"] = price
                    continue

                tol_qty = _zero_tolerance(sym)
                steps_done = 0

                # Walk UP rungs: each rung -> trim SHORT by exactly STEP_QTY
                while price >= anchor * (1.0 + STEP_PCT) - 1e-12:
                    short_sz = await get_position_size_fresh(cli, sym, "SHORT")
                    amt = quantize_qty(sym, min(STEP_QTY, max(0.0, short_sz)))
                    if amt <= tol_qty:
                        break

                    logging.warning(
                        f"{sym} STEP UP -> trim SHORT by {amt} | price={price:.6f} anchor={anchor:.6f}"
                    )
                    await safe_order_execution(cli, exit_short(sym, amt), sym, "EXIT STEP SHORT (1x)")
                    steps_done += 1

                    # zero-hit check after EACH step
                    long_sz2  = await get_position_size_fresh(cli, sym, "LONG")
                    short_sz2 = await get_position_size_fresh(cli, sym, "SHORT")
                    if long_sz2 <= tol_qty or short_sz2 <= tol_qty:
                        logging.warning(f"{sym} ZERO-HIT after step -> RESET & REBUILD")
                        await flatten_all_and_rebaseline(cli, sym)
                        # re-anchor to current price after rebuild and stop this burst
                        st["anchor_price"] = state[sym]["last_price"] or price
                        break

                    # advance anchor by exactly one rung up
                    anchor = anchor * (1.0 + STEP_PCT)
                    st["anchor_price"] = anchor

                    if steps_done >= MAX_STEPS_PER_BURST:
                        break

                # If we didn't reset above, walk DOWN rungs similarly
                if steps_done < MAX_STEPS_PER_BURST and st["anchor_price"] == anchor:
                    while price <= anchor * (1.0 - STEP_PCT) + 1e-12:
                        long_sz = await get_position_size_fresh(cli, sym, "LONG")
                        amt = quantize_qty(sym, min(STEP_QTY, max(0.0, long_sz)))
                        if amt <= tol_qty:
                            break

                        logging.warning(
                            f"{sym} STEP DOWN -> trim LONG by {amt} | price={price:.6f} anchor={anchor:.6f}"
                        )
                        await safe_order_execution(cli, exit_long(sym, amt), sym, "EXIT STEP LONG (1x)")
                        steps_done += 1

                        long_sz2  = await get_position_size_fresh(cli, sym, "LONG")
                        short_sz2 = await get_position_size_fresh(cli, sym, "SHORT")
                        if long_sz2 <= tol_qty or short_sz2 <= tol_qty:
                            logging.warning(f"{sym} ZERO-HIT after step -> RESET & REBUILD")
                            await flatten_all_and_rebaseline(cli, sym)
                            st["anchor_price"] = state[sym]["last_price"] or price
                            break

                        # advance anchor by exactly one rung down
                        anchor = anchor * (1.0 - STEP_PCT)
                        st["anchor_price"] = anchor

                        if steps_done >= MAX_STEPS_PER_BURST:
                            break

                # If we executed any steps, start cooldown
                if steps_done > 0:
                    st["last_burst_ts"] = time.time()

# ========================= PNL SUMMARY =========================
async def pnl_summary_loop(cli: AsyncClient):
    while True:
        total_upnl = 0.0
        lines = []
        for sym in SYMBOLS:
            snap = await get_positions_snapshot(cli, sym)
            lp = state[sym]["last_price"]
            anchor = state[sym]["anchor_price"]
            L = snap["LONG"]; S = snap["SHORT"]
            upnl_sym = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0)
            total_upnl += upnl_sym
            lines.append(
                f"[SUMMARY15] {sym} "
                f"uPnL: L={L['uPnL']:.2f} S={S['uPnL']:.2f} USDT | "
                f"sizes: L={L['size']:.4f}@{L['entry']:.6f} "
                f"S={S['size']:.4f}@{S['entry']:.6f} | "
                f"last={lp if lp is not None else 'n/a'} | "
                f"anchor={anchor if anchor is not None else 'n/a'} | "
                f"target={BASE_QTY_PER_SIDE} step_qty={STEP_QTY} step_pct={STEP_PCT*100:.3f}%"
            )
        for line in lines:
            logging.info(line)
        logging.info(f"[SUMMARY15] TOTAL uPnL across {len(SYMBOLS)} syms: {total_upnl:.2f} USDT")
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
        logging.info("Hedge Mode detected — long & short sides will be managed independently.")
        # Set leverage per symbol
        for s in SYMBOLS:
            try:
                await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except Exception as e:
                logging.warning(f"{s} set leverage failed: {e}")

        await seed_symbol_filters(cli)

        # Start price feed first so last_price populates
        logging.info("Starting price feed...")
        feed_task = asyncio.create_task(price_feed_loop(cli))

        # Give price feed time to establish
        await asyncio.sleep(5.0)

        # Launch realtime driver & PnL loop
        driver_task = asyncio.create_task(realtime_driver(cli))
        pnl_task    = asyncio.create_task(pnl_summary_loop(cli))

        await asyncio.gather(feed_task, driver_task, pnl_task)
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

    # ---- PNL-only log filter: allow 15-min summaries + WARN/ERROR ----
    class PNLOnlyFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "[SUMMARY15]" in msg:
                return True
            if record.levelno >= logging.WARNING:
                if "executed - OrderID" in msg:
                    return False
                return True
            return False

    logging.getLogger().addFilter(PNLOnlyFilter())
    asyncio.run(main())
