#!/usr/bin/env python3
import os, json, asyncio, threading, logging, websockets
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
    "ADAUSDT": 10,  "BNBUSDT": 0.03,  "SOLUSDT": 0.10,
    "XRPUSDT": 10,
}

# Rolling historical analysis configuration (for initial direction only)
HISTORICAL_PERIOD_MINUTES = 720  # 12 hours rolling window
SWING_THRESHOLD_PCT = 0.02       # 2%
ANALYSIS_INTERVAL_SECONDS = 60

# Trailing stop configuration (for all position flips)
TRAIL_PCT = 0.02                 # 2%
FLIP_COOLDOWN_SECS = 1.0

# Use exchange-native trailing stop for tighter exits
USE_EXCHANGE_TRAILING = True
WORKING_TYPE = "MARK_PRICE"                     # activation price type
CALLBACK_RATE = round(TRAIL_PCT * 100, 1)       # e.g., 2.0

# Dynamic trailing mapping (PnL% → trailing distance %)
# Example: at ≥100% PnL tighten to 1.5%; ≥150% → 1.0%; ≥200% → 0.5%
DYNAMIC_TRAIL_RULES = [
    (50.0,  2.0),   # matches your example
    (100.0, 1.5),
    (150.0, 1.0),
    (200.0, 0.5),
]

# For LOSSES (tighten as loss increases – reverse)
# -50% → 1.5%, -100% → 1.0%, -150% → 0.5%
DYNAMIC_TRAIL_RULES_LOSS = [
    (-50.0,  1.5),
    (-100.0, 1.0),
    (-150.0, 0.5),
]

# Flip confirmation (Option 1): keep instant flip, then confirm with swing level or flatten
CONFIRM_FLIP = True
CONFIRM_WINDOW_SECS = 5         # time allowed to confirm the new side (changed from 60 to 5)
CONFIRM_LOOKBACK_MIN = 60       # minutes of 1m candles to derive swing levels
CONFIRM_BREAK_PCT = 0.0         # extra break beyond swing level (0% = touch/break)

# Order de-duplication window (prevents accidental duplicate sends within a short time)
ORDER_DEDUP_WINDOW_SECS = 1.5

# ────────────── QUIET /ping ──────────
class Ping(BaseHTTPRequestHandler):
    def do_GET(self, *args, **kwargs):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
    def log_message(self, *_) : pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

# ─────────── Dynamic trailing helpers ───────────
def _pnl_percent(entry_price, price, leverage, side):
    """
    Unrealized PnL% on equity, ignoring fees; side is 'LONG' or 'SHORT'.
    Returns None if missing data.
    """
    if entry_price is None or price is None:
        return None
    if side == "LONG":
        return leverage * ((price / entry_price) - 1.0) * 100.0
    else:  # SHORT
        return leverage * (1.0 - (price / entry_price)) * 100.0

def _target_callback_rate(pnl_pct):
    """
    Map PnL% → trailing distance (%) using step rules.
    Returns None if below first threshold (no change).
    """
    if pnl_pct is None:
        return None

    target = None
    if pnl_pct >= 0.0:
        # PROFIT side: e.g., 50%→2.0, 100%→1.5, 150%→1.0, 200%→0.5
        for thr, dist_pct in DYNAMIC_TRAIL_RULES:
            if pnl_pct >= thr:
                target = dist_pct
            else:
                break
    else:
        # LOSS side (reverse): e.g., -50%→1.5, -100%→1.0, -150%→0.5
        for thr, dist_pct in DYNAMIC_TRAIL_RULES_LOSS:
            if pnl_pct <= thr:
                target = dist_pct
            else:
                break

    return target

# ───────────── SWING HIGH/LOW FINDER ─────────
def find_swing_points(candles, swing_period=5):
    """
    Find swing highs and lows from historical candle data
    swing_period: minimum bars between swing points
    Returns: (swing_highs, swing_lows) as lists of (price, index)
    """
    if len(candles) < swing_period * 2 + 1:
        return [], []
    swing_highs, swing_lows = [], []
    for i in range(swing_period, len(candles) - swing_period):
        current_high = candles[i][2]
        current_low  = candles[i][3]
        is_swing_high = all(candles[j][2] < current_high for j in range(i - swing_period, i + swing_period + 1) if j != i)
        is_swing_low  = all(candles[j][3] > current_low  for j in range(i - swing_period, i + swing_period + 1) if j != i)
        if is_swing_high: swing_highs.append((current_high, i))
        if is_swing_low:  swing_lows.append((current_low,  i))
    return swing_highs, swing_lows

