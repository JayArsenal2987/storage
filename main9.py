#!/usr/bin/env python3
import os, json, asyncio, threading, logging, random, websockets
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
import time

# ────────────── CONFIG ──────────────
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

SYMBOLS = {
    "ADAUSDT": 10
}

# Rolling windows
HISTORICAL_PERIOD_MINUTES = 720      # 720 × 1m candles
TICK_ROLLING_WINDOW_SECS   = 720*60  # previous second → 720 minutes ago (true rolling)

SWING_THRESHOLD_PCT = 0.02           # 2% breakout threshold
ANALYSIS_INTERVAL_SECONDS = 60       # (kept) timer; real-time path handles entries

# Trailing stop configuration
TRAIL_PCT = 0.02                 # 2%
FLIP_COOLDOWN_SECS = 1.0

USE_EXCHANGE_TRAILING = True
WORKING_TYPE = "MARK_PRICE"
CALLBACK_RATE = round(TRAIL_PCT * 100, 1)

# ROE tighten-only tiers (as requested)
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

# Flip confirmation
CONFIRM_FLIP = True
CONFIRM_WINDOW_SECS = 2
CONFIRM_LOOKBACK_MIN = 60
CONFIRM_BREAK_PCT = 0.0

ORDER_DEDUP_WINDOW_SECS = 1.5

# WebSocket hardening & rate-limit avoidance
WS_PING_INTERVAL = 15
WS_PING_TIMEOUT  = 10
WS_RECONNECT_MIN = 1.0
WS_RECONNECT_MAX = 30.0

POSITION_SYNC_INTERVAL_SECS = 60
REST_SYNC_IF_STALE_SECS     = 600  # only REST-sync if no ACCOUNT_UPDATE for ≥10 min

def _jitter(t): return t * (0.9 + 0.2 * random.random())

# ────────────── QUIET /ping ──────────
class Ping(BaseHTTPRequestHandler):
    def do_GET(self, *args, **kwargs):
        if self.path == "/ping":
            self.send_response(200); self.end_headers(); self.wfile.write(b"pong")
    def log_message(self, *_) : pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

# ─────────── ROE helpers ───────────
def _pnl_percent(entry_price, price, leverage, side):
    if entry_price is None or price is None: return None
    if side == "LONG":
        return leverage * ((price / entry_price) - 1.0) * 100.0
    else:
        return leverage * (1.0 - (price / entry_price)) * 100.0

def _target_callback_rate(pnl_pct):
    if pnl_pct is None: return None
    target = None
    if pnl_pct >= 0.0:
        for thr, cb in DYNAMIC_TRAIL_RULES:
            if pnl_pct >= thr: target = cb
            else: break
    else:
        for thr, cb in DYNAMIC_TRAIL_RULES_LOSS:
            if pnl_pct <= thr: target = cb
            else: break
    return target

# ───────────── SWING FINDER (plateau-inclusive; kept) ─────────
def find_swing_points(candles, swing_period=5, mode="causal"):
    """
    Inclusive (<=/>=) so plateaus count. Causal mode lets the newest bar be a swing.
    Candle: [open_time, open, high, low, close, volume]
    """
    n = len(candles)
    if n == 0: return [], []
    highs = [c[2] for c in candles]; lows = [c[3] for c in candles]
    swing_highs, swing_lows = [], []

    if mode == "symmetric":
        if n < swing_period*2 + 1: return [], []
        for i in range(swing_period, n - swing_period):
            h, l = highs[i], lows[i]
            if all(highs[j] <= h for j in range(i - swing_period, i + swing_period + 1) if j != i):
                swing_highs.append((h, i))
            if all(lows[j]  >= l for j in range(i - swing_period, i + swing_period + 1) if j != i):
                swing_lows.append((l, i))
    else:  # causal (look-back k bars only)
        if n < swing_period + 1: return [], []
        for i in range(swing_period, n):
            h, l = highs[i], lows[i]
            if all(x <= h for x in highs[i - swing_period:i]): swing_highs.append((h, i))
            if all(x >= l for x in lows [i - swing_period:i]): swing_lows.append((l, i))
    return swing_highs, swing_lows

