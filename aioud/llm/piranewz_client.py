"""Piranewz client for fetching crypto news relevant to the watchlist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from aioud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class NewsArticle:
    title: str
    summary: str
    source: str
    url: str
    published_at: str
    sentiment: str = "neutral"
    symbols: list[str] | None = None


class PiranewzClient:
    """Fetch news from the Piranewz Trappist API.

    Configure via environment variables:
    - PIRANEWZ_API_URL: base URL of the API (default: http://localhost:8000)
    - PIRANEWZ_API_KEY: optional API key
    """

    def __init__(self, api_url: str = "", api_key: str = "") -> None:
        self.api_url = (api_url or "http://localhost:8000").rstrip("/")
        self.api_key = api_key
        self._session = requests.Session()
        if self.api_key:
            self._session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _get(self, path: str, params: dict[str, Any] | None = None, timeout: int = 10) -> dict[str, Any] | None:
        try:
            response = self._session.get(
                f"{self.api_url}{path}",
                params=params,
                timeout=(5, timeout),
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.debug("Piranewz request failed: %s", exc)
            return None

    def fetch_news(
        self,
        symbols: list[str] | None = None,
        query: str | None = None,
        limit: int = 10,
        hours: int = 24,
    ) -> list[NewsArticle]:
        """Fetch recent news articles optionally filtered by symbols or query."""
        if not self.api_url or self.api_url == "http://localhost:8000" and not self._healthcheck():
            return []

        params: dict[str, Any] = {"limit": limit, "hours": hours}
        if symbols:
            params["symbols"] = ",".join(symbols)
        if query:
            params["q"] = query

        data = self._get("/api/news", params=params)
        if not data:
            return []

        items = data if isinstance(data, list) else data.get("items", data.get("articles", data.get("news", [])))
        articles: list[NewsArticle] = []
        for item in items:
            try:
                article = NewsArticle(
                    title=str(item.get("title", "")),
                    summary=str(item.get("summary", item.get("description", ""))),
                    source=str(item.get("source", "")),
                    url=str(item.get("url", "")),
                    published_at=str(item.get("published_at", item.get("publishedAt", ""))),
                    sentiment=str(item.get("sentiment", "neutral")),
                    symbols=item.get("symbols", item.get("tokens", [])),
                )
                articles.append(article)
            except Exception:
                continue
        return articles

    def fetch_sentiment(self, symbols: list[str]) -> dict[str, str]:
        """Fetch aggregated sentiment for given symbols."""
        if not symbols:
            return {}
        data = self._get("/api/sentiment", params={"symbols": ",".join(symbols)})
        if not data:
            return {}
        result = data if isinstance(data, dict) else {}
        return {str(k): str(v) for k, v in result.items() if isinstance(v, (str, int, float))}

    def _healthcheck(self) -> bool:
        try:
            response = self._session.get(f"{self.api_url}/health", timeout=(2, 5))
            return response.status_code == 200
        except Exception:
            return False