def analyze_rolling_swings(candles, current_price, symbol):
    """
    Analyze rolling window to determine INITIAL position direction.
    Returns: "LONG", "SHORT", or None
    """
    swing_highs, swing_lows = find_swing_points(candles)
    if not swing_highs and not swing_lows:
        logging.warning(f"{symbol} No swing points found in rolling analysis")
        return None

    recent_swing_high = max(swing_highs, key=lambda x: x[1])[0] if swing_highs else None
    recent_swing_low  = min(swing_lows , key=lambda x: x[1])[0] if swing_lows  else None

    decision = None
    if recent_swing_high and current_price >= recent_swing_high * (1 + SWING_THRESHOLD_PCT):
        decision = "LONG"
        logging.info(f"{symbol} ROLLING: LONG signal - price {current_price:.4f} > swing high {recent_swing_high:.4f}")
    elif recent_swing_low and current_price <= recent_swing_low * (1 - SWING_THRESHOLD_PCT):
        decision = "SHORT"
        logging.info(f"{symbol} ROLLING: SHORT signal - price {current_price:.4f} < swing low {recent_swing_low:.4f}")

    state[symbol]["current_swing_high"] = recent_swing_high
    state[symbol]["current_swing_low"]  = recent_swing_low
    return decision

# ───────────── ORDER HELPERS ─────────
def open_long(sym):
    return dict(symbol=sym, side="BUY",  type="MARKET",
                quantity=SYMBOLS[sym], positionSide="LONG")

def close_long(sym):
    return dict(symbol=sym, side="SELL", type="MARKET",
                quantity=SYMBOLS[sym], positionSide="LONG", reduceOnly=True)

def open_short(sym):
    return dict(symbol=sym, side="SELL", type="MARKET",
                quantity=SYMBOLS[sym], positionSide="SHORT")

def close_short(sym):
    return dict(symbol=sym, side="BUY",  type="MARKET",
                quantity=SYMBOLS[sym], positionSide="SHORT", reduceOnly=True)

# ─ NEW: exchange-native trailing stop helpers
async def cancel_trailing_stop(cli: AsyncClient, symbol: str, side: str):
    """Cancel existing trailing stop for symbol+side."""
    try:
        orders = await cli.futures_get_open_orders(symbol=symbol)
        for o in orders:
            if o.get("origType") == "TRAILING_STOP_MARKET" and o.get("positionSide") == side:
                await cli.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
                logging.info(f"{symbol} {side} canceled existing trailing stop {o['orderId']}")
    except Exception as e:
        logging.error(f"{symbol} {side} cancel trailing stop failed: {e}")

async def place_trailing_stop(cli: AsyncClient, symbol: str, side: str, callback_rate: float = None):
    """
    Place TRAILING_STOP_MARKET at the given callback rate (%) for the side.
    Side == 'LONG'  -> SELL trailing stop
    Side == 'SHORT' -> BUY trailing stop
    Note: reduceOnly is NOT accepted for TRAILING_STOP_MARKET.
    """
    try:
        qty = await get_position_size(cli, symbol, side)
        if qty <= 0:
            return False
        await cancel_trailing_stop(cli, symbol, side)
        order_side = "SELL" if side == "LONG" else "BUY"

        cb = CALLBACK_RATE if callback_rate is None else float(callback_rate)
        cb = round(cb, 1)  # Binance expects one decimal place

        res = await cli.futures_create_order(
            symbol=symbol,
            side=order_side,
            positionSide=side,
            type="TRAILING_STOP_MARKET",
            quantity=qty,
            callbackRate=cb,                # e.g., 2.0 (%)
            workingType=WORKING_TYPE,       # MARK_PRICE
            newOrderRespType="RESULT"
        )
        st = state[symbol]
        st["server_trailing_active"] = True
        st["server_trailing_order_id"] = res.get("orderId")
        st["server_trailing_cb_rate"] = cb  # remember active callback (%)
        logging.info(f"{symbol} {side} trailing stop placed @ {cb}% (order {res.get('orderId')})")
        return True
    except Exception as e:
        logging.error(f"{symbol} {side} place trailing stop failed: {e}")
        return False

