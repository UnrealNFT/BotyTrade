"""Multi-asset scanner for Solana tokens via DexScreener."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from aioud.utils.logger import get_logger

logger = get_logger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com"

DEFAULT_BLUE_CHIPS = [
    ("SOL", "So11111111111111111111111111111111111111112"),
    ("WBTC", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmLJ"),
    ("WETH", "7vfCXTUXx5WJV5JMG6DAqz3gWC4jEUDnvka4vvtL9aKR"),
    ("JUP", "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"),
    ("RAY", "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"),
    ("BONK", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"),
    ("WIF", "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"),
]

# DexScreener discovery endpoints used to find new tokens.
DISCOVERY_ENDPOINTS = [
    "/token-boosts/top/v1",
    "/token-boosts/latest/v1",
    "/token-profiles/latest/v1",
]


@dataclass
class TokenPair:
    """Normalized pair representation."""

    symbol: str
    mint: str
    quote_symbol: str
    quote_mint: str
    price_usd: float
    liquidity_usd: float
    volume_24h: float
    market_cap: float
    dex_id: str
    pair_address: str
    # Discovery metadata
    source_endpoints: list[str] = field(default_factory=list)
    profile_updated_at: datetime | None = None
    boost_amount: float = 0.0
    total_boost_amount: float = 0.0
    price_change_24h: float | None = None
    txn_count_24h: int | None = None
    holders: int | None = None


class SolanaScanner:
    """Discover and filter tradeable Solana tokens."""

    def __init__(
        self,
        min_liquidity_usd: float = 50_000.0,
        min_volume_24h: float = 10_000.0,
        max_pairs_per_scan: int = 50,
        microcap_enabled: bool = True,
        microcap_max_mcap: float = 500_000.0,
        microcap_min_liquidity_usd: float = 10_000.0,
        microcap_min_volume_24h: float = 5_000.0,
        microcap_max_pairs: int = 20,
        discovery_enabled: bool = True,
        discovery_max_age_hours: float = 48.0,
        discovery_min_score: float = 0.0,
        max_workers: int = 2,
    ) -> None:
        self.min_liquidity_usd = min_liquidity_usd
        self.min_volume_24h = min_volume_24h
        self.max_pairs_per_scan = max_pairs_per_scan
        self.microcap_enabled = microcap_enabled
        self.microcap_max_mcap = microcap_max_mcap
        self.microcap_min_liquidity_usd = microcap_min_liquidity_usd
        self.microcap_min_volume_24h = microcap_min_volume_24h
        self.microcap_max_pairs = microcap_max_pairs
        self.discovery_enabled = discovery_enabled
        self.discovery_max_age_hours = discovery_max_age_hours
        self.discovery_min_score = discovery_min_score
        self.max_workers = max_workers
        self.previous_mints: set[str] = set()
        self.new_tokens: list[dict[str, Any]] = []
        self._profile_meta: dict[str, dict[str, Any]] = {}

    def _get(self, path: str, params: dict | None = None, timeout: int = 15) -> dict[str, Any]:
        url = f"{DEXSCREENER_BASE}{path}"
        response = requests.get(url, params=params, timeout=(5, timeout))
        response.raise_for_status()
        return response.json()

    def _get_fast(self, path: str, params: dict | None = None, timeout: int = 10) -> dict[str, Any] | None:
        """Fast GET that returns None on timeout/error instead of raising."""
        url = f"{DEXSCREENER_BASE}{path}"
        try:
            response = requests.get(url, params=params, timeout=(5, timeout))
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.debug("DexScreener fast request failed for %s: %s", url, exc)
            return None

    def _normalize(self, raw: dict[str, Any]) -> TokenPair | None:
        if raw.get("chainId") != "solana":
            return None
        base = raw.get("baseToken", {})
        quote = raw.get("quoteToken", {})
        liquidity = raw.get("liquidity", {})
        volume = raw.get("volume", {})
        profile = self._profile_meta.get(base.get("address", ""), {})
        updated_at = profile.get("updatedAt")
        return TokenPair(
            symbol=base.get("symbol", "UNKNOWN"),
            mint=base.get("address", ""),
            quote_symbol=quote.get("symbol", ""),
            quote_mint=quote.get("address", ""),
            price_usd=float(raw.get("priceUsd") or 0),
            liquidity_usd=float(liquidity.get("usd") or 0),
            volume_24h=float(volume.get("h24") or 0),
            market_cap=float(raw.get("marketCap") or 0),
            dex_id=raw.get("dexId", ""),
            pair_address=raw.get("pairAddress", ""),
            source_endpoints=profile.get("sources", []),
            profile_updated_at=self._parse_iso(updated_at) if updated_at else None,
            boost_amount=profile.get("amount", 0.0),
            total_boost_amount=profile.get("totalAmount", 0.0),
            price_change_24h=self._safe_float(raw.get("priceChange", {}).get("h24")),
            txn_count_24h=self._safe_int(raw.get("txns", {}).get("h24", {}).get("buys"))
            + self._safe_int(raw.get("txns", {}).get("h24", {}).get("sells")),
            holders=self._safe_int(raw.get("fdv")),  # DexScreener does not expose holders; placeholder.
        )

    @staticmethod
    def _parse_iso(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _prefer_stable_pair(self, pairs: list[TokenPair]) -> TokenPair | None:
        """Return the pair with the highest liquidity, preferring stable quotes."""
        if not pairs:
            return None
        stable = [p for p in pairs if p.quote_symbol in ("USDC", "USDT")]
        candidates = stable or pairs
        return max(candidates, key=lambda p: p.liquidity_usd)

    def _search_batch(self, mints: list[str]) -> dict[str, list[TokenPair]]:
        """Search DexScreener for many mints in one batch request.

        DexScreener's ``/tokens/v1/solana/{addresses}`` endpoint returns the
        best pairs for up to 30 addresses in a single call, which avoids the
        ~30s per-request latency of the search endpoint on slow networks.
        """
        results: dict[str, list[TokenPair]] = {mint: [] for mint in mints}
        if not mints:
            return results
        data = self._get_fast(f"/tokens/v1/solana/{','.join(mints[:30])}", timeout=10)
        if data is None:
            logger.warning("Batch search failed/timeout for blue chips")
            return results
        items = data if isinstance(data, list) else data.get("pairs", [])
        for raw in items:
            pair = self._normalize(raw)
            if pair and pair.mint in results:
                results[pair.mint].append(pair)
        return results

    def search(self, query: str) -> list[TokenPair]:
        """Search DexScreener for pairs matching a query."""
        data = self._get("/latest/dex/search", params={"q": query}, timeout=15)
        pairs = data.get("pairs", [])
        results: list[TokenPair] = []
        for raw in pairs:
            pair = self._normalize(raw)
            if pair and pair.liquidity_usd >= self.min_liquidity_usd:
                results.append(pair)
        # Deduplicate by base mint, keeping the most liquid pair.
        deduped: dict[str, TokenPair] = {}
        for pair in results:
            existing = deduped.get(pair.mint)
            if existing is None or pair.liquidity_usd > existing.liquidity_usd:
                deduped[pair.mint] = pair
        return sorted(
            deduped.values(),
            key=lambda p: p.liquidity_usd,
            reverse=True,
        )[: self.max_pairs_per_scan]

    def _lookup_token(self, address: str) -> TokenPair | None:
        """Fetch the best Solana pair for a single token address."""
        try:
            data = self._get("/latest/dex/tokens/" + address, timeout=15)
        except requests.RequestException:
            return None
        pairs = [self._normalize(p) for p in data.get("pairs", [])]
        pairs = [p for p in pairs if p is not None]
        return self._prefer_stable_pair(pairs)

    def _fetch_discovery_endpoint(self, endpoint: str) -> list[dict[str, Any]]:
        """Fetch one discovery endpoint and decorate items with their source."""
        try:
            data = self._get(endpoint)
            items = data if isinstance(data, list) else []
            for item in items:
                item["_source_endpoint"] = endpoint
                # Merge boost amount metadata if present.
                if "amount" in item:
                    item["boost_amount"] = item["amount"]
            return items
        except requests.RequestException as exc:
            logger.warning("Discovery endpoint %s failed: %s", endpoint, exc)
            return []

    def _fetch_discovery_parallel(self) -> list[dict[str, Any]]:
        """Fetch all discovery endpoints in parallel and merge unique tokens."""
        if not self.discovery_enabled:
            return []

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._fetch_discovery_endpoint, ep): ep
                for ep in DISCOVERY_ENDPOINTS
            }
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception as exc:
                    logger.warning("Discovery fetch error: %s", exc)

        # Preserve ordering: top boosts first, then latest boosts, then profiles.
        endpoint_rank = {ep: idx for idx, ep in enumerate(DISCOVERY_ENDPOINTS)}
        results.sort(key=lambda item: (endpoint_rank.get(item.get("_source_endpoint"), 99), item.get("totalAmount", 0)), reverse=True)
        return results

    def _resolve_pairs_parallel(self, addresses: list[str]) -> dict[str, TokenPair]:
        """Resolve full pair data for many addresses in parallel."""
        resolved: dict[str, TokenPair] = {}
        if not addresses:
            return resolved

        def _resolve_one(address: str) -> tuple[str, TokenPair | None]:
            try:
                pair = self._lookup_token(address)
                return address, pair
            except Exception as exc:
                logger.debug("Resolve failed for %s: %s", address, exc)
                return address, None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(_resolve_one, addr) for addr in addresses]
            for future in as_completed(futures):
                address, pair = future.result()
                if pair:
                    resolved[address] = pair
        return resolved

    @staticmethod
    def _score_token(pair: TokenPair) -> float:
        """Compute a discovery score from liquidity, volume and market cap."""
        # Volume-to-liquidity ratio indicates trading interest vs depth.
        vlr = pair.volume_24h / (pair.liquidity_usd or 1.0)
        # Prefer smaller market caps for upside, but not too tiny (less noise).
        mcap_factor = 1.0
        if pair.market_cap > 0:
            mcap_factor = min(1.0, 500_000.0 / pair.market_cap)
        # Boosted tokens get a small bonus.
        boost_factor = min(1.0, (pair.total_boost_amount or 0.0) / 100.0)
        # Price momentum bonus.
        momentum = 0.0
        if pair.price_change_24h is not None and pair.price_change_24h > 0:
            momentum = min(1.0, pair.price_change_24h / 100.0)
        score = (
            vlr * 30.0
            + mcap_factor * 25.0
            + boost_factor * 20.0
            + momentum * 25.0
        )
        return score

    def _freshness_filter(self, pair: TokenPair) -> bool:
        """Keep tokens whose profile/boost was updated recently."""
        if self.discovery_max_age_hours <= 0 or pair.profile_updated_at is None:
            return True
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.discovery_max_age_hours)
        return pair.profile_updated_at >= cutoff

    def _build_discovery_watchlist(self) -> dict[str, TokenPair]:
        """Discover tokens from all endpoints, resolve pairs, score and filter."""
        raw_items = self._fetch_discovery_parallel()
        if not raw_items:
            return {}

        # Track metadata per address so we can attach boost/profile info later.
        address_meta: dict[str, dict[str, Any]] = {}
        ordered_addresses: list[str] = []
        for item in raw_items:
            address = item.get("tokenAddress") or item.get("address")
            if not address:
                continue
            meta = address_meta.setdefault(address, {
                "sources": set(),
                "amount": 0.0,
                "totalAmount": 0.0,
                "updatedAt": item.get("updatedAt"),
            })
            meta["sources"].add(item.get("_source_endpoint", ""))
            meta["amount"] = max(meta["amount"], float(item.get("amount") or 0.0))
            meta["totalAmount"] = max(meta["totalAmount"], float(item.get("totalAmount") or 0.0))
            if item.get("updatedAt"):
                meta["updatedAt"] = item["updatedAt"]
            if address not in ordered_addresses:
                ordered_addresses.append(address)

        # Resolve all unique addresses in parallel.
        resolved = self._resolve_pairs_parallel(ordered_addresses)

        # Attach metadata and score.
        scored: list[tuple[float, TokenPair]] = []
        for address, pair in resolved.items():
            meta = address_meta.get(address, {})
            self._profile_meta[address] = meta
            pair.source_endpoints = sorted(meta.get("sources", set()))
            pair.boost_amount = meta.get("amount", 0.0)
            pair.total_boost_amount = meta.get("totalAmount", 0.0)
            pair.profile_updated_at = self._parse_iso(meta.get("updatedAt")) if meta.get("updatedAt") else None

            if not self._freshness_filter(pair):
                continue
            if pair.volume_24h < self.min_volume_24h:
                continue
            if pair.liquidity_usd < self.min_liquidity_usd:
                continue

            score = self._score_token(pair)
            if score < self.discovery_min_score:
                continue
            scored.append((score, pair))

        # Sort by score descending and keep top picks.
        scored.sort(key=lambda x: x[0], reverse=True)
        return {pair.symbol: pair for _, pair in scored[: self.max_pairs_per_scan]}

    def microcaps(self) -> list[TokenPair]:
        """Discover newly created / low market-cap tokens from all endpoints."""
        if not self.microcap_enabled:
            return []

        raw_items = self._fetch_discovery_parallel()
        address_meta: dict[str, dict[str, Any]] = {}
        ordered_addresses: list[str] = []
        for item in raw_items:
            address = item.get("tokenAddress") or item.get("address")
            if not address:
                continue
            meta = address_meta.setdefault(address, {
                "sources": set(),
                "amount": 0.0,
                "totalAmount": 0.0,
                "updatedAt": item.get("updatedAt"),
            })
            meta["sources"].add(item.get("_source_endpoint", ""))
            meta["amount"] = max(meta["amount"], float(item.get("amount") or 0.0))
            meta["totalAmount"] = max(meta["totalAmount"], float(item.get("totalAmount") or 0.0))
            if item.get("updatedAt"):
                meta["updatedAt"] = item["updatedAt"]
            if address not in ordered_addresses:
                ordered_addresses.append(address)

        resolved = self._resolve_pairs_parallel(ordered_addresses)
        results: dict[str, TokenPair] = {}
        for address, pair in resolved.items():
            if pair is None:
                continue
            meta = address_meta.get(address, {})
            self._profile_meta[address] = meta
            pair.source_endpoints = sorted(meta.get("sources", set()))
            pair.boost_amount = meta.get("amount", 0.0)
            pair.total_boost_amount = meta.get("totalAmount", 0.0)
            pair.profile_updated_at = self._parse_iso(meta.get("updatedAt")) if meta.get("updatedAt") else None

            if pair.market_cap == 0 or pair.market_cap > self.microcap_max_mcap:
                continue
            if pair.liquidity_usd < self.microcap_min_liquidity_usd:
                continue
            if pair.volume_24h < self.microcap_min_volume_24h:
                continue
            results[address] = pair

        return sorted(
            results.values(),
            key=lambda p: p.volume_24h,
            reverse=True,
        )[: self.microcap_max_pairs]

    def build_watchlist(self, alert_callback: Callable[[str], None] | None = None) -> list[TokenPair]:
        """Build a combined watchlist of blue chips and discovery tokens.

        Tracks newly discovered tokens across scans. If ``alert_callback`` is
        provided, it is invoked with a markdown alert for each new token.
        """
        watchlist: dict[str, TokenPair] = {}

        try:
            blue_chip_mints = [mint for _, mint in DEFAULT_BLUE_CHIPS]
            batch_results = self._search_batch(blue_chip_mints)
            for symbol, mint in DEFAULT_BLUE_CHIPS:
                pairs = batch_results.get(mint, [])
                pair = self._prefer_stable_pair(pairs)
                if pair and pair.symbol not in watchlist:
                    watchlist[pair.symbol] = pair
        except requests.RequestException as exc:
            logger.warning("Blue chip batch search failed: %s", exc)

        if self.discovery_enabled:
            try:
                for symbol, pair in self._build_discovery_watchlist().items():
                    if symbol not in watchlist:
                        watchlist[symbol] = pair
            except requests.RequestException as exc:
                logger.warning("Discovery scan failed: %s", exc)

        if self.microcap_enabled:
            try:
                for pair in self.microcaps():
                    if pair.symbol not in watchlist:
                        watchlist[pair.symbol] = pair
            except requests.RequestException as exc:
                logger.warning("Microcap scan failed: %s", exc)

        current_mints = set(watchlist.keys())
        discovered = current_mints - self.previous_mints
        self.previous_mints = current_mints
        for mint in discovered:
            pair = watchlist[mint]
            token_info = {
                "symbol": pair.symbol,
                "mint": pair.mint,
                "price_usd": pair.price_usd,
                "liquidity_usd": pair.liquidity_usd,
                "volume_24h": pair.volume_24h,
                "market_cap": pair.market_cap,
                "score": self._score_token(pair),
                "sources": pair.source_endpoints,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
            self.new_tokens.append(token_info)
            logger.info(
                "New token discovered: %s | mcap=$%.0f | liq=$%.0f | vol24=$%.0f | score=%.1f | sources=%s",
                pair.symbol,
                pair.market_cap,
                pair.liquidity_usd,
                pair.volume_24h,
                token_info["score"],
                ",".join(pair.source_endpoints) or "none",
            )
            if alert_callback:
                alert_callback(
                    f"🆕 *Nouveau token détecté — {pair.symbol}*\n"
                    f"💰 Prix: `${pair.price_usd:.8f}`\n"
                    f"📊 Mcap: `${pair.market_cap:,.0f}`\n"
                    f"💧 Liq: `${pair.liquidity_usd:,.0f}`\n"
                    f"📈 Vol 24h: `${pair.volume_24h:,.0f}`\n"
                    f"⭐ Score: `{token_info['score']:.1f}`"
                )

        # Keep only the most recent discoveries to avoid unbounded growth.
        self.new_tokens = self.new_tokens[-100:]

        # Final sort: discovery score first, then volume.
        return sorted(
            watchlist.values(),
            key=lambda p: (self._score_token(p), p.volume_24h),
            reverse=True,
        )[: self.max_pairs_per_scan]
