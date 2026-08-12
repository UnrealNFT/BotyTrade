"""PostgreSQL persistence helpers for Render free durability.

This module mirrors the JSON file operations used by the bot so that state
survives Render free restarts, which wipe the local filesystem.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from aioud.utils.logger import get_logger

logger = get_logger(__name__)


def _get_connection_url() -> str | None:
    """Return Render PostgreSQL URL if present, otherwise None."""
    return os.environ.get("DATABASE_URL")


def _is_postgres_available() -> bool:
    return bool(_get_connection_url())


def _get_db() -> Any:
    """Lazy import psycopg2 so the module can load without it."""
    import psycopg2  # type: ignore[import-not-found]

    url = _get_connection_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(url)


@contextmanager
def _cursor(commit: bool = False) -> Generator[Any, None, None]:
    conn = _get_db()
    cur = conn.cursor()
    try:
        yield cur
        if commit:
            conn.commit()
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Instance lock: ensure only ONE running instance polls Telegram at a time.
# Render keeps the old instance alive for a few seconds during deploys; both
# polling getUpdates with the same token triggers a permanent Conflict crash.
# We use a Postgres advisory lock (visible to ALL instances sharing the DB).
# ---------------------------------------------------------------------------

_INSTANCE_LOCK_KEY = 727272  # arbitrary int64 key for the advisory lock


def acquire_instance_lock() -> Any:
    """Try to acquire the global instance lock.

    Returns an open psycopg2 connection holding a session-level advisory lock
    if this instance is the leader, or None if another instance holds it.
    The lock is released automatically when the connection closes / process dies.
    """
    if not _is_postgres_available():
        return None
    try:
        conn = _get_db()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_INSTANCE_LOCK_KEY,))
        acquired = bool(cur.fetchone()[0])
        cur.close()
        if acquired:
            return conn  # keep the connection open; it holds the lock
        conn.close()
        return None
    except Exception as exc:
        logger.warning("Instance lock failed: %s", exc)
        return None


def init_schema() -> None:
    """Create tables needed by the bot."""
    if not _is_postgres_available():
        return
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                mint TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry_price REAL NOT NULL,
                amount REAL NOT NULL,
                side TEXT NOT NULL,
                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,
                tp3 REAL NOT NULL,
                sl REAL NOT NULL,
                trailing_stop REAL NOT NULL,
                highest_price REAL NOT NULL,
                tp1_hit BOOLEAN NOT NULL,
                tp2_hit BOOLEAN NOT NULL,
                tp3_hit BOOLEAN NOT NULL,
                opened_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_history (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry REAL NOT NULL,
                exit REAL NOT NULL,
                pnl_usd REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                reason TEXT NOT NULL,
                duration_min REAL NOT NULL,
                opened_at TIMESTAMPTZ NOT NULL,
                closed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                mint TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS removed_mints (
                mint TEXT PRIMARY KEY,
                removed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                alert_type TEXT PRIMARY KEY,
                last_alert TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS price_snapshots (
                mint TEXT NOT NULL,
                ts REAL NOT NULL,
                price REAL NOT NULL,
                volume_24h REAL NOT NULL,
                PRIMARY KEY (mint, ts)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS price_candles (
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
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts
            ON price_snapshots (mint, ts)
            """
        )


def load_positions() -> list[dict[str, Any]]:
    if not _is_postgres_available():
        return []
    try:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT mint, symbol, entry_price, amount, side, tp1, tp2, tp3, sl,
                       trailing_stop, highest_price, tp1_hit, tp2_hit, tp3_hit, opened_at
                FROM positions
                """
            )
            rows = cur.fetchall()
            return [
                {
                    "mint": r[0],
                    "symbol": r[1],
                    "entry_price": r[2],
                    "amount": r[3],
                    "side": r[4],
                    "tp1": r[5],
                    "tp2": r[6],
                    "tp3": r[7],
                    "sl": r[8],
                    "trailing_stop": r[9],
                    "highest_price": r[10],
                    "tp1_hit": r[11],
                    "tp2_hit": r[12],
                    "tp3_hit": r[13],
                    "opened_at": r[14].isoformat(),
                }
                for r in rows
            ]
    except Exception as exc:
        logger.warning("Failed to load positions from Postgres: %s", exc)
        return []


def save_positions(positions: list[dict[str, Any]]) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM positions")
            for p in positions:
                cur.execute(
                    """
                    INSERT INTO positions (
                        mint, symbol, entry_price, amount, side, tp1, tp2, tp3, sl,
                        trailing_stop, highest_price, tp1_hit, tp2_hit, tp3_hit, opened_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        p["mint"],
                        p["symbol"],
                        p["entry_price"],
                        p["amount"],
                        p.get("side", "long"),
                        p["tp1"],
                        p["tp2"],
                        p["tp3"],
                        p["sl"],
                        p.get("trailing_stop", p["sl"]),
                        p.get("highest_price", p["entry_price"]),
                        p.get("tp1_hit", False),
                        p.get("tp2_hit", False),
                        p.get("tp3_hit", False),
                        p["opened_at"],
                    ),
                )
    except Exception as exc:
        logger.warning("Failed to save positions to Postgres: %s", exc)


