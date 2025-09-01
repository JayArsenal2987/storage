#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, threading, logging, websockets, time, math, random
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance import AsyncClient
from collections import deque
from typing import Optional, Tuple, Deque, Dict, Any, Union
from decimal import Decimal, ROUND_HALF_UP, getcontext
import smtplib
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart

getcontext().prec = 28  # safe precision

# ========================= CONFIG =========================
load_dotenv()
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
LEVERAGE   = int(os.getenv("LEVERAGE", "50"))

# ---- RISK MANAGEMENT CONFIG ----
DAILY_PNL_LIMIT = float(os.getenv("DAILY_PNL_LIMIT", "-500.0"))  # Stop trading if daily PnL drops below this
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "300.0"))  # Max ADA per side
EMERGENCY_SHUTDOWN_PNL = float(os.getenv("EMERGENCY_SHUTDOWN_PNL", "-1000.0"))  # Emergency flatten all
SYNC_ERROR_THRESHOLD = int(os.getenv("SYNC_ERROR_THRESHOLD", "3"))  # Max consecutive sync errors

# ---- ALERTING CONFIG ----
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

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

# ========================= ALERTING SYSTEM =========================
async def send_alert(subject: str, message: str, critical: bool = False):
    """Send email alert if configured"""
    if not all([EMAIL_USER, EMAIL_PASS, ALERT_EMAIL]):
        logging.warning(f"ALERT (email not configured): {subject} - {message}")
        return
    
    try:
        msg = MimeMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = ALERT_EMAIL
        msg['Subject'] = f"{'🚨 CRITICAL' if critical else '⚠️ WARNING'} Trading Bot: {subject}"
        
        body = f"""
Trading Bot Alert
Time: {time.strftime('%Y-%m-%d %H:%M:%S')}
Severity: {'CRITICAL' if critical else 'WARNING'}

{message}

Bot will {'SHUTDOWN' if critical else 'continue with safety measures'}.
        """
        
        msg.attach(MimeText(body, 'plain'))
        
        # Use asyncio to avoid blocking
        def send_email():
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
            server.quit()
        
        await asyncio.get_event_loop().run_in_executor(None, send_email)
        logging.info(f"Alert sent: {subject}")
        
    except Exception as e:
        logging.error(f"Failed to send alert: {e}")

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

# ========================= ENHANCED BINANCE CALL WITH RETRY ===================
_last_rest_ts = 0.0
async def call_binance(fn, *args, max_retries=3, **kwargs):
    global _last_rest_ts
    now = time.time()
    min_gap = 0.08
    if now - _last_rest_ts < min_gap:
        await asyncio.sleep(min_gap - (now - _last_rest_ts) + random.uniform(0.01, 0.03))
    _last_rest_ts = time.time()
    
    delay = RATE_LIMIT_BASE_SLEEP
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "-1003" in msg or "Too many requests" in msg or "IP banned" in msg:
                sleep_for = min(delay, RATE_LIMIT_MAX_SLEEP)
                logging.warning(f"Rate limit/backoff attempt {attempt+1}/{max_retries}: sleeping {sleep_for:.1f}s ({msg})")
                await asyncio.sleep(sleep_for + random.uniform(0.0, 0.5))
                delay = min(delay * 2.0, RATE_LIMIT_MAX_SLEEP)
                continue
            elif attempt < max_retries - 1:
                # Transient errors - retry with shorter delay
                if any(x in msg.lower() for x in ["timeout", "connection", "network", "server error"]):
                    await asyncio.sleep(1.0 + random.uniform(0.0, 0.5))
                    logging.warning(f"Retrying transient error attempt {attempt+1}/{max_retries}: {msg}")
                    continue
            # Re-raise on final attempt or non-retryable errors
            raise
    
    # Should never reach here, but just in case
    raise Exception(f"Max retries {max_retries} exceeded")

# ========================= ENHANCED POSITION HELPERS ====================
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

