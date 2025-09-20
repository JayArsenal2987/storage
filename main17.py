if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%b %d %H:%M:%S")
    class CleanLogFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "[SUMMARY]" in msg: return True
            if "[DIAG" in msg: return True
            if record.levelno >= logging.WARNING: return True
            if any(x in msg for x in [
                "submitted - OrderID", "executed - OrderID", "Signal change", "BOT ACTIVATED",
                "Circuit breaker", "FILLED", "Bounce conflict resolved", "TRAIL",
                "BOUNCE:", "BOUNCE LONG:", "BOUNCE SHORT:", "BREAKOUT LONG:", "BREAKOUT SHORT:", "BREAKOUT CHECK"
            ]): return True
            return False
    logging.getLogger().addFilter(CleanLogFilter())

    logging.info("=== DUAL STRATEGY TRADING BOT (Breakout + Bounce with Priority System | STRICT Donchian Hi/Lo + Adaptive Windows) ===")
    logging.info(f"Symbols & target sizes: {SYMBOLS}")
    logging.info(f"Leverage: {LEVERAGE}x | Bounce threshold={BOUNCE_THRESHOLD_PCT*100:.1f}% | Min hold={MIN_HOLD_SEC}s")
    logging.info(f"STRATEGY PRIORITY: 1st=BREAKOUT (strict Donchian), 2nd=BOUNCE (strict Donchian thresholds)")
    logging.info(f"Exchange trailing: {EXCHANGE_TRAIL_CALLBACK_RATE:.3f}% (internal callback disabled)")

    # Safe + pretty logging for window behavior
    _bounce_list   = BOUNCE_WINDOWS_HOURS or [20]   # fallback to 20h if empty
    _breakout_list = sorted(set(BREAKOUT_WINDOWS_HOURS or [20]))
    _breakout_str  = ", ".join(f"{h}h" for h in _breakout_list)
    logging.info(f"Bounce: uses widest window ({max(_bounce_list)}h) | Breakout: triggers on any window [{_breakout_str}]")

    logging.info(f"Order validation: timeout={ORDER_FILL_TIMEOUT_SEC}s, attempts={MAX_FILL_CHECK_ATTEMPTS}")
    asyncio.run(main())
