"""Pump.fun sniper strategy: buy brand-new tokens at creation, exit at +20%.

Rationale (proven memecoin scalping): freshly created pump.fun tokens often
pump violently in their first minutes as early buyers pile in. The edge is to
enter at launch and take a fast, mechanical +20% profit before the inevitable
pullback — no need to wait for slow technical indicators on a token with no
candle history yet.

This is a SEPARATE, faster strategy than the indicator-based scalper:
- Entry trigger: a token just appeared on pump.fun (age < N minutes)
- Exit: fixed +20% take-profit, with a tight protective stop
- No RSI/MACD/ADX: the token has no candles yet, we trade pure launch momentum
"""

from dataclasses import dataclass
from typing import Any

from aioud.utils.logger import get_logger

logger = get_logger(__name__)

# Pump.fun program address — tokens created via pump.fun have this as authority
# or appear in the pump.fun recent-creation feeds.
PUMPFUN_PROGRAM = "6EF8rrecthR5DkZon8NwWuZ5fNBBuCgHpBdMeKBbTpump"


@dataclass
class SniperConfig:
    """Tunable parameters for the pump.fun sniper."""

    take_profit_pct: float = 0.20   # exit at +20%
    stop_loss_pct: float = 0.10     # tight -10% stop (fast invalidation)
    max_token_age_minutes: float = 15.0  # only snipe tokens younger than this
    min_liquidity_usd: float = 3_000.0   # avoid dust/illiquid launches
    position_usd: float = 0.0       # 0 -> use engine's percentage sizing


def is_fresh_pumpfun_token(
    pair: Any,
    max_age_minutes: float = 15.0,
    min_liquidity_usd: float = 3_000.0,
) -> bool:
    """Return True if a token looks like a freshly-launched pump.fun token.

    A token qualifies when it is very young (recently created/discovered) and
    has enough liquidity to enter/exit without huge slippage. We detect youth
    via the discovery profile timestamp when available, else via the scanner's
    ``price_change_24h`` being extremely high (a hallmark of a fresh launch).
    """
    from datetime import datetime, timezone

    # Liquidity floor: can't scalp a token with $200 liquidity.
    if getattr(pair, "liquidity_usd", 0.0) < min_liquidity_usd:
        return False

    # ONLY snipe tokens with a verified recent creation/profile timestamp.
    # The old price_change_24h>=100 fallback caused false positives: it sniped
    # OLD watchlist tokens that were merely pumping today (HAIRINU, HONOR...).
    updated = getattr(pair, "profile_updated_at", None)
    if updated is None:
        return False  # no reliable creation time -> not a fresh launch
    try:
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60
        return age_min <= max_age_minutes
    except Exception:
        return False


def sniper_exit_levels(entry_price: float, cfg: SniperConfig) -> dict[str, float]:
    """Compute the sniper's mechanical exit levels: +20% TP, -10% SL.

    Unlike the scalper's multi-TP ladder, the sniper uses a single aggressive
    take-profit because the goal is a fast in-and-out on launch momentum.
    """
    return {
        "tp": entry_price * (1 + cfg.take_profit_pct),
        "sl": entry_price * (1 - cfg.stop_loss_pct),
    }


def should_snipe_exit(
    entry_price: float,
    current_price: float,
    cfg: SniperConfig,
) -> dict[str, Any]:
    """Decide whether a sniped position should exit at +20% TP or -10% SL."""
    if entry_price <= 0:
        return {"action": "hold", "reason": "invalid entry"}
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct >= cfg.take_profit_pct:
        return {
            "action": "close",
            "reason": f"Sniper TP +{cfg.take_profit_pct*100:.0f}% hit at {current_price:.6f}",
        }
    if pnl_pct <= -cfg.stop_loss_pct:
        return {
            "action": "close",
            "reason": f"Sniper SL -{cfg.stop_loss_pct*100:.0f}% hit at {current_price:.6f}",
        }
    return {"action": "hold", "reason": f"Sniper holding {pnl_pct*100:+.1f}%"}