# ========================= ENHANCED EXECUTION WRAPPER ===================
async def safe_order_execution(cli: AsyncClient, order_params: dict, symbol: str, action: str) -> bool:
    """Enhanced execution with better error handling and retry logic"""
    max_attempts = 3
    
    for attempt in range(max_attempts):
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
            msg = str(e)
            if attempt < max_attempts - 1:
                # Retry on certain errors
                if any(x in msg.lower() for x in ["timeout", "connection", "network", "-1001"]):
                    wait_time = (attempt + 1) * 0.5
                    logging.warning(f"{symbol} {action} attempt {attempt+1} failed, retrying in {wait_time}s: {msg}")
                    await asyncio.sleep(wait_time)
                    continue
            
            logging.error(f"{symbol} {action} failed after {attempt+1} attempts: {e}")
            
            # Send alert for critical order failures
            if "FLATTEN" in action or "EXIT" in action:
                await send_alert(
                    f"Critical Order Failure - {symbol}",
                    f"Failed to execute {action} after {max_attempts} attempts: {e}",
                    critical=True
                )
            return False
    
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
        "last_price_time": 0.0,              # Track when price was last updated
        "price_buffer": deque(maxlen=5000),  # (ts, price) feed buffer
        "micro_fifo": deque(),               # FIFO across BOTH sides
        "max_slots": calculate_max_slots(s),
        "order_lock": asyncio.Lock(),
        "last_order_id": None,
        "fifo_sync_error_count": 0,
        "consecutive_sync_errors": 0,        # Track consecutive errors
        "daily_pnl_start": 0.0,              # Track daily PnL baseline (to compute realized PnL if needed)
        "trading_paused": False,             # Emergency pause flag
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

# ========================= ENHANCED PRICE FALLBACK ======================
async def get_fallback_price(cli: AsyncClient, symbol: str) -> Optional[float]:
    """Get current price from REST API as fallback when WebSocket is down"""
    try:
        ticker = await call_binance(cli.get_symbol_ticker, symbol=symbol)
        price = float(ticker["price"])
        logging.warning(f"{symbol} using fallback REST price: {price}")
        return price
    except Exception as e:
        logging.error(f"{symbol} fallback price failed: {e}")
        return None

async def ensure_fresh_price(cli: AsyncClient, symbol: str, max_age_seconds: float = 10.0) -> Optional[float]:
    """Ensure we have a recent price, using fallback if needed"""
    st = state[symbol]
    now = time.time()
    
    # Check if current price is fresh enough
    if (st["last_price"] is not None and 
        st["last_price_time"] > 0 and 
        now - st["last_price_time"] <= max_age_seconds):
        return st["last_price"]
    
    # Price is stale, try to get fresh one
    fallback_price = await get_fallback_price(cli, symbol)
    if fallback_price is not None:
        st["last_price"] = fallback_price
        st["last_price_time"] = now
        return fallback_price
    
    # Fallback failed, use stale price if available
    if st["last_price"] is not None:
        age = now - st["last_price_time"]
        logging.warning(f"{symbol} using stale price ({age:.1f}s old): {st['last_price']}")
        return st["last_price"]
    
    return None

# ========================= ADOPT POSITIONS (on restart) ========
async def adopt_positions(cli: AsyncClient):
    for s in SYMBOLS:
        try:
            await refresh_position_cache(cli, s)
            long_sz  = await get_position_size(cli, s, "LONG")
            short_sz = await get_position_size(cli, s, "SHORT")
            st       = state[s]
            max_slots = st["max_slots"]
            
            # Get current price with fallback
            lp = await ensure_fresh_price(cli, s, max_age_seconds=60.0)
            if lp is None:
                logging.warning(f"{s} adopt: No price data available, skipping")
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

