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

# ---- FIXED-QUANTITY MODE (hard-locked) ----
USE_NOTIONAL_MODE = False
SYMBOLS = {
    "ADAUSDT": 10.0,   # 10 ADA per micro-order
}
CAP_VALUES: Dict[str, float] = {
    "ADAUSDT": 200.0,  # TOTAL cap across long+short = 200 ADA (20 slots)
}

# Quiet logging: 15-min summaries + warnings/errors
PNL_SUMMARY_SEC = 900.0

# ====== 1-hour bars (strict) ======
BAR_SECONDS = 3600  # 1 hour

# REST backoff / position cache
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "45.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

# ====== MAKER-FIRST ENTRY (entries only; exits/flatten are market) ======
MAKER_FIRST          = True
MAKER_WAIT_SEC       = float(os.getenv("MAKER_WAIT_SEC", "2.0"))      # short wait for maker fills
MAKER_RETRY_TICKS    = int(os.getenv("MAKER_RETRY_TICKS", "1"))       # small price nudge if GTX rejected
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

def open_long(sym: str, qty: float):
    return dict(symbol=sym, side="BUY",  type="MARKET",
                quantity=qty, positionSide="LONG")

def open_short(sym: str, qty: float):
    return dict(symbol=sym, side="SELL", type="MARKET",
                quantity=qty, positionSide="SHORT")

def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET",
             quantity=qty, positionSide="LONG")
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return p

def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET",
             quantity=qty, positionSide="SHORT")
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return p

# ========================= EXECUTION WRAPPER ===================
async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    """Executes and logs quietly. On exits, verifies available size.
       If -1106 due to reduceOnly, retry once without reduceOnly."""
    try:
        if action.startswith("CLOSE") or "EXIT" in action or "FLATTEN" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size(cli, symbol, side)
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
    # fields: side ('LONG'|'SHORT'), qty, ts, price
    pass

def calculate_max_slots(symbol: str) -> int:
    micro_qty = SYMBOLS[symbol]
    total_cap = CAP_VALUES[symbol]
    return max(1, int(total_cap / micro_qty))  # 200/10 = 20 slots