def analyze_rolling_swings(candles, current_price, symbol):
    # Kept for reference and confirmation logic; not used for tick-level entry
    highs, lows = find_swing_points(candles, swing_period=5, mode="causal")
    if not highs and not lows:
        logging.warning(f"{symbol} No swing points found (1m causal)."); return None
    recent_high = max(highs, key=lambda x: x[1])[0] if highs else None
    recent_low  = max(lows , key=lambda x: x[1])[0] if lows  else None
    state[symbol]["current_swing_high"] = recent_high
    state[symbol]["current_swing_low"]  = recent_low
    if recent_high and current_price >= recent_high * (1 + SWING_THRESHOLD_PCT): return "LONG"
    if recent_low  and current_price <= recent_low  * (1 - SWING_THRESHOLD_PCT): return "SHORT"
    return None

# ───────────── ORDERS ─────────
def open_long(sym):  return dict(symbol=sym, side="BUY",  type="MARKET", quantity=SYMBOLS[sym], positionSide="LONG")
def close_long(sym): return dict(symbol=sym, side="SELL", type="MARKET", quantity=SYMBOLS[sym], positionSide="LONG",  reduceOnly=True)
def open_short(sym): return dict(symbol=sym, side="SELL", type="MARKET", quantity=SYMBOLS[sym], positionSide="SHORT")
def close_short(sym):return dict(symbol=sym, side="BUY",  type="MARKET", quantity=SYMBOLS[sym], positionSide="SHORT", reduceOnly=True)

async def cancel_trailing_stop(cli: AsyncClient, symbol: str, side: str):
    try:
        orders = await cli.futures_get_open_orders(symbol=symbol)
        for o in orders:
            if o.get("origType") == "TRAILING_STOP_MARKET" and o.get("positionSide") == side:
                await cli.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                logging.info(f"{symbol} {side} canceled trailing stop {o['orderId']}")
    except Exception as e:
        logging.error(f"{symbol} {side} cancel trailing stop failed: {e}")

async def place_trailing_stop(cli: AsyncClient, symbol: str, side: str, callback_rate: float = None):
    try:
        qty = await get_position_size(cli, symbol, side)
        if qty <= 0: return False
        await cancel_trailing_stop(cli, symbol, side)
        order_side = "SELL" if side == "LONG" else "BUY"
        cb = CALLBACK_RATE if callback_rate is None else float(callback_rate)
        cb = round(cb, 1)
        res = await cli.futures_create_order(
            symbol=symbol, side=order_side, positionSide=side, type="TRAILING_STOP_MARKET",
            quantity=qty, callbackRate=cb, workingType=WORKING_TYPE, newOrderRespType="RESULT"
        )
        st = state[symbol]
        st["server_trailing_active"] = True
        st["server_trailing_order_id"] = res.get("orderId")
        st["server_trailing_cb_rate"] = cb
        logging.info(f"{symbol} {side} trailing stop placed @ {cb}% (order {res.get('orderId')})")
        return True
    except Exception as e:
        logging.error(f"{symbol} {side} place trailing stop failed: {e}")
        return False

# ───────────── STATE ─────────
state = {
    s: {
        "in_long": False, "long_pending": False,
        "in_short": False, "short_pending": False,
        "long_entry_price": None, "short_entry_price": None,
        "current_swing_high": None, "current_swing_low": None,
        "last_analysis_time": 0, "current_signal": None,
        "initial_position_taken": False,
        "trail_extremum": None, "trail_side": None,
        "cooldown_until": 0.0,
        "last_price": None, "order_lock": asyncio.Lock(),
        "last_order_id": None,
        # server trailing
        "server_trailing_active": False,
        "server_trailing_order_id": None,
        "server_trailing_cb_rate": None,
        # local trailing (decimal)
        "local_trail_pct": None,
        # flip confirm
        "confirm_active": False, "confirm_until": 0.0,
        "confirm_side": None, "confirm_swing_level": None,
        # de-dup
        "last_action_ts": {}, "last_ts_filled_id": None,
        # 1m candles (for context & confirmation)
        "candles": deque(maxlen=HISTORICAL_PERIOD_MINUTES), "history_loaded": False,
        # tick-level rolling extremes (second resolution)
        "tick_prices": deque(),     # (ts_sec, price)
        "tick_maxdq": deque(),      # monotonic decreasing (ts, price)
        "tick_mindq": deque(),      # monotonic increasing (ts, price)
        "rolling_high": None, "rolling_low": None,
        # ws-first position sync
        "last_account_update_ts": 0.0,
    }
    for s in SYMBOLS
}

