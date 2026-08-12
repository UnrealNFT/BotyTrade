"""Groq-based market reporter that produces periodic situation reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GroqDecision:
    summary: str
    reasoning: str
    buy_suggestions: list[dict[str, Any]]
    hold_suggestions: list[dict[str, Any]]
    sell_suggestions: list[dict[str, Any]]
    raw_response: str


class GroqReporter:
    """Generates periodic situation reports and buy/hold/sell decisions."""

    GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile") -> None:
        self.api_key = api_key
        self.model = model

    def _build_prompt(
        self,
        balances: dict[str, float],
        open_positions: list[dict[str, Any]],
        watchlist: list[dict[str, Any]],
        recent_evaluations: dict[str, dict[str, Any]],
        trade_history: list[dict[str, Any]],
        news_articles: list[Any] | None = None,
        piranewz_sentiment: dict[str, Any] | None = None,
        timeframe_minutes: int = 10,
    ) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "Tu es un analyste trading crypto spécialisé scalping 1m sur Solana.",
            "Fais un point situation toutes les 10 minutes et recommande des actions.",
            "",
            f"Date/heure: {now}",
            f"Horizon d'analyse: dernières {timeframe_minutes} minutes",
            "",
            "=== Soldes ===",
            f"SOL: {balances.get('sol_balance', 0):.4f}",
            f"USDC: {balances.get('usdc_balance', 0):.2f}",
            f"SOL price USD: {balances.get('sol_price_usd', 0):.2f}",
            "",
            "=== Positions ouvertes ===",
        ]

        news_articles = news_articles or []
        piranewz_sentiment = piranewz_sentiment or {}
        if open_positions:
            for pos in open_positions:
                pnl = pos.get("unrealized_pnl_pct", 0.0)
                lines.append(
                    f"- {pos['symbol']} entry=${pos['entry']:.6f} "
                    f"current=${pos.get('current_price', 0):.6f} "
                    f"PnL={pnl:+.2f}% TP1={pos['tp1']:.6f} SL={pos['sl']:.6f}"
                )
        else:
            lines.append("Aucune position ouverte.")

        lines.extend(["", "=== Watchlist & signaux récents ==="])
        for token in sorted(watchlist, key=lambda x: x.get("score", 0), reverse=True)[:25]:
            mint = token.get("mint", "")
            ev = recent_evaluations.get(mint, {})
            signal = ev.get("signal", "hold")
            score = ev.get("score", 0)
            price = token.get("price", 0)
            mcap = token.get("market_cap", 0)
            vol = token.get("volume_24h", 0)
            liq = token.get("liquidity", 0)
            lines.append(
                f"- {token['symbol']} price=${price:.8f} mcap=${mcap:,.0f} "
                f"liq=${liq:,.0f} vol24=${vol:,.0f} signal={signal} score={score}"
            )

        lines.extend(["", "=== Sentiment marché Piranewz (@PIRANEWZ) ==="])
        overall = piranewz_sentiment.get("overall", "neutral")
        counts = piranewz_sentiment.get("counts", {})
        lines.append(f"Sentiment global: {overall} (bullish={counts.get('bullish', 0)}, bearish={counts.get('bearish', 0)}, neutral={counts.get('neutral', 0)})")
        symbol_sentiment = piranewz_sentiment.get("symbol_sentiment", {})
        if symbol_sentiment:
            lines.append("Sentiment par token (24h):")
            for sym, vals in sorted(symbol_sentiment.items(), key=lambda x: x[1].get("bullish", 0) - x[1].get("bearish", 0), reverse=True)[:15]:
                lines.append(f"  {sym}: bullish={vals.get('bullish', 0)}, bearish={vals.get('bearish', 0)}")
        recent_signals = piranewz_sentiment.get("signals", [])
        if recent_signals:
            lines.append("Messages récents:")
            for s in recent_signals[-5:]:
                lines.append(f"  [{s.get('sentiment', 'neutral')}] {s.get('text', '')[:120]}")

        lines.extend(["", "=== Actualités Piranewz récentes ==="])
        if news_articles:
            for article in news_articles[:10]:
                title = getattr(article, "title", "") or ""
                summary = getattr(article, "summary", "") or ""
                sentiment = getattr(article, "sentiment", "neutral") or "neutral"
                symbols = getattr(article, "symbols", None) or []
                symbols_str = ", ".join(symbols) if symbols else ""
                lines.append(
                    f"- [{sentiment}] {title}"
                    + (f" ({symbols_str})" if symbols_str else "")
                    + (f"\n  {summary[:200]}" if summary else "")
                )
        else:
            lines.append("Aucune actualité récente.")

        lines.extend(["", "=== Historique trades récents ==="])
        if trade_history:
            for t in trade_history[-10:]:
                lines.append(
                    f"- {t['symbol']} {t.get('pnl_usd', 0):+.2f}$ ({t.get('pnl_pct', 0):+.2f}%) "
                    f"reason={t.get('reason', '')}"
                )
        else:
            lines.append("Aucun trade récent.")

        lines.extend(
            [
                "",
                "=== Instructions ===",
                "1. Résume la tendance générale du marché sur les tokens suivis.",
                "2. Prends en compte le sentiment Piranewz: si un token est mentionné avec des mots bull/bear, intègre cela dans ta confiance.",
                "3. IMPORTANT: tu ne peux suggérer des achats UNIQUEMENT pour les tokens listés dans la watchlist ci-dessus (tokens Solana suivis par le bot).",
                "4. Identifie les tokens les plus intéressants pour un achat (momentum, volume, score élevé).", 
                "5. Pour chaque position ouverte, dis si on hold, partiel ou ferme.",
                "6. Reste factuel, court et orienté action.",
                "",
                "Réponds UNIQUEMENT au format JSON suivant (pas de markdown autour) :",
                "{",
                '  "summary": "string",',
                '  "reasoning": "string",',
                '  "buy_suggestions": [{"symbol": "TOKEN", "reason": "...", "confidence": 0-100}],',
                '  "hold_suggestions": [{"symbol": "TOKEN", "reason": "...", "confidence": 0-100}],',
                '  "sell_suggestions": [{"symbol": "TOKEN", "reason": "...", "confidence": 0-100}]',
                "}",
            ]
        )
        return "\n".join(lines)

    def _parse_response(self, text: str) -> GroqDecision:
        import json
        import re

        # Try to extract JSON from markdown code block or raw response.
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            text = match.group(1)
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Groq response is not valid JSON, using fallback parsing")
            data = {}

        return GroqDecision(
            summary=data.get("summary", "Rapport indisponible"),
            reasoning=data.get("reasoning", text[:500]),
            buy_suggestions=data.get("buy_suggestions", []),
            hold_suggestions=data.get("hold_suggestions", []),
            sell_suggestions=data.get("sell_suggestions", []),
            raw_response=text,
        )

    def generate_report(
        self,
        balances: dict[str, float],
        open_positions: list[dict[str, Any]],
        watchlist: list[dict[str, Any]],
        recent_evaluations: dict[str, dict[str, Any]],
        trade_history: list[dict[str, Any]],
        news_articles: list[Any] | None = None,
        piranewz_sentiment: dict[str, Any] | None = None,
        timeframe_minutes: int = 10,
    ) -> GroqDecision:
        """Ask Groq for a periodic trading situation report."""
        if not self.api_key:
            logger.warning("Groq API key missing; skipping LLM report")
            return GroqDecision(
                summary="Groq API key manquante.",
                reasoning="Aucune analyse LLM disponible.",
                buy_suggestions=[],
                hold_suggestions=[],
                sell_suggestions=[],
                raw_response="",
            )

        prompt = self._build_prompt(
            balances=balances,
            open_positions=open_positions,
            watchlist=watchlist,
            recent_evaluations=recent_evaluations,
            trade_history=trade_history,
            news_articles=news_articles,
            piranewz_sentiment=piranewz_sentiment,
            timeframe_minutes=timeframe_minutes,
        )

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
                                "Tu es un trader quantitatif crypto. Tu réponds "
                                "toujours en JSON valide avec des recommandations d'achat, "
                                "de vente ou de maintien. Sois concis et data-driven."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 1200,
                    "response_format": {"type": "json_object"},
                },
                timeout=(10, 60),
            )
            if response.status_code == 400:
                logger.warning(
                    "Groq 400 response: %s | model=%s | prompt_len=%s",
                    response.text[:500],
                    self.model,
                    len(prompt),
                )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"]["content"] or ""
            return self._parse_response(raw)
        except Exception as exc:
            logger.warning("Groq report generation failed: %s", exc)
            return GroqDecision(
                summary=f"Erreur Groq: {exc}",
                reasoning="",
                buy_suggestions=[],
                hold_suggestions=[],
                sell_suggestions=[],
                raw_response="",
            )
