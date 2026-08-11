"""Piranewz Telegram channel reader and sentiment analyzer.

This module listens to messages posted in the Piranewz Telegram channel
(@PIRANEWZ) using the bot token, extracts crypto-related signals/sentiment,
and stores them so the Groq reporter can factor news sentiment into trading
decisions.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from aioud.utils.config import Config
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PiranewzSignal:
    """A normalized signal extracted from a Piranewz channel message."""

    text: str
    symbols: list[str]
    sentiment: str  # bullish / bearish / neutral
    keywords: list[str]
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_id: int | None = None


class PiranewzTelegramReader:
    """Listens to the Piranewz Telegram channel and extracts sentiment."""

    BULLISH_KEYWORDS = [
        "explosé", "exploser", "bull run", "bullish", "pump", "achat", "acheter",
        "breakout", "ath", "moon", "rocket", "hausse", "montée", "surperforme",
        "accumuler", "accumulation", "entrée", "buy", "long", "support", "rebond",
        "rallye", "envolée", "décollage", "décoller", "forte", "opportunité",
    ]
    BEARISH_KEYWORDS = [
        "baisse", "chute", "dump", "bearish", "vente", "vendre", "short", "correction",
        "crash", "chuter", "dumper", "resistance", "rejet", "sell", "liquidation",
        "recul", "repli", "baissier", "inquiétude", "risque",
    ]
    # Words that should count toward sentiment but never be returned as symbols.
    SENTIMENT_ONLY_TOKENS = {
        "PUMP", "DUMP", "MOON", "ATH", "SUPPORT", "RESISTANCE", "BULL", "BEAR",
        "BULLISH", "BEARISH", "BUY", "SELL", "HOLD", "LONG", "SHORT", "ACCUMULER",
        "ACHAT", "VENTE", "TOKEN", "COIN", "CRYPTO", "NFT", "DEFI", "DAO", "WEB3",
        "ALTCOIN", "MEME", "BLOCKCHAIN", "BITCOIN", "ETHEREUM", "SOLANA",
    }
    # Common crypto tickers and top tokens to catch even without $ or #.
    COMMON_TICKERS = {
        "BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "DOT", "AVAX", "LINK", "UNI",
        "AAVE", "LDO", "OP", "ARB", "MATIC", "SUI", "SEI", "TIA", "JUP", "RAY",
        "BONK", "WIF", "PEPE", "SHIB", "FLOKI", "SAND", "MANA", "AXS", "GALA",
        "EGLD", "APT", "NEAR", "FTM", "ALGO", "VET", "ICP", "FIL", "TRX", "ETC",
        "LTC", "BCH", "XLM", "ATOM", "XTZ", "EOS", "ZEC", "DASH", "XMR", "MKR",
        "COMP", "CRV", "SNX", "YFI", "1INCH", "SUSHI", "GRT", "ENJ", "BAT", "ZRX",
        "KNC", "REN", "BNT", "LRC", "OCEAN", "POLY", "STORJ", "ANT", "REP", "BAL",
        "NMR", "UMA", "CVC", "NU", "KEEP", "SKL", "COTI", "CHR", "DODO", "PERP",
        "MIR", "DIA", "REEF", "SFP", "AKRO", "ALPHA", "BAND", "RLC", "OXT", "KAVA",
        "RUNE", "REN", "KSM", "QTUM", "IOST", "ZEN", "WAVES", "ONT", "NANO", "RVN",
        "SC", "BTG", "DGB", "XEM", "LSK", "STEEM", "HIVE", "SBD", "BTS", "XZC",
        "PIVX", "KMD", "ARK", "NAS", "QASH", "PAY", "MCO", "TENX", "OMG", "REQ",
    }

    def __init__(self, config: Config) -> None:
        self.config = config
        self.token = config.piranewz_api_key or ""
        self.channel = getattr(config, "piranewz_channel", "") or "@PIRANEWZ"
        # Extra tickers provided by the user via .env (comma separated).
        extra = getattr(config, "piranewz_extra_tickers", "") or ""
        self.extra_tickers = {t.strip().upper() for t in extra.split(",") if t.strip()}
        self.known_tickers = self.COMMON_TICKERS | self.extra_tickers
        self.application: Application | None = None
        self.signals: list[PiranewzSignal] = []
        self.enabled = bool(self.token)
        if self.enabled:
            try:
                self.application = Application.builder().token(self.token).build()
                self.application.add_handler(
                    MessageHandler(filters.ChatType.CHANNEL, self._on_channel_post)
                )
            except Exception as exc:
                logger.warning("Failed to initialize Piranewz reader: %s", exc)
                self.enabled = False

    async def _on_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle new posts in the Piranewz channel."""
        if not update.channel_post:
            return
        message = update.channel_post
        # Optionally filter by channel username if known.
        channel_name = getattr(message.sender_chat, "username", None) or ""
        if channel_name and not channel_name.lower() == self.channel.lstrip("@").lower():
            return
        text = message.text or message.caption or ""
        if not text:
            return
        signal = self._extract_signal(text, message_id=message.message_id)
        self.signals.append(signal)
        if len(self.signals) > 200:
            self.signals = self.signals[-200:]
        logger.info(
            "Piranewz signal extracted: %s | sentiment=%s | symbols=%s",
            signal.text[:60].replace("\n", " "),
            signal.sentiment,
            ",".join(signal.symbols),
        )

    def _extract_signal(self, text: str, message_id: int | None = None) -> PiranewzSignal:
        """Extract symbols, sentiment and keywords from raw channel text."""
        lower = text.lower()

        upper = text.upper()
        # Strong signal: tickers prefixed with $ or #.
        ticker_pattern = re.compile(r"[\$#]\b([A-Z][A-Z0-9]{1,8})\b")
        symbols = list(dict.fromkeys(ticker_pattern.findall(upper)))

        # Known tickers from our expanded list (with or without prefix).
        known_pattern = re.compile(r"\b(" + "|".join(sorted(self.known_tickers, key=len, reverse=True)) + r")\b")
        for sym in known_pattern.findall(upper):
            if sym not in symbols:
                symbols.append(sym)

        # Generic uppercase 2-6 letter tokens only when the message is clearly about crypto
        # (contains $, #, or crypto keywords). Helps catch new/meme tokens without a hardcoded list.
        crypto_context = bool(ticker_pattern.search(upper) or any(kw in lower for kw in (
            "crypto", "token", "meme", "altcoin", "bitcoin", "ethereum", "solana", "blockchain",
            "bull", "bear", "pump", "dump", "achat", "vente", "buy", "sell", "long", "short",
        )))
        if crypto_context:
            generic_pattern = re.compile(r"\b([A-Z][A-Z0-9]{2,6})\b")
            ignore = {
                # English common
                "THE", "AND", "FOR", "BUT", "NOT", "ARE", "YOU", "ALL", "CAN", "HER", "WAS",
                "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM", "HIS", "HOW", "ITS", "MAY",
                "NEW", "NOW", "OLD", "SEE", "TWO", "WAY", "WHO", "BOY", "DID", "SHE", "HAD",
                "EACH", "EVEN", "HERE", "COME", "MADE", "MOST", "OVER", "SUCH", "TAKE", "THAN",
                "THEM", "WELL", "WERE", "WHAT", "WITH", "YOUR", "THIS", "THAT", "FROM", "THEY",
                "KNOW", "WANT", "BEEN", "GOOD", "MUCH", "SOME", "TIME", "VERY", "WHEN", "WILL",
                "ABOUT", "AFTER", "BACK", "OTHER", "MANY", "THEN", "THEM", "THESE", "COULD",
                # French common
                "OUI", "NON", "SUR", "DANS", "PAR", "POUR", "LES", "DES", "EST", "QUE", "SON",
                "CES", "VOUS", "PLUS", "FAIT", "TOUT", "BIEN", "VOIR", "ALORS", "AVANT", "APRES",
                "ET", "OU", "MAIS", "DONC", "OR", "NI", "CAR", "AUX", "NOS", "MES", "TES", "SES",
                "CEUX", "CELLE", "CELUI", "CETTE", "MONSIEUR", "MADAME", "AVOIR", "ETRE", "FAIRE",
                "ALLER", "VENIR", "VOULOIR", "POUVOIR", "DEVOIR", "PRENDRE", "METTRE", "TENIR",
                "SAVOIR", "VOIR", "DIRE", "PARDON", "MERCI", "BONJOUR", "SALUT", "AUJOURD", "HUI",
                "BON", "BONNE", "MAUVAIS", "TRES", "PEU", "BEAUCOUP", "ASSEZ", "TROP", "MOINS",
                "PLUS", "ENCORE", "DEJA", "TOUJOURS", "JAMAIS", "MAINTENANT", "ALORS", "ENSUITE",
                "VONT", "MOMENT", "LISTE", "FEU", "MARCHE",
                # Crypto/tech false positives
                "HTTPS", "HTTP", "WWW", "HTML", "JSON", "API", "URL", "APP", "NFT", "WEB3",
                "TELEGRAM", "TWITTER", "DISCORD", "YOUTUBE", "REDDIT", "MEDIUM", "GITHUB",
                "TOKEN", "COIN", "CRYPTO", "BLOCKCHAIN", "ALTCOIN", "MEME", "DEFI", "DAO",
            }
            ignore |= self.SENTIMENT_ONLY_TOKENS
            for sym in generic_pattern.findall(upper):
                if sym in ignore or sym in symbols:
                    continue
                symbols.append(sym)

        bullish_hits = [kw for kw in self.BULLISH_KEYWORDS if kw in lower]
        bearish_hits = [kw for kw in self.BEARISH_KEYWORDS if kw in lower]

        if len(bullish_hits) > len(bearish_hits):
            sentiment = "bullish"
        elif len(bearish_hits) > len(bullish_hits):
            sentiment = "bearish"
        else:
            sentiment = "neutral"

        keywords = list(dict.fromkeys(bullish_hits + bearish_hits))

        return PiranewzSignal(
            text=text,
            symbols=symbols,
            sentiment=sentiment,
            keywords=keywords,
            posted_at=datetime.now(timezone.utc),
            message_id=message_id,
        )

    def get_recent_signals(self, hours: int = 24, limit: int = 50) -> list[PiranewzSignal]:
        """Return recent signals within the last N hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = [s for s in self.signals if s.posted_at >= cutoff]
        return recent[-limit:]

    def get_sentiment_summary(self, hours: int = 24) -> dict[str, Any]:
        """Aggregate sentiment from recent signals."""
        recent = self.get_recent_signals(hours=hours)
        if not recent:
            return {"overall": "neutral", "counts": {"bullish": 0, "bearish": 0, "neutral": 0}, "signals": []}
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

    async def start(self) -> None:
        """Start listening to Piranewz channel posts."""
        if not self.enabled or not self.application:
            logger.info("Piranewz reader is disabled (no token).")
            return
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        logger.info("Piranewz Telegram reader started on %s", self.channel)

    async def stop(self) -> None:
        """Stop the reader."""
        if not self.enabled or not self.application:
            return
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        logger.info("Piranewz Telegram reader stopped")