# ───────────── RUNTIME STATE ─────────
state = {
    s: {
        "in_long": False, "long_pending": False,
        "in_short": False, "short_pending": False,

        "long_entry_price": None,
        "short_entry_price": None,

        "current_swing_high": None,
        "current_swing_low": None,
        "last_analysis_time": 0,
        "current_signal": None,
        "initial_position_taken": False,

        "trail_extremum": None,
        "trail_side": None,

        "cooldown_until": 0.0,

        "last_price": None,
        "order_lock": asyncio.Lock(),
        "last_order_id": None,

        # server-side trailing stop tracking
        "server_trailing_active": False,
        "server_trailing_order_id": None,
        "server_trailing_cb_rate": None,   # current exchange callback (%)

        # local trailing (software) dynamic distance (decimal, e.g., 0.02)
        "local_trail_pct": None,

        # flip confirmation tracking
        "confirm_active": False,
        "confirm_until": 0.0,
        "confirm_side": None,              # "LONG"/"SHORT"
        "confirm_swing_level": None,       # float swing level captured at flip time

        # ── Added for de-duplication ──
        "last_action_ts": {},              # action-key -> last sent time
        "last_ts_filled_id": None,         # last processed trailing-stop FILLED orderId
    }
    for s in SYMBOLS
}

# ───────────── POSITION VALIDATION ─────────
async def get_position_size(cli: AsyncClient, symbol: str, side: str) -> float:
    """Get current position size from Binance."""
    try:
        positions = await cli.futures_position_information(symbol=symbol)
        for pos in positions:
            if pos['positionSide'] == side:
                return abs(float(pos['positionAmt']))
    except Exception as e:
        logging.error(f"Failed to get position for {symbol} {side}: {e}")
    return 0.0

def _dedup_key(symbol: str, action: str, order_params: dict) -> str:
    """Create a lightweight key to detect accidental duplicate sends."""
    return f"{symbol}|{action}|{order_params.get('side')}|{order_params.get('type')}|{order_params.get('positionSide')}|{order_params.get('reduceOnly', False)}"

def _new_coid(symbol: str, action: str) -> str:
    """Generate a client order ID for idempotency on Binance side."""
    return f"{symbol}-{action}-{int(time.time()*1000)}"

async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    """Execute order with duplicate prevention and validation."""
    try:
        # ── De-dup guard (in-memory) ──
        now = time.time()
        st = state[symbol]
        key = _dedup_key(symbol, action, order_params)
        last = st["last_action_ts"].get(key, 0.0)
        if now - last < ORDER_DEDUP_WINDOW_SECS:
            logging.warning(f"{symbol} {action}: skipped duplicate within {ORDER_DEDUP_WINDOW_SECS}s window")
            return False
        st["last_action_ts"][key] = now

        # Size validation for closes
        if action.startswith("CLOSE") or "EXIT" in action:
            side = "LONG" if "LONG" in action else "SHORT"
            current_pos = await get_position_size(cli, symbol, side)
            required_qty = order_params['quantity']
            if current_pos < required_qty * 0.99:
                logging.warning(f"{symbol} {action}: Insufficient position size {current_pos} < {required_qty}")
                return False

        # Add client order ID for exchange-side idempotency if not provided
        order_params = dict(order_params)  # shallow copy
        order_params.setdefault("newClientOrderId", _new_coid(symbol, action))

        result = await cli.futures_create_order(**order_params)
        st["last_order_id"] = result.get('orderId')
        logging.info(f"{symbol} {action} executed - OrderID: {st['last_order_id']}")
        return True
    except Exception as e:
        logging.error(f"{symbol} {action} failed: {e}")
        return False