# ───────────── POSITION HELPERS ─────────
async def get_position_size(cli: AsyncClient, symbol: str, side: str) -> float:
    try:
        positions = await cli.futures_position_information(symbol=symbol)
        for pos in positions:
            if pos['positionSide'] == side:
                return abs(float(pos['positionAmt']))
    except Exception as e:
        logging.error(f"Failed to get position for {symbol} {side}: {e}")
    return 0.0

async def get_position_sizes(cli: AsyncClient, symbol: str):
    long_sz = short_sz = 0.0
    try:
        positions = await cli.futures_position_information(symbol=symbol)
        for pos in positions:
            ps = pos.get('positionSide'); amt = abs(float(pos.get('positionAmt', 0)))
            if ps == "LONG":  long_sz  = amt
            if ps == "SHORT": short_sz = amt
    except Exception as e:
        logging.error(f"Failed to get positions for {symbol}: {e}")
    return long_sz, short_sz

def _dedup_key(symbol: str, action: str, order_params: dict) -> str:
    return f"{symbol}|{action}|{order_params.get('side')}|{order_params.get('type')}|{order_params.get('positionSide')}|{order_params.get('reduceOnly', False)}"

def _new_coid(symbol: str, action: str) -> str:
    return f"{symbol}-{action}-{int(time.time()*1000)}"

async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    try:
        now = time.time()
        st = state[symbol]
        key = _dedup_key(symbol, action, order_params)
        if now - st["last_action_ts"].get(key, 0.0) < ORDER_DEDUP_WINDOW_SECS:
            logging.warning(f"{symbol} {action}: skipped duplicate within {ORDER_DEDUP_WINDOW_SECS}s"); return False
        st["last_action_ts"][key] = now

        if action.startswith("CLOSE") or "EXIT" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size(cli, symbol, side)
            if current_pos < order_params['quantity'] * 0.99:
                logging.warning(f"{symbol} {action}: insufficient position size"); return False

        p = dict(order_params); p.setdefault("newClientOrderId", _new_coid(symbol, action))
        result = await cli.futures_create_order(**p)
        st["last_order_id"] = result.get('orderId')
        logging.info(f"{symbol} {action} executed - OrderID: {st['last_order_id']}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} failed: {e}")
        return False

