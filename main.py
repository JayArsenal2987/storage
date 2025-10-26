//@version=5
indicator("TAMA - Triple Adaptive MA with Dual ER", overlay=true, max_bars_back=500)

// ==================== INPUTS ====================

// Kalman Filter Parameters
kalman_q = input.float(0.001, "Kalman Q (Process Noise)", minval=0.0001, maxval=0.1, step=0.001, group="Layer 1: Kalman Filter")
kalman_r = input.float(0.01, "Kalman R (Measurement Noise)", minval=0.001, maxval=1.0, step=0.001, group="Layer 1: Kalman Filter")

// JMA Parameters
jma_length_fast = input.int(7, "JMA Fast Length", minval=1, maxval=500, group="Layer 2: JMA")
jma_length_slow = input.int(100, "JMA Slow Length", minval=1, maxval=500, group="Layer 2: JMA")
jma_phase = input.int(0, "JMA Phase", minval=-100, maxval=100, group="Layer 2: JMA")
jma_power = input.int(3, "JMA Power", minval=1, maxval=10, group="Layer 2: JMA")

// Efficiency Ratio Parameters
er_periods_fast = input.int(20, "ER Fast Periods", minval=5, maxval=200, group="Layer 3: Efficiency Ratio")
er_periods_slow = input.int(100, "ER Slow Periods", minval=10, maxval=500, group="Layer 3: Efficiency Ratio")
alpha_weight = input.float(1.0, "Alpha Weight", minval=0.1, maxval=2.0, step=0.1, group="Layer 3: Efficiency Ratio")

// Visual Settings
show_fast = input.bool(true, "Show TAMA Fast", group="Display")
show_slow = input.bool(true, "Show TAMA Slow", group="Display")
show_signals = input.bool(true, "Show Entry Signals", group="Display")
show_er_info = input.bool(true, "Show ER Values in Table", group="Display")

// Entry Mode Selection
use_crossover_entry = input.bool(false, "Use Crossover Entry Mode", tooltip="TRUE = Requires actual crossover event | FALSE = Symmetrical mode (Price > Fast > Slow)", group="Entry Mode")
entry_mode_info = use_crossover_entry ? "CROSSOVER MODE: Waits for MA cross" : "SYMMETRICAL MODE: Continuous condition"

// ==================== KALMAN FILTER ====================

var float kalman_x = na
var float kalman_p = 1.0

f_kalman_filter(float measurement) =>
    var float x = na
    var float p = 1.0
    
    if na(x)
        x := measurement
        p := 1.0
        x
    else
        // Prediction
        x_pred = x
        p_pred = p + kalman_q
        
        // Update
        kalman_gain = p_pred / (p_pred + kalman_r)
        x := x_pred + kalman_gain * (measurement - x_pred)
        p := (1 - kalman_gain) * p_pred
        x

// Apply Kalman filter to close price
kalman_close = f_kalman_filter(close)

// ==================== JMA CALCULATION ====================

f_jma(series float src, simple int length, simple int phase, simple int power) =>
    phaseRatio = phase < -100 ? 0.5 : phase > 100 ? 2.5 : phase / 100 + 1.5
    beta = 0.45 * (length - 1) / (0.45 * (length - 1) + 2)
    alpha = math.pow(beta, power)
    
    var float e0 = 0.0
    var float e1 = 0.0
    var float e2 = 0.0
    var float jma = 0.0
    
    e0 := (1 - alpha) * src + alpha * e0
    e1 := (src - e0) * (1 - beta) + beta * e1
    e2 := (e0 + phaseRatio * e1 - jma) * math.pow((1 - alpha), 2) + math.pow(alpha, 2) * e2
    jma := e2 + jma
    jma

// Calculate JMA on Kalman-filtered data
jma_fast = f_jma(kalman_close, jma_length_fast, jma_phase, jma_power)
jma_slow = f_jma(kalman_close, jma_length_slow, jma_phase, jma_power)

// ==================== EFFICIENCY RATIO ====================

f_efficiency_ratio(simple int periods) =>
    if bar_index < periods
        na
    else
        net_change = math.abs(close - close[periods])
        sum_changes = 0.0
        for i = 1 to periods
            sum_changes := sum_changes + math.abs(close[i-1] - close[i])
        
        sum_changes == 0 ? 0 : net_change / sum_changes

