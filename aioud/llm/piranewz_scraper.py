"""Piranewz scraper: reads the public Telegram channel page t.me/s/<channel>.

No bot token, no getUpdates conflict — the channel page is public HTML.
Extracts text posts, detects tickers + sentiment, and exposes the same
interface as PiranewzTelegramReader so the engine can use either backend.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from aioud.utils.config import Config
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScrapedSignal:
    text: str
    symbols: list[str] = field(default_factory=list)
    sentiment: str = "neutral"
    keywords: list[str] = field(default_factory=list)
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PiranewzScraper:
    """Scrape the public Telegram channel web page for news/sentiment."""

    BULLISH_KEYWORDS = [
        "bull", "bullish", "pump", "moon", "breakout", "surge", "rally", "ath",
        "achat", "buy", "long", "hausse", "haussier", "explose", "record",
        "partnership", "listing", "adoption", "upgrade", "integration",
    ]
    BEARISH_KEYWORDS = [
        "bear", "bearish", "dump", "crash", "dump", "plunge", "selloff", "fud",
        "vente", "sell", "short", "baisse", "baissier", "chute", "effondre",
        "hack", "exploit", "rug", "scam", "ban", "lawsuit", "delisting",
    ]

    COMMON_TICKERS = {
        "BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "DOT", "AVAX", "LINK", "UNI",
        "AAVE", "LDO", "OP", "ARB", "MATIC", "SUI", "SEI", "TIA", "JUP", "RAY",
        "BONK", "WIF", "PEPE", "SHIB", "FLOKI", "SAND", "MANA", "AXS", "GALA",
        "EGLD", "APT", "NEAR", "FTM", "ALGO", "VET", "ICP", "FIL", "TRX", "ETC",
        "LTC", "BCH", "XLM", "ATOM", "XTZ", "EOS", "ZEC", "DASH", "XMR", "MKR",
        "COMP", "CRV", "SNX", "YFI", "SUSHI", "GRT", "ENJ", "BAT", "ZRX",
        "KNC", "OCEAN", "STORJ", "BAL", "NMR", "UMA", "CVC", "COTI", "PERP",
        "BAND", "RLC", "OXT", "KAVA", "RUNE", "KSM", "QTUM", "ZEN", "WAVES",
        "ONT", "RVN", "SC", "DGB", "XEM", "LSK", "HIVE", "PIVX", "KMD", "ARK",
        "PAY", "OMG", "REQ", "RENDER", "INJ", "TIA", "PYTH", "JTO", "W", "HNT",
        "MOBILE", "BOME", "SLERF", "MEW", "POPCAT", "GIGA", "PONKE", "MYRO",
    }

    def __init__(self, config: Config) -> None:
        self.config = config
        # Channel handle from PIRANEWZ_CHANNEL (@PIRANEWZ -> PIRANEWZ)
        raw = getattr(config, "piranewz_channel", "") or "@PIRANEWZ"
        self.channel = raw.lstrip("@").strip() or "PIRANEWZ"
        self.url = f"https://t.me/s/{self.channel}"
        extra = getattr(config, "piranewz_extra_tickers", "") or ""
        self.known_tickers = self.COMMON_TICKERS | {
            t.strip().upper() for t in extra.split(",") if t.strip()
        }
        self.signals: list[ScrapedSignal] = []
        self._last_fetch: float = 0.0
        self._cache_ttl = 300.0  # refetch at most every 5 minutes
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )

    # ------------------------------------------------------------------ fetch

    def refresh(self, force: bool = False) -> int:
        """Fetch the public channel page and extract signals. Returns # of new signals."""
        now = time.time()
        if not force and now - self._last_fetch < self._cache_ttl:
            return 0
        self._last_fetch = now
        try:
            resp = self._session.get(self.url, timeout=(5, 15))
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Piranewz scrape failed (%s): %s", self.url, exc)
            return 0

        posts = self._extract_posts(resp.text)
        new_count = 0
        existing_texts = {s.text[:80] for s in self.signals}
        for text, posted_at in posts:
            if text[:80] in existing_texts:
                continue
            signal = self._extract_signal(text, posted_at)
            self.signals.append(signal)
            existing_texts.add(text[:80])
            new_count += 1
        # Keep memory bounded.
        if len(self.signals) > 300:
            self.signals = self.signals[-300:]
        if new_count:
            logger.info("Piranewz: %s new posts scraped from @%s", new_count, self.channel)
        return new_count

    def _extract_posts(self, html: str) -> list[tuple[str, datetime]]:
        """Extract (text, datetime) pairs from the t.me/s/ public page HTML."""
        posts: list[tuple[str, datetime]] = []
        # Each message block has a .tgme_widget_message_text and a <time datetime=...>
        blocks = re.findall(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>.*?'
            r'<time[^>]*datetime="([^"]+)"',
            html,
            flags=re.DOTALL,
        )
        for raw_html, dt_str in blocks:
            text = self._html_to_text(raw_html)
            if not text or len(text) < 15:
                continue
            try:
                posted_at = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            except Exception:
                posted_at = datetime.now(timezone.utc)
            posts.append((text, posted_at))
        return posts

    @staticmethod
    def _html_to_text(raw: str) -> str:
        """Strip HTML tags from a message block."""
        text = re.sub(r"<br\s*/?>", "\n", raw)
        text = re.sub(r"<[^>]+>", "", text)
        # Unescape common entities.
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
            text = text.replace(a, b)
        return text.strip()

    # --------------------------------------------------------------- analysis

    def _extract_signal(self, text: str, posted_at: datetime) -> ScrapedSignal:
        """Detect tickers + sentiment in a post."""
        lower = text.lower()
        upper = text.upper()

        symbols: list[str] = list(dict.fromkeys(
            re.findall(r"[\$#]([A-Z][A-Z0-9]{1,8})\b", upper)
        ))
        known_pattern = re.compile(
            r"\b(" + "|".join(sorted(self.known_tickers, key=len, reverse=True)) + r")\b"
        )
        for sym in known_pattern.findall(upper):
            if sym not in symbols:
                symbols.append(sym)

        bullish_hits = [kw for kw in self.BULLISH_KEYWORDS if kw in lower]
        bearish_hits = [kw for kw in self.BEARISH_KEYWORDS if kw in lower]

        if len(bullish_hits) > len(bearish_hits):
            sentiment = "bullish"
        elif len(bearish_hits) > len(bullish_hits):
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        return ScrapedSignal(
            text=text,
            symbols=symbols,
            sentiment=sentiment,
            keywords=list(dict.fromkeys(bullish_hits + bearish_hits)),
            posted_at=posted_at,
        )

    # ----------------------------------------------------------------- query

    def get_recent_signals(self, hours: int = 24, limit: int = 50) -> list[ScrapedSignal]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = [s for s in self.signals if s.posted_at >= cutoff]
        return recent[-limit:]

    def get_sentiment_summary(self, hours: int = 24) -> dict[str, Any]:
        """Same output shape as PiranewzTelegramReader.get_sentiment_summary."""
        self.refresh()
        recent = self.get_recent_signals(hours=hours)
        if not recent:
            return {
                "overall": "neutral",
                "counts": {"bullish": 0, "bearish": 0, "neutral": 0},
                "symbol_sentiment": {},
                "signals": [],
            }
        counts = {"bullish": 0, "bearish": 0, "neutral": 0}
        symbol_sentiment: dict[str, dict[str, int]] = {}
        for s in recent:
            counts[s.sentiment] += 1
            for sym in s.symbols:
                symbol_sentiment.setdefault(sym, {"bullish": 0, "bearish": 0, "neutral": 0})
                symbol_sentiment[sym][s.sentiment] += 1

        if counts["bullish"] > counts["bearish"] * 1.2:
            overall = "bullish"
        elif counts["bearish"] > counts["bullish"] * 1.2:
            overall = "bearish"
        else:
            overall = "neutral"

        return {
            "overall": overall,
            "counts": counts,
            "symbol_sentiment": symbol_sentiment,
            "signals": [
                {
                    "text": s.text,
                    "symbols": s.symbols,
                    "sentiment": s.sentiment,
                    "keywords": s.keywords,
                    "posted_at": s.posted_at.isoformat(),
                }
                for s in recent[-10:]
            ],
        }
