#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Binance Futures bot with ATR-cross entries and asymmetric HHV/LLV windows:
- ATR = 24h Wilder ATR on 1-minute bars (ATR_PERIOD_MIN=1440), multiplier = 2
- LONG entry: price > HHV(high - 2*ATR_24h, 24h) * (1 + 0.1%)
- SHORT entry: price < LLV(low  + 2*ATR_24h,  4h) * (1 - 0.1%)
- On entry: place TRAILING_STOP_MARKET with callback = max(0.1%, ATR_floor%)
- While in position: tighten-only using ROE tiers (profit & loss), never loosen; ATR floor respected
- Re-entry: must re-cross ATR line (with 0.1% margin) AND break last exit; smart reset via WMA if price returns to trend
"""

import os, json, asyncio, threading, logging, websockets, time, math, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, List
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 28

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# Per-symbol order sizes
SYMBOLS = { "XRPUSDT": 10,
}

# Optional WMA gates (avoid trading far from trend); can be disabled
USE_WMA_GATES            = os.getenv("USE_WMA_GATES", "1") != "0"
SHORT_WMA_PERIOD         = 4 * 60 * 60      # 4h (SHORT side)
LONG_WMA_PERIOD          = 24 * 60 * 60     # 24h (LONG side)
WMA_ACTIVATION_THRESHOLD = 0.001            # 0.1% proximity → activate side

# Trailing activation distance from last price
ACTIVATION_OFFSET   = float(os.getenv("ACTIVATION_OFFSET", "0.001"))   # ≈0.1%
POS_CHECK_INTERVAL  = float(os.getenv("POS_CHECK_INTERVAL", "3.0"))

# Anti-thrash
ENTRY_COOLDOWN_SEC    = float(os.getenv("ENTRY_COOLDOWN_SEC", "2.0"))
TRAILING_COOLDOWN_SEC = float(os.getenv("TRAILING_COOLDOWN_SEC", "2.0"))
ENTRY_GUARD_TTL_SEC   = float(os.getenv("ENTRY_GUARD_TTL_SEC", "5.0"))

# REST backoff / position cache
POSITION_CACHE_TTL    = float(os.getenv("POSITION_CACHE_TTL", "15.0"))
RATE_LIMIT_BASE_SLEEP = float(os.getenv("RATE_LIMIT_BASE_SLEEP", "2.0"))
RATE_LIMIT_MAX_SLEEP  = float(os.getenv("RATE_LIMIT_MAX_SLEEP", "60.0"))

TRAIL_GUARD_TAG = os.getenv("TRAIL_GUARD_TAG", "CALLBK")
USE_EXCHANGE_TRAILING = os.getenv("USE_EXCHANGE_TRAILING", "1") != "0"

# ── Entry: must cross ATR line by this margin (default 0.1%) ──
ENTRY_ATR_CROSS_MARGIN = float(os.getenv("ENTRY_ATR_CROSS_MARGIN", "0.001"))  # 0.1%

# ── Initial trailing on entry: starts at 0.1% or ATR floor ──
START_CALLBACK_PCT     = float(os.getenv("START_CALLBACK_PCT", "0.1"))

# ── ATR = 24h Wilder ATR (1m bars) with multiplier 2 ──
ATR_PERIOD_MIN   = int(os.getenv("ATR_PERIOD_MIN", "1440"))  # 24h of 1m bars
ATR_MULT         = float(os.getenv("ATR_MULT", "2.0"))
ATR_REFRESH_SEC  = float(os.getenv("ATR_REFRESH_SEC", "30.0"))

# ── Asymmetric HHV/LLV windows tied to your trend frames ──
HHV_LONG_PERIOD_MIN   = int(os.getenv("HHV_LONG_PERIOD_MIN", "1440"))  # 24h for LONG HHV
LLV_SHORT_PERIOD_MIN  = int(os.getenv("LLV_SHORT_PERIOD_MIN", "240"))   # 4h  for SHORT LLV

# Apply ATR floor even in no-tier ROE zone? 0=keep current, 1=allow tightening by ATR
ALWAYS_APPLY_ATR_FLOOR = int(os.getenv("ALWAYS_APPLY_ATR_FLOOR", "0"))

# Optional fixed-dollar trailing floor (e.g., $2). 0 disables.
FIXED_DOLLAR_TRAIL = float(os.getenv("FIXED_DOLLAR_TRAIL", "0.0"))

# ROE tightening tiers (tighten-only; symmetric)
DYNAMIC_TRAIL_RULES = [
    (25.0,  2.0),
    (50.0,  1.5),
    (75.0,  1.0),
    (100.0, 0.5),
]
DYNAMIC_TRAIL_RULES_LOSS = [
    (-25.0,  2.0),
    (-50.0,  1.5),
    (-75.0,  1.0),
    (-100.0, 0.5),
]

EXIT_HISTORY_MAX = int(os.getenv("EXIT_HISTORY_MAX", "5"))

# ========================= QUIET /ping =========================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200); self.end_headers(); self.wfile.write(b"pong")
    def log_message(self, *_): pass
def start_ping(): HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

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
            self.value = price; self.last_update = now; return
        dt = now - (self.last_update or now)
        a = min(self.alpha * dt, 1.0)
        self.value = price * a + self.value * (1.0 - a)
        self.last_update = now

# ========================= ORDER HELPERS =======================
def open_long(sym):  return dict(symbol=sym, side="BUY",  type="MARKET", quantity=SYMBOLS[sym], positionSide="LONG")
def close_long(sym): return dict(symbol=sym, side="SELL", type="MARKET", quantity=SYMBOLS[sym], positionSide="LONG")
def open_short(sym): return dict(symbol=sym, side="SELL", type="MARKET", quantity=SYMBOLS[sym], positionSide="SHORT")
def close_short(sym):return dict(symbol=sym, side="BUY",  type="MARKET", quantity=SYMBOLS[sym], positionSide="SHORT")

# ========================= RUNTIME STATE =======================
state = {
    s: {
        # Smoothers (optional gates)
        "wma_short": TickWMA(SHORT_WMA_PERIOD),
        "wma_long":  TickWMA(LONG_WMA_PERIOD),

        "long_active": not USE_WMA_GATES,
        "short_active": not USE_WMA_GATES,

        # Position state
        "in_long": False, "long_pending": False,
        "long_entry_price": None, "long_peak": None,
        "long_exit_history": [],

        "in_short": False, "short_pending": False,
        "short_entry_price": None, "short_trough": None,
        "short_exit_history": [],

        # Exchange trailing
        "long_trailing":  {"order_id": None, "callback": None, "activation": None},
        "short_trailing": {"order_id": None, "callback": None, "activation": None},
        "long_trailing_lock": asyncio.Lock(),
        "short_trailing_lock": asyncio.Lock(),

        # Guards
        "last_long_entry_ts": 0.0, "last_short_entry_ts": 0.0,
        "last_long_trailing_ts": 0.0, "last_short_trailing_ts": 0.0,
        "long_entry_guard": None,  "long_entry_guard_ts": 0.0,
        "short_entry_guard": None, "short_entry_guard_ts": 0.0,

        "last_price": None, "order_lock": asyncio.Lock(),
        "last_pos_check": 0.0,

        # ATR & ATR-based lines
        "atr": None, "atr_ts": 0.0,
        "tv_ts_long": None, "tv_ts_short": None, "tv_ts_ts": 0.0,
    }
    for s in SYMBOLS
}

# ========================= PRICE FILTERS =======================
PRICE_TICK, PRICE_DECIMALS = {}, {}
def _dec(x) -> Decimal: return x if isinstance(x, Decimal) else Decimal(str(x))
def _decimals_from_tick(tick: str) -> int: return 0 if "." not in tick else len(tick.split(".")[1].rstrip("0"))
def quantize_price(symbol: str, price: float) -> str:
    tick = PRICE_TICK.get(symbol)
    if not tick: return f"{price:.8f}"
    p = _dec(price)
    q = (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    decs = PRICE_DECIMALS.get(symbol, 8)
    return f"{q:.{decs}f}"

# ========================= SAFE BINANCE CALL ===================
async def call_binance(fn, *a, **kw):
    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*a, **kw)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "-1003" in msg or "Too many requests" in msg or "IP banned" in msg:
                sleep_for = min(delay, RATE_LIMIT_MAX_SLEEP)
                logging.warning(f"Rate limit/backoff: sleeping {sleep_for:.1f}s ({msg})")
                await asyncio.sleep(sleep_for); delay = min(delay * 2.0, RATE_LIMIT_MAX_SLEEP); continue
            raise

# ========================= POS HELPERS =========================
position_cache = { s: {"LONG":{"size":0.0,"ts":0.0},"SHORT":{"size":0.0,"ts":0.0}} for s in SYMBOLS }
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
        sizes = {"LONG":0.0,"SHORT":0.0}
        for pos in positions:
            side = pos.get("positionSide")
            if side in sizes:
                sizes[side] = abs(float(pos.get("positionAmt", 0.0)))
        now = time.time()
        for sd in ("LONG","SHORT"):
            position_cache[symbol][sd]["size"] = sizes[sd]
            position_cache[symbol][sd]["ts"] = now
    except Exception as e:
        logging.error(f"{symbol} refresh_position_cache failed: {e}")

async def get_position_size(cli: AsyncClient, symbol: str, side: str) -> float:
    now = time.time(); pc = position_cache[symbol][side]
    if now - pc["ts"] <= POSITION_CACHE_TTL: return pc["size"]
    await refresh_position_cache(cli, symbol); return position_cache[symbol][side]["size"]

async def safe_order_execution(cli: AsyncClient, params: dict, symbol: str, label: str) -> bool:
    try:
        res = await call_binance(cli.futures_create_order, **params)
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} {label} OK (orderId={res.get('orderId')})")
        return True
    except Exception as e:
        logging.error(f"{symbol} {label} failed: {e}")
        return False

# ========================= ATR / TV LINES ======================
def _wilders_rma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period: return None
    sma = sum(values[:period]) / period; rma = sma
    for v in values[period:]: rma = (rma*(period-1)+v)/period
    return rma

def _wilders_rma_series(values: List[float], period: int) -> List[Optional[float]]:
    out = [None]*len(values)
    if len(values) < period: return out
    sma = sum(values[:period]) / period; out[period-1] = sma; rma = sma
    for i in range(period, len(values)):
        rma = (rma*(period-1)+values[i])/period
        out[i] = rma
    return out

def _compute_atr_from_klines(kl: List[list], period: int) -> Optional[float]:
    if not kl or len(kl) < period + 1: return None
    trs: List[float] = []
    prev_close = float(kl[0][4])
    for i in range(1, len(kl)):
        hi = float(kl[i][2]); lo = float(kl[i][3]); cl = float(kl[i][4])
        tr = max(hi-lo, abs(hi-prev_close), abs(lo-prev_close))
        trs.append(tr); prev_close = cl
    return _wilders_rma(trs, period)

def _compute_tv_trailing_lines_asymmetric(
    kl: List[list],
    atr_period: int,
    hhv_long_period: int,
    llv_short_period: int,
    mult: float
) -> Tuple[Optional[float], Optional[float]]:
    """
    TS_long  = HHV( high - mult*ATR(24h), lookback = 24h in minutes )
    TS_short = LLV( low  + mult*ATR(24h), lookback =  4h in minutes )
    """
    if not kl or len(kl) < atr_period + max(hhv_long_period, llv_short_period) + 2:
        return None, None

    highs = [float(k[2]) for k in kl]
    lows  = [float(k[3]) for k in kl]
    closes= [float(k[4]) for k in kl]

    # Build ATR series (aligned to bar index i>=1)
    trs: List[float] = []
    prev_close = closes[0]
    for i in range(1, len(kl)):
        hi = highs[i]; lo = lows[i]; cl = closes[i]
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr); prev_close = cl
    atr_series = _wilders_rma_series(trs, atr_period)
    last = len(kl) - 1
    if atr_series[last-1] is None:
        return None, None

    # LONG: HHV over last hhv_long_period bars
    win_L_start = max(1, last - hhv_long_period + 1)
    vals_long: List[float] = []
    for i in range(win_L_start, last + 1):
        atr = atr_series[i-1]
        if atr is None: continue
        vals_long.append(highs[i] - mult * atr)

    # SHORT: LLV over last llv_short_period bars
    win_S_start = max(1, last - llv_short_period + 1)
    vals_short: List[float] = []
    for i in range(win_S_start, last + 1):
        atr = atr_series[i-1]
        if atr is None: continue
        vals_short.append(lows[i] + mult * atr)

    if not vals_long or not vals_short:
        return None, None

    return max(vals_long), min(vals_short)

async def atr_tv_scheduler(cli: AsyncClient):
    """Refresh ATR(24h) and ATR-based HHV/LLV lines from 1m klines."""
    limit = max(ATR_PERIOD_MIN + max(HHV_LONG_PERIOD_MIN, LLV_SHORT_PERIOD_MIN) + 5, 2000)
    while True:
        try:
            for sym in SYMBOLS:
                try:
                    kl = await call_binance(cli.get_klines, symbol=sym, interval="1m", limit=limit)
                    atr = _compute_atr_from_klines(kl, ATR_PERIOD_MIN)
                    if atr and atr > 0:
                        state[sym]["atr"] = atr; state[sym]["atr_ts"] = time.time()
                    ts_long, ts_short = _compute_tv_trailing_lines_asymmetric(
                        kl, ATR_PERIOD_MIN, HHV_LONG_PERIOD_MIN, LLV_SHORT_PERIOD_MIN, ATR_MULT
                    )
                    if ts_long and ts_short:
                        state[sym]["tv_ts_long"]  = ts_long
                        state[sym]["tv_ts_short"] = ts_short
                        state[sym]["tv_ts_ts"]    = time.time()
                except Exception as e:
                    logging.warning(f"{sym} ATR/TV update failed: {e}")
        except Exception as e:
            logging.error(f"atr_tv_scheduler error: {e}")
        await asyncio.sleep(ATR_REFRESH_SEC)

def atr_floor_cb_pct(sym: str, price: Optional[float]) -> Optional[float]:
    """ATR*mult (and optional $ floor) → callback% at current price."""
    if price is None or not math.isfinite(price) or price <= 0: return None
    atr = state[sym].get("atr")
    if not atr or atr <= 0: return None
    floor_abs = ATR_MULT * atr
    if FIXED_DOLLAR_TRAIL > 0.0:
        floor_abs = max(floor_abs, FIXED_DOLLAR_TRAIL)
    return (floor_abs / price) * 100.0

# ========================= TIERS & COMBINATION =================
def _tier_callback_from_roe(roe_pct: float) -> Optional[float]:
    if roe_pct >= 0:
        best = None
        for th, cb in DYNAMIC_TRAIL_RULES:
            if roe_pct >= th: best = cb
        return best
    else:
        best = None
        for th, cb in DYNAMIC_TRAIL_RULES_LOSS:
            if roe_pct <= th: best = cb
        return best

def combined_target_callback(sym: str, roe_pct: float, price: Optional[float]) -> Optional[float]:
    """
    If a ROE tier applies → target = max(tier_cb, ATR_floor_cb).
    If no tier applies → None (keep current), unless ALWAYS_APPLY_ATR_FLOOR=1 → ATR floor.
    """
    floor_cb = atr_floor_cb_pct(sym, price)
    tier_cb  = _tier_callback_from_roe(roe_pct)
    if tier_cb is None:
        return floor_cb if ALWAYS_APPLY_ATR_FLOOR and floor_cb is not None else None
    if floor_cb is None:
        return tier_cb
    return max(tier_cb, floor_cb)

# ========================= TRAILING CORE =======================
def clamp_callback(cb: float) -> float: return max(0.1, min(5.0, cb))

def trailing_client_id(symbol: str, side: str) -> str:
    return f"{TRAIL_GUARD_TAG}-{symbol}-{side}-{int(time.time())}"

async def cancel_trailing(cli: AsyncClient, symbol: str, order_id: Optional[int]):
    if not order_id: return
    try:
        await call_binance(cli.futures_cancel_order, symbol=symbol, orderId=order_id)
        invalidate_position_cache(symbol)
        logging.info(f"{symbol} cancelled trailing {order_id}")
    except Exception as e:
        logging.warning(f"{symbol} cancel trailing failed: {e}")

async def place_trailing(cli: AsyncClient, symbol: str, side: str, px: float, cb_pct: float, reduce_only: bool) -> Tuple[Optional[int], Optional[float], Optional[str]]:
    if px is None or not math.isfinite(px) or px <= 0.0:
        logging.warning(f"{symbol} {side} place_trailing skipped: bad price {px}")
        return None, None, None
    if side.upper()=="LONG":
        order_side="SELL"; position_side="LONG"; raw_act = px*(1.0+ACTIVATION_OFFSET)
    else:
        order_side="BUY";  position_side="SHORT"; raw_act = px*(1.0-ACTIVATION_OFFSET)
    act_str = quantize_price(symbol, raw_act)
    cb = clamp_callback(float(cb_pct))
    params = dict(symbol=symbol, side=order_side, type="TRAILING_STOP_MARKET",
                  quantity=SYMBOLS[symbol], positionSide=position_side,
                  callbackRate=cb, activationPrice=act_str,
                  workingType="CONTRACT_PRICE", newOrderRespType="ACK",
                  newClientOrderId=trailing_client_id(symbol, side))
    if reduce_only: params["reduceOnly"]=True
    try:
        res = await call_binance(cli.futures_create_order, **params)
        oid = res.get("orderId"); invalidate_position_cache(symbol)
        logging.info(f"{symbol} {side} trailing placed: cb={cb:.3f}% act={act_str} oid={oid}")
        return oid, float(act_str), params["newClientOrderId"]
    except Exception as e:
        msg=str(e)
        if "-2021" in msg or "-1102" in msg:
            bump = ACTIVATION_OFFSET*2.0
            raw_act = px*(1.0+bump) if side.upper()=="LONG" else px*(1.0-bump)
            act_str = quantize_price(symbol, raw_act)
            params["activationPrice"]=act_str
            try:
                res = await call_binance(cli.futures_create_order, **params)
                oid = res.get("orderId"); invalidate_position_cache(symbol)
                logging.info(f"{symbol} {side} trailing retried: cb={cb:.3f}% act={act_str} oid={oid}")
                return oid, float(act_str), params["newClientOrderId"]
            except Exception as e2:
                logging.error(f"{symbol} {side} trailing retry failed: {e2}")
                return None, None, params["newClientOrderId"]
        logging.error(f"{symbol} {side} trailing failed: {e}")
        return None, None, params["newClientOrderId"]

async def ensure_trailing(cli: AsyncClient, sym: str, st: dict, px: float, side: str, reduce_only: bool):
    lock = st["long_trailing_lock"] if side=="LONG" else st["short_trailing_lock"]
    async with lock:
        now = time.time()
        if side=="LONG":
            if now - st["last_long_trailing_ts"] < TRAILING_COOLDOWN_SEC: return
            have = st["long_trailing"]; entry = st["long_entry_price"]
        else:
            if now - st["last_short_trailing_ts"] < TRAILING_COOLDOWN_SEC: return
            have = st["short_trailing"]; entry = st["short_entry_price"]

        # Initial trailing on entry: max(0.1%, ATR floor)
        if have["order_id"] is None:
            base_cb = START_CALLBACK_PCT
            floor_cb = atr_floor_cb_pct(sym, px)
            init_cb = clamp_callback(max(base_cb, floor_cb or 0.0))
            oid, act, _ = await place_trailing(cli, sym, side, px, init_cb, reduce_only)
            if oid:
                have.update(order_id=oid, callback=init_cb, activation=act)
                if side=="LONG": st["last_long_trailing_ts"]=time.time()
                else:            st["last_short_trailing_ts"]=time.time()
            return

        # Tighten only: combine ROE tiers with ATR floor
        roe = pnl_percent(entry, px, side)
        target_cb = combined_target_callback(sym, roe, px)
        if target_cb is None:
            return
        target_cb = clamp_callback(target_cb)

        if have["callback"] is None or target_cb < have["callback"] - 1e-9:
            await cancel_trailing(cli, sym, have["order_id"])
            oid, act, _ = await place_trailing(cli, sym, side, px, target_cb, reduce_only)
            if oid:
                have.update(order_id=oid, callback=target_cb, activation=act)
                if side=="LONG": st["last_long_trailing_ts"]=time.time()
                else:            st["last_short_trailing_ts"]=time.time()

# ========================= PNL ================================
def pnl_percent(entry: Optional[float], px: float, side: str, lev: float = LEVERAGE) -> float:
    if not entry: return 0.0
    if side.upper()=="LONG": base = (px - entry) / entry * 100.0
    else:                    base = (entry - px) / entry * 100.0
    return base * lev

# Background: keep tightening when allowed
async def dynamic_trailing_scheduler(cli: AsyncClient, reduce_only: bool):
    while True:
        try:
            if not USE_EXCHANGE_TRAILING:
                await asyncio.sleep(1.0); continue
            for sym in SYMBOLS:
                st = state[sym]; px = st.get("last_price")
                if px is None: continue

                # LONG
                if st.get("in_long") and st["long_trailing"].get("order_id"):
                    entry = st.get("long_entry_price")
                    roe = pnl_percent(entry, px, "LONG")
                    target_cb = combined_target_callback(sym, roe, px)
                    have_cb  = st["long_trailing"].get("callback")
                    if target_cb is not None and (have_cb is None or target_cb < have_cb - 1e-9):
                        async with st["long_trailing_lock"]:
                            have_cb = st["long_trailing"].get("callback")
                            if st["long_trailing"].get("order_id") and (have_cb is None or target_cb < have_cb - 1e-9):
                                await cancel_trailing(cli, sym, st["long_trailing"]["order_id"])
                                oid, act, _ = await place_trailing(cli, sym, "LONG", px, target_cb, reduce_only)
                                if oid:
                                    st["long_trailing"].update(order_id=oid, callback=target_cb, activation=act)
                                    st["last_long_trailing_ts"]=time.time()

                # SHORT
                if st.get("in_short") and st["short_trailing"].get("order_id"):
                    entry = st.get("short_entry_price")
                    roe = pnl_percent(entry, px, "SHORT")
                    target_cb = combined_target_callback(sym, roe, px)
                    have_cb  = st["short_trailing"].get("callback")
                    if target_cb is not None and (have_cb is None or target_cb < have_cb - 1e-9):
                        async with st["short_trailing_lock"]:
                            have_cb = st["short_trailing"].get("callback")
                            if st["short_trailing"].get("order_id") and (have_cb is None or target_cb < have_cb - 1e-9):
                                await cancel_trailing(cli, sym, st["short_trailing"]["order_id"])
                                oid, act, _ = await place_trailing(cli, sym, "SHORT", px, target_cb, reduce_only)
                                if oid:
                                    st["short_trailing"].update(order_id=oid, callback=target_cb, activation=act)
                                    st["last_short_trailing_ts"]=time.time()
        except Exception as e:
            logging.error(f"dynamic_trailing_scheduler error: {e}")
        await asyncio.sleep(1.0)

# ========================= ADOPTION/SEED =======================
async def adopt_positions(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            positions = await call_binance(cli.futures_position_information, symbol=s)
        except Exception as e:
            logging.warning(f"{s} adopt: positions failed: {e}"); continue
        st = state[s]
        for pos in positions:
            side = pos.get("positionSide","BOTH"); amt = float(pos.get("positionAmt","0") or 0.0)
            entry = float(pos.get("entryPrice","0") or 0.0)
            if side=="LONG":
                if abs(amt)>0 and entry>0:
                    st["in_long"]=True; st["long_entry_price"]=entry; st["long_peak"]=entry
                    st["long_trailing"]={"order_id":None,"callback":None,"activation":None}
                    logging.info(f"{s} adopt LONG size {amt} entry {entry}")
                else:
                    st["in_long"]=False; st["long_entry_price"]=None; st["long_peak"]=None
                    st["long_trailing"]={"order_id":None,"callback":None,"activation":None}
            if side=="SHORT":
                if abs(amt)>0 and entry>0:
                    st["in_short"]=True; st["short_entry_price"]=entry; st["short_trough"]=entry
                    st["short_trailing"]={"order_id":None,"callback":None,"activation":None}
                    logging.info(f"{s} adopt SHORT size {amt} entry {entry}")
                else:
                    st["in_short"]=False; st["short_entry_price"]=None; st["short_trough"]=None
                    st["short_trailing"]={"order_id":None,"callback":None,"activation":None}
        await refresh_position_cache(cli, s)

async def seed_symbol_filters(cli: AsyncClient):
    try:
        info = await call_binance(cli.futures_exchange_info)
        symbols_info = {s["symbol"]:s for s in info.get("symbols", [])}
        for sym in SYMBOLS:
            si = symbols_info.get(sym); 
            if not si: continue
            pf = next((f for f in si.get("filters", []) if f.get("filterType")=="PRICE_FILTER"), None)
            if not pf: continue
            tick = pf.get("tickSize")
            if tick:
                PRICE_TICK[sym] = _dec(tick); PRICE_DECIMALS[sym] = _decimals_from_tick(tick)
                logging.info(f"{sym} tickSize={tick} decimals={PRICE_DECIMALS[sym]}")
    except Exception as e:
        logging.warning(f"seed_symbol_filters failed: {e}")

async def seed_wmas(cli: AsyncClient):
    if not USE_WMA_GATES: return
    for s in SYMBOLS:
        kl = await call_binance(cli.get_klines, symbol=s, interval="1m", limit=1440)
        closes=[float(k[4]) for k in kl]; closes_4h = closes[-240:] if len(closes)>=240 else closes
        wL = state[s]["wma_long"]; wS = state[s]["wma_short"]
        for c in closes: wL.update(c)
        for c in closes_4h: wS.update(c)
        logging.info(f"{s} WMA24h={wL.value:.4f} WMA4h={wS.value:.4f}")

# ========================= MAIN LOOP ==========================
async def run(cli: AsyncClient):
    dual_side = await get_dual_side(cli)
    reduce_only = not dual_side

    for s in SYMBOLS:
        try: await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
        except Exception as e: logging.warning(f"{s} set leverage failed: {e}")

    await seed_symbol_filters(cli)
    await seed_wmas(cli)
    await adopt_positions(cli)
    asyncio.create_task(atr_tv_scheduler(cli))
    asyncio.create_task(dynamic_trailing_scheduler(cli, reduce_only))

    streams=[f"{s.lower()}@trade" for s in SYMBOLS]
    url=f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    while True:
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    m=json.loads(raw)
                    if not m["stream"].endswith("@trade"): continue
                    d=m["data"]; sym=d["s"]; price=float(d["p"]); st=state[sym]
                    st["last_price"]=price

                    # Update WMAs (if used)
                    if USE_WMA_GATES:
                        st["wma_short"].update(price); st["wma_long"].update(price)
                        wS=st["wma_short"].value; wL=st["wma_long"].value
                        if not st["long_active"]:
                            if abs(price - wL)/wL <= WMA_ACTIVATION_THRESHOLD:
                                st["long_active"]=True; logging.info(f"{sym} LONG activated near 24h WMA")
                        if not st["short_active"]:
                            if abs(price - wS)/wS <= WMA_ACTIVATION_THRESHOLD:
                                st["short_active"]=True; logging.info(f"{sym} SHORT activated near 4h WMA")

                    ts_long = st.get("tv_ts_long")
                    ts_short= st.get("tv_ts_short")

                    # ---------------- LONG ENTRY / RE-ENTRY (ATR-cross + 0.1%) ----------------
                    if st["long_active"] and (not st["in_long"]) and (not st["long_pending"]) and ts_long is not None:
                        entry_triggered=False; entry_type=""
                        # Smart reset: if price <= long WMA (when gates ON), clear old exits
                        if USE_WMA_GATES and len(st["long_exit_history"])>0:
                            if price <= st["wma_long"].value:
                                st["long_exit_history"]=[]

                        atr_cross_ok = price > ts_long * (1.0 + ENTRY_ATR_CROSS_MARGIN)

                        if len(st["long_exit_history"]) == 0:
                            if atr_cross_ok:
                                entry_triggered=True; entry_type="LONG FIRST ENTRY (ATR-cross)"
                        else:
                            H = max(st["long_exit_history"])
                            above_h_ok = price > H
                            wma_ok = True if not USE_WMA_GATES else (H > st["wma_long"].value)
                            if atr_cross_ok and above_h_ok and wma_ok:
                                entry_triggered=True; entry_type=f"LONG RE-ENTRY (ATR-cross > last exit {H:.4f})"

                        if entry_triggered:
                            async with st["order_lock"]:
                                if st["in_long"] or st["long_pending"]: continue
                                now=time.time()
                                if st["long_entry_guard"] and (now-st["long_entry_guard_ts"]<=ENTRY_GUARD_TTL_SEC): continue
                                if now - st["last_long_entry_ts"] < ENTRY_COOLDOWN_SEC: continue
                                st["long_entry_guard"]=f"ENTRYGUARD-{sym}-LONG-{int(now)}"; st["long_entry_guard_ts"]=now
                                st["long_pending"]=True
                            try:
                                params=open_long(sym); params["newClientOrderId"]=st["long_entry_guard"]
                                ok=await safe_order_execution(cli, params, sym, entry_type)
                                if ok:
                                    st["in_long"]=True; st["long_entry_price"]=price; st["long_peak"]=price
                                    st["long_trailing"]={"order_id":None,"callback":None,"activation":None}
                                    st["last_long_entry_ts"]=time.time()
                                    await ensure_trailing(cli, sym, st, price, side="LONG", reduce_only=reduce_only)
                                else:
                                    st["long_entry_guard"]=None; st["long_entry_guard_ts"]=0.0
                            except Exception as e:
                                logging.error(f"{sym} LONG entry error: {e}")
                            finally:
                                st["long_pending"]=False

                    # ---------------- SHORT ENTRY / RE-ENTRY (ATR-cross + 0.1%) ---------------
                    if st["short_active"] and (not st["in_short"]) and (not st["short_pending"]) and ts_short is not None:
                        entry_triggered=False; entry_type=""
                        if USE_WMA_GATES and len(st["short_exit_history"])>0:
                            if price >= st["wma_short"].value:
                                st["short_exit_history"]=[]

                        atr_cross_ok = price < ts_short * (1.0 - ENTRY_ATR_CROSS_MARGIN)

                        if len(st["short_exit_history"]) == 0:
                            if atr_cross_ok:
                                entry_triggered=True; entry_type="SHORT FIRST ENTRY (ATR-cross)"
                        else:
                            L = min(st["short_exit_history"])
                            below_l_ok = price < L
                            wma_ok = True if not USE_WMA_GATES else (L < st["wma_short"].value)
                            if atr_cross_ok and below_l_ok and wma_ok:
                                entry_triggered=True; entry_type=f"SHORT RE-ENTRY (ATR-cross < last exit {L:.4f})"

                        if entry_triggered:
                            async with st["order_lock"]:
                                if st["in_short"] or st["short_pending"]: continue
                                now=time.time()
                                if st["short_entry_guard"] and (now-st["short_entry_guard_ts"]<=ENTRY_GUARD_TTL_SEC): continue
                                if now - st["last_short_entry_ts"] < ENTRY_COOLDOWN_SEC: continue
                                st["short_entry_guard"]=f"ENTRYGUARD-{sym}-SHORT-{int(now)}"; st["short_entry_guard_ts"]=now
                                st["short_pending"]=True
                            try:
                                params=open_short(sym); params["newClientOrderId"]=st["short_entry_guard"]
                                ok=await safe_order_execution(cli, params, sym, entry_type)
                                if ok:
                                    st["in_short"]=True; st["short_entry_price"]=price; st["short_trough"]=price
                                    st["short_trailing"]={"order_id":None,"callback":None,"activation":None}
                                    st["last_short_entry_ts"]=time.time()
                                    await ensure_trailing(cli, sym, st, price, side="SHORT", reduce_only=reduce_only)
                                else:
                                    st["short_entry_guard"]=None; st["short_entry_guard_ts"]=0.0
                            except Exception as e:
                                logging.error(f"{sym} SHORT entry error: {e}")
                            finally:
                                st["short_pending"]=False

                    # ---------------- Manage trailing while in pos ----------------------
                    if st["in_long"]:  await ensure_trailing(cli, sym, st, price, "LONG",  reduce_only)
                    if st["in_short"]: await ensure_trailing(cli, sym, st, price, "SHORT", reduce_only)

                    # Detect auto-close (trailing filled) and record exit
                    now=time.time()
                    if now - st["last_pos_check"] >= POS_CHECK_INTERVAL:
                        st["last_pos_check"]=now
                        if st["in_long"]:
                            size=await get_position_size(cli, sym, "LONG")
                            if size <= 1e-12:
                                exit_px = st.get("last_price")
                                if exit_px:
                                    st["long_exit_history"].append(exit_px)
                                    st["long_exit_history"]=st["long_exit_history"][-EXIT_HISTORY_MAX:]
                                st["in_long"]=False
                                st["long_entry_price"]=None; st["long_peak"]=None
                                st["long_trailing"]={"order_id":None,"callback":None,"activation":None}
                        if st["in_short"]:
                            size=await get_position_size(cli, sym, "SHORT")
                            if size <= 1e-12:
                                exit_px = st.get("last_price")
                                if exit_px:
                                    st["short_exit_history"].append(exit_px)
                                    st["short_exit_history"]=st["short_exit_history"][-EXIT_HISTORY_MAX:]
                                st["in_short"]=False
                                st["short_entry_price"]=None; st["short_trough"]=None
                                st["short_trailing"]={"order_id":None,"callback":None,"activation":None}

        except Exception as e:
            logging.warning(f"WebSocket dropped: {e}. Reconnecting..."); await asyncio.sleep(2.0); continue

# ========================= BOOTSTRAP ===========================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET): raise RuntimeError("Missing Binance API creds")
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    try: await run(cli)
    finally:
        try: await cli.close_connection()
        except Exception: pass

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%b %d %H:%M:%S"
    )
    asyncio.run(main())
