#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, random, contextlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Deque, Dict, Any
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
MAKER_WAIT_SEC       = float(os.getenv("MAKER_WAIT_SEC", "2.0"))
MAKER_RETRY_TICKS    = int(os.getenv("MAKER_RETRY_TICKS", "1"))
MAKER_MIN_FILL_RATIO = float(os.getenv("MAKER_MIN_FILL_RATIO", "0.995"))

# Per-micro trailing stop (0.5%) — attached EXACTLY +1 hour after each micro entry
TRAIL_CB_RATE = 0.5  # percent

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
    """Executes orders safely. On exits, trims quantity to available; retries -1106 without reduceOnly."""
    try:
        if action.startswith("CLOSE") or "EXIT" in action or "FLATTEN" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size_fresh(cli, symbol, side)
            required_qty = float(order_params["quantity"])
            if current_pos < required_qty * 0.995:
                if current_pos > 0:
                    order_params = dict(order_params, quantity=quantize_qty(symbol, current_pos))
                else:
                    logging.warning(f"{symbol} {action}: position already flat; skipping")
                    return True

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
    # plus trailing schedule/ids:
    #   trail_due_ts, trail_attached (bool), trail_cid (str|None), trail_oid (int|None)
    pass

def calculate_max_slots(symbol: str) -> int:
    micro_qty = SYMBOLS[symbol]
    total_cap = CAP_VALUES[symbol]
    return max(1, int(total_cap / micro_qty))  # 200/10 = 20 slots

