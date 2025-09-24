#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, random
import numpy as np
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

# ---- SIMPLE STRATEGY ----
CALLBACK_PERCENT = 0.5   # ONLY exit mechanism (exchange trailing stop)

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

# ---- BOLLINGER PARAMETERS ----
WMA_PERIOD = 1200
STD_MULTIPLIER = 0.5
PRICE_HISTORY_SIZE = 1500

# ---- PRICE SOURCE ----
def _ws_host() -> str:
    return "fstream.binance.com"
def _stream_name(sym: str) -> str:
    return f"{sym.lower()}@markPrice@1s"

# ---- RISK MANAGEMENT ----
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_BASE_SEC = 300
PRICE_STALENESS_SEC = 5.0
RATE_LIMIT_BASE_SLEEP = 2.0
RATE_LIMIT_MAX_SLEEP = 60.0

# ---- REST 403 BLOCKING ----
class RestBlockedError(Exception):
    pass

REST_BLOCKED_UNTIL = 0.0
_last_rest_warn = 0.0

def rest_blocked() -> bool:
    return time.time() < REST_BLOCKED_UNTIL

def _note_rest_block():
    remaining = int(REST_BLOCKED_UNTIL - time.time())
    if remaining > 0:
        logging.warning(f"REST temporarily blocked (HTTP 403). Backing off ~{remaining}s.")

# ---- LOG THROTTLING (only show some infos every 5 min) ----
LOG_SUPPRESS_SECS = int(os.getenv("LOG_SUPPRESS_SECS", "300"))
_LAST_LOG_TS: Dict[Tuple[str, str], float] = {}

def _log_every(symbol: str, key: str, message: str, level: int = logging.INFO):
    """
    Rate-limit a specific info message per symbol+key to once per LOG_SUPPRESS_SECS.
    Does nothing to trading; purely cosmetic.
    """
    now = time.time()
    k = (symbol, key)
    last = _LAST_LOG_TS.get(k, 0.0)
    if now - last >= LOG_SUPPRESS_SECS:
        logging.log(level, message)
        _LAST_LOG_TS[k] = now

class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200); self.end_headers()
    def log_message(self, *_): pass

def start_ping():
    HTTPServer(("0.0.0.0", 10000), Ping).serve_forever()

# ========================= BINANCE HELPERS =========================
QUANTITY_STEP: Dict[str, Decimal] = {}

def quantize_qty(symbol: str, qty: float) -> float:
    step = QUANTITY_STEP.get(symbol)
    if step:
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_HALF_UP) * step
        return float(q)
    return round(qty, 3)

_last_rest_ts = 0.0
async def call_binance(fn, *args, **kwargs):
    global _last_rest_ts, REST_BLOCKED_UNTIL, _last_rest_warn
    now = time.time()

    # Respect current block
    if now < REST_BLOCKED_UNTIL:
        raise RuntimeError("REST blocked")

    if now - _last_rest_ts < 0.2:
        await asyncio.sleep(0.2 - (now - _last_rest_ts))
    _last_rest_ts = time.time()

    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            es = str(e)
            # 403: set a timed block and show the warning at most ~10s
            if "403" in es:
                backoff = 90 + random.randint(15, 45)
                REST_BLOCKED_UNTIL = time.time() + backoff
                if time.time() - _last_rest_warn > 10:
                    _last_rest_warn = time.time()
                    logging.warning(f"REST temporarily blocked (HTTP 403). Backing off ~{backoff}s.")
                raise RuntimeError("REST blocked")
            if "429" in es or "-1003" in es:
                await asyncio.sleep(min(delay, RATE_LIMIT_MAX_SLEEP))
                delay = min(delay * 2.0, RATE_LIMIT_MAX_SLEEP)
                continue
            raise
            
# ========================= POSITION STATE SYNC =========================
async def sync_position_state(cli: AsyncClient):
    if rest_blocked():
        return
    logging.info("=== SYNCING POSITION STATE ===")
    # ... rest unchanged
    for symbol in SYMBOLS:
        st = state[symbol]
        try:
            long_size, short_size = await get_position_sizes(cli, symbol)
            if long_size > 0.1:
                st["current_signal"] = "LONG"
                logging.info(f"{symbol}: Found existing LONG position - State synced")
            elif short_size > 0.1:
                st["current_signal"] = "SHORT"
                logging.info(f"{symbol}: Found existing SHORT position - State synced")
            else:
                st["current_signal"] = None
                logging.info(f"{symbol}: No existing position - Starting fresh")
        except RestBlockedError:
            _note_rest_block()
            return
        except Exception as e:
            logging.error(f"{symbol}: Failed to sync position state: {e}")

