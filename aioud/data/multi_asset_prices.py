"""Multi-source price fetcher for any crypto symbol (Solana + cross-chain)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from aioud.utils.logger import get_logger

logger = get_logger(__name__)

# Minimal mapping for the most traded symbols on each API.
# Symbols are upper-case tickers (BTC, ETH, ADA, DOGE, FLOKI, ALGO, CSPR...).
SYMBOL_TO_COINGECKO_ID: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "DOT": "polkadot",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "AAVE": "aave",
    "LDO": "lider-board",
    "OP": "optimism",
    "ARB": "arbitrum",
    "MATIC": "matic-network",
    "SUI": "sui",
    "SEI": "sei-network",
    "TIA": "celestia",
    "JUP": "jupiter-exchange-solana",
    "RAY": "raydium",
    "BONK": "bonk",
    "WIF": "dogwifcoin",
    "PEPE": "pepe",
    "SHIB": "shiba-inu",
    "FLOKI": "floki",
    "SAND": "the-sandbox",
    "MANA": "decentraland",
    "AXS": "axie-infinity",
    "GALA": "gala",
    "EGLD": "elrond-erd-2",
    "APT": "aptos",
    "NEAR": "near",
    "FTM": "fantom",
    "ALGO": "algorand",
    "VET": "vechain",
    "ICP": "internet-computer",
    "FIL": "filecoin",
    "TRX": "tron",
    "ETC": "ethereum-classic",
    "LTC": "litecoin",
    "BCH": "bitcoin-cash",
    "XLM": "stellar",
    "ATOM": "cosmos",
    "XTZ": "tezos",
    "EOS": "eos",
    "ZEC": "zcash",
    "DASH": "dash",
    "XMR": "monero",
    "MKR": "maker",
    "COMP": "compound-governance-token",
    "CRV": "curve-dao-token",
    "SNX": "havven",
    "YFI": "yearn-finance",
    "1INCH": "1inch",
    "SUSHI": "sushi",
    "GRT": "the-graph",
    "ENJ": "enjincoin",
    "BAT": "basic-attention-token",
    "ZRX": "0x",
    "KNC": "kyber-network-crystal",
    "LRC": "loopring",
    "OCEAN": "ocean-protocol",
    "KAVA": "kava",
    "RUNE": "thorchain",
    "KSM": "kusama",
    "QTUM": "qtum",
    "WAVES": "waves",
}

# Kraken uses XBT for BTC, XDG for DOGE, etc.
SYMBOL_TO_KRAKEN_PAIR: dict[str, str] = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "ADA": "ADAUSD",
    "DOGE": "XDGUSD",
    "XRP": "XRPUSD",
    "DOT": "DOTUSD",
    "AVAX": "AVAXUSD",
    "LINK": "LINKUSD",
    "UNI": "UNIUSD",
    "AAVE": "AAVEUSD",
    "OP": "OPUSD",
    "ARB": "ARBUSD",
    "MATIC": "MATICUSD",
    "APT": "APTUSD",
    "NEAR": "NEARUSD",
    "FTM": "FTMUSD",
    "ALGO": "ALGOUSD",
    "VET": "VETUSD",
    "ICP": "ICPUSD",
    "FIL": "FILUSD",
    "TRX": "TRXUSD",
    "ETC": "ETCUSD",
    "LTC": "LTCUSD",
    "BCH": "BCHUSD",
    "XLM": "XLMUSD",
    "ATOM": "ATOMUSD",
    "XTZ": "XTZUSD",
    "EOS": "EOSUSD",
    "DASH": "DASHUSD",
    "XMR": "XMRUSD",
    "MKR": "MKRUSD",
    "COMP": "COMPUSD",
    "CRV": "CRVUSD",
    "SNX": "SNXUSD",
    "YFI": "YFIUSD",
    "1INCH": "1INCHUSD",
    "SUSHI": "SUSHIUSD",
    "GRT": "GRTUSD",
    "BAT": "BATUSD",
    "ZRX": "ZRXUSD",
    "KNC": "KNCUSD",
    "LRC": "LRCUSD",
    "OCEAN": "OCEANUSD",
    "KAVA": "KAVAUSD",
    "RUNE": "RUNEUSD",
    "KSM": "KSMUSD",
    "QTUM": "QTUMUSD",
    "WAVES": "WAVESUSD",
}

# MEXC uses BTCUSDT, ETHUSDT, etc.
SYMBOL_TO_MEXC_SYMBOL: dict[str, str] = {s: f"{s}USDT" for s in SYMBOL_TO_COINGECKO_ID}

# CoinMarketCap uses the plain ticker.


@dataclass
class MultiAssetPrice:
    """Normalized price snapshot from any supported source."""

    symbol: str
    price: float
    volume_24h: float
    source: str
    timestamp: float
    extra: dict[str, Any] = field(default_factory=dict)


class MultiAssetPriceFeed:
    """Fetch current prices for any symbol from multiple redundant sources."""

    def __init__(
        self,
        coingecko_api_key: str | None = None,
        cmc_api_key: str | None = None,
        cache_ttl_seconds: float = 15.0,
    ) -> None:
        self.coingecko_api_key = coingecko_api_key
        self.cmc_api_key = cmc_api_key
        self._cache: dict[str, tuple[float, MultiAssetPrice]] = {}
        self._cache_ttl = cache_ttl_seconds

    def _cached(self, symbol: str, fetcher) -> MultiAssetPrice | None:
        now = time.time()
        entry = self._cache.get(symbol)
        if entry and now - entry[0] < self._cache_ttl:
            return entry[1]
        price = fetcher(symbol)
        if price and price.price > 0:
            self._cache[symbol] = (now, price)
        return price

    def _fetch_coingecko(self, symbol: str) -> MultiAssetPrice | None:
        """Free CoinGecko simple price endpoint (no key required, rate limited)."""
        coin_id = SYMBOL_TO_COINGECKO_ID.get(symbol.upper())
        if not coin_id:
            return None
        try:
            headers = {}
            if self.coingecko_api_key:
                headers["x-cg-demo-api-key"] = self.coingecko_api_key
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": coin_id,
                    "vs_currencies": "usd",
                    "include_24hr_vol": "true",
                    "include_24hr_change": "true",
                },
                headers=headers,
                timeout=(5, 10),
            )
            response.raise_for_status()
            data = response.json()
            item = data.get(coin_id, {})
            price = item.get("usd")
            if price is None:
                return None
            return MultiAssetPrice(
                symbol=symbol.upper(),
                price=float(price),
                volume_24h=float(item.get("usd_24h_vol") or 0),
                source="coingecko",
                timestamp=time.time(),
                extra={"change_24h_pct": float(item.get("usd_24h_change") or 0)},
            )
        except Exception as exc:
            logger.debug("CoinGecko price failed for %s: %s", symbol, exc)
            return None

    def _fetch_kraken(self, symbol: str) -> MultiAssetPrice | None:
        """Kraken public ticker endpoint."""
        pair = SYMBOL_TO_KRAKEN_PAIR.get(symbol.upper())
        if not pair:
            return None
        try:
            response = requests.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": pair},
                timeout=(5, 10),
            )
            response.raise_for_status()
            data = response.json()
            if data.get("error"):
                return None
            ticker = list(data.get("result", {}).values())[0]
            # c[0] = last closed price, v[1] = 24h volume in base asset
            price = float(ticker["c"][0])
            volume = float(ticker["v"][1])
            return MultiAssetPrice(
                symbol=symbol.upper(),
                price=price,
                volume_24h=volume * price,
                source="kraken",
                timestamp=time.time(),
            )
        except Exception as exc:
            logger.debug("Kraken price failed for %s: %s", symbol, exc)
            return None

    def _fetch_mexc(self, symbol: str) -> MultiAssetPrice | None:
        """MEXC 24hr ticker endpoint."""
        mexc_symbol = SYMBOL_TO_MEXC_SYMBOL.get(symbol.upper())
        if not mexc_symbol:
            return None
        try:
            response = requests.get(
                "https://api.mexc.com/api/v3/ticker/24hr",
                params={"symbol": mexc_symbol},
                timeout=(5, 10),
            )
            response.raise_for_status()
            data = response.json()
            price = float(data.get("lastPrice") or 0)
            volume = float(data.get("volume") or 0)
            if price <= 0:
                return None
            return MultiAssetPrice(
                symbol=symbol.upper(),
                price=price,
                volume_24h=volume * price,
                source="mexc",
                timestamp=time.time(),
                extra={"change_24h_pct": float(data.get("priceChangePercent") or 0)},
            )
        except Exception as exc:
            logger.debug("MEXC price failed for %s: %s", symbol, exc)
            return None

    def _fetch_cmc(self, symbol: str) -> MultiAssetPrice | None:
        """CoinMarketCap pro quotes endpoint (requires API key)."""
        if not self.cmc_api_key:
            return None
        try:
            response = requests.get(
                "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                params={"symbol": symbol.upper(), "convert": "USD"},
                headers={"X-CMC_PRO_API_KEY": self.cmc_api_key},
                timeout=(5, 10),
            )
            response.raise_for_status()
            data = response.json()
            quote = data.get("data", {}).get(symbol.upper(), {}).get("quote", {}).get("USD", {})
            price = quote.get("price")
            if price is None:
                return None
            return MultiAssetPrice(
                symbol=symbol.upper(),
                price=float(price),
                volume_24h=float(quote.get("volume_24h") or 0),
                source="cmc",
                timestamp=time.time(),
                extra={"change_24h_pct": float(quote.get("percent_change_24h") or 0)},
            )
        except Exception as exc:
            logger.debug("CMC price failed for %s: %s", symbol, exc)
            return None

    def fetch_price(self, symbol: str) -> MultiAssetPrice:
        """Return the best available price, trying sources in order."""
        symbol = symbol.upper()

        # Try CoinGecko first (free, broad coverage).
        price = self._cached(symbol, self._fetch_coingecko)
        if price:
            return price

        # Then Kraken (reliable, no key).
        price = self._cached(symbol, self._fetch_kraken)
        if price:
            return price

        # Then MEXC.
        price = self._cached(symbol, self._fetch_mexc)
        if price:
            return price

        # Finally CMC if key provided.
        price = self._cached(symbol, self._fetch_cmc)
        if price:
            return price

        raise ValueError(f"No price source succeeded for {symbol}")

    def fetch_prices(self, symbols: list[str]) -> dict[str, MultiAssetPrice]:
        """Best-effort batch fetch; ignores failures for individual symbols."""
        results: dict[str, MultiAssetPrice] = {}
        for symbol in symbols:
            try:
                results[symbol.upper()] = self.fetch_price(symbol)
            except Exception as exc:
                logger.warning("Price fetch failed for %s: %s", symbol, exc)
        return results

    def is_supported(self, symbol: str) -> bool:
        """Check if we have at least one source mapping for a symbol."""
        symbol = symbol.upper()
        return bool(
            SYMBOL_TO_COINGECKO_ID.get(symbol)
            or SYMBOL_TO_KRAKEN_PAIR.get(symbol)
            or SYMBOL_TO_MEXC_SYMBOL.get(symbol)
        )

    def add_symbol_mapping(self, symbol: str, coingecko_id: str | None = None, kraken_pair: str | None = None, mexc_symbol: str | None = None) -> None:
        """Dynamically add a symbol mapping at runtime."""
        symbol = symbol.upper()
        if coingecko_id:
            SYMBOL_TO_COINGECKO_ID[symbol] = coingecko_id
        if kraken_pair:
            SYMBOL_TO_KRAKEN_PAIR[symbol] = kraken_pair
        if mexc_symbol:
            SYMBOL_TO_MEXC_SYMBOL[symbol] = mexc_symbol