# ───────────── ROLLING ANALYSIS (Initial Direction Only) ─────────
async def perform_rolling_analysis(cli: AsyncClient, symbol: str, current_price: float):
    """Analyze rolling 720-minute window for INITIAL position direction only."""
    try:
        klines = await cli.get_klines(symbol=symbol, interval="1m", limit=HISTORICAL_PERIOD_MINUTES)
        candles = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]
        signal = analyze_rolling_swings(candles, current_price, symbol)
        st = state[symbol]
        st["current_signal"] = signal
        st["last_analysis_time"] = time.time()
        return signal
    except Exception as e:
        logging.error(f"{symbol} Rolling analysis failed: {e}")
        return None

# ───────────── ENTRY/EXIT HELPERS ─────────
async def enter_long(cli: AsyncClient, sym: str, price: float, reason: str = ""):
    st = state[sym]
    async with st["order_lock"]:
        if st["in_long"] or st["long_pending"] or st["in_short"] or st["short_pending"]:
            return False
        st["long_pending"] = True
    try:
        if await safe_order_execution(cli, open_long(sym), sym, f"LONG ENTRY {reason}"):
            st["in_long"] = True
            st["long_entry_price"] = price
            st["trail_side"] = "LONG"
            st["trail_extremum"] = price
            st["local_trail_pct"] = TRAIL_PCT   # baseline for local trailing
            if USE_EXCHANGE_TRAILING:
                await place_trailing_stop(cli, sym, "LONG", CALLBACK_RATE)
            logging.info(f"{sym} LONG OPEN @ ${price:.4f} | exchange trailing {CALLBACK_RATE}%")
            return True
    finally:
        st["long_pending"] = False
    return False

async def enter_short(cli: AsyncClient, sym: str, price: float, reason: str = ""):
    st = state[sym]
    async with st["order_lock"]:
        if st["in_short"] or st["short_pending"] or st["in_long"] or st["long_pending"]:
            return False
        st["short_pending"] = True
    try:
        if await safe_order_execution(cli, open_short(sym), sym, f"SHORT ENTRY {reason}"):
            st["in_short"] = True
            st["short_entry_price"] = price
            st["trail_side"] = "SHORT"
            st["trail_extremum"] = price
            st["local_trail_pct"] = TRAIL_PCT   # baseline for local trailing
            if USE_EXCHANGE_TRAILING:
                await place_trailing_stop(cli, sym, "SHORT", CALLBACK_RATE)
            logging.info(f"{sym} SHORT OPEN @ ${price:.4f} | exchange trailing {CALLBACK_RATE}%")
            return True
    finally:
        st["short_pending"] = False
    return False

# ───────────── INITIAL POSITION MANAGEMENT ─────────
async def manage_initial_position(cli: AsyncClient, sym: str, price: float, signal: str):
    st = state[sym]
    if st["initial_position_taken"]:
        return
    if signal == "LONG" and not st["in_long"] and not st["in_short"]:
        if await enter_long(cli, sym, price, "(ROLLING SIGNAL)"):
            st["initial_position_taken"] = True
            logging.info(f"{sym} INITIAL position established: LONG")
    elif signal == "SHORT" and not st["in_short"] and not st["in_long"]:
        if await enter_short(cli, sym, price, "(ROLLING SIGNAL)"):
            st["initial_position_taken"] = True
            logging.info(f"{sym} INITIAL position established: SHORT")