# ───────────── ROLLING (1m) ANALYSIS ─────────
async def perform_rolling_analysis(cli: AsyncClient, symbol: str, current_price: float):
    try:
        buf = state[symbol]["candles"]
        candles = list(buf) if buf and len(buf) >= 6 else [
            [int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
            for k in await cli.get_klines(symbol=symbol, interval="1m", limit=HISTORICAL_PERIOD_MINUTES)
        ]
        signal = analyze_rolling_swings(candles, current_price, symbol)
        st = state[symbol]; st["current_signal"] = signal; st["last_analysis_time"] = time.time()
        return signal
    except Exception as e:
        logging.error(f"{symbol} Rolling analysis failed: {e}")
        return None

# ───────────── ENTRY/EXIT HELPERS ─────────
async def enter_long(cli: AsyncClient, sym: str, price: float, reason: str = ""):
    st = state[sym]
    async with st["order_lock"]:
        if st["in_long"] or st["long_pending"] or st["in_short"] or st["short_pending"]: return False
        st["long_pending"] = True
    try:
        if await safe_order_execution(cli, open_long(sym), sym, f"LONG ENTRY {reason}"):
            st["in_long"] = True; st["long_entry_price"] = price
            st["trail_side"] = "LONG"; st["trail_extremum"] = price; st["local_trail_pct"] = TRAIL_PCT
            if USE_EXCHANGE_TRAILING: await place_trailing_stop(cli, sym, "LONG", CALLBACK_RATE)
            logging.info(f"{sym} LONG OPEN @ ${price:.4f} | trailing {CALLBACK_RATE}%"); return True
    finally:
        st["long_pending"] = False
    return False

async def enter_short(cli: AsyncClient, sym: str, price: float, reason: str = ""):
    st = state[sym]
    async with st["order_lock"]:
        if st["in_short"] or st["short_pending"] or st["in_long"] or st["long_pending"]: return False
        st["short_pending"] = True
    try:
        if await safe_order_execution(cli, open_short(sym), sym, f"SHORT ENTRY {reason}"):
            st["in_short"] = True; st["short_entry_price"] = price
            st["trail_side"] = "SHORT"; st["trail_extremum"] = price; st["local_trail_pct"] = TRAIL_PCT
            if USE_EXCHANGE_TRAILING: await place_trailing_stop(cli, sym, "SHORT", CALLBACK_RATE)
            logging.info(f"{sym} SHORT OPEN @ ${price:.4f} | trailing {CALLBACK_RATE}%"); return True
    finally:
        st["short_pending"] = False
    return False

async def manage_initial_position(cli: AsyncClient, sym: str, price: float, signal: str):
    st = state[sym]
    if st["initial_position_taken"]: return
    if signal == "LONG" and not st["in_long"] and not st["in_short"]:
        if await enter_long(cli, sym, price, "(SIGNAL)"):
            st["initial_position_taken"] = True; logging.info(f"{sym} INITIAL LONG")
    elif signal == "SHORT" and not st["in_short"] and not st["in_long"]:
        if await enter_short(cli, sym, price, "(SIGNAL)"):
            st["initial_position_taken"] = True; logging.info(f"{sym} INITIAL SHORT")

# ───────────── LOCAL TRAILING (fallback) ─────────
async def handle_trailing_stops(cli: AsyncClient, sym: str, price: float):
    st = state[sym]
    if USE_EXCHANGE_TRAILING and st.get("server_trailing_active"): return
    side = st["trail_side"]
    if side not in ("LONG","SHORT") or not st["initial_position_taken"]: return

    entry = st["long_entry_price"] if side == "LONG" else st["short_entry_price"]
    pnl = _pnl_percent(entry, price, LEVERAGE, side)
    target_cb = _target_callback_rate(pnl)
    if st["local_trail_pct"] is None: st["local_trail_pct"] = TRAIL_PCT
    if target_cb is not None:
        target_dec = target_cb / 100.0
        if target_dec < st["local_trail_pct"]:
            prev = st["local_trail_pct"]; st["local_trail_pct"] = target_dec
            logging.info(f"{sym} {side} local trailing tightened: {prev*100:.1f}% → {st['local_trail_pct']*100:.1f}% (ROE {pnl:.2f}%)")

    now = time.time()
    if st["cooldown_until"] and now < st["cooldown_until"]:
        if side == "LONG":
            if st["trail_extremum"] is None or price > st["trail_extremum"]: st["trail_extremum"] = price
        else:
            if st["trail_extremum"] is None or price < st["trail_extremum"]: st["trail_extremum"] = price
        return

    dist = st["local_trail_pct"]
    if side == "LONG":
        if st["trail_extremum"] is None or price > st["trail_extremum"]: st["trail_extremum"] = price
        trigger_price = st["trail_extremum"] * (1 - dist)
        if price <= trigger_price:
            async with st["order_lock"]:
                if st["long_pending"] or st["short_pending"]: return
                st["long_pending"] = True
            try:
                if await safe_order_execution(cli, close_long(sym), sym, "LONG EXIT (TRAIL)"):
                    st["in_long"] = False; st["long_entry_price"] = None
                    st["trail_side"] = None; st["trail_extremum"] = None; st["local_trail_pct"] = None
            finally:
                st["long_pending"] = False
    else:
        if st["trail_extremum"] is None or price < st["trail_extremum"]: st["trail_extremum"] = price
        trigger_price = st["trail_extremum"] * (1 + dist)
        if price >= trigger_price:
            async with st["order_lock"]:
                if st["short_pending"] or st["long_pending"]: return
                st["short_pending"] = True
            try:
                if await safe_order_execution(cli, close_short(sym), sym, "SHORT EXIT (TRAIL)"):
                    st["in_short"] = False; st["short_entry_price"] = None
                    st["trail_side"] = None; st["trail_extremum"] = None; st["local_trail_pct"] = None
            finally:
                st["short_pending"] = False

# ───────────── Exchange trailing tightener ─────────
async def dynamic_trailing_scheduler(cli: AsyncClient):
    while True:
        try:
            if not USE_EXCHANGE_TRAILING: await asyncio.sleep(1.0); continue
            for symbol in SYMBOLS:
                st = state[symbol]
                if not st.get("server_trailing_active"): continue
                if st.get("in_long"):  side, entry = "LONG",  st.get("long_entry_price")
                elif st.get("in_short"): side, entry = "SHORT", st.get("short_entry_price")
                else: continue
                price = st.get("last_price")
                pnl = _pnl_percent(entry, price, LEVERAGE, side)
                target_cb = _target_callback_rate(pnl)
                if target_cb is None: continue
                current_cb = st.get("server_trailing_cb_rate") or CALLBACK_RATE
                if target_cb < current_cb:
                    ok = await place_trailing_stop(cli, symbol, side, callback_rate=target_cb)
                    if ok: logging.info(f"{symbol} {side} tightened trailing: {current_cb:.1f}% → {target_cb:.1f}% (ROE {pnl:.2f}%)")
        except Exception as e:
            logging.error(f"dynamic_trailing_scheduler error: {e}")
        await asyncio.sleep(1.0)

# ───────────── 60s timer (kept) ─────────
async def rolling_analysis_scheduler(cli: AsyncClient):
    while True:
        try:
            for symbol in SYMBOLS:
                st = state[symbol]; current_price = st["last_price"]
                if st["initial_position_taken"] or current_price is None: continue
                now = time.time()
                if now - st["last_analysis_time"] >= ANALYSIS_INTERVAL_SECONDS:
                    logging.info(f"{symbol} Running timer-based initial direction check")
                    signal = await perform_rolling_analysis(cli, symbol, current_price)
                    if signal: await manage_initial_position(cli, symbol, current_price, signal)
            await asyncio.sleep(10)
        except Exception as e:
            logging.error(f"Rolling analysis scheduler error: {e}")
            await asyncio.sleep(30)

# ───────────── WS-first position sync; REST fallback ─────────
async def position_sync_scheduler(cli: AsyncClient):
    while True:
        try:
            now = time.time()
            for symbol in SYMBOLS:
                st = state[symbol]
                if now - st.get("last_account_update_ts", 0) < REST_SYNC_IF_STALE_SECS:
                    continue
                long_sz, short_sz = await get_position_sizes(cli, symbol)
                if long_sz <= 0 and st["in_long"]:
                    st["in_long"] = False; st["trail_side"] = None; st["trail_extremum"] = None
                    st["server_trailing_active"] = False; st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None; st["local_trail_pct"] = None
                    st["initial_position_taken"] = False
                    logging.info(f"{symbol} LONG flat (rest-sync).")
                if short_sz <= 0 and st["in_short"]:
                    st["in_short"] = False; st["trail_side"] = None; st["trail_extremum"] = None
                    st["server_trailing_active"] = False; st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None; st["local_trail_pct"] = None
                    st["initial_position_taken"] = False
                    logging.info(f"{symbol} SHORT flat (rest-sync).")
        except Exception as e:
            logging.error(f"position sync error: {e}")
        await asyncio.sleep(POSITION_SYNC_INTERVAL_SECS)

# ───────────── TICK-LEVEL ROLLING EXTREMA (new) ─────────
def _tick_extrema_push(st, ts_sec: int, price: float):
    """Push a price into the second-level rolling window and prune old entries."""
    dq = st["tick_prices"]; maxdq = st["tick_maxdq"]; mindq = st["tick_mindq"]
    dq.append((ts_sec, price))
    while maxdq and maxdq[-1][1] < price: maxdq.pop()
    maxdq.append((ts_sec, price))
    while mindq and mindq[-1][1] > price: mindq.pop()
    mindq.append((ts_sec, price))
    cutoff = ts_sec - TICK_ROLLING_WINDOW_SECS
    while dq and dq[0][0] < cutoff:
        ots, op = dq.popleft()
        if maxdq and maxdq[0][0] == ots and maxdq[0][1] == op: maxdq.popleft()
        if mindq and mindq[0][0] == ots and mindq[0][1] == op: mindq.popleft()
    st["rolling_high"] = maxdq[0][1] if maxdq else None
    st["rolling_low"]  = mindq[0][1] if mindq else None

def _preseed_tick_extrema_from_candles(symbol: str):
    """Seed second-level rolling window using the last 720×1m candles (use their time + highs/lows)."""
    st = state[symbol]
    st["tick_prices"].clear(); st["tick_maxdq"].clear(); st["tick_mindq"].clear()
    buf = st["candles"]
    for c in buf:
        ts_sec = int(c[0] // 1000)  # candle open time (approx is fine here)
        _tick_extrema_push(st, ts_sec, c[2])  # high
        _tick_extrema_push(st, ts_sec, c[3])  # low

# ───────────── preload 720×1m and seed ticks ─────────
async def preload_candles(cli: AsyncClient):
    for symbol in SYMBOLS:
        try:
            klines = await cli.get_klines(symbol=symbol, interval="1m", limit=HISTORICAL_PERIOD_MINUTES)
            buf = state[symbol]["candles"]; buf.clear()
            for k in klines:
                buf.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])])
            state[symbol]["history_loaded"] = True
            _preseed_tick_extrema_from_candles(symbol)
            logging.info(f"{symbol} preloaded {len(buf)} × 1m candles and seeded tick-rolling extrema.")
        except Exception as e:
            logging.error(f"{symbol} preload_candles error: {e}")

