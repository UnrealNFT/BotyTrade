"""Multi-channel Telegram discovery scraper.

Scrapes the public web pages (t.me/s/<channel>) of several crypto signal
channels — e.g. SOL TRENDING, Detective Shitcoin — to surface new token
mentions, exactly like the Piranewz scraper does for news sentiment.

No bot token required: these are public channel pages.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TokenMention:
    """A token mentioned in a discovery channel."""

    symbol: str
    channel: str
    text: str
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    count: int = 1  # how many times this symbol was mentioned recently


class DiscoveryScraper:
    """Scrape multiple Telegram channels for new token tickers."""

    def __init__(self, channels: list[str]) -> None:
        # Normalise handles: strip @, drop empties.
        self.channels = [c.lstrip("@").strip() for c in channels if c.strip()]
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
        self._mentions: dict[str, TokenMention] = {}  # symbol -> mention
        self._last_fetch: float = 0.0
        self._cache_ttl = 180.0  # refetch at most every 3 minutes

    def _html_to_text(self, raw: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", raw)
        text = re.sub(r"<[^>]+>", "", text)
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
            text = text.replace(a, b)
        return text.strip()

    # Common English / connector words that look like tickers after uppercase.
    _STOPWORDS = {
        "THE", "AND", "FOR", "BUY", "SELL", "NEW", "HOT", "TOP", "DEX", "SOL",
        "SOLANA", "PUMP", "BUYER", "HOLDER", "POSITION", "MARKET", "CAP", "TX",
        "SCREENER", "GMGN", "TRENDING", "ALERT", "CALL", "GEM", "MOON",
    }

    def _extract_tickers(self, text: str) -> list[str]:
        """Pull token symbols from a discovery post.

        Handles the formats used by Solana trending channels:
        - '⏺ | Gunicorn / GUNICORN ...'   -> GUNICORN  (name / SYMBOL)
        - '$BONK' / '#WIF'                 -> BONK, WIF  (prefixed)
        - '... / ZEREBRO ...'              -> ZEREBRO   (after a slash)
        """
        upper = text.upper()
        tickers: list[str] = []

        # 1) '$TICKER' / '#TICKER' (explicit crypto prefix).
        tickers += re.findall(r"[\$#]([A-Z][A-Z0-9]{1,9})\b", upper)

        # 2) 'name / SYMBOL' — the dominant format on SOLTRENDING-style feeds.
        tickers += re.findall(r"/\s*([A-Z][A-Z0-9]{2,9})\b", upper)

        # De-dup, drop stopwords and pure numbers, preserve order.
        out: list[str] = []
        for t in tickers:
            if t in self._STOPWORDS or t.isdigit() or len(t) < 2:
                continue
            if t not in out:
                out.append(t)
        return out

    def refresh(self, force: bool = False) -> int:
        """Fetch all channels and record new token mentions. Returns new count."""
        now = time.time()
        if not force and now - self._last_fetch < self._cache_ttl:
            return 0
        self._last_fetch = now
        new_count = 0
        for channel in self.channels:
            url = f"https://t.me/s/{channel}"
            try:
                resp = self._session.get(url, timeout=(5, 12))
                resp.raise_for_status()
            except Exception as exc:
                logger.debug("Discovery scrape failed %s: %s", url, exc)
                continue
            blocks = re.findall(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                resp.text,
                flags=re.DOTALL,
            )
            for raw_html in blocks[-15:]:  # last ~15 posts per channel
                text = self._html_to_text(raw_html)
                if len(text) < 5:
                    continue
                for sym in self._extract_tickers(text):
                    existing = self._mentions.get(sym)
                    if existing:
                        existing.count += 1
                        existing.posted_at = datetime.now(timezone.utc)
                    else:
                        self._mentions[sym] = TokenMention(
                            symbol=sym, channel=channel, text=text[:200]
                        )
                        new_count += 1
        # Keep memory bounded.
        if len(self._mentions) > 200:
            # Drop oldest mentions.
            ordered = sorted(self._mentions.values(), key=lambda m: m.posted_at)
            for m in ordered[: len(self._mentions) - 200]:
                self._mentions.pop(m.symbol, None)
        if new_count:
            logger.info("Discovery: %s new tickers from %s channels", new_count, len(self.channels))
        return new_count

    def get_hot_tickers(self, min_mentions: int = 1, limit: int = 15) -> list[TokenMention]:
        """Return the most-mentioned tickers across all discovery channels."""
        self.refresh()
        mentions = [m for m in self._mentions.values() if m.count >= min_mentions]
        mentions.sort(key=lambda m: (m.count, m.posted_at), reverse=True)
        return mentions[:limit]

    def get_summary(self) -> dict[str, Any]:
        """Dashboard-friendly summary of discovery activity."""
        self.refresh()
        hot = self.get_hot_tickers()
        return {
            "channels": self.channels,
            "tracked_tickers": len(self._mentions),
            "hot": [
                {"symbol": m.symbol, "channel": m.channel, "mentions": m.count}
                for m in hot[:10]
            ],
        }
