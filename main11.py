#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Dict, Any
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# ---- SYMBOLS & TARGET SIZE ----
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

# ---- DONCHIAN (Donchian-only, zero Bollinger) ----
DONCHIAN_HOURS       = 20         # 20h window of 1m candles
BOUNCE_THRESHOLD_PCT = 0.005      # 0.5% of channel width for bounce confirm
SIGNAL_PRIORITY      = ("BREAKOUT", "BOUNCE")  # or ("BOUNCE","BREAKOUT")
REFLIP_GUARD_SEC     = 1.0        # anti-chop: minimum secs between flips
EPS                  = 1e-8

# ---- STARTUP WITHOUT SEEDING (warmup) ----
MIN_VALID_MINUTES = 15  # trade after this many completed minutes if seeding was blocked

# ---- POSITION SIZE CACHE ----
POSITION_SIZE_TTL = 2.0  # seconds

# ---- PRICE SOURCE ----
def _ws_host() -> str: return "fstream.binance.com"
def _stream_name(sym: str) -> str: return f"{sym.lower()}@markPrice@1s"

# ---- RISK MANAGEMENT ----
MAX_CONSECUTIVE_FAILURES = 3
CIRCUIT_BREAKER_BASE_SEC = 300
PRICE_STALENESS_SEC      = 5.0
RATE_LIMIT_BASE_SLEEP    = 2.0
RATE_LIMIT_MAX_SLEEP     = 60.0

# ---- REST 403 BLOCKING ----
class RestBlockedError(Exception): pass
REST_BLOCKED_UNTIL = 0.0
_last_rest_warn    = 0.0

def rest_blocked() -> bool:
    return time.time() < REST_BLOCKED_UNTIL

def _is_rest_blocked() -> bool:
    return rest_blocked()

def _note_rest_block():
    remaining = int(REST_BLOCKED_UNTIL - time.time())
    if remaining > 0:
        logging.warning(f"REST temporarily blocked (HTTP 403). Backing off ~{remaining}s.")

# ---- LOG THROTTLING (for spammy infos) ----
LOG_SUPPRESS_SECS = int(os.getenv("LOG_SUPPRESS_SECS", "300"))
_LAST_LOG_TS: Dict[tuple, float] = {}

def _log_every(symbol: str, key: str, message: str, level: int = logging.INFO):
    now = time.time()
    k = (symbol, key)
    if now - _LAST_LOG_TS.get(k, 0.0) >= LOG_SUPPRESS_SECS:
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
    """
    Centralized REST caller:
    - pacing (0.2s gap)
    - 403 -> set timed block & raise
    - 429/-1003 -> exponential backoff
    """
    global _last_rest_ts, REST_BLOCKED_UNTIL, _last_rest_warn
    now = time.time()

    if now < REST_BLOCKED_UNTIL:
        raise RestBlockedError("REST blocked")

    if now - _last_rest_ts < 0.2:
        await asyncio.sleep(0.2 - (now - _last_rest_ts))
    _last_rest_ts = time.time()

    delay = RATE_LIMIT_BASE_SLEEP
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            es = str(e)
            if "403" in es:
                backoff = 90 + random.randint(15, 45)
                REST_BLOCKED_UNTIL = time.time() + backoff
                if time.time() - _last_rest_warn > 10:
                    _last_rest_warn = time.time()
                    logging.warning(f"REST temporarily blocked (HTTP 403). Backing off ~{backoff}s.")
                raise RestBlockedError("REST blocked")
            if "429" in es or "-1003" in es:
                await asyncio.sleep(min(delay, RATE_LIMIT_MAX_SLEEP))
                delay = min(delay * 2.0, RATE_LIMIT_MAX_SLEEP)
                continue
            raise

# ========================= POSITION STATE SYNC =========================
async def sync_position_state(cli: AsyncClient):
    if rest_blocked(): return
    logging.info("=== SYNCING POSITION STATE ===")
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
            _note_rest_block(); return
        except Exception as e:
            logging.error(f"{symbol}: Failed to sync position state: {e}")