DUAL_SIDE = False

async def get_dual_side(cli: AsyncClient) -> bool:
    try:
        res = await call_binance(cli.futures_get_position_mode)
        return bool(res.get("dualSidePosition", False))
    except RestBlockedError:
        _note_rest_block()
        return False
    except Exception:
        return False

async def get_position_sizes(cli: AsyncClient, symbol: str) -> Tuple[float, float]:
    try:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
        long_size = short_size = 0.0
        for pos in positions:
            side = pos.get("positionSide")
            size = abs(float(pos.get("positionAmt", 0.0)))
            if side == "LONG":
                long_size = size
            elif side == "SHORT":
                short_size = size
        return long_size, short_size
    except RestBlockedError:
        raise
    except Exception as e:
        logging.error(f"{symbol} get_position_sizes failed: {e}")
        return 0.0, 0.0

# ========================= STATE ===============================
state: Dict[str, Dict[str, Any]] = {
    s: {
        # Price data
        "last_price": None,
        "price_timestamp": None,
        "price_history": deque(maxlen=PRICE_HISTORY_SIZE),

        # Current state
        "current_signal": None,

        # Risk management
        "consecutive_failures": 0,
        "circuit_breaker_until": 0.0,
        "order_lock": asyncio.Lock(),
        "position_state": "IDLE",

        # Bollinger Band state
        "last_bollinger_bands": None,

        # ONLY for bounce-back logic - no internal exit tracking
        "entry_price": None,

        # Exit history (for bounce-back)
        "long_exit_history": deque(maxlen=5),
        "short_exit_history": deque(maxlen=5),
    } for s in SYMBOLS
}

def symbol_target_qty(sym: str) -> float:
    return float(SYMBOLS[sym])