# ───────────── TRAILING STOP LOGIC (local software) ─────────
async def handle_trailing_stops(cli: AsyncClient, sym: str, price: float):
    """
    Local (software) trailing stop.
    When exchange-native trailing is active, we defer to the exchange for exits.
    With server trailing OFF/INACTIVE, we trail from the local extremum using a
    dynamic distance that tightens with unrealized PnL% per DYNAMIC_TRAIL_RULES.
    """
    st = state[sym]
    if USE_EXCHANGE_TRAILING and st.get("server_trailing_active"):
        return

    side = st["trail_side"]
    if side not in ("LONG", "SHORT") or not st["initial_position_taken"]:
        return

    # Dynamic trailing distance based on PnL% (tighten-only)
    entry = st["long_entry_price"] if side == "LONG" else st["short_entry_price"]
    pnl = _pnl_percent(entry, price, LEVERAGE, side)
    target_cb = _target_callback_rate(pnl)  # percent, e.g. 1.5

    if st["local_trail_pct"] is None:
        st["local_trail_pct"] = TRAIL_PCT  # decimal, e.g., 0.02

    if target_cb is not None:
        target_dec = target_cb / 100.0
        if target_dec < st["local_trail_pct"]:
            prev = st["local_trail_pct"]
            st["local_trail_pct"] = target_dec
            logging.info(
                f"{sym} {side} local trailing tightened: {prev*100:.1f}% → {st['local_trail_pct']*100:.1f}% (PnL {pnl:.2f}%)"
            )

    now = time.time()
    if st["cooldown_until"] and now < st["cooldown_until"]:
        # keep updating extremum during cooldown
        if side == "LONG":
            if st["trail_extremum"] is None or price > st["trail_extremum"]:
                st["trail_extremum"] = price
        else:
            if st["trail_extremum"] is None or price < st["trail_extremum"]:
                st["trail_extremum"] = price
        return

    # Track extremum and compute dynamic trigger
    dist = st["local_trail_pct"]  # decimal, e.g., 0.02 for 2%
    if side == "LONG":
        if st["trail_extremum"] is None or price > st["trail_extremum"]:
            st["trail_extremum"] = price
        trigger_price = st["trail_extremum"] * (1 - dist)
        if price <= trigger_price:
            async with st["order_lock"]:
                if st["long_pending"] or st["short_pending"]:
                    return
                st["long_pending"] = True
            try:
                if await safe_order_execution(cli, close_long(sym), sym, "LONG EXIT (TRAIL)"):
                    st["in_long"] = False
                    st["long_entry_price"] = None
                    st["trail_side"] = None
                    st["trail_extremum"] = None
                    st["local_trail_pct"] = None
            finally:
                st["long_pending"] = False
    else:  # SHORT
        if st["trail_extremum"] is None or price < st["trail_extremum"]:
            st["trail_extremum"] = price
        trigger_price = st["trail_extremum"] * (1 + dist)
        if price >= trigger_price:
            async with st["order_lock"]:
                if st["short_pending"] or st["long_pending"]:
                    return
                st["short_pending"] = True
            try:
                if await safe_order_execution(cli, close_short(sym), sym, "SHORT EXIT (TRAIL)"):
                    st["in_short"] = False
                    st["short_entry_price"] = None
                    st["trail_side"] = None
                    st["trail_extremum"] = None
                    st["local_trail_pct"] = None
            finally:
                st["short_pending"] = False

# ───────────── Exchange trailing dynamic-tightening ─────────
async def dynamic_trailing_scheduler(cli: AsyncClient):
    """
    Periodically tighten the exchange-native trailing stop based on unrealized PnL%.
    We NEVER loosen (only replace with a SMALLER callbackRate).
    """
    while True:
        try:
            if not USE_EXCHANGE_TRAILING:
                await asyncio.sleep(1.0)
                continue

            for symbol in SYMBOLS:
                st = state[symbol]
                if not st.get("server_trailing_active"):
                    continue

                # Determine current side and entry price
                if st.get("in_long"):
                    side = "LONG"
                    entry = st.get("long_entry_price")
                elif st.get("in_short"):
                    side = "SHORT"
                    entry = st.get("short_entry_price")
                else:
                    continue

                price = st.get("last_price")
                pnl = _pnl_percent(entry, price, LEVERAGE, side)
                target_cb = _target_callback_rate(pnl)  # e.g., 1.5 (percent)

                if target_cb is None:
                    continue  # below first threshold → keep current trailing

                current_cb = st.get("server_trailing_cb_rate")
                if current_cb is None:
                    current_cb = CALLBACK_RATE

                # Only tighten (smaller callback = tighter)
                if target_cb < current_cb:
                    ok = await place_trailing_stop(cli, symbol, side, callback_rate=target_cb)
                    if ok:
                        logging.info(
                            f"{symbol} {side} tightened trailing: {current_cb:.1f}% → {target_cb:.1f}% (PnL {pnl:.2f}%)"
                        )
        except Exception as e:
            logging.error(f"dynamic_trailing_scheduler error: {e}")

        await asyncio.sleep(1.0)