state: Dict[str, Dict[str, Any]] = {
    s: {
        "last_price": None,
        "price_buffer": deque(maxlen=5000),  # (ts, price) feed buffer
        "micro_fifo": deque(),               # FIFO across BOTH sides
        "max_slots": calculate_max_slots(s),
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "fifo_sync_error_count": 0,
        # --- 1h bar manager ---
        "bar_open": None,           # price at bar start
        "bar_start": None,          # epoch (aligned hour)
        "bars_in_cycle": 0,         # 0..20 (entries expected on 1..20)
        "cycle_start_ts": None,     # when cycle started (first bar close that placed an order)
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

# ========================= ADOPT POSITIONS (on restart) ========
async def adopt_positions(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            await refresh_position_cache(cli, s)
            long_sz  = await get_position_size(cli, s, "LONG")
            short_sz = await get_position_size(cli, s, "SHORT")
            st       = state[s]
            max_slots = st["max_slots"]
            lp = st["last_price"]
            if lp is None:
                logging.warning(f"{s} adopt: No price data yet, skipping")
                continue
            if len(st["micro_fifo"]) > 0:
                continue
            micro_qty = SYMBOLS[s]
            n_long  = int(long_sz  / micro_qty)
            n_short = int(short_sz / micro_qty)
            total = min(max_slots, n_long + n_short)
            st["micro_fifo"].clear()
            now = time.time()
            # Backfill timestamps one per hour so "oldest >= 21h" guard can trigger if needed
            start_ts = now - total * BAR_SECONDS
            for i in range(n_long):
                st["micro_fifo"].append(MicroEntry(side="LONG", qty=micro_qty,
                                                   ts=start_ts + i * BAR_SECONDS, price=lp))
            for i in range(n_short):
                st["micro_fifo"].append(MicroEntry(side="SHORT", qty=micro_qty,
                                                   ts=start_ts + (n_long + i) * BAR_SECONDS, price=lp))
            logging.info(f"{s} adopt: fifo={len(st['micro_fifo'])}/{max_slots} "
                         f"(L slots={n_long}, S slots={n_short}) fixed={SYMBOLS[s]}")
        except Exception as e:
            logging.warning(f"{s} adopt: could not adopt positions: {e}")

# ========================= FIFO SYNC ===========================
async def verify_fifo_sync(cli: AsyncClient, symbol: str) -> bool:
    try:
        st = state[symbol]
        actual_long = await get_position_size(cli, symbol, "LONG")
        actual_short = await get_position_size(cli, symbol, "SHORT")
        micro_qty = SYMBOLS[symbol]
        fifo_long_qty = sum(e["qty"] for e in st["micro_fifo"] if e["side"] == "LONG")
        fifo_short_qty = sum(e["qty"] for e in st["micro_fifo"] if e["side"] == "SHORT")
        tolerance = micro_qty * 0.1
        long_sync = abs(fifo_long_qty - actual_long) <= tolerance
        short_sync = abs(fifo_short_qty - actual_short) <= tolerance
        sync_ok = long_sync and short_sync
        if not sync_ok:
            st["fifo_sync_error_count"] += 1
            logging.error(f"{symbol} FIFO DESYNC #{st['fifo_sync_error_count']}: "
                          f"FIFO(L:{fifo_long_qty:.2f} S:{fifo_short_qty:.2f}) vs "
                          f"ACTUAL(L:{actual_long:.2f} S:{actual_short:.2f})")
        else:
            st["fifo_sync_error_count"] = 0
        return sync_ok
    except Exception as e:
        logging.error(f"{symbol} verify_fifo_sync failed: {e}")
        return False

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

async def cancel_all_open(cli: AsyncClient, symbol: str):
    try:
        await call_binance(cli.futures_cancel_all_open_orders, symbol=symbol)
    except Exception as e:
        logging.warning(f"{symbol} cancel_all_open failed: {e}")

async def maker_first_entry(cli: AsyncClient, symbol: str, side: str, qty: float) -> float:
    """Return actual executed qty (0.0 if failed)."""
    if not MAKER_FIRST:
        params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
        ok = await safe_order_execution(cli, params, symbol, f"FIFO ENTRY {side}")
        return qty if ok else 0.0

    pre_sz = await get_position_size_fresh(cli, symbol, side)
    ba = await get_best_bid_ask(cli, symbol)
    if not ba:
        params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
        ok = await safe_order_execution(cli, params, symbol, f"FIFO ENTRY {side}")
        return qty if ok else 0.0
    bid, ask = ba
    price = maker_limit_price(symbol, side, bid, ask)
    order = dict(
        symbol=symbol,
        side=("BUY" if side == "LONG" else "SELL"),
        type="LIMIT",
        timeInForce="GTX",
        positionSide=side,
        quantity=quantize_qty(symbol, qty),
        price=price,
        newOrderRespType="ACK",
    )
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
            ok = await safe_order_execution(cli, params, symbol, f"FIFO ENTRY {side}")
            return qty if ok else 0.0

    if not oid:
        params = open_long(symbol, qty) if side == "LONG" else open_short(symbol, qty)
        ok = await safe_order_execution(cli, params, symbol, f"FIFO ENTRY {side}")
        return qty if ok else 0.0

    await asyncio.sleep(MAKER_WAIT_SEC)

    post_sz = await get_position_size_fresh(cli, symbol, side)
    filled = max(0.0, post_sz - pre_sz)

    if filled >= qty * MAKER_MIN_FILL_RATIO:
        invalidate_position_cache(symbol)
        logging.warning(f"{symbol} ENTRY {side} filled as MAKER ~{filled:.6f}/{qty:.6f} at {order['price']}")
        return filled

    await cancel_all_open(cli, symbol)  # be safe if multiple were posted
    post_sz2 = await get_position_size_fresh(cli, symbol, side)
    filled2 = max(0.0, post_sz2 - pre_sz)
    remaining = max(0.0, qty - filled2)
    remaining = quantize_qty(symbol, remaining)
    if remaining <= 0:
        invalidate_position_cache(symbol)
        return filled2

    logging.warning(f"{symbol} ENTRY {side} maker underfilled {filled2:.6f}/{qty:.6f}; MARKET remaining {remaining:.6f}")
    params = open_long(symbol, remaining) if side == "LONG" else open_short(symbol, remaining)
    ok = await safe_order_execution(cli, params, symbol, f"FIFO ENTRY {side} (market remainder)")
    if not ok:
        return filled2
    post_sz3 = await get_position_size_fresh(cli, symbol, side)
    return max(0.0, post_sz3 - pre_sz)

# ========================= FLATTEN-ALL (hour 21) ===============
async def flatten_all(cli: AsyncClient, symbol: str) -> bool:
    st = state[symbol]
    async with st["order_lock"]:
        await cancel_all_open(cli, symbol)
        long_sz  = await get_position_size_fresh(cli, symbol, "LONG")
        short_sz = await get_position_size_fresh(cli, symbol, "SHORT")
        ok = True
        if long_sz > 0:
            ok &= await safe_order_execution(cli, exit_long(symbol, quantize_qty(symbol, long_sz)), symbol, "FLATTEN LONG")
        if short_sz > 0:
            ok &= await safe_order_execution(cli, exit_short(symbol, quantize_qty(symbol, short_sz)), symbol, "FLATTEN SHORT")
        st["micro_fifo"].clear()
        st["bars_in_cycle"] = 0
        st["cycle_start_ts"] = time.time()
        logging.warning(f"{symbol} FLATTENED ALL — cycle reset")
        return ok

# ========================= FEEDS & ENGINES =====================
async def price_feed_loop(cli: AsyncClient):
    streams = [f"{s.lower()}@trade" for s in SYMBOLS]
    url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
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
                    if not m.get("stream", "").endswith("@trade"):
                        continue
                    d = m["data"]
                    sym = d["s"]
                    price = float(d["p"])
                    st = state[sym]
                    st["last_price"] = price
                    st["price_buffer"].append((time.time(), price))
        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting shortly...")
            await asyncio.sleep(2.0 + random.uniform(0.0, 0.8))
            continue

async def try_entry_with_fifo(cli: AsyncClient, sym: str, side_to_open: Optional[str]):
    """Cap-aware entry with opposite-first eviction; rotate if all same side."""
    if side_to_open is None:
        return
    st = state[sym]
    max_slots = st["max_slots"]
    micro_qty = SYMBOLS[sym]
    now_price = st["last_price"] or 0.0

    async with st["order_lock"]:
        fifo = st["micro_fifo"]

        # full? try opposite-first eviction
        if len(fifo) >= max_slots:
            # find oldest opposite
            opp = "SHORT" if side_to_open == "LONG" else "LONG"
            idx = None
            for i, e in enumerate(fifo):
                if e["side"] == opp:
                    idx = i
                    break
            if idx is not None:
                # exit that specific slot
                victim = fifo[idx]
                qty = victim["qty"]
                if victim["side"] == "LONG":
                    ok = await safe_order_execution(cli, exit_long(sym, qty), sym, "FIFO EXIT LONG")
                else:
                    ok = await safe_order_execution(cli, exit_short(sym, qty), sym, "FIFO EXIT SHORT")
                if not ok:
                    logging.error(f"{sym} FIFO exit failed - keeping queue intact")
                    return
                # remove victim and then proceed to entry
                del fifo[idx]
            else:
                # no opposite -> rotate oldest, no orders
                oldest = fifo.popleft()
                fifo.append(MicroEntry(side=oldest["side"], qty=oldest["qty"], ts=time.time(), price=now_price))
                return

        # capacity available -> place one entry
        want_qty = calculate_order_quantity(sym, now_price)
        executed = await maker_first_entry(cli, sym, side_to_open, want_qty)
        executed = quantize_qty(sym, executed)
        if executed > 0:
            fifo.append(MicroEntry(side=side_to_open, qty=executed, ts=time.time(), price=now_price))
        else:
            logging.error(f"{sym} FIFO entry failed - no size executed")

async def hourly_driver(cli: AsyncClient):
    """Strict 1-hour bar close logic with hour-21 flatten-all + continuous cycles."""
    # align to top-of-hour boundary
    def next_hour_t(now=None):
        if now is None: now = time.time()
        return (int(now // BAR_SECONDS) + 1) * BAR_SECONDS

    # initialize bar_open at first hour close
    while True:
        # wait until next hour boundary
        await asyncio.sleep(max(0.0, next_hour_t() - time.time()) + 0.05)

        for sym in SYMBOLS:
            st = state[sym]
            close_price = st["last_price"]
            if close_price is None:
                continue

            # set bar_open on first pass and start tracking from next bar
            if st["bar_open"] is None:
                st["bar_open"] = close_price
                st["bar_start"] = int(time.time() // BAR_SECONDS) * BAR_SECONDS
                logging.info(f"{sym} init bar_open={st['bar_open']}")
                continue

            # Determine bar direction by close - open
            bar_open = st["bar_open"]
            direction = "LONG" if close_price > bar_open else ("SHORT" if close_price < bar_open else None)

            # Update guard for oldest>=21h (handles restarts)
            oldest_ts = st["micro_fifo"][0]["ts"] if st["micro_fifo"] else None
            oldest_age_ok = oldest_ts is not None and (time.time() - oldest_ts >= 21 * BAR_SECONDS)

            # Increment bars_in_cycle
            st["bars_in_cycle"] = (st["bars_in_cycle"] or 0) + 1
            if st["cycle_start_ts"] is None:
                st["cycle_start_ts"] = time.time()

            # Hour-21 flatten trigger OR oldest >= 21h
            if st["bars_in_cycle"] >= 21 or oldest_age_ok:
                # 1) flatten all
                await flatten_all(cli, sym)
                # 2) immediate re-entry based on THIS just-closed bar
                if direction is not None:
                    await try_entry_with_fifo(cli, sym, direction)
                    state[sym]["bars_in_cycle"] = 1  # first order of new cycle
                else:
                    state[sym]["bars_in_cycle"] = 0  # flat bar, start new cycle with zero until next bar
            else:
                # Normal hourly entry (one per hour)
                await try_entry_with_fifo(cli, sym, direction)

            # prepare next bar
            st["bar_open"]  = close_price
            st["bar_start"] = int(time.time() // BAR_SECONDS) * BAR_SECONDS

            # periodic sync check
            if (st["bars_in_cycle"] % 5) == 0:
                await verify_fifo_sync(cli, sym)

async def pnl_summary_loop(cli: AsyncClient):
    while True:
        total_upnl = 0.0
        lines = []
        for sym in SYMBOLS:
            snap = await get_positions_snapshot(cli, sym)
            lp = state[sym]["last_price"]
            L = snap["LONG"]; S = snap["SHORT"]
            upnl_sym = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0)
            total_upnl += upnl_sym
            st = state[sym]
            fifo_long_qty = sum(e["qty"] for e in st["micro_fifo"] if e["side"] == "LONG")
            fifo_short_qty = sum(e["qty"] for e in st["micro_fifo"] if e["side"] == "SHORT")
            lines.append(
                f"[SUMMARY15] {sym} "
                f"uPnL: L={L['uPnL']:.2f} S={S['uPnL']:.2f} USDT | "
                f"sizes: L={L['size']:.4f}@{L['entry']:.6f} "
                f"S={S['size']:.4f}@{S['entry']:.6f} | "
                f"last={lp if lp is not None else 'n/a'} | "
                f"fifo={len(st['micro_fifo'])}/{st['max_slots']} "
                f"(L:{fifo_long_qty:.1f} S:{fifo_short_qty:.1f}) fixed={SYMBOLS[sym]} | "
                f"errors={st.get('fifo_sync_error_count',0)} | bars_in_cycle={st.get('bars_in_cycle',0)}"
            )
        for line in lines:
            logging.info(line)
        logging.info(f"[SUMMARY15] TOTAL uPnL across {len(SYMBOLS)} syms: {total_upnl:.2f} USDT [MODE: FIXED]")
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
        if DUAL_SIDE:
            logging.info("Hedge Mode detected — exits will omit reduceOnly (avoid -1106).")
        else:
            logging.info("One-way mode detected — exits will include reduceOnly for safety.")

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

        await adopt_positions(cli)

        # Launch remaining tasks
        driver_task = asyncio.create_task(hourly_driver(cli))
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