# ========================= ENHANCED FIFO SYNC WITH AUTO-CORRECTION =======
async def verify_and_fix_fifo_sync(cli: AsyncClient, symbol: str) -> bool:
    """Enhanced FIFO sync with auto-correction capabilities"""
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
            st["consecutive_sync_errors"] += 1
            
            logging.error(f"{symbol} FIFO DESYNC #{st['fifo_sync_error_count']}: "
                          f"FIFO(L:{fifo_long_qty:.2f} S:{fifo_short_qty:.2f}) vs "
                          f"ACTUAL(L:{actual_long:.2f} S:{actual_short:.2f})")
            
            # AUTO-CORRECTION: Attempt to rebuild FIFO from actual positions
            if st["consecutive_sync_errors"] >= 2:  # Allow one error, correct on second
                logging.warning(f"{symbol} Auto-correcting FIFO sync...")
                await auto_correct_fifo(cli, symbol)
                
                # Re-verify after correction
                sync_ok = await verify_and_fix_fifo_sync(cli, symbol)
                if sync_ok:
                    logging.warning(f"{symbol} FIFO auto-correction succeeded")
                    await send_alert(
                        f"FIFO Sync Corrected - {symbol}",
                        f"Auto-correction succeeded after {st['consecutive_sync_errors']} errors"
                    )
                else:
                    # Correction failed - this is serious
                    logging.error(f"{symbol} FIFO auto-correction FAILED")
                    await send_alert(
                        f"FIFO Sync Correction FAILED - {symbol}",
                        f"Auto-correction failed. Bot may be in dangerous state.",
                        critical=True
                    )
                    
                    # Emergency pause trading for this symbol
                    if st["consecutive_sync_errors"] >= SYNC_ERROR_THRESHOLD:
                        st["trading_paused"] = True
                        logging.error(f"{symbol} EMERGENCY: Trading paused due to sync failures")
                        await send_alert(
                            f"EMERGENCY: Trading Paused - {symbol}",
                            f"Trading paused after {st['consecutive_sync_errors']} consecutive sync errors",
                            critical=True
                        )
        else:
            st["consecutive_sync_errors"] = 0  # Reset on successful sync
            
        return sync_ok
    except Exception as e:
        logging.error(f"{symbol} verify_and_fix_fifo_sync failed: {e}")
        return False

async def auto_correct_fifo(cli: AsyncClient, symbol: str):
    """Attempt to rebuild FIFO queue from actual positions"""
    try:
        st = state[symbol]
        actual_long = await get_position_size(cli, symbol, "LONG")
        actual_short = await get_position_size(cli, symbol, "SHORT")
        micro_qty = SYMBOLS[symbol]
        
        # Calculate how many micro-entries we should have
        n_long = int(actual_long / micro_qty)
        n_short = int(actual_short / micro_qty)
        total = n_long + n_short
        
        if total > st["max_slots"]:
            logging.error(f"{symbol} auto_correct: Total positions {total} exceed max_slots {st['max_slots']}")
            return False
        
        # Get current price
        current_price = await ensure_fresh_price(cli, symbol)
        if current_price is None:
            logging.error(f"{symbol} auto_correct: No price available")
            return False
        
        # Rebuild FIFO with estimated timestamps
        st["micro_fifo"].clear()
        now = time.time()
        base_ts = now - total * BAR_SECONDS  # Estimate entry times
        
        # Add long positions first
        for i in range(n_long):
            st["micro_fifo"].append(MicroEntry(
                side="LONG", 
                qty=micro_qty,
                ts=base_ts + i * BAR_SECONDS, 
                price=current_price
            ))
        
        # Add short positions
        for i in range(n_short):
            st["micro_fifo"].append(MicroEntry(
                side="SHORT", 
                qty=micro_qty,
                ts=base_ts + (n_long + i) * BAR_SECONDS, 
                price=current_price
            ))
        
        logging.warning(f"{symbol} auto_correct: Rebuilt FIFO with {n_long}L + {n_short}S = {total} entries")
        return True
        
    except Exception as e:
        logging.error(f"{symbol} auto_correct failed: {e}")
        return False

# ========================= RISK MANAGEMENT SYSTEM ======================
# Track cumulative realized PnL for the current day (since bot start or midnight)
cumulative_realized_pnl = 0.0
last_pnl_summary_time = time.time()