DUAL_SIDE = False

async def get_dual_side(cli: AsyncClient) -> bool:
    try:
        res = await call_binance(cli.futures_get_position_mode)
        return bool(res.get("dualSidePosition", False))
    except RestBlockedError:
        _note_rest_block(); return False
    except Exception:
        return False

async def get_position_sizes(cli: AsyncClient, symbol: str) -> Tuple[float, float]:
    try:
        positions = await call_binance(cli.futures_position_information, symbol=symbol)
        long_size = short_size = 0.0
        for pos in positions:
            side = pos.get("positionSide")
            size = abs(float(pos.get("positionAmt", 0.0)))
            if side == "LONG": long_size = size
            elif side == "SHORT": short_size = size
        return long_size, short_size
    except RestBlockedError:
        raise
    except Exception as e:
        logging.error(f"{symbol} get_position_sizes failed: {e}")
        return 0.0, 0.0

# ========================= STATE ===============================
state: Dict[str, Dict[str, Any]] = {
    s: {
        # Price data (tick)
        "last_price": None,
        "price_timestamp": None,

        # 1-min OHLC for Donchian
        "klines": deque(maxlen=DONCHIAN_HOURS * 60),
        "ws_last_heartbeat": 0.0,

        # Current state
        "current_signal": None,  # None | "LONG" | "SHORT"

        # Risk management
        "consecutive_failures": 0,
        "circuit_breaker_until": 0.0,
        "order_lock": asyncio.Lock(),
        "position_state": "IDLE",

        # Donchian bounce tracking
        "bounce_breach_state": "NONE",   # NONE | BREACHED_LOW | BREACHED_HIGH
        "bounce_breach_ts": 0.0,
        "bounce_reverse_confirmed": False,

        # Anti-chop flip timestamp
        "last_flip_ts": 0.0,

        # Position size cache
        "pos_cache": {"ts": 0.0, "long": 0.0, "short": 0.0},
    } for s in SYMBOLS
}

def symbol_target_qty(sym: str) -> float:
    return float(SYMBOLS[sym])