# ───────────── ROLLING ANALYSIS SCHEDULER ─────────
async def rolling_analysis_scheduler(cli: AsyncClient):
    """Run rolling analysis every 60 seconds ONLY for initial position determination."""
    while True:
        try:
            for symbol in SYMBOLS:
                st = state[symbol]
                current_price = st["last_price"]
                if st["initial_position_taken"] or current_price is None:
                    continue
                now = time.time()
                if now - st["last_analysis_time"] >= ANALYSIS_INTERVAL_SECONDS:
                    logging.info(f"{symbol} Running rolling analysis for INITIAL direction")
                    signal = await perform_rolling_analysis(cli, symbol, current_price)
                    if signal:
                        await manage_initial_position(cli, symbol, current_price, signal)
            await asyncio.sleep(10)
        except Exception as e:
            logging.error(f"Rolling analysis scheduler error: {e}")
            await asyncio.sleep(30)

# ─ NEW: keep state in sync if something flattened positions
async def position_sync_scheduler(cli: AsyncClient):
    while True:
        try:
            for symbol in SYMBOLS:
                st = state[symbol]
                long_sz  = await get_position_size(cli, symbol, "LONG")
                short_sz = await get_position_size(cli, symbol, "SHORT")
                if long_sz <= 0 and st["in_long"]:
                    st["in_long"] = False
                    st["trail_side"] = None
                    st["trail_extremum"] = None
                    st["server_trailing_active"] = False
                    st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None
                    st["local_trail_pct"] = None
                    st["initial_position_taken"] = False
                    logging.info(f"{symbol} LONG flat (sync).")
                if short_sz <= 0 and st["in_short"]:
                    st["in_short"] = False
                    st["trail_side"] = None
                    st["trail_extremum"] = None
                    st["server_trailing_active"] = False
                    st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None
                    st["local_trail_pct"] = None
                    st["initial_position_taken"] = False
                    logging.info(f"{symbol} SHORT flat (sync).")
        except Exception as e:
            logging.error(f"position sync error: {e}")
        await asyncio.sleep(3)

# ─── Helper: get listen key robustly across client versions
async def _get_futures_listen_key(cli: AsyncClient) -> str:
    resp = await cli.futures_stream_get_listen_key()
    if isinstance(resp, dict):
        lk = resp.get("listenKey") or resp.get("listen_key")
        if not lk and resp:
            lk = next(iter(resp.values()))
        return lk
    elif isinstance(resp, (list, tuple)) and resp:
        first = resp[0]
        if isinstance(first, dict):
            return first.get("listenKey") or first.get("listen_key")
        return str(first)
    else:
        return str(resp)

# ─── Helper: fetch recent candles and compute latest swing levels for confirmation
async def get_recent_swing_levels(cli: AsyncClient, symbol: str):
    """
    Returns (recent_swing_high, recent_swing_low) from last CONFIRM_LOOKBACK_MIN minutes of 1m candles.
    """
    try:
        limit = max(30, min(1000, CONFIRM_LOOKBACK_MIN))  # 1m candles
        klines = await cli.get_klines(symbol=symbol, interval="1m", limit=limit)
        candles = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]
        highs, lows = find_swing_points(candles)
        recent_high = max(highs, key=lambda x: x[1])[0] if highs else None
        recent_low  = min(lows , key=lambda x: x[1])[0] if lows  else None
        return recent_high, recent_low
    except Exception as e:
        logging.error(f"{symbol} get_recent_swing_levels failed: {e}")
        return None, None

