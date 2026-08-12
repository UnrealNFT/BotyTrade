"""Generate manual trading signals for any token (cross-chain / CEX)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from aioud.data.multi_asset_prices import MultiAssetPriceFeed
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ManualSignal:
    """A manually-executable trading signal for any token."""

    symbol: str
    current_price: float
    buy_limit: float
    tp1: float
    tp2: float
    tp3: float
    sl: float
    confidence: int
    reason: str
    sentiment: str
    source_price: str
    exchange_hint: str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MultiAssetReporter:
    """Uses Groq to generate buy-limit / TP / SL signals for non-Solana tokens."""

    GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

    # Reasonable default levels if the LLM fails to provide numeric values.
    DEFAULT_TP1_PCT = 0.03
    DEFAULT_TP2_PCT = 0.06
    DEFAULT_TP3_PCT = 0.12
    DEFAULT_SL_PCT = 0.05
    DEFAULT_BUY_LIMIT_DISCOUNT_PCT = 0.005  # buy limit 0.5% below current price

    def __init__(self, api_key: str, price_feed: MultiAssetPriceFeed | None = None, model: str = "llama-3.3-70b-versatile") -> None:
        self.api_key = api_key
        self.model = model
        self.price_feed = price_feed or MultiAssetPriceFeed()

    def _build_prompt(self, symbol: str, price: float, sentiment: str, piranewz_texts: list[str]) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        context = "\n".join(f"- {t[:200]}" for t in piranewz_texts[-5:]) or "Aucun contexte Piranewz."
        return (
            f"Tu es un analyste trading crypto. Date/heure: {now}\n"
            f"Génère un signal manuel d'achat CEX pour le token {symbol}.\n"
            f"Prix actuel observé: ${price:.8f}\n"
            f"Sentiment Piranewz détecté: {sentiment}\n\n"
            f"Contexte récent Piranewz:\n{context}\n\n"
            "Instructions:\n"
            "1. Détermine si le setup est intéressant pour un achat manuel sur MEXC/Kraken.\n"
            "2. Propose un buy limit légèrement sous le prix actuel (limit order).\n"
            "3. Propose 3 take-profit (TP1/TP2/TP3) et un stop-loss (SL).\n"
            "4. Donne une confiance 0-100 et une raison concise.\n"
            "5. Si le setup n'est pas favorable, confidence=0 et explique pourquoi.\n\n"
            "Réponds UNIQUEMENT au format JSON suivant (pas de markdown autour):\n"
            "{\n"
            '  "tradeable": true|false,\n'
            '  "buy_limit": float,\n'
            '  "tp1": float,\n'
            '  "tp2": float,\n'
            '  "tp3": float,\n'
            '  "sl": float,\n'
            '  "confidence": 0-100,\n'
            '  "reason": "string",\n'
            '  "exchange": "MEXC|Kraken|Binance"\n'
            "}"
        )

    def _parse_number(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _call_groq(self, symbol: str, price: float, sentiment: str, piranewz_texts: list[str]) -> dict[str, Any]:
        """Call Groq for a manual signal and return parsed JSON."""
        import json
        import re

        if not self.api_key:
            return {}
        prompt = self._build_prompt(symbol, price, sentiment, piranewz_texts)
        try:
            response = requests.post(
                self.GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Tu es un trader crypto spécialisé signaux manuels CEX. "
                                "Tu réponds toujours en JSON valide avec des niveaux d'achat, TP et SL."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 600,
                },
                timeout=(10, 60),
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            logger.warning("Groq manual signal request failed for %s: %s", symbol, exc)
            return {}

        try:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            text = match.group(1) if match else raw.strip()
            return json.loads(text) if text else {}
        except Exception as exc:
            logger.warning("Groq manual signal parse failed for %s: %s", symbol, exc)
            return {}

    def _generate_for_symbol(
        self,
        symbol: str,
        sentiment: str,
        piranewz_texts: list[str],
    ) -> ManualSignal | None:
        symbol = symbol.upper()
        try:
            price_data = self.price_feed.fetch_price(symbol)
            price = price_data.price
        except Exception as exc:
            logger.warning("Cannot fetch price for manual signal %s: %s", symbol, exc)
            return None

        if not self.api_key:
            # Fallback signal with default levels when Groq is unavailable.
            buy_limit = price * (1 - self.DEFAULT_BUY_LIMIT_DISCOUNT_PCT)
            return ManualSignal(
                symbol=symbol,
                current_price=price,
                buy_limit=buy_limit,
                tp1=buy_limit * (1 + self.DEFAULT_TP1_PCT),
                tp2=buy_limit * (1 + self.DEFAULT_TP2_PCT),
                tp3=buy_limit * (1 + self.DEFAULT_TP3_PCT),
                sl=buy_limit * (1 - self.DEFAULT_SL_PCT),
                confidence=50,
                reason=f"Signal fallback basé sur sentiment {sentiment} (Groq API key manquante).",
                sentiment=sentiment,
                source_price=price_data.source,
                exchange_hint="MEXC",
            )

        prompt = self._build_prompt(symbol, price, sentiment, piranewz_texts)
        try:
            response = requests.post(
                self.GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Tu es un trader crypto spécialisé signaux manuels CEX. "
                                "Tu réponds toujours en JSON valide avec des niveaux d'achat, TP et SL."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 600,
                    "response_format": {"type": "json_object"},
                },
                timeout=(10, 60),
            )
            if response.status_code == 400:
                logger.warning("Groq 400 for manual signal %s: %s", symbol, response.text[:500])
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"] or ""
            import json
            import re
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            text = match.group(1) if match else raw.strip()
            data = json.loads(text) if text else {}
        except Exception as exc:
            logger.warning("Groq manual signal parse failed for %s: %s", symbol, exc)
            data = {}

        tradeable = bool(data.get("tradeable", True))
        confidence = min(100, max(0, self._parse_number(data.get("confidence"), 0)))
        if not tradeable or confidence < 40:
            return None

        buy_limit = self._parse_number(data.get("buy_limit"), price * (1 - self.DEFAULT_BUY_LIMIT_DISCOUNT_PCT))
        # Ensure buy limit is below current price and positive.
        if buy_limit <= 0 or buy_limit >= price * 1.01:
            buy_limit = price * (1 - self.DEFAULT_BUY_LIMIT_DISCOUNT_PCT)

        tp1 = self._parse_number(data.get("tp1"), buy_limit * (1 + self.DEFAULT_TP1_PCT))
        tp2 = self._parse_number(data.get("tp2"), buy_limit * (1 + self.DEFAULT_TP2_PCT))
        tp3 = self._parse_number(data.get("tp3"), buy_limit * (1 + self.DEFAULT_TP3_PCT))
        sl = self._parse_number(data.get("sl"), buy_limit * (1 - self.DEFAULT_SL_PCT))

        # Sanity order: SL < buy_limit < TP1 < TP2 < TP3.
        sl = min(sl, buy_limit * 0.99)
        tp1 = max(tp1, buy_limit * 1.01)
        tp2 = max(tp2, tp1 * 1.01)
        tp3 = max(tp3, tp2 * 1.01)

        return ManualSignal(
            symbol=symbol,
            current_price=price,
            buy_limit=buy_limit,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            sl=sl,
            confidence=int(confidence),
            reason=data.get("reason", "Signal IA basé sur sentiment et prix actuel."),
            sentiment=sentiment,
            source_price=price_data.source,
            exchange_hint=data.get("exchange", "MEXC"),
        )

    def generate_signals(
        self,
        symbol_sentiment: dict[str, Any],
        recent_signals: list[dict[str, Any]],
        min_confidence: int = 50,
    ) -> list[ManualSignal]:
        """Generate manual signals for symbols with strong Piranewz sentiment."""
        # Build texts per symbol.
        texts_by_symbol: dict[str, list[str]] = {}
        for sig in recent_signals:
            for sym in sig.get("symbols", []):
                texts_by_symbol.setdefault(sym.upper(), []).append(sig.get("text", ""))

        results: list[ManualSignal] = []
        for symbol, counts in symbol_sentiment.items():
            symbol = symbol.upper()
            if not self.price_feed.is_supported(symbol):
                continue
            bullish = counts.get("bullish", 0)
            bearish = counts.get("bearish", 0)
            if bullish >= bearish and bullish > 0:
                sentiment = "bullish"
            elif bearish > bullish:
                sentiment = "bearish"
            else:
                continue

            signal = self._generate_for_symbol(
                symbol=symbol,
                sentiment=sentiment,
                piranewz_texts=texts_by_symbol.get(symbol, []),
            )
            if signal and signal.confidence >= min_confidence:
                results.append(signal)

        # Sort by confidence desc.
        results.sort(key=lambda s: s.confidence, reverse=True)
        return results