// Calculate separate ERs
er_fast = f_efficiency_ratio(er_periods_fast)
er_slow = f_efficiency_ratio(er_periods_slow)

// ==================== TAMA CALCULATION ====================

f_tama(float jma_value, float kalman_price, float er) =>
    if na(jma_value) or na(kalman_price) or na(er)
        na
    else
        adjustment = alpha_weight * er * (kalman_price - jma_value)
        jma_value + adjustment

// Calculate TAMA Fast and Slow
tama_fast = f_tama(jma_fast, kalman_close, er_fast)
tama_slow = f_tama(jma_slow, kalman_close, er_slow)

// ==================== SIGNALS ====================

// Crossover detection
bullish_cross = ta.crossover(tama_fast, tama_slow)
bearish_cross = ta.crossunder(tama_fast, tama_slow)

// Entry conditions (Symmetrical mode - can be changed)
bullish_condition = close > tama_fast and tama_fast > tama_slow
bearish_condition = close < tama_fast and tama_fast < tama_slow

// ==================== PLOTTING ====================

// Plot TAMA lines
plot(show_fast ? tama_fast : na, "TAMA Fast", color=color.new(color.blue, 0), linewidth=2)
plot(show_slow ? tama_slow : na, "TAMA Slow", color=color.new(color.red, 0), linewidth=2)

// Plot crossover signals
plotshape(show_signals and bullish_cross, "Bullish Cross", shape.triangleup, location.belowbar, color.new(color.green, 0), size=size.small)
plotshape(show_signals and bearish_cross, "Bearish Cross", shape.triangledown, location.abovebar, color.new(color.red, 0), size=size.small)

// Background color for trend
bgcolor(bullish_condition ? color.new(color.green, 95) : bearish_condition ? color.new(color.red, 95) : na)

// ==================== INFO TABLE ====================

if show_er_info and barstate.islast
    var table info_table = table.new(position.top_right, 2, 6, border_width=1)
    
    // Header
    table.cell(info_table, 0, 0, "TAMA Info", text_color=color.white, bgcolor=color.gray)
    table.cell(info_table, 1, 0, "Value", text_color=color.white, bgcolor=color.gray)
    
    // Trend
    trend_text = tama_fast > tama_slow ? "BULL ▲" : tama_fast < tama_slow ? "BEAR ▼" : "FLAT ═"
    trend_color = tama_fast > tama_slow ? color.green : tama_fast < tama_slow ? color.red : color.gray
    table.cell(info_table, 0, 1, "Trend", text_color=color.white, bgcolor=color.gray)
    table.cell(info_table, 1, 1, trend_text, text_color=color.white, bgcolor=trend_color)
    
    // ER Fast
    table.cell(info_table, 0, 2, "ER Fast", text_color=color.white, bgcolor=color.gray)
    table.cell(info_table, 1, 2, str.tostring(er_fast, "#.###"), text_color=color.white, bgcolor=color.blue)
    
    // ER Slow
    table.cell(info_table, 0, 3, "ER Slow", text_color=color.white, bgcolor=color.gray)
    table.cell(info_table, 1, 3, str.tostring(er_slow, "#.###"), text_color=color.white, bgcolor=color.red)
    
    // TAMA Fast
    table.cell(info_table, 0, 4, "TAMA Fast", text_color=color.white, bgcolor=color.gray)
    table.cell(info_table, 1, 4, str.tostring(tama_fast, "#.####"), text_color=color.white, bgcolor=color.blue)
    
    // TAMA Slow
    table.cell(info_table, 0, 5, "TAMA Slow", text_color=color.white, bgcolor=color.gray)
    table.cell(info_table, 1, 5, str.tostring(tama_slow, "#.####"), text_color=color.white, bgcolor=color.red)

// ==================== ALERTS ====================

alertcondition(bullish_cross, "TAMA Bullish Cross", "TAMA Fast crossed above TAMA Slow - Potential LONG entry")
alertcondition(bearish_cross, "TAMA Bearish Cross", "TAMA Fast crossed below TAMA Slow - Potential SHORT entry")
alertcondition(bullish_condition, "TAMA Bullish Condition", "Price > Fast > Slow - Bullish trend")
alertcondition(bearish_condition, "TAMA Bearish Condition", "Price < Fast < Slow - Bearish trend")