# ───────────── FLIP CONFIRMATION (Option 1) ─────────
async def confirm_flip_if_needed(cli: AsyncClient, symbol: str, price: float):
    """
    After an instant flip, require price to confirm by breaking the recent swing.
    If not confirmed within CONFIRM_WINDOW_SECS, flatten and wait for next initial signal.
    """
    st = state[symbol]
    if not CONFIRM_FLIP or not st["confirm_active"]:
        return

    now = time.time()
    side = st["confirm_side"]
    level = st["confirm_swing_level"]

    # If nothing to confirm, reset flags
    if side is None or level is None:
        st["confirm_active"] = False
        st["confirm_side"] = None
        st["confirm_swing_level"] = None
        return

    if side == "LONG":
        needed = level * (1 + CONFIRM_BREAK_PCT)
        if price >= needed:
            st["confirm_active"] = False
            st["confirm_side"] = None
            st["confirm_swing_level"] = None
            logging.info(f"{symbol} LONG flip confirmed (price {price:.4f} >= swingHigh {needed:.4f}).")
            return
    elif side == "SHORT":
        needed = level * (1 - CONFIRM_BREAK_PCT)
        if price <= needed:
            st["confirm_active"] = False
            st["confirm_side"] = None
            st["confirm_swing_level"] = None
            logging.info(f"{symbol} SHORT flip confirmed (price {price:.4f} <= swingLow {needed:.4f}).")
            return

    # keep waiting if within window
    if now < st["confirm_until"]:
        return

    # Window expired → flatten the flipped side and await next initial signal
    async with st["order_lock"]:
        try:
            if side == "LONG" and st["in_long"]:
                await cancel_trailing_stop(cli, symbol, "LONG")
                if await safe_order_execution(cli, close_long(symbol), symbol, "LONG EXIT (UNCONFIRMED FLIP)"):
                    st["in_long"] = False
                    st["long_entry_price"] = None
                    st["server_trailing_active"] = False
                    st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None
                    st["local_trail_pct"] = None
                    st["initial_position_taken"] = False
            elif side == "SHORT" and st["in_short"]:
                await cancel_trailing_stop(cli, symbol, "SHORT")
                if await safe_order_execution(cli, close_short(symbol), symbol, "SHORT EXIT (UNCONFIRMED FLIP)"):
                    st["in_short"] = False
                    st["short_entry_price"] = None
                    st["server_trailing_active"] = False
                    st["server_trailing_order_id"] = None
                    st["server_trailing_cb_rate"] = None
                    st["local_trail_pct"] = None
                    st["initial_position_taken"] = False
        except Exception as e:
            logging.error(f"{symbol} confirm_flip flatten error: {e}")
        finally:
            st["confirm_active"] = False
            st["confirm_side"] = None
            st["confirm_swing_level"] = None

# ───────────── FLIP ON TRAILING STOP FILL ─────────
async def on_trailing_stop_filled(cli: AsyncClient, symbol: str, exited_side: str):
    """
    Called from user stream when our TRAILING_STOP_MARKET is FILLED.
    Immediately flip to the opposite side, attach a new trailing stop,
    and start the confirmation window based on recent swing levels.
    """
    st = state[symbol]
    async with st["order_lock"]:
        # Clear the exited side
        if exited_side == "LONG":
            st["in_long"] = False
            st["long_entry_price"] = None
        else:
            st["in_short"] = False
            st["short_entry_price"] = None

        st["server_trailing_active"] = False
        st["server_trailing_order_id"] = None
        st["server_trailing_cb_rate"] = None
        st["trail_extremum"] = None
        st["trail_side"] = None
        st["cooldown_until"] = 0.0
        st["local_trail_pct"] = None

        # Instant flip
        price = st.get("last_price") or 0.0
        if exited_side == "LONG":
            if await safe_order_execution(cli, open_short(symbol), symbol, "SHORT ENTRY (FLIP on TS fill)"):
                st["in_short"] = True
                st["short_entry_price"] = price
                st["trail_side"] = "SHORT"
                st["trail_extremum"] = price
                st["local_trail_pct"] = TRAIL_PCT
                await place_trailing_stop(cli, symbol, "SHORT", CALLBACK_RATE)
                logging.info(f"{symbol} FLIP: LONG→SHORT on trailing stop fill @ ~${price:.4f}")
                if CONFIRM_FLIP:
                    recent_high, recent_low = await get_recent_swing_levels(cli, symbol)
                    st["confirm_active"] = True
                    st["confirm_until"] = time.time() + CONFIRM_WINDOW_SECS
                    st["confirm_side"] = "SHORT"
                    st["confirm_swing_level"] = recent_low
        else:
            if await safe_order_execution(cli, open_long(symbol), symbol, "LONG ENTRY (FLIP on TS fill)"):
                st["in_long"] = True
                st["long_entry_price"] = price
                st["trail_side"] = "LONG"
                st["trail_extremum"] = price
                st["local_trail_pct"] = TRAIL_PCT
                await place_trailing_stop(cli, symbol, "LONG", CALLBACK_RATE)
                logging.info(f"{symbol} FLIP: SHORT→LONG on trailing stop fill @ ~${price:.4f}")
                if CONFIRM_FLIP:
                    recent_high, recent_low = await get_recent_swing_levels(cli, symbol)
                    st["confirm_active"] = True
                    st["confirm_until"] = time.time() + CONFIRM_WINDOW_SECS
                    st["confirm_side"] = "LONG"
                    st["confirm_swing_level"] = recent_high

        st["initial_position_taken"] = True