def load_trade_history(limit: int = 500) -> list[dict[str, Any]]:
    if not _is_postgres_available():
        return []
    try:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT symbol, entry, exit, pnl_usd, pnl_pct, reason, duration_min, opened_at
                FROM trade_history
                ORDER BY closed_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "symbol": r[0],
                    "entry": r[1],
                    "exit": r[2],
                    "pnl_usd": r[3],
                    "pnl_pct": r[4],
                    "reason": r[5],
                    "duration_min": r[6],
                    "opened_at": r[7].isoformat(),
                }
                for r in reversed(rows)
            ]
    except Exception as exc:
        logger.warning("Failed to load trade history from Postgres: %s", exc)
        return []


def add_trade(trade: dict[str, Any]) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO trade_history (
                    symbol, entry, exit, pnl_usd, pnl_pct, reason, duration_min, opened_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    trade["symbol"],
                    trade["entry"],
                    trade["exit"],
                    trade["pnl_usd"],
                    trade["pnl_pct"],
                    trade["reason"],
                    trade["duration_min"],
                    trade["opened_at"],
                ),
            )
    except Exception as exc:
        logger.warning("Failed to add trade to Postgres: %s", exc)


def load_watchlist() -> list[dict[str, Any]]:
    if not _is_postgres_available():
        return []
    try:
        with _cursor() as cur:
            cur.execute("SELECT data FROM watchlist ORDER BY updated_at DESC")
            return [r[0] for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("Failed to load watchlist from Postgres: %s", exc)
        return []


def save_watchlist(pairs: list[dict[str, Any]]) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM watchlist")
            now = datetime.now(timezone.utc).isoformat()
            for p in pairs:
                cur.execute(
                    "INSERT INTO watchlist (mint, data, updated_at) VALUES (%s, %s, %s)",
                    (p.get("mint"), json.dumps(p), now),
                )
    except Exception as exc:
        logger.warning("Failed to save watchlist to Postgres: %s", exc)


def remove_from_watchlist(mint: str) -> bool:
    if not _is_postgres_available():
        return False
    try:
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM watchlist WHERE mint = %s", (mint,))
            return cur.rowcount > 0
    except Exception as exc:
        logger.warning("Failed to remove watchlist item from Postgres: %s", exc)
        return False


def load_removed_mints() -> set[str]:
    if not _is_postgres_available():
        return set()
    try:
        with _cursor() as cur:
            cur.execute("SELECT mint FROM removed_mints")
            return {r[0] for r in cur.fetchall()}
    except Exception as exc:
        logger.warning("Failed to load removed mints from Postgres: %s", exc)
        return set()


def save_removed_mints(mints: set[str]) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM removed_mints")
            for mint in mints:
                cur.execute(
                    "INSERT INTO removed_mints (mint) VALUES (%s)",
                    (mint,),
                )
    except Exception as exc:
        logger.warning("Failed to save removed mints to Postgres: %s", exc)


def load_alert_cooldowns() -> dict[str, datetime]:
    if not _is_postgres_available():
        return {}
    try:
        with _cursor() as cur:
            cur.execute("SELECT alert_type, last_alert FROM alert_cooldowns")
            return {r[0]: r[1] for r in cur.fetchall()}
    except Exception as exc:
        logger.warning("Failed to load alert cooldowns from Postgres: %s", exc)
        return {}


def save_alert_cooldown(alert_type: str, last_alert: datetime) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO alert_cooldowns (alert_type, last_alert)
                VALUES (%s, %s)
                ON CONFLICT (alert_type) DO UPDATE SET last_alert = EXCLUDED.last_alert
                """,
                (alert_type, last_alert),
            )
    except Exception as exc:
        logger.warning("Failed to save alert cooldown to Postgres: %s", exc)


def record_snapshot(mint: str, ts: float, price: float, volume_24h: float) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO price_snapshots (mint, ts, price, volume_24h)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (mint, ts) DO UPDATE SET price = EXCLUDED.price, volume_24h = EXCLUDED.volume_24h
                """,
                (mint, ts, price, volume_24h),
            )
    except Exception as exc:
        logger.warning("Failed to record snapshot in Postgres: %s", exc)


def get_snapshots(mint: str) -> list[tuple[float, float, float]]:
    if not _is_postgres_available():
        return []
    try:
        with _cursor() as cur:
            cur.execute(
                "SELECT ts, price, volume_24h FROM price_snapshots WHERE mint = %s ORDER BY ts",
                (mint,),
            )
            return cur.fetchall()
    except Exception as exc:
        logger.warning("Failed to get snapshots from Postgres: %s", exc)
        return []


def save_candles(mint: str, candles: list[dict[str, Any]]) -> None:
    if not _is_postgres_available():
        return
    try:
        with _cursor(commit=True) as cur:
            for c in candles:
                cur.execute(
                    """
                    INSERT INTO price_candles (
                        mint, timestamp, open, high, low, close, volume
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (mint, timestamp) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume
                    """,
                    (mint, c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"]),
                )
    except Exception as exc:
        logger.warning("Failed to save candles to Postgres: %s", exc)


def get_candles(mint: str, limit: int = 100) -> list[dict[str, Any]]:
    if not _is_postgres_available():
        return []
    try:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM price_candles
                WHERE mint = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (mint, limit),
            )
            rows = cur.fetchall()
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
    except Exception as exc:
        logger.warning("Failed to get candles from Postgres: %s", exc)
        return []


def prune_snapshots(max_age_days: int = 3) -> None:
    if not _is_postgres_available():
        return
    try:
        cutoff = time.time() - (max_age_days * 24 * 3600)
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM price_snapshots WHERE ts < %s", (cutoff,))
            cur.execute("DELETE FROM price_candles WHERE timestamp < %s", (cutoff,))
    except Exception as exc:
        logger.warning("Failed to prune old snapshots: %s", exc)
