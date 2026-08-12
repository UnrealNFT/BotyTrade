"""1-minute scalping strategy using RSI/MACD/Heikin-Ashi/ADX/EMA/Volume."""

from dataclasses import dataclass
from typing import Any

import pandas as pd

from aioud.data.indicators import generate_signal
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class StrategyResult:
    signal: str  # buy, sell, hold
    score: int
    details: dict[str, Any]


class SolanaScalper:
    """Multi-indicator scalping strategy for Solana tokens."""

    MIN_CANDLES = 23  # enough for ADX(14), EMA(21), MACD(26)

    def __init__(self, symbol: str = "SOL/USDC", mint: str = "") -> None:
        self.symbol = symbol
        self.mint = mint or "So11111111111111111111111111111111111111112"

    def analyze(self, ohlcv: list[dict[str, Any]]) -> StrategyResult:
        """Analyze OHLCV candles and return a signal."""
        df = pd.DataFrame(ohlcv)
        if len(df) < self.MIN_CANDLES:
            return StrategyResult(signal="hold", score=0, details={
                "reason": f"Not enough candles ({len(df)}/{self.MIN_CANDLES})"
            })

        df = df.sort_values("timestamp").reset_index(drop=True)
        signal_data = generate_signal(df["close"], df[["open", "high", "low", "close", "volume"]])

        return StrategyResult(
            signal=signal_data["signal"],
            score=signal_data["score"],
            details=signal_data,
        )

    def should_enter(self, ohlcv: list[dict[str, Any]]) -> bool:
        """Convenience method to check if we should buy."""
        result = self.analyze(ohlcv)
        return result.signal == "buy" and result.score >= 40

    def should_exit(self, ohlcv: list[dict[str, Any]]) -> bool:
        """Convenience method to check if we should sell."""
        result = self.analyze(ohlcv)
        return result.signal == "sell" or result.score <= -40