async def check_risk_limits(cli: AsyncClient) -> bool:
    """Check all risk limits and return True if trading should continue"""
    global cumulative_realized_pnl, last_pnl_summary_time
    try:
        total_upnl = 0.0
        total_position_size = 0.0
        
        for sym in SYMBOLS:
            st = state[sym]
            
            # Skip if trading is paused for this symbol
            if st.get("trading_paused", False):
                continue
                
            snap = await get_positions_snapshot(cli, sym)
            L, S = snap["LONG"], snap["SHORT"]
            sym_upnl = (L["uPnL"] or 0.0) + (S["uPnL"] or 0.0)
            total_upnl += sym_upnl
            
            # Check individual position sizes
            long_size = L["size"]
            short_size = S["size"]
            total_position_size += long_size + short_size
            
            if long_size > MAX_POSITION_SIZE or short_size > MAX_POSITION_SIZE:
                logging.error(f"{sym} Position size exceeded: L={long_size} S={short_size} (max={MAX_POSITION_SIZE})")
                await send_alert(
                    f"Position Size Limit Exceeded - {sym}",
                    f"LONG: {long_size}, SHORT: {short_size} (max: {MAX_POSITION_SIZE})",
                    critical=True
                )
                return False
        
        # Calculate total PnL including realized
        total_pnl = cumulative_realized_pnl + total_upnl
        
        # Periodic summary logging
        now = time.time()
        if now - last_pnl_summary_time >= PNL_SUMMARY_SEC:
            logging.info(f"PnL summary: Unrealized={total_upnl:.2f}, Realized={cumulative_realized_pnl:.2f}, Total={total_pnl:.2f}")
            last_pnl_summary_time = now
        
        # Check daily PnL limit
        if total_pnl < DAILY_PNL_LIMIT:
            logging.error(f"Daily PnL limit exceeded: {total_pnl:.2f} < {DAILY_PNL_LIMIT}")
            await send_alert(
                "Daily PnL Limit Exceeded",
                f"Current total PnL: {total_pnl:.2f} USDT (limit: {DAILY_PNL_LIMIT} USDT)",
                critical=True
            )
            # Pause trading for all symbols
            for sym in SYMBOLS:
                state[sym]["trading_paused"] = True
            return False
        
        # Emergency shutdown check
        if total_pnl < EMERGENCY_SHUTDOWN_PNL:
            logging.error(f"EMERGENCY SHUTDOWN: PnL {total_pnl:.2f} < {EMERGENCY_SHUTDOWN_PNL}")
            await send_alert(
                "EMERGENCY SHUTDOWN TRIGGERED",
                f"Catastrophic loss detected: {total_pnl:.2f} USDT. Initiating emergency flatten.",
                critical=True
            )
            
            # Emergency flatten all positions for all symbols
            for sym in SYMBOLS:
                await emergency_flatten_all(cli, sym)
            return False
        
        return True
        
    except Exception as e:
        logging.error(f"Risk limit check failed: {e}")
        # In case of error, default to continue to avoid stopping erroneously
        return True

# ========================= EMERGENCY FLATTEN ALL ======================
async def emergency_flatten_all(cli: AsyncClient, symbol: str):
    """Immediately close all positions for the given symbol"""
    st = state[symbol]
    async with st["order_lock"]:
        try:
            long_sz = await get_position_size_fresh(cli, symbol, "LONG")
            short_sz = await get_position_size_fresh(cli, symbol, "SHORT")
            if long_sz > 0:
                await safe_order_execution(cli, exit_long(symbol, long_sz), symbol, "FLATTEN LONG")
            if short_sz > 0:
                await safe_order_execution(cli, exit_short(symbol, short_sz), symbol, "FLATTEN SHORT")
            # Clear local state
            st["micro_fifo"].clear()
            st["bars_in_cycle"] = 0
            st["cycle_start_ts"] = None
            logging.info(f"{symbol} emergency flattened (L={long_sz}, S={short_sz})")
        except Exception as e:
            logging.error(f"{symbol} emergency_flatten_all failed: {e}")

