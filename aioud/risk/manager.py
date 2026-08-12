"""Risk manager: position sizing, TP/SL, trailing stop, cooldown, daily limits."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aioud.utils.config import Config
from aioud.utils.logger import get_logger
from aioud.utils import postgres_store

logger = get_logger(__name__)

STATE_DIR = Path("data")
POSITIONS_FILE = STATE_DIR / "positions.json"
HISTORY_FILE = STATE_DIR / "trade_history.json"


def _postgres_enabled() -> bool:
    return postgres_store._is_postgres_available()


@dataclass
class Position:
    """Open position tracking."""

    mint: str
    symbol: str
    entry_price: float
    amount: float
    side: str = "long"
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    sl: float = 0.0
    trailing_stop: float = 0.0
    highest_price: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price * 100

    def pnl_usd(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.amount


class RiskManager:
    """Enforce risk rules and compute exit levels."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.daily_loss_usd = 0.0
        self.daily_reset = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self.positions: dict[str, Position] = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.trade_history: list[dict[str, Any]] = []
        self.last_reject_reason: str = ""
        self._load_state()

    @staticmethod
    def _state_dir() -> Path:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return STATE_DIR

    def _save_state(self) -> None:
        """Persist open positions and recent trade history to disk/DB."""
        try:
            self._state_dir()
            positions_data = [
                {
                    "mint": p.mint,
                    "symbol": p.symbol,
                    "entry_price": p.entry_price,
                    "amount": p.amount,
                    "side": p.side,
                    "tp1": p.tp1,
                    "tp2": p.tp2,
                    "tp3": p.tp3,
                    "sl": p.sl,
                    "trailing_stop": p.trailing_stop,
                    "highest_price": p.highest_price,
                    "tp1_hit": p.tp1_hit,
                    "tp2_hit": p.tp2_hit,
                    "tp3_hit": p.tp3_hit,
                    "opened_at": p.opened_at.isoformat(),
                }
                for p in self.positions.values()
            ]
            with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(positions_data, f, indent=2)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.trade_history[-500:], f, indent=2)
            postgres_store.save_positions(positions_data)
        except Exception as exc:
            logger.warning("Failed to save risk state: %s", exc)

    def _load_state(self) -> None:
        """Restore open positions and trade history from disk/DB."""
        try:
            if _postgres_enabled():
                self._load_from_postgres()
            elif POSITIONS_FILE.exists():
                with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
                for item in data:
                    opened_at = datetime.fromisoformat(item["opened_at"])
                    # Skip stale positions older than 48h; likely abandoned state.
                    if opened_at < cutoff:
                        logger.info("Ignoring stale position %s from %s", item["symbol"], opened_at)
                        continue
                    pos = Position(
                        mint=item["mint"],
                        symbol=item["symbol"],
                        entry_price=float(item["entry_price"]),
                        amount=float(item["amount"]),
                        side=item.get("side", "long"),
                        tp1=float(item["tp1"]),
                        tp2=float(item["tp2"]),
                        tp3=float(item["tp3"]),
                        sl=float(item["sl"]),
                        trailing_stop=float(item.get("trailing_stop", item["sl"])),
                        highest_price=float(item.get("highest_price", item["entry_price"])),
                        tp1_hit=bool(item.get("tp1_hit", False)),
                        tp2_hit=bool(item.get("tp2_hit", False)),
                        tp3_hit=bool(item.get("tp3_hit", False)),
                        opened_at=opened_at,
                    )
                    self.positions[pos.mint] = pos
                logger.info("Loaded %s open positions from disk", len(self.positions))
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    self.trade_history = json.load(f)
                logger.info("Loaded %s historical trades from disk", len(self.trade_history))
        except Exception as exc:
            logger.warning("Failed to load risk state: %s", exc)

    def _load_from_postgres(self) -> None:
        """Load state from PostgreSQL when available."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            for item in postgres_store.load_positions():
                opened_at = datetime.fromisoformat(item["opened_at"])
                if opened_at < cutoff:
                    logger.info("Ignoring stale Postgres position %s from %s", item["symbol"], opened_at)
                    continue
                pos = Position(
                    mint=item["mint"],
                    symbol=item["symbol"],
                    entry_price=float(item["entry_price"]),
                    amount=float(item["amount"]),
                    side=item.get("side", "long"),
                    tp1=float(item["tp1"]),
                    tp2=float(item["tp2"]),
                    tp3=float(item["tp3"]),
                    sl=float(item["sl"]),
                    trailing_stop=float(item.get("trailing_stop", item["sl"])),
                    highest_price=float(item.get("highest_price", item["entry_price"])),
                    tp1_hit=bool(item.get("tp1_hit", False)),
                    tp2_hit=bool(item.get("tp2_hit", False)),
                    tp3_hit=bool(item.get("tp3_hit", False)),
                    opened_at=opened_at,
                )
                self.positions[pos.mint] = pos
            logger.info("Loaded %s open positions from Postgres", len(self.positions))
            self.trade_history = postgres_store.load_trade_history(limit=500)
            logger.info("Loaded %s historical trades from Postgres", len(self.trade_history))
        except Exception as exc:
            logger.warning("Failed to load Postgres risk state: %s", exc)

    def _reset_day_if_needed(self) -> None:
        now = datetime.now(timezone.utc)
        if now >= self.daily_reset:
            self.daily_loss_usd = 0.0
            self.daily_reset = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)

    def compute_position_size_usd(
        self,
        capital_sol: float,
        sol_price: float,
        usdc_balance: float | None = None,
    ) -> float:
        """Position size as a percentage of total capital (SOL + USDC).

        Scales automatically with the account balance: the position is always
        `max_position_pct` of capital, capped by `trading_amount_usd` (which
        acts as a max per trade) and never below the USDC dust threshold.
        """
        capital_usd = capital_sol * sol_price
        if usdc_balance is not None:
            capital_usd += usdc_balance
        size = capital_usd * self.config.max_position_pct
        return min(size, self.config.trading_amount_usd)

    def can_open_position(
        self,
        mint: str,
        symbol: str,
        capital_sol: float,
        sol_price: float,
        open_positions: int,
        usdc_balance: float | None = None,
    ) -> bool:
        """Check if a new position respects risk limits and wallet balance."""
        self._reset_day_if_needed()

        if self.daily_loss_usd >= self.config.max_daily_loss_usd:
            self.last_reject_reason = (
                f"Perte journalière max atteinte: {self.daily_loss_usd:.2f} / "
                f"{self.config.max_daily_loss_usd:.2f} USD"
            )
            logger.warning("Daily loss limit reached: %.2f / %.2f USD",
                           self.daily_loss_usd, self.config.max_daily_loss_usd)
            return False

        if open_positions >= 5:
            self.last_reject_reason = f"Max positions ouvertes atteint ({open_positions}/5)"
            logger.warning("Max open positions reached (%s)", open_positions)
            return False

        now = datetime.now(timezone.utc)
        cooldown = self.cooldown_until.get(mint)
        if cooldown and now < cooldown:
            self.last_reject_reason = f"{symbol} en cooldown jusqu'à {cooldown:%H:%M} UTC"
            logger.debug("%s in cooldown until %s", symbol, cooldown)
            return False

        # SOL fee reserve check
        if capital_sol < self.config.min_sol_balance_for_fees:
            self.last_reject_reason = (
                f"SOL trop faible pour les fees: {capital_sol:.4f} SOL "
                f"(min {self.config.min_sol_balance_for_fees})"
            )
            logger.warning("SOL balance too low for fees: %.4f SOL (need %.4f)",
                           capital_sol, self.config.min_sol_balance_for_fees)
            return False

        # Percentage-based sizing: position = max_position_pct of total capital.
        position_value = self.compute_position_size_usd(
            capital_sol, sol_price, usdc_balance
        )

        # USDC balance check for real trading
        if not self.config.paper_trading and usdc_balance is not None:
            required = max(position_value, self.config.min_usdc_balance_to_trade)
            if usdc_balance < required:
                self.last_reject_reason = (
                    f"USDC insuffisant: {usdc_balance:.2f}$ (besoin {required:.2f}$ "
                    f"pour une position de {position_value:.2f}$)"
                )
                logger.warning("USDC balance too low: %.2f (need %.2f to trade)",
                               usdc_balance, required)
                return False

        if not self.config.paper_trading and position_value < self.config.min_usdc_balance_to_trade:
            capital_usd = capital_sol * sol_price + (usdc_balance or 0.0)
            self.last_reject_reason = (
                f"Capital trop faible: {capital_usd:.2f}$ -> position "
                f"{position_value:.2f}$ < min {self.config.min_usdc_balance_to_trade:.2f}$"
            )
            logger.warning(
                "Capital too low: %.2f USD -> position %.2f below dust minimum",
                capital_usd, position_value,
            )
            return False

        self.last_reject_reason = ""
        return True

    def compute_exit_levels(self, entry_price: float) -> dict[str, float]:
        """Compute TP1/TP2/TP3 and SL levels from entry price."""
        return {
            "tp1": entry_price * (1 + self.config.default_tp1_pct),
            "tp2": entry_price * (1 + self.config.default_tp2_pct),
            "tp3": entry_price * (1 + self.config.default_tp3_pct),
            "sl": entry_price * (1 - self.config.default_sl_pct),
        }

    def add_position(
        self, mint: str, symbol: str, entry_price: float, amount: float
    ) -> Position:
        """Register a new position with TP/SL levels."""
        levels = self.compute_exit_levels(entry_price)
        pos = Position(
            mint=mint,
            symbol=symbol,
            entry_price=entry_price,
            amount=amount,
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            sl=levels["sl"],
            trailing_stop=levels["sl"],
            highest_price=entry_price,
        )
        self.positions[mint] = pos
        self._save_state()
        logger.info(
            "Position opened: %s @ %.6f | TP1=%.6f TP2=%.6f TP3=%.6f SL=%.6f",
            symbol,
            entry_price,
            pos.tp1,
            pos.tp2,
            pos.tp3,
            pos.sl,
        )
        return pos

    def _record_close(self, pos: Position, exit_price: float, reason: str) -> dict[str, Any]:
        pnl_usd = pos.pnl_usd(exit_price)
        pnl_pct = pos.pnl_pct(exit_price)
        duration = (datetime.now(timezone.utc) - pos.opened_at).total_seconds() / 60
        trade = {
            "symbol": pos.symbol,
            "entry": pos.entry_price,
            "exit": exit_price,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "duration_min": duration,
            "opened_at": pos.opened_at.isoformat(),
        }
        self.trade_history.append(trade)
        if pnl_usd < 0:
            self.daily_loss_usd += -pnl_usd
        del self.positions[pos.mint]
        self._save_state()
        postgres_store.add_trade(trade)
        # 15-minute cooldown after close
        self.cooldown_until[pos.mint] = datetime.now(timezone.utc) + timedelta(minutes=15)
        return trade

    def check_exit(self, mint: str, current_price: float) -> dict[str, Any]:
        """Check if a position should be closed or partially taken profit."""
        pos = self.positions.get(mint)
        if not pos:
            return {"action": "none", "reason": "No position"}

        # Update highest price and trailing stop after TP1
        if current_price > pos.highest_price:
            pos.highest_price = current_price
            if pos.tp1_hit and not pos.tp3_hit:
                # Trail at 40% of max gain below highest price
                trail_distance = (pos.highest_price - pos.entry_price) * 0.4
                new_stop = max(pos.trailing_stop, pos.highest_price - trail_distance)
                pos.trailing_stop = max(new_stop, pos.sl)

        # Stop loss or trailing stop
        effective_sl = max(pos.sl, pos.trailing_stop)
        if current_price <= effective_sl:
            reason = f"SL hit at {current_price:.6f}"
            if pos.trailing_stop > pos.sl:
                reason = f"Trailing stop hit at {current_price:.6f}"
            trade = self._record_close(pos, current_price, reason)
            return {"action": "close", **trade}

        # Take profits (echeloned)
        if current_price >= pos.tp3 and not pos.tp3_hit:
            pos.tp3_hit = True
            return {
                "action": "partial",
                "level": "tp3",
                "reason": f"TP3 reached at {current_price:.6f}",
                "exit_price": current_price,
                "portion": 0.4,
            }
        if current_price >= pos.tp2 and not pos.tp2_hit:
            pos.tp2_hit = True
            return {
                "action": "partial",
                "level": "tp2",
                "reason": f"TP2 reached at {current_price:.6f}",
                "exit_price": current_price,
                "portion": 0.3,
            }
        if current_price >= pos.tp1 and not pos.tp1_hit:
            pos.tp1_hit = True
            return {
                "action": "partial",
                "level": "tp1",
                "reason": f"TP1 reached at {current_price:.6f}",
                "exit_price": current_price,
                "portion": 0.3,
            }

        return {"action": "hold", "reason": "No exit condition met"}

    def get_status(self) -> dict[str, Any]:
        """Return current risk status."""
        self._reset_day_if_needed()
        return {
            "daily_loss_usd": round(self.daily_loss_usd, 2),
            "daily_loss_limit_usd": self.config.max_daily_loss_usd,
            "open_positions": len(self.positions),
            "positions": [
                {
                    "mint": p.mint,
                    "symbol": p.symbol,
                    "entry": p.entry_price,
                    "amount": p.amount,
                    "tp1": p.tp1,
                    "tp2": p.tp2,
                    "tp3": p.tp3,
                    "sl": p.sl,
                    "trailing_stop": p.trailing_stop,
                }
                for p in self.positions.values()
            ],
            "trade_history": self.trade_history[-50:],
        }