# ========================= DONCHIAN HELPERS =========================
def _minute_bucket(ts: float) -> int:
    return int(ts // 60)

def get_donchian_levels(symbol: str) -> Tuple[Optional[float], Optional[float]]:
    st = state[symbol]
    klines = st["klines"]
    if not klines: return None, None

    now = time.time()
    cutoff     = now - DONCHIAN_HOURS * 3600
    cur_minute = _minute_bucket(now)

    # Use only completed minutes inside the window
    valid = [k for k in klines if k["time"] >= cutoff and k["minute"] < cur_minute]
    if len(valid) < MIN_VALID_MINUTES:
        return None, None

    highs = [k["high"] for k in valid]
    lows  = [k["low"]  for k in valid]
    return (min(lows), max(highs))

def get_bounce_levels(d_low: float, d_high: float) -> Tuple[float, float]:
    ch  = d_high - d_low
    off = ch * BOUNCE_THRESHOLD_PCT
    return d_low + off, d_high - off

# ========================= SIGNAL DETECTION (Donchian-only) =========================
def detect_breakout_signal(symbol: str) -> Optional[str]:
    st = state[symbol]
    price = st.get("last_price")
    if price is None: return None
    d_low, d_high = get_donchian_levels(symbol)
    if d_low is None or d_high is None: return None

    if price >= d_high - EPS:
        logging.info(f"{symbol}: BREAKOUT LONG | px={price:.8f} ≥ H={d_high:.8f}")
        return "LONG"
    if price <= d_low + EPS:
        logging.info(f"{symbol}: BREAKOUT SHORT | px={price:.8f} ≤ L={d_low:.8f}")
        return "SHORT"
    return None

def detect_bounce_signal(symbol: str) -> Optional[str]:
    st = state[symbol]
    price = st.get("last_price")
    if price is None: return None
    d_low, d_high = get_donchian_levels(symbol)
    if d_low is None or d_high is None: return None

    b_low, b_high = get_bounce_levels(d_low, d_high)
    breach = st.get("bounce_breach_state", "NONE")
    now = time.time()

    # Phase 1: mark breach of extreme
    if price <= d_low and breach != "BREACHED_LOW":
        st["bounce_breach_state"] = "BREACHED_LOW"
        st["bounce_breach_ts"] = now
        st["bounce_reverse_confirmed"] = False
        logging.info(f"{symbol}: BOUNCE breach LOW at {price:.8f} (L={d_low:.8f})")
        return None

    if price >= d_high and breach != "BREACHED_HIGH":
        st["bounce_breach_state"] = "BREACHED_HIGH"
        st["bounce_breach_ts"] = now
        st["bounce_reverse_confirmed"] = False
        logging.info(f"{symbol}: BOUNCE breach HIGH at {price:.8f} (H={d_high:.8f})")
        return None

    # Phase 2: confirm reversal toward channel
    if breach == "BREACHED_LOW" and not st.get("bounce_reverse_confirmed", False):
        if price >= b_low:
            st["bounce_reverse_confirmed"] = True
            logging.info(f"{symbol}: BOUNCE LONG confirm | px={price:.8f} ≥ {b_low:.8f}")
            return "LONG"

    if breach == "BREACHED_HIGH" and not st.get("bounce_reverse_confirmed", False):
        if price <= b_high:
            st["bounce_reverse_confirmed"] = True
            logging.info(f"{symbol}: BOUNCE SHORT confirm | px={price:.8f} ≤ {b_high:.8f}")
            return "SHORT"

    return None

def _pick_with_priority(symbol: str):
    br = detect_breakout_signal(symbol)
    bn = detect_bounce_signal(symbol)
    if not br and not bn: return None, None
    if br and bn:
        if br == bn: return br, SIGNAL_PRIORITY[0]
        return (br, "BREAKOUT") if SIGNAL_PRIORITY[0] == "BREAKOUT" else (bn, "BOUNCE")
    return (br, "BREAKOUT") if br else (bn, "BOUNCE")

def detect_signals(symbol: str) -> Optional[str]:
    """Return 'LONG'/'SHORT'/None. Exits are by flip only."""
    st = state[symbol]
    current = st.get("current_signal")
    picked, src = _pick_with_priority(symbol)
    if not picked:
        return None

    if current is None:
        logging.info(f"{symbol}: ENTRY {picked} by {src}")
        return picked

    if picked != current:
        now = time.time()
        if now - st.get("last_flip_ts", 0.0) < REFLIP_GUARD_SEC:
            logging.debug(f"{symbol}: skip flip {current}->{picked} (guard {REFLIP_GUARD_SEC}s)")
            return None
        logging.info(f"{symbol}: FLIP {current}→{picked} ({src})")
        return picked

    return None

# ========================= ORDER EXECUTION =========================
def _maybe_pos_side(params: dict, side: str) -> dict:
    if DUAL_SIDE: params["positionSide"] = side
    else: params.pop("positionSide", None)
    return params

def open_long(sym: str, qty: float):
    return _maybe_pos_side(dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty)), "LONG")

def open_short(sym: str, qty: float):
    return _maybe_pos_side(dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty)), "SHORT")

