"""TrappistAI backend price connector.

Calls the user's own TrappistAI backend to fetch prices instead of relying
solely on DexScreener. This avoids the shared-IP rate limits on Render free
and lets the bot run 24/7.
"""

from typing import Any

import requests

from aioud.utils.logger import get_logger

logger = get_logger(__name__)


class TrappistPriceFeed:
    """Fetch crypto prices from a TrappistAI backend instance.

    Expected endpoint:
        POST /api/prices/batch
        Body: {"mints": ["So1111...", "EPjFWdd5...", "mint pumpfun..."]}
        Response: {
            "So1111...": {"price": 76.15, "volume_24h": 64511099, "market_cap": 0},
            ...
        }
    """

    def __init__(self, api_url: str, api_key: str | None = None, timeout: int = 15) -> None:
        self.api_url = (api_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._disabled = False
        self._session = requests.Session()
        if self.api_key:
            self._session.headers["Authorization"] = f"Bearer {self.api_key}"
        self._session.headers["Content-Type"] = "application/json"

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.api_url or self._disabled:
            return None
        try:
            response = self._session.post(
                f"{self.api_url}{path}",
                json=payload,
                timeout=(5, self.timeout),
            )
            if response.status_code == 404:
                logger.warning("TrappistAI endpoint %s not found; disabling TrappistAI", path)
                self._disabled = True
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("TrappistAI price request failed: %s", exc)
            return None

    def fetch_prices(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch prices for many Solana mints at once.

        Fallback: if /api/prices/batch is not implemented yet on the backend,
        silently return {} so DexScreener takes over.
        """
        if not mints or not self.api_url:
            return {}
        data = self._post("/api/prices/batch", {"mints": mints})
        if data is None:
            # Endpoint missing or backend error -> let DexScreener handle it.
            return {}
        if not isinstance(data, dict):
            return {}
        results: dict[str, dict[str, Any]] = {}
        for mint, info in data.items():
            if not isinstance(info, dict):
                continue
            price = info.get("price") or info.get("usd")
            if price is None:
                continue
            results[mint] = {
                "price": float(price),
                "volume_24h": float(info.get("volume_24h") or info.get("volume") or 0),
                "liquidity_usd": float(info.get("liquidity_usd") or info.get("liquidity") or 0),
                "market_cap": float(info.get("market_cap") or info.get("market_cap_usd") or 0),
                "source": "trappist",
            }
        return results

    def fetch_price(self, mint: str) -> dict[str, Any]:
        """Fetch price for a single Solana mint."""
        data = self.fetch_prices([mint]).get(mint)
        if not data:
            raise ValueError(f"No price from TrappistAI for {mint}")
        return data