# ========================= TRADING STRATEGY LOGIC ======================
async def handle_bar_close(cli: AsyncClient, symbol: str, open_price: float, close_price: float):
    """Handle actions to take at the close of each 1h bar for the symbol"""
    st = state[symbol]
    # Skip if trading is globally paused for this symbol
    if st.get("trading_paused", False):
        logging.info(f"{symbol} trading paused - no new entries on bar close")
        return
    
    # Determine if bar closed up or down
    bar_up = close_price > open_price
    bar_down = close_price < open_price
    
    # Get current positions sizes
    long_sz = await get_position_size(cli, symbol, "LONG")
    short_sz = await get_position_size(cli, symbol, "SHORT")
    side_in_cycle = None
    if long_sz > 0 and short_sz == 0:
        side_in_cycle = "LONG"
    elif short_sz > 0 and long_sz == 0:
        side_in_cycle = "SHORT"
    elif long_sz > 0 and short_sz > 0:
        # Both sides have positions (unexpected in normal strategy, skip new entries)
        side_in_cycle = None
    
    # Check profit or stop-out conditions at bar close
    # (Though profit is also checked continuously, check here as backup)
    net_upnl = 0.0
    for entry in st["micro_fifo"]:
        if entry["side"] == "LONG":
            net_upnl += (close_price - entry["price"]) * entry["qty"]
        elif entry["side"] == "SHORT":
            net_upnl += (entry["price"] - close_price) * entry["qty"]
    # If net PnL positive, flatten all positions (take profit)
    if net_upnl > 0.0 and (long_sz > 0 or short_sz > 0):
        logging.info(f"{symbol} net PnL {net_upnl:.2f} > 0 at bar close, taking profit")
        # Realize profit
        global cumulative_realized_pnl
        cumulative_realized_pnl += net_upnl
        await emergency_flatten_all(cli, symbol)
        return
    
    # If oldest entry > 21h old, stop out (flatten all)
    if st["micro_fifo"]:
        oldest_ts = st["micro_fifo"][0]["ts"]
        if time.time() - oldest_ts >= 21 * BAR_SECONDS:
            logging.error(f"{symbol} oldest position > 21h, stopping out")
            # Calculate net PnL to record realized loss
            net_upnl = 0.0
            last_price = close_price
            for entry in st["micro_fifo"]:
                if entry["side"] == "LONG":
                    net_upnl += (last_price - entry["price"]) * entry["qty"]
                elif entry["side"] == "SHORT":
                    net_upnl += (entry["price"] - last_price) * entry["qty"]
            cumulative_realized_pnl += net_upnl
            await emergency_flatten_all(cli, symbol)
            await send_alert(
                f"21h Stop-Out Triggered - {symbol}",
                f"Closed all positions after 21h without profit. Realized PnL: {net_upnl:.2f} USDT",
                critical=True
            )
            return
    
    # Trading logic: open new positions if applicable
    async with st["order_lock"]:
        # If no active cycle (no positions open)
        if side_in_cycle is None:
            # Start a new cycle based on bar direction
            if bar_up:
                # Price went up -> open SHORT (mean reversion)
                qty = calculate_order_quantity(symbol, close_price)
                if await safe_order_execution(cli, open_short(symbol, qty), symbol, "OPEN SHORT"):
                    entry = MicroEntry(side="SHORT", qty=qty, ts=time.time(), price=close_price)
                    st["micro_fifo"].append(entry)
                    st["bars_in_cycle"] = 1
                    st["cycle_start_ts"] = time.time()
                    logging.info(f"{symbol} New cycle started: SHORT {qty}@{close_price}")
            elif bar_down:
                # Price went down -> open LONG (mean reversion)
                qty = calculate_order_quantity(symbol, close_price)
                if await safe_order_execution(cli, open_long(symbol, qty), symbol, "OPEN LONG"):
                    entry = MicroEntry(side="LONG", qty=qty, ts=time.time(), price=close_price)
                    st["micro_fifo"].append(entry)
                    st["bars_in_cycle"] = 1
                    st["cycle_start_ts"] = time.time()
                    logging.info(f"{symbol} New cycle started: LONG {qty}@{close_price}")
            # If bar was flat (no significant move), do nothing
        else:
            # We have an active cycle
            st["bars_in_cycle"] += 1
            # Only add new entry if we have slots available
            if st["bars_in_cycle"] <= st["max_slots"]:
                if side_in_cycle == "LONG" and bar_down:
                    # Adverse move for LONG (price further down) -> add another LONG
                    qty = calculate_order_quantity(symbol, close_price)
                    if await safe_order_execution(cli, open_long(symbol, qty), symbol, "ADD LONG"):
                        entry = MicroEntry(side="LONG", qty=qty, ts=time.time(), price=close_price)
                        st["micro_fifo"].append(entry)
                        logging.info(f"{symbol} Added LONG {qty}@{close_price} (cycle size={len(st['micro_fifo'])})")
                elif side_in_cycle == "SHORT" and bar_up:
                    # Adverse move for SHORT (price further up) -> add another SHORT
                    qty = calculate_order_quantity(symbol, close_price)
                    if await safe_order_execution(cli, open_short(symbol, qty), symbol, "ADD SHORT"):
                        entry = MicroEntry(side="SHORT", qty=qty, ts=time.time(), price=close_price)
                        st["micro_fifo"].append(entry)
                        logging.info(f"{symbol} Added SHORT {qty}@{close_price} (cycle size={len(st['micro_fifo'])})")
                else:
                    logging.info(f"{symbol} No adverse move -> no new entry (side={side_in_cycle}, bars_in_cycle={st['bars_in_cycle']})")
            else:
                logging.warning(f"{symbol} max slots reached ({st['max_slots']}), no further entries")
    # End of handle_bar_close