# ───────────── recent swing levels for flip confirm (kept) ─────────
async def get_recent_swing_levels(cli: AsyncClient, symbol: str):
    try:
        buf = state[symbol]["candles"]
        if buf and len(buf) >= 6:
            recent = list(buf)[-max(30, min(1000, CONFIRM_LOOKBACK_MIN)):]
            highs, lows = find_swing_points(recent, swing_period=5, mode="causal")
            recent_high = max(highs, key=lambda x: x[1])[0] if highs else None
            recent_low  = max(lows , key=lambda x: x[1])[0] if lows  else None
            return recent_high, recent_low
        kl = await cli.get_klines(symbol=symbol, interval="1m", limit=max(30, min(1000, CONFIRM_LOOKBACK_MIN)))
        candles = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in kl]
        highs, lows = find_swing_points(candles, swing_period=5, mode="causal")
        return (max(highs, key=lambda x: x[1])[0] if highs else None,
                max(lows , key=lambda x: x[1])[0] if lows  else None)
    except Exception as e:
        logging.error(f"{symbol} get_recent_swing_levels failed: {e}")
        return None, None

# ───────────── Flip confirmation ─────────
async def confirm_flip_if_needed(cli: AsyncClient, symbol: str, price: float):
    st = state[symbol]
    if not CONFIRM_FLIP or not st["confirm_active"]: return
    now = time.time(); side = st["confirm_side"]; level = st["confirm_swing_level"]
    if side is None or level is None:
        st["confirm_active"] = False; st["confirm_side"] = None; st["confirm_swing_level"] = None; return
    if side == "LONG":
        if price >= level * (1 + CONFIRM_BREAK_PCT):
            st["confirm_active"] = False; st["confirm_side"] = None; st["confirm_swing_level"] = None
            logging.info(f"{symbol} LONG flip confirmed."); return
    elif side == "SHORT":
        if price <= level * (1 - CONFIRM_BREAK_PCT):
            st["confirm_active"] = False; st["confirm_side"] = None; st["confirm_swing_level"] = None
            logging.info(f"{symbol} SHORT flip confirmed."); return
    if now < st["confirm_until"]: return
    # unconfirmed within 2s → flatten
    async with st["order_lock"]:
        try:
            if side == "LONG" and st["in_long"]:
                await cancel_trailing_stop(cli, symbol, "LONG")
                if await safe_order_execution(cli, close_long(symbol), symbol, "LONG EXIT (UNCONFIRMED FLIP)"):
                    st["in_long"] = False; st["long_entry_price"] = None
                    st["server_trailing_active"] = False; st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None; st["local_trail_pct"] = None; st["initial_position_taken"] = False
            elif side == "SHORT" and st["in_short"]:
                await cancel_trailing_stop(cli, symbol, "SHORT")
                if await safe_order_execution(cli, close_short(symbol), symbol, "SHORT EXIT (UNCONFIRMED FLIP)"):
                    st["in_short"] = False; st["short_entry_price"] = None
                    st["server_trailing_active"] = False; st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None; st["local_trail_pct"] = None; st["initial_position_taken"] = False
        except Exception as e:
            logging.error(f"{symbol} confirm_flip flatten error: {e}")
        finally:
            st["confirm_active"] = False; st["confirm_side"] = None; st["confirm_swing_level"] = None