# ========================= BOLLINGER CALCULATIONS =========================
def weighted_moving_average(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    recent_prices = prices[-period:]
    weights = np.arange(1, period + 1)
    try:
        wma = np.average(recent_prices, weights=weights)
        return float(wma)
    except Exception as e:
        logging.warning(f"WMA calculation error: {e}")
        return None

def calculate_bollinger_bands(symbol: str) -> Optional[Dict[str, float]]:
    st = state[symbol]
    price_history = list(st["price_history"])

    if len(price_history) < WMA_PERIOD:
        return None

    wma = weighted_moving_average(price_history, WMA_PERIOD)
    if wma is None:
        return None

    recent_prices = price_history[-WMA_PERIOD:]
    try:
        std_dev = np.std(recent_prices)
        upper_band = wma + (std_dev * STD_MULTIPLIER)
        lower_band = wma - (std_dev * STD_MULTIPLIER)

        bands = {
            'wma': wma,
            'upper_band': upper_band,
            'lower_band': lower_band,
            'std_dev': std_dev
        }

        st["last_bollinger_bands"] = bands
        return bands

    except Exception as e:
        logging.warning(f"{symbol}: Bollinger calculation error: {e}")
        return None

# ========================= SIMPLE SIGNAL DETECTION =========================
def detect_signals(symbol: str) -> Optional[str]:
    """
    ENTRY-ONLY (no exits, no flips). Exits are handled by the exchange trailing stop.

    Logic per side (LONG/SHORT):

    1) If already in a position: hold (return current side).
    2) If no prior exits on that side -> require Bollinger gate (first-ever entry).
    3) If re-entry with >=2 exits:
        - If trend improving (LONG: latest > prev; SHORT: latest < prev) -> ALLOW
        - If trend worsening BUT bounce back (LONG: price > prev; SHORT: price < prev) -> ALLOW
        - If trend worsening AND no bounce back -> FALL BACK to Bollinger gate (do NOT hard-block)
    4) If exactly 1 prior exit: allow on bounce, else fall back to Bollinger gate.
    5) If both sides allowed, prefer the side whose Bollinger gate is true now; else use tiny momentum tie-breaker.
    """
    st = state[symbol]
    cur = st.get("current_signal")
    price = st.get("last_price")

    # If already in a position: keep holding (exits via exchange trailing stop)
    if cur in ("LONG", "SHORT"):
        return cur

    if price is None:
        return None

    bands = calculate_bollinger_bands(symbol)
    upper_band = bands["upper_band"] if bands else None
    lower_band = bands["lower_band"] if bands else None

    long_exits = list(st["long_exit_history"])
    short_exits = list(st["short_exit_history"])

    # --- helpers implementing your rules with Bollinger fallback when "blocked" ---
    def long_allowed() -> bool:
        # First-ever LONG: require Bollinger gate
        if len(long_exits) == 0:
            return (upper_band is not None) and (price >= upper_band)

        # Exactly 1 prior LONG exit: allow on bounce; else fall back to Bollinger gate
        if len(long_exits) == 1:
            prev = long_exits[-1]
            if price > prev:
                return True
            return (upper_band is not None) and (price >= upper_band)

        # 2+ prior exits: apply improving/worsening + bounce, else fall back to Bollinger
        latest, prev = long_exits[-1], long_exits[-2]
        if latest > prev:                        # trend improving -> ALLOW
            return True
        if price > prev:                         # trend worsening but bounced -> ALLOW
            return True
        # trend worsening AND no bounce -> fall back to entry condition (Bollinger gate)
        return (upper_band is not None) and (price >= upper_band)

    def short_allowed() -> bool:
        # First-ever SHORT: require Bollinger gate
        if len(short_exits) == 0:
            return (lower_band is not None) and (price <= lower_band)

        # Exactly 1 prior SHORT exit: allow on bounce; else fall back to Bollinger gate
        if len(short_exits) == 1:
            prev = short_exits[-1]
            if price < prev:
                return True
            return (lower_band is not None) and (price <= lower_band)

        # 2+ prior exits: apply improving/worsening + bounce, else fall back to Bollinger
        latest, prev = short_exits[-1], short_exits[-2]
        if latest < prev:                        # trend improving -> ALLOW
            return True
        if price < prev:                         # trend worsening but bounced -> ALLOW
            return True
        # trend worsening AND no bounce -> fall back to entry condition (Bollinger gate)
        return (lower_band is not None) and (price <= lower_band)

    can_long = long_allowed()
    can_short = short_allowed()

    if not can_long and not can_short:
        return None
    if can_long and not can_short:
        return "LONG"
    if can_short and not can_long:
        return "SHORT"

    # Both sides allowed → prefer the one whose Bollinger gate is currently true (if any)
    long_gate = (upper_band is not None) and (price >= upper_band)
    short_gate = (lower_band is not None) and (price <= lower_band)
    if long_gate and not short_gate:
        return "LONG"
    if short_gate and not long_gate:
        return "SHORT"

    # Tie-breaker: micro momentum from last 2 ticks
    ph = st.get("price_history", [])
    if len(ph) >= 2 and ph[-1] is not None and ph[-2] is not None:
        delta = ph[-1] - ph[-2]
        if delta > 0:
            return "LONG"
        if delta < 0:
            return "SHORT"

    # Final fallback: no trade
    return None
    
# ========================= ORDER EXECUTION =========================
def _maybe_pos_side(params: dict, side: str) -> dict:
    if DUAL_SIDE:
        params["positionSide"] = side
    else:
        params.pop("positionSide", None)
    return params

def open_long(sym: str, qty: float):
    return _maybe_pos_side(dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty)), "LONG")

def open_short(sym: str, qty: float):
    return _maybe_pos_side(dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty)), "SHORT")

def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return _maybe_pos_side(p, "LONG")

def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE:
        p["reduceOnly"] = True
    return _maybe_pos_side(p, "SHORT")