# ─ NEW: listen for order updates to catch trailing-stop fills
async def user_stream_listener(cli: AsyncClient):
    try:
        listen_key = await _get_futures_listen_key(cli)
        if not listen_key:
            raise RuntimeError("Empty futures listen key")
        logging.info("Got futures listen key")
    except Exception as e:
        logging.error(f"Get futures listen key failed: {e}")
        return

    async def keepalive():
        while True:
            try:
                await cli.futures_stream_keepalive(listenKey=listen_key)
                logging.debug("Futures listen key keepalive ok")
            except Exception as e:
                logging.warning(f"Keepalive failed (will retry): {e}")
            await asyncio.sleep(30 * 60)

    asyncio.create_task(keepalive())
    url = f"wss://fstream.binance.com/ws/{listen_key}"

    while True:
        try:
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    evt = json.loads(raw)
                    if evt.get("e") != "ORDER_TRADE_UPDATE":
                        continue
                    o = evt.get("o", {})
                    # Watch for our trailing stop fills
                    if o.get("ot") == "TRAILING_STOP_MARKET" and o.get("X") == "FILLED":
                        symbol = o.get("s")
                        st = state.get(symbol)
                        if not st:
                            continue
                        this_id = o.get("i")  # orderId
                        # ── De-dup: ignore the same FILLED TS event more than once ──
                        if st.get("last_ts_filled_id") == this_id:
                            logging.warning(f"{symbol} duplicate TS FILLED event ignored (order {this_id})")
                            continue
                        st["last_ts_filled_id"] = this_id

                        side_ps = o.get("ps")  # 'LONG' or 'SHORT'
                        logging.info(f"{symbol} trailing stop FILLED for {side_ps} (order {this_id})")
                        await on_trailing_stop_filled(cli, symbol, side_ps)
        except Exception as e:
            logging.error(f"user_stream_listener error: {e}")
            await asyncio.sleep(5)

# ───────────── MAIN LOOP ─────────
async def run(cli: AsyncClient):
    for s in SYMBOLS:
        await cli.futures_change_leverage(symbol=s, leverage=LEVERAGE)

    # Ensure dual-side (hedge) mode for positionSide LONG/SHORT
    try:
        await cli.futures_change_position_mode(dualSidePosition="true")
    except Exception as e:
        logging.warning(f"Could not set dualSidePosition=true (may already be set): {e}")

    asyncio.create_task(rolling_analysis_scheduler(cli))
    asyncio.create_task(position_sync_scheduler(cli))
    asyncio.create_task(user_stream_listener(cli))  # enable flip on trailing fill
    asyncio.create_task(dynamic_trailing_scheduler(cli))  # dynamic tightening of server trailing

    streams = [f"{s.lower()}@trade" for s in SYMBOLS]
    url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    async with websockets.connect(url) as ws:
        async for raw in ws:
            m = json.loads(raw)
            stype = m["stream"]; d = m["data"]
            if not stype.endswith("@trade"):
                continue
            sym = d["s"]
            price = float(d["p"])
            st = state[sym]
            st["last_price"] = price

            # confirm flip if needed (lightweight)
            await confirm_flip_if_needed(cli, sym, price)

            # local trailing logic only if server trailing not active
            await handle_trailing_stops(cli, sym, price)

async def main():
    threading.Thread(target=start_ping, daemon=True).start()
    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API creds")
    cli = await AsyncClient.create(API_KEY, API_SECRET)
    await run(cli)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(message)s",
        datefmt="%b %d %H:%M:%S"
    )
    asyncio.run(main())