# ========================= WEBSOCKET PRICE LOOP ======================
async def price_loop(cli: AsyncClient):
    """Connect to Binance WebSocket for price updates and bar closes"""
    symbol = "adausdt"
    stream_url = f"wss://fstream.binance.com/stream?streams={symbol}@markPrice@1s/{symbol}@kline_1h"
    logging.info(f"Connecting to WebSocket: {stream_url}")
    global trading_active
    while trading_active:
        try:
            async with websockets.connect(stream_url) as ws:
                # Send ping/pong handled by websockets library automatically
                async for msg in ws:
                    data = json.loads(msg)
                    stream = data.get("stream", "")
                    content = data.get("data", {})
                    if stream.endswith("@markPrice@1s"):
                        # Mark price update
                        price = float(content.get("p", 0.0))
                        st = state["ADAUSDT"]
                        st["last_price"] = price
                        st["last_price_time"] = time.time()
                        st["price_buffer"].append((st["last_price_time"], price))
                        # Check if any open positions have turned profitable, flatten immediately
                        if st["micro_fifo"] and not st.get("trading_paused", False):
                            # Calculate approximate net PnL
                            net_pnl = 0.0
                            for entry in st["micro_fifo"]:
                                if entry["side"] == "LONG":
                                    net_pnl += (price - entry["price"]) * entry["qty"]
                                elif entry["side"] == "SHORT":
                                    net_pnl += (entry["price"] - price) * entry["qty"]
                            if net_pnl > 0.0:
                                logging.info(f"Realtime PnL {net_pnl:.2f} > 0, flattening positions immediately")
                                # Add realized profit
                                global cumulative_realized_pnl
                                cumulative_realized_pnl += net_pnl
                                await emergency_flatten_all(cli, "ADAUSDT")
                                continue
                    elif stream.endswith("@kline_1h"):
                        k = content.get("k", {})
                        if k.get("x"):  # bar closed
                            open_price = float(k.get("o", 0.0))
                            close_price = float(k.get("c", 0.0))
                            await handle_bar_close(cli, "ADAUSDT", open_price, close_price)
        except Exception as e:
            if trading_active:
                logging.error(f"WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
                continue
            else:
                break

# ========================= MAIN EXECUTION ======================
trading_active = True

async def main():
    global DUAL_SIDE, trading_active
    logging.basicConfig(level=logging.INFO)
    # Start HTTP ping server in background thread
    threading.Thread(target=start_ping, daemon=True).start()
    logging.info("Ping server started on port 10000")
    
    # Initialize Binance AsyncClient
    client = await AsyncClient.create(API_KEY, API_SECRET)
    # Set hedged mode if not already
    DUAL_SIDE = await get_dual_side(client)
    if not DUAL_SIDE:
        try:
            await call_binance(client.futures_change_position_mode, dualSidePosition=True)
            DUAL_SIDE = True
            logging.info("Enabled dual-side (hedge) position mode")
        except Exception as e:
            logging.error(f"Failed to set dual-side mode: {e}")
    # Set leverage for symbols
    for sym in SYMBOLS:
        try:
            await call_binance(client.futures_change_leverage, symbol=sym, leverage=LEVERAGE)
            logging.info(f"Leverage set to {LEVERAGE}x for {sym}")
        except Exception as e:
            logging.error(f"Failed to set leverage for {sym}: {e}")
    
    # Seed price/quantity filters
    await seed_symbol_filters(client)
    # Adopt existing positions into micro_fifo
    await adopt_positions(client)
    
    # Check initial risk (to ensure not starting in a bad state)
    ok = await check_risk_limits(client)
    if not ok:
        logging.error("Initial risk check failed, shutting down")
        trading_active = False
    
    # Start concurrent tasks: price loop and periodic risk management loop
    risk_task = asyncio.create_task(risk_loop(client))
    price_task = asyncio.create_task(price_loop(client))
    # Wait for tasks to complete (they run until cancelled or error)
    await asyncio.gather(risk_task, price_task)
    
    # Clean up
    try:
        await client.close_connection()
    except Exception as e:
        logging.error(f"Error on closing client: {e}")
    logging.info("Main execution completed.")

async def risk_loop(cli: AsyncClient):
    global trading_active
    while trading_active:
        # Check risk limits every 5 seconds
        cont = await check_risk_limits(cli)
        if not cont:
            # If risk check returns False, signal to stop trading
            trading_active = False
            # Break out to end risk loop
            break
        await asyncio.sleep(5)
    logging.info("Risk loop terminated.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error(f"Unhandled exception in main: {e}")