async def execute_order(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    try:
        res = await call_binance(cli.futures_create_order, **order_params)
        oid = res.get("orderId")
        logging.info(f"{symbol} {action} EXECUTED - OrderID: {oid}")
        return True
    except RestBlockedError:
        _note_rest_block()
        return False
    except Exception as e:
        msg = str(e)
        if "-1106" in msg and "reduceonly" in msg.lower() and "reduceOnly" in order_params:
            try:
                params_retry = {k: v for k, v in order_params.items() if k != "reduceOnly"}
                res = await call_binance(cli.futures_create_order, **params_retry)
                oid = res.get("orderId")
                logging.info(f"{symbol} {action} EXECUTED (no reduceOnly) - OrderID: {oid}")
                return True
            except RestBlockedError:
                _note_rest_block()
                return False
            except Exception as e2:
                logging.error(f"{symbol} {action} RETRY FAILED: {e2}")
        else:
            logging.error(f"{symbol} {action} FAILED: {e}")
        return False

async def set_position(cli: AsyncClient, symbol: str, target_signal: Optional[str]):
    st = state[symbol]
    async with st["order_lock"]:
        if st["position_state"] != "IDLE":
            return False
        st["position_state"] = "PROCESSING"

        try:
            if _is_rest_blocked():
                _note_rest_block()
                return False

            long_size, short_size = await get_position_sizes(cli, symbol)
            target_qty = symbol_target_qty(symbol)
            current_price = st.get("last_price")

            success = True

            if target_signal in ("LONG", "SHORT"):
                if target_signal == "LONG":
                    if short_size > 0.1:
                        success &= await execute_order(cli, exit_short(symbol, short_size), symbol, "CLOSE SHORT")
                    if success and long_size < target_qty * 0.9:
                        delta = target_qty - long_size
                        if await execute_order(cli, open_long(symbol, delta), symbol, "OPEN LONG"):
                            trailing_params = {
                                "symbol": symbol,
                                "side": "SELL",
                                "type": "TRAILING_STOP_MARKET",
                                "quantity": quantize_qty(symbol, delta),
                                "callbackRate": str(CALLBACK_PERCENT),
                                "timeInForce": "GTC"
                            }
                            if DUAL_SIDE:
                                trailing_params["positionSide"] = "LONG"
                            else:
                                trailing_params["reduceOnly"] = True
                            if await execute_order(cli, trailing_params, symbol, "TRAILING STOP LONG"):
                                if current_price:
                                    st["entry_price"] = current_price
                                    logging.info(f"{symbol} LONG position opened with {CALLBACK_PERCENT}% trailing stop")
                            else:
                                success = False
                        else:
                            success = False

                elif target_signal == "SHORT":
                    if long_size > 0.1:
                        success &= await execute_order(cli, exit_long(symbol, long_size), symbol, "CLOSE LONG")
                    if success and short_size < target_qty * 0.9:
                        delta = target_qty - short_size
                        if await execute_order(cli, open_short(symbol, delta), symbol, "OPEN SHORT"):
                            trailing_params = {
                                "symbol": symbol,
                                "side": "BUY",
                                "type": "TRAILING_STOP_MARKET",
                                "quantity": quantize_qty(symbol, delta),
                                "callbackRate": str(CALLBACK_PERCENT),
                                "timeInForce": "GTC"
                            }
                            if DUAL_SIDE:
                                trailing_params["positionSide"] = "SHORT"
                            else:
                                trailing_params["reduceOnly"] = True
                            if await execute_order(cli, trailing_params, symbol, "TRAILING STOP SHORT"):
                                if current_price:
                                    st["entry_price"] = current_price
                                    logging.info(f"{symbol} SHORT position opened with {CALLBACK_PERCENT}% trailing stop")
                            else:
                                success = False
                        else:
                            success = False

            if success:
                st["consecutive_failures"] = 0
                action = f"SET TO {target_signal}" if target_signal else "NO-OP"
                logging.info(f"{symbol} POSITION {action}")
            else:
                st["consecutive_failures"] += 1
                st["circuit_breaker_until"] = time.time() + (CIRCUIT_BREAKER_BASE_SEC * st["consecutive_failures"])

            return success
        finally:
            st["position_state"] = "IDLE"

# ========================= PRICE FEED =========================
async def price_feed_loop(cli: AsyncClient):
    streams = [_stream_name(s) for s in SYMBOLS]
    url = f"wss://{_ws_host()}/stream?streams={'/'.join(streams)}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logging.info("WebSocket connected")
                async for raw in ws:
                    try:
                        now = time.time()
                        m = json.loads(raw)
                        d = m.get("data", {})
                        if not d:
                            continue

                        sym = d.get("s")
                        if not sym or sym not in state:
                            continue

                        p_str = d.get("p") or d.get("c") or d.get("ap") or d.get("a")
                        if not p_str:
                            continue

                        price = float(p_str)
                        st = state[sym]

                        st["last_price"] = price
                        st["price_timestamp"] = now
                        st["price_history"].append(price)

                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
                        continue

        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ========================= POSITION WATCHER =========================
async def position_watch_loop(cli: AsyncClient):
    while True:
        await asyncio.sleep(1.5)
        if _is_rest_blocked():
            continue
        for sym in SYMBOLS:
            try:
                long_size, short_size = await get_position_sizes(cli, sym)
                st = state[sym]
                prev = st.get("current_signal")

                # Exchange trailing stop closed LONG?
                if prev == "LONG" and long_size < 0.1:
                    st["current_signal"] = None
                    px = st.get("last_price")
                    if px:
                        st["long_exit_history"].append(px)
                    st["entry_price"] = None
                    logging.info(f"{sym} POSITION EXITED on exchange (LONG -> None)")

                # Exchange trailing stop closed SHORT?
                if prev == "SHORT" and short_size < 0.1:
                    st["current_signal"] = None
                    px = st.get("last_price")
                    if px:
                        st["short_exit_history"].append(px)
                    st["entry_price"] = None
                    logging.info(f"{sym} POSITION EXITED on exchange (SHORT -> None)")

            except RestBlockedError:
                _note_rest_block()
                break
            except Exception as e:
                logging.warning(f"{sym} watcher error: {e}")
                continue

# ========================= MAIN TRADING LOOP =========================
async def trading_loop(cli: AsyncClient):
    while True:
        await asyncio.sleep(0.1)

        if _is_rest_blocked():
            continue

        now = time.time()
        for sym in SYMBOLS:
            st = state[sym]

            if st["last_price"] is None or now - st.get("price_timestamp", 0) > PRICE_STALENESS_SEC:
                continue

            if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                if now < st["circuit_breaker_until"]:
                    continue
                st["consecutive_failures"] = 0

            if len(st["price_history"]) < WMA_PERIOD:
                continue

            new_signal = detect_signals(sym)
            current = st["current_signal"]

            # Only open from None -> LONG/SHORT (no flips/exits)
            if current is None and new_signal in ("LONG", "SHORT"):
                logging.info(f"{sym} SIGNAL: {current} -> {new_signal}")
                try:
                    success = await set_position(cli, sym, new_signal)
                    if success:
                        st["current_signal"] = new_signal
                    else:
                        st["consecutive_failures"] += 1
                except RestBlockedError:
                    _note_rest_block()
                except Exception as e:
                    logging.error(f"{sym} Trade execution failed: {e}")
                    st["consecutive_failures"] += 1

# ========================= INITIALIZATION =========================
async def init_filters(cli: AsyncClient):
    try:
        info = await call_binance(cli.futures_exchange_info)
        for s in info.get("symbols", []):
            sym = s["symbol"]
            if sym not in SYMBOLS:
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = f.get("stepSize")
                    if step:
                        QUANTITY_STEP[sym] = Decimal(step)
    except RestBlockedError:
        _note_rest_block()
    except Exception as e:
        logging.warning(f"Filter initialization failed: {e}")

async def seed_price_history(cli: AsyncClient):
    for sym in SYMBOLS:
        try:
            klines = await call_binance(cli.futures_klines, symbol=sym, interval="1m", limit=WMA_PERIOD + 100)
            st = state[sym]
            for k in klines:
                close_price = float(k[4])
                st["price_history"].append(close_price)
            logging.info(f"{sym} seeded with {len(st['price_history'])} price points")
        except RestBlockedError:
            _note_rest_block()
            return
        except Exception as e:
            logging.warning(f"{sym} price history seeding failed: {e}")

# ========================= MAIN =========================
async def main():
    threading.Thread(target=start_ping, daemon=True).start()

    if not (API_KEY and API_SECRET):
        raise RuntimeError("Missing Binance API credentials")

    cli = await AsyncClient.create(API_KEY, API_SECRET)

    try:
        global DUAL_SIDE
        try:
            DUAL_SIDE = await get_dual_side(cli)
        except RestBlockedError:
            _note_rest_block()
            DUAL_SIDE = False
        logging.info(f"Hedge Mode: {'Enabled' if DUAL_SIDE else 'Disabled'}")

        for s in SYMBOLS:
            try:
                await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except RestBlockedError:
                _note_rest_block()
                break
            except Exception as e:
                logging.warning(f"{s} leverage setting failed: {e}")

        await init_filters(cli)
        await seed_price_history(cli)
        await sync_position_state(cli)

        feed_task    = asyncio.create_task(price_feed_loop(cli))
        trading_task = asyncio.create_task(trading_loop(cli))
        watch_task   = asyncio.create_task(position_watch_loop(cli))

        await asyncio.gather(feed_task, trading_task, watch_task)

    finally:
        try:
            await cli.close_connection()
        except Exception:
            pass

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%b %d %H:%M:%S"
    )

    logging.info("=== SIMPLE BOLLINGER BOT WITH BOUNCE-BACK LOGIC ===")
    logging.info(f"ENTRY: Price crosses Bollinger bands (±{STD_MULTIPLIER} std dev)")
    logging.info(f"EXIT: ONLY {CALLBACK_PERCENT}% trailing callback from peak/trough (exchange-managed)")
    logging.info(f"Guardrails: no flips/exits; REST pauses on 403; spammy info logs throttled to every {LOG_SUPPRESS_SECS}s")
    logging.info(f"Symbols: {list(SYMBOLS.keys())}")

    asyncio.run(main())
