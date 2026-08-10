"""Persistent watchlist store backed by JSON."""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aioud.data.scanner import TokenPair
from aioud.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_WATCHLIST_PATH = Path("data/watchlist.json")


def _pair_to_dict(pair: TokenPair) -> dict[str, Any]:
    data = asdict(pair)
    if data.get("profile_updated_at"):
        data["profile_updated_at"] = data["profile_updated_at"].isoformat()
    return data


def _pair_from_dict(data: dict[str, Any]) -> TokenPair:
    updated_at = data.get("profile_updated_at")
    if updated_at:
        try:
            updated_at = datetime.fromisoformat(updated_at)
        except Exception:
            updated_at = None
    return TokenPair(
        symbol=data["symbol"],
        mint=data["mint"],
        quote_symbol=data.get("quote_symbol", ""),
        quote_mint=data.get("quote_mint", ""),
        price_usd=float(data.get("price_usd", 0)),
        liquidity_usd=float(data.get("liquidity_usd", 0)),
        volume_24h=float(data.get("volume_24h", 0)),
        market_cap=float(data.get("market_cap", 0)),
        dex_id=data.get("dex_id", ""),
        pair_address=data.get("pair_address", ""),
        source_endpoints=data.get("source_endpoints", []),
        profile_updated_at=updated_at,
        boost_amount=float(data.get("boost_amount", 0.0)),
        total_boost_amount=float(data.get("total_boost_amount", 0.0)),
        price_change_24h=data.get("price_change_24h"),
        txn_count_24h=data.get("txn_count_24h"),
        holders=data.get("holders"),
    )


class WatchlistStore:
    """Load/save the manual + discovered watchlist to disk."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_WATCHLIST_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[TokenPair]:
        """Load persisted pairs. Returns empty list if missing or corrupt."""
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                items = json.load(f)
            pairs = []
            for item in items:
                try:
                    pairs.append(_pair_from_dict(item))
                except Exception as exc:
                    logger.warning("Skipping corrupt watchlist entry: %s", exc)
            return pairs
        except Exception as exc:
            logger.warning("Failed to load watchlist from %s: %s", self.path, exc)
            return []

    def save(self, pairs: list[TokenPair]) -> None:
        """Persist pairs to disk."""
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump([_pair_to_dict(p) for p in pairs], f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Failed to save watchlist to %s: %s", self.path, exc)

    def remove(self, mint: str) -> bool:
        """Remove a pair by mint. Returns True if it existed."""
        pairs = self.load()
        new_pairs = [p for p in pairs if p.mint != mint]
        if len(new_pairs) == len(pairs):
            return False
        self.save(new_pairs)
        return True