def exit_long(sym: str, qty: float):
    p = dict(symbol=sym, side="SELL", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE: p["reduceOnly"] = True
    return _maybe_pos_side(p, "LONG")

def exit_short(sym: str, qty: float):
    p = dict(symbol=sym, side="BUY", type="MARKET", quantity=quantize_qty(sym, qty))
    if not DUAL_SIDE: p["reduceOnly"] = True
    return _maybe_pos_side(p, "SHORT")

async def execute_order(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    try:
        res = await call_binance(cli.futures_create_order, **order_params)
        oid = res.get("orderId")
        logging.info(f"{symbol} {action} EXECUTED - OrderID: {oid}")
        return True
    except RestBlockedError:
        _note_rest_block(); return False
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
                _note_rest_block(); return False
            except Exception as e2:
                logging.error(f"{symbol} {action} RETRY FAILED: {e2}")
        else:
            logging.error(f"{symbol} {action} FAILED: {e}")
        return False

# ========================= POSITION SIZE CACHE =========================
async def get_position_sizes_cached(cli: AsyncClient, symbol: str, st: Dict[str, Any]) -> Tuple[float, float]:
    now = time.time()
    c = st.get("pos_cache") or {"ts": 0.0}
    if now - c.get("ts", 0.0) <= POSITION_SIZE_TTL:
        return c.get("long", 0.0), c.get("short", 0.0)
    long_size, short_size = await get_position_sizes(cli, symbol)
    st["pos_cache"] = {"ts": now, "long": long_size, "short": short_size}
    return long_size, short_size

async def set_position(cli: AsyncClient, symbol: str, target_signal: Optional[str]):
    st = state[symbol]
    async with st["order_lock"]:
        if st["position_state"] != "IDLE": return False
        st["position_state"] = "PROCESSING"
        try:
            if _is_rest_blocked(): _note_rest_block(); return False

            long_size, short_size = await get_position_sizes_cached(cli, symbol, st)
            target_qty = symbol_target_qty(symbol)

            success = True
            if target_signal in ("LONG", "SHORT"):
                if target_signal == "LONG":
                    if short_size > 0.1:
                        ok = await execute_order(cli, exit_short(symbol, short_size), symbol, "CLOSE SHORT")
                        success &= ok
                        if ok:
                            logging.info(f"{symbol} ▶ EXITED SHORT (size={short_size})")
                    if success and long_size < target_qty * 0.9:
                        delta = max(0.0, target_qty - long_size)
                        if delta > 0:
                            ok = await execute_order(cli, open_long(symbol, delta), symbol, "OPEN LONG")
                            success &= ok
                            if ok:
                                logging.info(f"{symbol} ▶ ENTERED LONG (added={delta}, target={target_qty})")

                elif target_signal == "SHORT":
                    if long_size > 0.1:
                        ok = await execute_order(cli, exit_long(symbol, long_size), symbol, "CLOSE LONG")
                        success &= ok
                        if ok:
                            logging.info(f"{symbol} ▶ EXITED LONG (size={long_size})")
                    if success and short_size < target_qty * 0.9:
                        delta = max(0.0, target_qty - short_size)
                        if delta > 0:
                            ok = await execute_order(cli, open_short(symbol, delta), symbol, "OPEN SHORT")
                            success &= ok
                            if ok:
                                logging.info(f"{symbol} ▶ ENTERED SHORT (added={delta}, target={target_qty})")

            if success:
                st["consecutive_failures"] = 0
                prev = st.get("current_signal")
                if prev is None:
                    logging.info(f"{symbol} ✅ POSITION OPENED → {target_signal}")
                elif prev != target_signal:
                    logging.info(f"{symbol} 🔁 POSITION FLIPPED {prev} → {target_signal}")
                else:
                    logging.info(f"{symbol} ✅ POSITION REINFORCED → {target_signal}")
            else:
                st["consecutive_failures"] += 1
                st["circuit_breaker_until"] = time.time() + (CIRCUIT_BREAKER_BASE_SEC * st["consecutive_failures"])
            return success
        finally:
            st["position_state"] = "IDLE"

# ========================= PRICE FEED (build 1m candles) =========================
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
                        m = json.loads(raw); d = m.get("data", {})
                        if not d: continue
                        sym = d.get("s")
                        if not sym or sym not in state: continue
                        p_str = d.get("p") or d.get("c") or d.get("ap") or d.get("a")
                        if not p_str: continue
                        price = float(p_str); st = state[sym]

                        st["last_price"] = price
                        st["price_timestamp"] = now
                        st["ws_last_heartbeat"] = now

                        # 1m OHLC
                        current_minute = _minute_bucket(now)
                        kl = st["klines"]
                        if not kl or kl[-1]["minute"] != current_minute:
                            kl.append({"minute": current_minute, "time": now, "high": price, "low": price, "close": price})
                        else:
                            kl[-1]["high"]  = max(kl[-1]["high"], price)
                            kl[-1]["low"]   = min(kl[-1]["low"], price)
                            kl[-1]["close"] = price

                    except Exception as e:
                        logging.warning(f"Price processing error: {e}")
                        continue
        except Exception as e:
            logging.warning(f"WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ========================= GROUPED GATES =========================
def gates_ok(st: Dict[str, Any]) -> Tuple[bool, str]:
    now = time.time()
    if st["last_price"] is None or now - st.get("price_timestamp", 0.0) > PRICE_STALENESS_SEC:
        return False, "stale price"
    if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES and now < st["circuit_breaker_until"]:
        return False, "circuit breaker"
    # require MIN_VALID_MINUTES completed 1m candles (even if seed failed)
    kl = st["klines"]
    if not kl: return False, "no klines"
    cur_min = int(now // 60)
    valid = [k for k in kl if k["minute"] < cur_min]
    if len(valid) < MIN_VALID_MINUTES:
        return False, f"insufficient klines ({len(valid)}/{MIN_VALID_MINUTES})"
    return True, "ok"

# ========================= CONDITION SNAPSHOT & REPORTER =========================
def condition_snapshot(symbol: str) -> str:
    """One-line snapshot of current Donchian conditions."""
    st = state[symbol]
    price = st.get("last_price")
    d_low, d_high = get_donchian_levels(symbol)
    if price is None or d_low is None or d_high is None:
        return f"{symbol}: waiting (insufficient data)"
    # Breakout flags
    brk_long  = price >= d_high - EPS
    brk_short = price <= d_low  + EPS
    # Bounce context
    b_low, b_high = get_bounce_levels(d_low, d_high)
    breach = st.get("bounce_breach_state", "NONE")
    bnc_long  = (breach == "BREACHED_LOW"  and price >= b_low)
    bnc_short = (breach == "BREACHED_HIGH" and price <= b_high)
    return (f"{symbol}: px={price:.6f} | D[L,H]=[{d_low:.6f},{d_high:.6f}] "
            f"| breakout(L/S)={(1 if brk_long else 0)}/{(1 if brk_short else 0)} "
            f"| bounce(breach={breach}; conf L/S)={(1 if bnc_long else 0)}/{(1 if bnc_short else 0)}")

async def condition_report_loop():
    """Every 5 minutes, log a concise condition report per symbol."""
    while True:
        try:
            logging.info("=== CONDITION REPORT (Donchian breakout/bounce) ===")
            for sym in SYMBOLS:
                try:
                    logging.info(condition_snapshot(sym))
                except Exception as e:
                    logging.warning(f"{sym} condition snapshot error: {e}")
        except Exception as e:
            logging.warning(f"Condition reporter error: {e}")
        await asyncio.sleep(300)  # 5 minutes

# ========================= MAIN TRADING LOOP =========================
async def trading_loop(cli: AsyncClient):
    while True:
        await asyncio.sleep(0.1)
        if _is_rest_blocked():
            continue
        now = time.time()
        for sym in SYMBOLS:
            st = state[sym]
            ok, _ = gates_ok(st)
            if not ok: continue

            # reset circuit breaker when elapsed
            if st["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES and now >= st["circuit_breaker_until"]:
                st["consecutive_failures"] = 0

            new_signal = detect_signals(sym)
            current    = st["current_signal"]

            if (current is None and new_signal in ("LONG", "SHORT")) or \
               (current in ("LONG", "SHORT") and new_signal in ("LONG", "SHORT") and new_signal != current):
                logging.info(f"{sym} SIGNAL: {current} -> {new_signal}")
                try:
                    success = await set_position(cli, sym, new_signal)
                    if success:
                        if current is not None and current != new_signal:
                            st["last_flip_ts"] = time.time()
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
            if sym not in SYMBOLS: continue
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = f.get("stepSize")
                    if step:
                        QUANTITY_STEP[sym] = Decimal(step)
    except RestBlockedError:
        _note_rest_block()
    except Exception as e:
        logging.warning(f"Filter initialization failed: {e}")

async def seed_klines(cli: AsyncClient):
    """Seed last ~20h of 1m candles (OHLC) for all symbols."""
    for sym in SYMBOLS:
        try:
            # 20h * 60 = 1200; add a little buffer
            klines = await call_binance(cli.futures_klines, symbol=sym, interval="1m", limit=DONCHIAN_HOURS*60)
            st = state[sym]
            for k in klines:
                ts_open = float(k[0]) / 1000.0
                minute  = int(ts_open // 60)
                high    = float(k[2])
                low     = float(k[3])
                close   = float(k[4])
                st["klines"].append({"minute": minute, "time": ts_open, "high": high, "low": low, "close": close})
            logging.info(f"{sym} seeded with {len(st['klines'])} candles")
        except RestBlockedError:
            _note_rest_block(); return
        except Exception as e:
            logging.warning(f"{sym} kline seeding failed: {e}")

async def try_seed_after_block(cli: AsyncClient):
    """Retry seeding & sync when REST unblocks, so no manual restart needed."""
    while True:
        await asyncio.sleep(5)
        if not rest_blocked():
            try:
                await seed_klines(cli)
                await sync_position_state(cli)
                logging.info("Post-block seed & sync completed.")
                return
            except RestBlockedError:
                _note_rest_block()
            except Exception as e:
                logging.warning(f"Post-block seed/sync error: {e}")

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
            _note_rest_block(); DUAL_SIDE = False
        logging.info(f"Hedge Mode: {'Enabled' if DUAL_SIDE else 'Disabled'}")

        for s in SYMBOLS:
            try:
                await call_binance(cli.futures_change_leverage, symbol=s, leverage=LEVERAGE)
            except RestBlockedError:
                _note_rest_block(); break
            except Exception as e:
                logging.warning(f"{s} leverage setting failed: {e}")

        await init_filters(cli)
        await seed_klines(cli)          # may be blocked at startup
        await sync_position_state(cli)  # may be blocked at startup
        asyncio.create_task(try_seed_after_block(cli))  # keep trying in background

        feed_task    = asyncio.create_task(price_feed_loop(cli))
        trading_task = asyncio.create_task(trading_loop(cli))
        report_task  = asyncio.create_task(condition_report_loop())  # 5-min condition reports

        await asyncio.gather(feed_task, trading_task, report_task)

    finally:
        try: await cli.close_connection()
        except Exception: pass

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%b %d %H:%M:%S"
    )

    logging.info("=== DONCHIAN-ONLY BOT (breakout + bounce, flip-only exits) ===")
    logging.info(f"ENTRY: Donchian {DONCHIAN_HOURS}h breakout or bounce (priority={SIGNAL_PRIORITY})")
    logging.info(f"EXIT: by FLIP only (no trailing/stop orders)")
    logging.info(f"Gates: stale price / circuit breaker / >= {MIN_VALID_MINUTES} completed 1m candles")
    logging.info(f"Flip guard: {REFLIP_GUARD_SEC}s | REST backoff 403/429 | Size cache {POSITION_SIZE_TTL}s")
    logging.info(f"Symbols: {list(SYMBOLS.keys())}")

    asyncio.run(main())