# ───────────── Flip on trailing stop fill ─────────
async def on_trailing_stop_filled(cli: AsyncClient, symbol: str, exited_side: str):
    st = state[symbol]
    async with st["order_lock"]:
        if exited_side == "LONG": st["in_long"] = False; st["long_entry_price"] = None
        else: st["in_short"] = False; st["short_entry_price"] = None
        st["server_trailing_active"] = False; st["server_trailing_order_id"] = None
        st["server_trailing_cb_rate"] = None; st["trail_extremum"] = None; st["trail_side"] = None
        st["cooldown_until"] = 0.0; st["local_trail_pct"] = None
        price = st.get("last_price") or 0.0
        if exited_side == "LONG":
            if await safe_order_execution(cli, open_short(symbol), symbol, "SHORT ENTRY (FLIP on TS fill)"):
                st["in_short"] = True; st["short_entry_price"] = price
                st["trail_side"]="SHORT"; st["trail_extremum"]=price; st["local_trail_pct"]=TRAIL_PCT
                await place_trailing_stop(cli, symbol, "SHORT", CALLBACK_RATE)
                logging.info(f"{symbol} FLIP: LONG→SHORT @ ~${price:.4f}")
                if CONFIRM_FLIP:
                    recent_high, recent_low = await get_recent_swing_levels(cli, symbol)
                    st["confirm_active"]=True; st["confirm_until"]=time.time()+CONFIRM_WINDOW_SECS
                    st["confirm_side"]="SHORT"; st["confirm_swing_level"]=recent_low
        else:
            if await safe_order_execution(cli, open_long(symbol), symbol, "LONG ENTRY (FLIP on TS fill)"):
                st["in_long"]=True; st["long_entry_price"]=price
                st["trail_side"]="LONG"; st["trail_extremum"]=price; st["local_trail_pct"]=TRAIL_PCT
                await place_trailing_stop(cli, symbol, "LONG", CALLBACK_RATE)
                logging.info(f"{symbol} FLIP: SHORT→LONG @ ~${price:.4f}")
                if CONFIRM_FLIP:
                    recent_high, recent_low = await get_recent_swing_levels(cli, symbol)
                    st["confirm_active"]=True; st["confirm_until"]=time.time()+CONFIRM_WINDOW_SECS
                    st["confirm_side"]="LONG"; st["confirm_swing_level"]=recent_high
        st["initial_position_taken"] = True

