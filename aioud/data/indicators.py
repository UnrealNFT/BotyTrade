"""Technical indicators: RSI, MACD, Heikin-Ashi, ADX, volume spike, EMA trend."""

import math

import numpy as np
import pandas as pd


def _safe_float(value) -> float:
    """Return 0.0 for NaN/inf values."""
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


def compute_rsi(prices: pd.Series, period: int = 14) -> float:
    """Return the latest RSI value for a price series."""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return _safe_float(rsi.iloc[-1])


def compute_macd(
    prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> dict:
    """Return MACD line, signal line and histogram values."""
    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line

    return {
        "macd": _safe_float(macd_line.iloc[-1]),
        "signal": _safe_float(signal_line.iloc[-1]),
        "histogram": _safe_float(histogram.iloc[-1]),
        "crossover": bool(macd_line.iloc[-1] > signal_line.iloc[-1]),
        "previous_crossover": bool(macd_line.iloc[-2] > signal_line.iloc[-2]),
    }


def compute_heikin_ashi(df: pd.DataFrame) -> dict:
    """Return Heikin-Ashi trend state from OHLC dataframe."""
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2

    ha_high = pd.concat([df["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([df["low"], ha_open, ha_close], axis=1).min(axis=1)

    last_close = float(ha_close.iloc[-1])
    last_open = float(ha_open.iloc[-1])
    prev_close = float(ha_close.iloc[-2])
    prev_open = float(ha_open.iloc[-2])

    trend_up = last_close > last_open
    trend_strong = trend_up and (last_close - last_open) > (prev_close - prev_open)
    doji = abs(last_close - last_open) < (ha_high.iloc[-1] - ha_low.iloc[-1]) * 0.1

    return {
        "ha_open": _safe_float(last_open),
        "ha_close": _safe_float(last_close),
        "trend": "up" if trend_up else "down",
        "strong": bool(trend_strong),
        "doji": bool(doji),
        "body_size": _safe_float(abs(last_close - last_open)),
    }


def compute_adx(df: pd.DataFrame, period: int = 14) -> dict:
    """Compute ADX, +DI and -DI for trend strength."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff().clip(lower=0)
    minus_dm = -low.diff().clip(upper=0)
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, min_periods=period).mean()

    return {
        "adx": _safe_float(adx.iloc[-1]),
        "plus_di": _safe_float(plus_di.iloc[-1]),
        "minus_di": _safe_float(minus_di.iloc[-1]),
    }


def compute_volume_spike(volume: pd.Series, short: int = 5, long: int = 20) -> float:
    """Return ratio of short-term vs long-term average volume."""
    if len(volume) < long:
        return 1.0
    short_avg = volume.iloc[-short:].mean()
    long_avg = volume.iloc[-long:].mean()
    if long_avg == 0:
        return 1.0
    return _safe_float(short_avg / long_avg)


def compute_ema_trend(closes: pd.Series, fast: int = 9, slow: int = 21) -> dict:
    """Return EMA trend alignment score."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    return {
        "ema_fast": _safe_float(ema_fast.iloc[-1]),
        "ema_slow": _safe_float(ema_slow.iloc[-1]),
        "bullish": bool(ema_fast.iloc[-1] > ema_slow.iloc[-1]),
        "strength": _safe_float((ema_fast.iloc[-1] - ema_slow.iloc[-1]) / ema_slow.iloc[-1] * 100),
    }


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Return the latest Average True Range — the basis for volatility-aware stops.

    Used by the risk manager to size stops/targets to each token's actual
    volatility (Turtle Traders approach) instead of fixed percentages.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
    return _safe_float(atr.iloc[-1])


def generate_signal(
    closes: pd.Series,
    ohlc: pd.DataFrame,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> dict:
    """Generate a weighted trading signal from multiple indicators."""
    min_len = max(rsi_period, macd_slow, macd_signal, 21) + 2
    if len(closes) < min_len:
        return {"signal": "hold", "score": 0, "reason": f"Not enough data ({len(closes)}/{min_len})"}

    rsi = compute_rsi(closes, rsi_period)
    macd = compute_macd(closes, macd_fast, macd_slow, macd_signal)
    ha = compute_heikin_ashi(ohlc)
    adx_data = compute_adx(ohlc)
    volume_spike = compute_volume_spike(ohlc["volume"])
    ema = compute_ema_trend(closes)
    atr = compute_atr(ohlc)

    score = 0
    reasons = []

    # RSI: avoid overbought, reward oversold bounces in an uptrend
    if rsi < 30 and ema["bullish"]:
        score += 25
        reasons.append(f"RSI {rsi:.1f} oversold + EMA up")
    elif rsi < 45 and ema["bullish"]:
        score += 10
        reasons.append(f"RSI {rsi:.1f} lean oversold")
    elif rsi > 70:
        score -= 20
        reasons.append(f"RSI {rsi:.1f} overbought")

    # MACD crossover / alignment
    if macd["crossover"] and not macd["previous_crossover"]:
        score += 25
        reasons.append("MACD bullish crossover")
    elif macd["crossover"]:
        score += 10
        reasons.append("MACD above signal")
    elif not macd["crossover"] and macd["previous_crossover"]:
        score -= 15
        reasons.append("MACD bearish cross")

    # Heikin-Ashi trend
    if ha["trend"] == "up" and ha["strong"]:
        score += 20
        reasons.append("HA strong up")
    elif ha["trend"] == "up":
        score += 10
        reasons.append("HA up")
    elif ha["trend"] == "down":
        score -= 15
        reasons.append("HA down")

    # ADX trend strength filter: require ADX > 20 for strong signals
    adx = adx_data["adx"]
    if adx >= 25:
        score += 10
        reasons.append(f"ADX {adx:.1f} strong trend")
    elif adx < 15:
        score -= 10
        reasons.append(f"ADX {adx:.1f} no trend")

    # Volume confirmation
    if volume_spike >= 1.5:
        score += 10
        reasons.append(f"Volume spike {volume_spike:.1f}x")
    elif volume_spike < 0.8:
        score -= 5
        reasons.append("Volume low")

    # EMA trend alignment
    if ema["bullish"]:
        score += 10
        reasons.append("EMA bullish")
    else:
        score -= 10
        reasons.append("EMA bearish")

    signal = "hold"
    if score >= 50:
        signal = "buy"
    elif score <= -50:
        signal = "sell"

    return {
        "signal": signal,
        "score": score,
        "rsi": round(rsi, 2),
        "macd": macd,
        "heikin_ashi": ha,
        "adx": adx_data,
        "volume_spike": round(volume_spike, 2),
        "ema": ema,
        "atr": atr,
        "reason": "; ".join(reasons),
    }