state: Dict[str, Dict[str, Any]] = {
    s: {
        "last_price": None,
        "price_buffer": deque(maxlen=5000),
        "micro_fifo": deque(),               # both LONG and SHORT entries
        "max_slots": calculate_max_slots(s),
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "fifo_sync_error_count": 0,
        # --- 1h bar manager ---
        "bar_open": None,
        "bar_start": None,
        "bars_in_cycle": 0,
        "cycle_start_ts": None,
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
def _next_hour_t(t: float) -> float:
    return (int(t // BAR_SECONDS) + 1) * BAR_SECONDS

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
            # Backfill timestamps one per hour; schedule trailing at +1h from each ts
            start_ts = now - total * BAR_SECONDS
            for i in range(n_long):
                ts = start_ts + i * BAR_SECONDS
                st["micro_fifo"].append(MicroEntry(
                    side="LONG", qty=micro_qty, ts=ts, price=lp,
                    trail_due_ts=ts + BAR_SECONDS, trail_attached=False, trail_cid=None, trail_oid=None
                ))
            for i in range(n_short):
                ts = start_ts + (n_long + i) * BAR_SECONDS
                st["micro_fifo"].append(MicroEntry(
                    side="SHORT", qty=micro_qty, ts=ts, price=lp,
                    trail_due_ts=ts + BAR_SECONDS, trail_attached=False, trail_cid=None, trail_oid=None
                ))
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

# Cancel specific order by orderId (prevents nuking other trailings)
async def cancel_order_by_id(cli: AsyncClient, symbol: str, order_id: Optional[int]):
    if not order_id:
        return
    try:
        await call_binance(cli.futures_cancel_order, symbol=symbol, orderId=order_id)
    except Exception as e:
        logging.info(f"{symbol} cancel orderId={order_id} ignored: {e}")

# Cancel by client id (used for per-micro trailing when exiting aged)
async def cancel_order_by_cid(cli: AsyncClient, symbol: str, cid: Optional[str]):
    if not cid:
        return
    try:
        await call_binance(cli.futures_cancel_order, symbol=symbol, origClientOrderId=cid)
        logging.info(f"{symbol} canceled order cid={cid}")
    except Exception as e:
        logging.warning(f"{symbol} cancel cid={cid} ignored: {e}")

# ============== attach per-micro trailing stop (returns CID & OID) ===
async def attach_trailing_stop(cli: AsyncClient, symbol: str, position_side: str,
                               qty: float, callback_rate_pct: float):
    """Create a TRAILING_STOP_MARKET for exactly `qty` (10 ADA) and return (cid, oid)."""
    try:
        exit_side = "SELL" if position_side == "LONG" else "BUY"
        qty_q = quantize_qty(symbol, qty)  # 10.0 ADA

        # Short, unique client ID: always < 36 chars
        short_side = "L" if position_side == "LONG" else "S"
        cid = f"TS{symbol[:3]}{short_side}{int(time.time()*1000)}{random.randint(0,999):03d}"

        params = dict(
            symbol=symbol,
            side=exit_side,
            type="TRAILING_STOP_MARKET",
            positionSide=position_side,
            quantity=qty_q,                          # exactly this micro (10 ADA)
            callbackRate=str(callback_rate_pct),     # "0.5"
            newClientOrderId=cid,                    # safe length, unique
            newOrderRespType="RESULT"                # get orderId back
        )
        if not DUAL_SIDE:
            params["reduceOnly"] = True              # cannot close > qty

        res = await call_binance(cli.futures_create_order, **params)
        oid = (res or {}).get("orderId")
        logging.info(f"{symbol} attach trailing {callback_rate_pct}% ({position_side}) qty={qty_q} cid={cid} oid={oid}")
        return cid, oid
    except Exception as e:
        logging.warning(f"{symbol} attach_trailing_stop failed ({position_side}): {e}")
        return None, None

# ========================= ENTRY HELPERS =======================
async def maker_first_entry(cli: AsyncClient, symbol: str, side: str, qty: float) -> float:
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

    await cancel_order_by_id(cli, symbol, oid)  # cancel just this GTX order
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

# ========================= FLATTEN-ALL =========================
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

# ========= EXIT JUST ONE MICRO-ENTRY (oldest/aged)  ============
async def exit_micro_entry(cli: AsyncClient, symbol: str, entry: MicroEntry) -> None:
    # Cancel its trailing (if still open) — target exactly this order (OID first, CID fallback)
    oid = entry.get("trail_oid")
    cid = entry.get("trail_cid")
    if oid:
        await cancel_order_by_id(cli, symbol, oid)
    elif cid:
        await cancel_order_by_cid(cli, symbol, cid)

    qty = quantize_qty(symbol, float(entry["qty"]))
    if qty <= 0:
        return
    side = entry["side"]
    # Ensure we don't send more than available (if trailing already filled)
    avail = await get_position_size_fresh(cli, symbol, side)
    qty = quantize_qty(symbol, min(qty, avail))
    if qty <= 0:
        return
    if side == "LONG":
        await safe_order_execution(cli, exit_long(symbol, qty), symbol, "EXIT MICRO LONG (aged>21h)")
    else:
        await safe_order_execution(cli, exit_short(symbol, qty), symbol, "EXIT MICRO SHORT (aged>21h)")

# ======== IMMEDIATE AGING PURGE SCHEDULER (>21h)  ===============
async def aging_purge_scheduler_loop(cli: AsyncClient):
    """Exit ONLY micro-entries whose age is strictly > 21 hours (no wait for top-of-hour)."""
    while True:
        now = time.time()
        next_due: Optional[float] = None

        for sym in SYMBOLS:
            st = state[sym]
            async with st["order_lock"]:
                fifo = st["micro_fifo"]
                if not fifo:
                    continue
                keep = deque()
                for e in list(fifo):
                    due = e.get("ts", 0) + 21 * BAR_SECONDS
                    if now > due:  # strictly older than 21h
                        await exit_micro_entry(cli, sym, e)
                    else:
                        keep.append(e)
                        if next_due is None or due < next_due:
                            next_due = due
                st["micro_fifo"] = keep

        if next_due is None:
            await asyncio.sleep(5.0)
        else:
            sleep_for = max(0.0, next_due - time.time())
            await asyncio.sleep(sleep_for + 0.2)

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

# ========================= NO-REVERSAL ENTRY ===================
async def try_entry_with_fifo(cli: AsyncClient, sym: str, side_to_open: Optional[str]):
    """
    Keep existing micro-orders (and their trailing schedules). Do NOT auto-close opposite side.
    Only add a new 10-ADA on the signaled side if under the total 200-ADA cap.
    Trailing for this micro is scheduled for EXACTLY +1 hour after entry time.
    """
    if side_to_open is None:
        return
    st = state[sym]
    max_slots = st["max_slots"]
    micro_qty = SYMBOLS[sym]
    now_price = st["last_price"] or 0.0

    async with st["order_lock"]:
        fifo = st["micro_fifo"]

        # CAP GUARD: if already 20 micro-orders total, do nothing
        if len(fifo) >= max_slots:
            return

        # place one 10 ADA entry on the signaled side
        want_qty = calculate_order_quantity(sym, now_price)
        executed = await maker_first_entry(cli, sym, side_to_open, want_qty)
        executed = quantize_qty(sym, executed)
        if executed > 0:
            ts_now = time.time()
            fifo.append(MicroEntry(
                side=side_to_open, qty=executed, ts=ts_now, price=now_price,
                trail_due_ts=ts_now + BAR_SECONDS,   # EXACT +1 hour after entry
                trail_attached=False, trail_cid=None, trail_oid=None
            ))
        else:
            logging.error(f"{sym} FIFO entry failed - no size executed")

# =================== TRAILING SCHEDULER (+1h per micro) ========
async def trailing_scheduler_loop(cli: AsyncClient):
    """
    Watches all micro-entries; when now >= trail_due_ts and trailing not yet attached,
    creates a per-micro TRAILING_STOP_MARKET with qty = micro’s executed qty (10).
    """
    while True:
        now = time.time()
        next_due: Optional[float] = None

        for sym in SYMBOLS:
            st = state[sym]
            async with st["order_lock"]:
                fifo = st["micro_fifo"]
                for e in fifo:
                    if e.get("trail_attached"):
                        continue
                    due = e.get("trail_due_ts")
                    if due is None:
                        continue
                    if now >= due:
                        cid, oid = await attach_trailing_stop(cli, sym, e["side"], e["qty"], TRAIL_CB_RATE)
                        if cid:
                            e["trail_attached"] = True
                            e["trail_cid"] = cid
                            e["trail_oid"] = oid
                    else:
                        if next_due is None or due < next_due:
                            next_due = due

        if next_due is None:
            await asyncio.sleep(10.0)
        else:
            sleep_for = max(0.0, next_due - time.time())
            await asyncio.sleep(sleep_for + 0.05)

# =========== REMOVE MICRO ON TRAILING FILL (listener) ==========
async def remove_micro_by_trail(symbol: str, cid: Optional[str] = None, oid: Optional[int] = None) -> None:
    """Remove exactly the FIFO entry whose 'trail_cid' or 'trail_oid' matches."""
    if symbol not in state:
        return
    st = state[symbol]
    async with st["order_lock"]:
        fifo = st["micro_fifo"]
        if not fifo:
            return
        keep = deque()
        removed = 0
        for e in list(fifo):
            match = ((cid and e.get("trail_cid") == cid) or
                     (oid is not None and e.get("trail_oid") == oid))
            if removed == 0 and match:
                removed = 1
                continue
            keep.append(e)
        st["micro_fifo"] = keep
        if removed:
            invalidate_position_cache(symbol)
            logging.warning(f"{symbol} TRAILING FILLED → removed one micro (cid={cid}, oid={oid}); fifo={len(keep)}/{st['max_slots']}")

async def futures_user_stream_keepalive(cli: AsyncClient, listen_key: str):
    """Keep the futures listen key alive."""
    try:
        while True:
            await asyncio.sleep(30 * 60)  # 30 minutes
            try:
                await call_binance(cli.futures_stream_keepalive, listenKey=listen_key)
                logging.info("UserStream keepalive OK")
            except Exception as e:
                logging.warning(f"UserStream keepalive failed: {e}")
    except asyncio.CancelledError:
        try:
            await call_binance(cli.futures_stream_close, listenKey=listen_key)
        except Exception:
            pass
        raise

async def futures_user_stream_loop(cli: AsyncClient):
    """
    Listen to Futures User Data Stream; when a TRAILING_STOP_MARKET is FILLED,
    remove the matching micro from FIFO immediately.
    """
    while True:
        try:
            res = await call_binance(cli.futures_stream_get_listen_key)
            listen_key = (res.get("listenKey") if isinstance(res, dict) else res) or ""
            if not listen_key:
                logging.warning("Failed to get futures listenKey; retrying...")
                await asyncio.sleep(5.0)
                continue

            url = f"wss://fstream.binance.com/ws/{listen_key}"
            ka_task = asyncio.create_task(futures_user_stream_keepalive(cli, listen_key))

            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=1000
            ) as ws:
                logging.info("UserStream connected")
                async for raw in ws:
                    try:
                        evt = json.loads(raw)
                    except Exception:
                        continue
                    if evt.get("e") != "ORDER_TRADE_UPDATE":
                        continue
                    o = evt.get("o", {})
                    sym = o.get("s")
                    status = o.get("X")          # NEW status
                    order_type = o.get("ot")     # order type
                    cid = o.get("c")             # client order id
                    oid = o.get("i")             # exchange order id (int)
                    if sym in state and status == "FILLED" and order_type == "TRAILING_STOP_MARKET":
                        await remove_micro_by_trail(sym, cid=cid, oid=oid)

            ka_task.cancel()
            with contextlib.suppress(Exception):
                await ka_task
        except Exception as e:
            logging.warning(f"UserStream dropped: {e}. Reconnecting soon...")
            await asyncio.sleep(5.0 + random.uniform(0.0, 0.8))
            continue

# ========================= HOURLY DRIVER =======================
async def hourly_driver(cli: AsyncClient):
    def next_hour_t(now=None):
        if now is None: now = time.time()
        return (int(now // BAR_SECONDS) + 1) * BAR_SECONDS

    while True:
        # wait until the next hour boundary
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

            # Increment bars_in_cycle (for 21h checkpoint)
            st["bars_in_cycle"] = (st["bars_in_cycle"] or 0) + 1
            if st["cycle_start_ts"] is None:
                st["cycle_start_ts"] = time.time()

            # ---- CAP-CONDITIONED HOUR-21 FLATTEN ----
            fifo_total_qty = sum(e["qty"] for e in st["micro_fifo"]) if st["micro_fifo"] else 0.0
            cap_reached = fifo_total_qty >= CAP_VALUES[sym] - 1e-9

            if (st["bars_in_cycle"] >= 21 and cap_reached):
                # Only if 200 ADA reached, exit everything
                await flatten_all(cli, sym)
                # Immediate re-entry based on the just-closed bar (starts new cycle)
                if direction is not None:
                    await try_entry_with_fifo(cli, sym, direction)
                    state[sym]["bars_in_cycle"] = 1
                else:
                    state[sym]["bars_in_cycle"] = 0
            else:
                # Normal hourly entry (one per hour, if under 200 ADA total)
                await try_entry_with_fifo(cli, sym, direction)

            # prepare next bar
            st["bar_open"]  = close_price
            st["bar_start"] = int(time.time() // BAR_SECONDS) * BAR_SECONDS

            # periodic sync check
            if (st["bars_in_cycle"] % 5) == 0:
                await verify_fifo_sync(cli, sym)

# ========================= PNL SUMMARY =========================
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
            logging.info("Hedge Mode detected — trailing/exit orders may omit reduceOnly (per API).")
        else:
            logging.info("One-way mode detected — exits use reduceOnly for safety.")

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
        driver_task     = asyncio.create_task(hourly_driver(cli))
        trailing_task   = asyncio.create_task(trailing_scheduler_loop(cli))   # +1h trailing per micro
        aging_task      = asyncio.create_task(aging_purge_scheduler_loop(cli))# immediate >21h exits
        user_task       = asyncio.create_task(futures_user_stream_loop(cli))  # remove micro on trailing fill
        pnl_task        = asyncio.create_task(pnl_summary_loop(cli))

        await asyncio.gather(feed_task, driver_task, trailing_task, aging_task, user_task, pnl_task)
    finally:
        with contextlib.suppress(Exception):
            await cli.close_connection()

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