# ───────────── User stream (order + account updates) ─────────
async def user_stream_listener(cli: AsyncClient):
    async def _get_futures_listen_key(cli: AsyncClient) -> str:
        resp = await cli.futures_stream_get_listen_key()
        if isinstance(resp, dict):
            lk = resp.get("listenKey") or resp.get("listen_key") or next(iter(resp.values()), None)
            return lk
        elif isinstance(resp, (list, tuple)) and resp:
            first = resp[0]
            return first.get("listenKey") or first.get("listen_key") if isinstance(first, dict) else str(first)
        return str(resp)

    try:
        listen_key = await _get_futures_listen_key(cli)
        if not listen_key: raise RuntimeError("Empty futures listen key")
        logging.info("Got futures listen key")
    except Exception as e:
        logging.error(f"Get futures listen key failed: {e}")
        return

    async def keepalive():
        while True:
            try:
                await cli.futures_stream_keepalive(listenKey=listen_key)
                logging.debug("Futures keepalive ok")
            except Exception as e:
                logging.warning(f"Keepalive failed: {e}")
            await asyncio.sleep(30*60)

    asyncio.create_task(keepalive())
    url = f"wss://fstream.binance.com/ws/{listen_key}"

    backoff = WS_RECONNECT_MIN
    while True:
        try:
            async with websockets.connect(url, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT, max_size=None, close_timeout=5) as ws:
                logging.info("User stream WS connected"); backoff = WS_RECONNECT_MIN
                async for raw in ws:
                    evt = json.loads(raw); etype = evt.get("e")
                    if etype == "ORDER_TRADE_UPDATE":
                        o = evt.get("o", {})
                        if o.get("ot") == "TRAILING_STOP_MARKET" and o.get("X") == "FILLED":
                            symbol = o.get("s"); st = state.get(symbol); 
                            if not st: continue
                            this_id = o.get("i")
                            if st.get("last_ts_filled_id") == this_id:
                                logging.warning(f"{symbol} duplicate TS FILLED ignored ({this_id})"); continue
                            st["last_ts_filled_id"] = this_id
                            await on_trailing_stop_filled(cli, symbol, o.get("ps"))  # 'LONG' or 'SHORT'
                    elif etype == "ACCOUNT_UPDATE":
                        a = evt.get("a", {}); positions = a.get("P", []) or []
                        now = time.time()
                        for p in positions:
                            symbol = p.get("s"); ps = p.get("ps")
                            if symbol not in state: continue
                            st = state[symbol]; st["last_account_update_ts"] = now
                            try:
                                amt = float(p.get("pa", 0.0)); ep = float(p.get("ep", 0.0)) if p.get("ep") is not None else None
                            except Exception:
                                amt, ep = 0.0, None
                            size = abs(amt)
                            if ps == "LONG":
                                was = st["in_long"]; st["in_long"] = size > 0
                                if st["in_long"]: st["long_entry_price"] = ep
                                elif was:
                                    st["trail_side"]=None; st["trail_extremum"]=None; st["server_trailing_active"]=False
                                    st["server_trailing_order_id"]=None; st["server_trailing_cb_rate"]=None
                                    st["local_trail_pct"]=None; st["initial_position_taken"]=False
                            elif ps == "SHORT":
                                was = st["in_short"]; st["in_short"] = size > 0
                                if st["in_short"]: st["short_entry_price"] = ep
                                elif was:
                                    st["trail_side"]=None; st["trail_extremum"]=None; st["server_trailing_active"]=False
                                    st["server_trailing_order_id"]=None; st["server_trailing_cb_rate"]=None
                                    st["local_trail_pct"]=None; st["initial_position_taken"]=False
        except Exception as e:
            wait = _jitter(min(backoff, WS_RECONNECT_MAX))
            logging.error(f"user_stream_listener error: {e} | reconnecting in {wait:.2f}s")
            await asyncio.sleep(wait); backoff = min(backoff*2, WS_RECONNECT_MAX)

