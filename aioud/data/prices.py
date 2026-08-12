"""Price feed that builds 1m OHLCV candles from DexScreener + CoinGecko snapshots."""

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from aioud.utils import postgres_store
from aioud.utils.logger import get_logger

logger = get_logger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEXSCREENER_BASE = "https://api.dexscreener.com"
CANDLE_INTERVAL_SECONDS = 60  # 1-minute candles


@dataclass
class Candle:
    """A single 1-minute candle."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class PriceFeed:
    """Sample prices and maintain 1m candles for tracked tokens."""

    def __init__(
        self,
        db_path: Path | None = None,
        interval_seconds: int = CANDLE_INTERVAL_SECONDS,
        trappist_api_url: str = "",
        trappist_api_key: str = "",
    ):
        self.db_path = db_path or Path("data/prices.db")
        self.interval_seconds = interval_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_ttl = 15.0
        self.use_postgres = postgres_store._is_postgres_available()
        if self.use_postgres:
            postgres_store.init_schema()
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        self._trappist = None
        if trappist_api_url:
            from aioud.data.trappist_prices import TrappistPriceFeed

            self._trappist = TrappistPriceFeed(trappist_api_url, trappist_api_key)

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    mint TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (mint, timestamp)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    mint TEXT NOT NULL,
                    ts REAL NOT NULL,
                    price REAL NOT NULL,
                    volume_24h REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts
                ON snapshots (mint, ts)
                """
            )

    def _cached(self, mint: str, fetcher) -> dict[str, Any] | None:
        """Return cached price if fresh, otherwise fetch and cache."""
        now = time.time()
        entry = self._cache.get(mint)
        if entry and now - entry[0] < self._cache_ttl:
            return entry[1]
        data = fetcher(mint)
        if data and data.get("price", 0) > 0:
            self._cache[mint] = (now, data)
        return data

    def _fetch_dexscreener_price(self, mint: str) -> dict[str, Any] | None:
        """Fetch current price for a token mint from DexScreener."""
        try:
            # Direct token endpoint is far more reliable than search for exact mints.
            url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}"
            response = requests.get(url, timeout=(5, 10))
            response.raise_for_status()
            data = response.json()
            pairs = [
                p
                for p in data.get("pairs", [])
                if p.get("chainId") == "solana"
                and p.get("baseToken", {}).get("address") == mint
            ]
            if not pairs:
                return None
            stable = [p for p in pairs if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")]
            candidates = stable or pairs
            best = max(candidates, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
            return {
                "price": float(best.get("priceUsd") or 0),
                "volume_24h": float(best.get("volume", {}).get("h24") or 0),
                "liquidity_usd": float(best.get("liquidity", {}).get("usd") or 0),
                "source": "dexscreener",
            }
        except Exception as exc:
            logger.debug("DexScreener price failed for %s: %s", mint, exc)
            return None

    def _fetch_coingecko_price(self, mint: str) -> dict[str, Any] | None:
        """Fetch price from CoinGecko as fallback."""
        try:
            response = requests.get(
                f"{COINGECKO_BASE}/simple/token_price/solana",
                params={"contract_addresses": mint, "vs_currencies": "usd"},
                timeout=(5, 10),
            )
            response.raise_for_status()
            data = response.json()
            price = data.get(mint.lower(), {}).get("usd")
            if price:
                return {
                    "price": float(price),
                    "volume_24h": 0.0,
                    "liquidity_usd": 0.0,
                    "source": "coingecko",
                }
        except Exception as exc:
            logger.debug("CoinGecko price failed for %s: %s", mint, exc)
        return None

    def _fetch_dexscreener_batch(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch prices for many mints in a single DexScreener call.

        Uses the ``/tokens/v1/solana/{addresses}`` endpoint which accepts up to
        30 addresses and is more reliable than the search endpoint.
        Rate-limited to avoid 429 errors from DexScreener.
        """
        if not mints:
            return {}
        results: dict[str, dict[str, Any]] = {}
        for i in range(0, len(mints), 20):
            batch = mints[i : i + 20]
            try:
                url = f"{DEXSCREENER_BASE}/tokens/v1/solana/{','.join(batch)}"
                response = requests.get(url, timeout=(5, 15))
                if response.status_code == 429:
                    logger.warning("DexScreener rate limit hit, backing off")
                    time.sleep(2.5)
                    response = requests.get(url, timeout=(5, 15))
                response.raise_for_status()
                data = response.json()
                items = data if isinstance(data, list) else data.get("pairs", [])
                pairs_by_mint: dict[str, list[dict[str, Any]]] = {}
                for raw in items:
                    if raw.get("chainId") != "solana":
                        continue
                    mint = raw.get("baseToken", {}).get("address", "")
                    if mint not in batch:
                        continue
                    pairs_by_mint.setdefault(mint, []).append(raw)
                for mint, pairs in pairs_by_mint.items():
                    stable = [p for p in pairs if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")]
                    candidates = stable or pairs
                    best = max(candidates, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
                    if best.get("priceUsd"):
                        results[mint] = {
                            "price": float(best["priceUsd"]),
                            "volume_24h": float(best.get("volume", {}).get("h24") or 0),
                            "liquidity_usd": float(float(best.get("liquidity", {}).get("usd") or 0)),
                            "source": "dexscreener",
                        }
                # DexScreener free tier: larger delay between batches to avoid 429.
                time.sleep(1.0)
            except Exception as exc:
                logger.warning("DexScreener batch price fetch failed for batch %s: %s", batch, exc)
        return results

    def fetch_prices(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch prices for multiple mints (best effort).

        Uses TrappistAI backend first if configured, then DexScreener batch,
        then individual fetches for any missing tokens.
        """
        results: dict[str, dict[str, Any]] = {}
        remaining: list[str] = list(mints)
        now = time.time()

        if self._trappist:
            try:
                trappist_results = self._trappist.fetch_prices(mints)
                for mint in list(remaining):
                    data = trappist_results.get(mint)
                    if data and data.get("price", 0) > 0:
                        self._cache[mint] = (now, data)
                        results[mint] = data
                        remaining.remove(mint)
            except Exception as exc:
                logger.warning("TrappistAI price fetch failed: %s", exc)

        if remaining:
            batch_results = self._fetch_dexscreener_batch(remaining)
            for mint in list(remaining):
                data = batch_results.get(mint)
                if data and data.get("price", 0) > 0:
                    self._cache[mint] = (now, data)
                    results[mint] = data
                    remaining.remove(mint)

        for mint in remaining:
            try:
                results[mint] = self.fetch_price(mint)
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", mint, exc)
        return results

    def fetch_price(self, mint: str) -> dict[str, Any]:
        """Fetch current price for a single mint (cached)."""
        entry = self._cache.get(mint)
        if entry:
            return entry[1]
        data = self.fetch_prices([mint]).get(mint)
        if not data:
            raise ValueError(f"No price data available for {mint}")
        return data

    def record_snapshot(self, mint: str, price: float, volume_24h: float) -> None:
        """Store a raw price snapshot."""
        ts = time.time()
        if self.use_postgres:
            postgres_store.record_snapshot(mint, ts, price, volume_24h)
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO snapshots (mint, ts, price, volume_24h) VALUES (?, ?, ?, ?)",
                (mint, ts, price, volume_24h),
            )

    def build_candles(self, mint: str) -> list[Candle]:
        """Aggregate snapshots into 1m candles and persist them."""
        interval = self.interval_seconds
        if self.use_postgres:
            rows = postgres_store.get_snapshots(mint)
        else:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT ts, price, volume_24h FROM snapshots WHERE mint = ? ORDER BY ts",
                    (mint,),
                ).fetchall()

        if len(rows) < 2:
            return []

        df = pd.DataFrame(rows, columns=["ts", "price", "volume_24h"])
        df["timestamp"] = (df["ts"] // interval) * interval
        grouped = df.groupby("timestamp").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume_24h", "last"),
        )
        candles = [
            Candle(
                timestamp=int(idx),  # type: ignore[arg-type]
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
            for idx, row in grouped.iterrows()
        ]

        candle_dicts = [
            {
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        if self.use_postgres:
            postgres_store.save_candles(mint, candle_dicts)
        else:
            with sqlite3.connect(self.db_path) as conn:
                for c in candles:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO candles
                        (mint, timestamp, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (mint, c.timestamp, c.open, c.high, c.low, c.close, c.volume),
                    )

        return candles

    def get_candles(self, mint: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent candles for a token as strategy input."""
        if self.use_postgres:
            return postgres_store.get_candles(mint, limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM candles
                WHERE mint = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (mint, limit),
            ).fetchall()
        return [
            {
                "timestamp": r[0],
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "volume": r[5],
            }
            for r in reversed(rows)
        ]

    def snapshot_and_build(self, mint: str) -> list[dict[str, Any]]:
        """Fetch current price, store snapshot, rebuild candles, return candles."""
        data = self.fetch_prices([mint]).get(mint)
        if not data:
            logger.warning("No price data available for %s", mint)
            return self.get_candles(mint)
        self.record_snapshot(mint, data["price"], data.get("volume_24h", 0.0))
        self.build_candles(mint)
        return self.get_candles(mint)

    def snapshot_batch(self, mints: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch prices for many mints at once and rebuild candles for each."""
        prices = self.fetch_prices(mints)
        results: dict[str, list[dict[str, Any]]] = {}
        for mint in mints:
            data = prices.get(mint)
            if data:
                self.record_snapshot(mint, data["price"], data.get("volume_24h", 0.0))
                self.build_candles(mint)
            results[mint] = self.get_candles(mint)
        return results