# ───────────── Main market stream (trade + kline_1m) ─────────
async def run(cli: AsyncClient):
    for s in SYMBOLS: await cli.futures_change_leverage(symbol=s, leverage=LEVERAGE)
    try:
        await cli.futures_change_position_mode(dualSidePosition="true")
    except Exception as e:
        logging.warning(f"dualSidePosition already set? {e}")

    await preload_candles(cli)

    asyncio.create_task(rolling_analysis_scheduler(cli))
    asyncio.create_task(position_sync_scheduler(cli))
    asyncio.create_task(user_stream_listener(cli))
    asyncio.create_task(dynamic_trailing_scheduler(cli))

    streams = []
    for s in SYMBOLS:
        streams.append(f"{s.lower()}@trade")
        streams.append(f"{s.lower()}@kline_1m")
    url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    backoff = WS_RECONNECT_MIN
    while True:
        try:
            async with websockets.connect(url, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT, max_size=None, close_timeout=5) as ws:
                logging.info("Market WS (trade+kline_1m) connected"); backoff = WS_RECONNECT_MIN
                async for raw in ws:
                    m = json.loads(raw); stype = m.get("stream",""); d = m.get("data",{})
                    # Trade ticks: second-level rolling entries and trailing
                    if stype.endswith("@trade"):
                        sym = d["s"]; price = float(d["p"])
                        ts_ms = d.get("T")  # trade time (ms)
                        ts_sec = int(ts_ms/1000) if ts_ms is not None else int(time.time())
                        st = state[sym]

                        # Use PRE-update extrema as "previous second → 720 minutes" window
                        prev_high = st.get("rolling_high"); prev_low = st.get("rolling_low")

                        # Update window with the current tick (so next tick sees it)
                        _tick_extrema_push(st, ts_sec, price)

                        st["last_price"] = price

                        # Initial entry from second-level rolling breakouts (only if not in a position yet)
                        if not st["initial_position_taken"] and prev_high is not None and prev_low is not None:
                            if price >= prev_high * (1 + SWING_THRESHOLD_PCT):
                                await manage_initial_position(cli, sym, price, "LONG")
                            elif price <= prev_low * (1 - SWING_THRESHOLD_PCT):
                                await manage_initial_position(cli, sym, price, "SHORT")

                        await confirm_flip_if_needed(cli, sym, price)
                        await handle_trailing_stops(cli, sym, price)
                        continue

                    # 1m kline updates: maintain 720×1m buffer (kept for context/confirm)
                    if stype.endswith("@kline_1m"):
                        k = d.get("k", {}); sym = d.get("s") or k.get("s")
                        if not sym or sym not in state: continue
                        st = state[sym]; buf = st["candles"]
                        t = int(k["t"]); o=float(k["o"]); h=float(k["h"]); l=float(k["l"]); c=float(k["c"]); v=float(k["v"])
                        if not buf or buf[-1][0] != t: buf.append([t,o,h,l,c,v])
                        else: buf[-1] = [t,o,h,l,c,v]
                        continue

        except Exception as e:
            wait = _jitter(min(backoff, WS_RECONNECT_MAX))
            logging.error(f"market WS error: {e} | reconnecting in {wait:.2f}s")
            await asyncio.sleep(wait); backoff = min(backoff*2, WS_RECONNECT_MAX)

# ───────────── MAIN ─────────
async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET): raise RuntimeError("Missing Binance API creds")
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    try:
        await run(cli)
    finally:
        await cli.close_connection()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s", datefmt="%b %d %H:%M:%S")
    asyncio.run(main())
