"""Main trading loop: scan, sample prices, generate signals, execute trades."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aioud.connectors.solana import SolanaConnector
from aioud.data.multi_asset_prices import MultiAssetPriceFeed
from aioud.data.prices import PriceFeed
from aioud.data.scanner import SolanaScanner, TokenPair
from aioud.data.watchlist_store import WatchlistStore
from aioud.llm.groq_reporter import GroqReporter
from aioud.llm.multi_asset_reporter import MultiAssetReporter
from aioud.llm.piranewz_client import PiranewzClient
from aioud.llm.piranewz_scraper import PiranewzScraper
from aioud.llm.piranewz_tg_reader import PiranewzTelegramReader
from aioud.risk.manager import RiskManager
from aioud.strategies.pumpfun_sniper import SniperConfig, is_fresh_pumpfun_token
from aioud.strategies.scalper import SolanaScalper
from aioud.telegram.bot import TelegramBot
from aioud.utils.config import Config
from aioud.utils import postgres_store
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


class TradingEngine:
    """Autonomous trading engine for Solana tokens."""

    # Watchlist size: 20 gives more opportunities. The background 60s sampler
    # keeps all candles fresh, and we evaluate a rotating subset each cycle so
    # a bigger watchlist doesn't slow the loop.
    MAX_WATCHLIST_SIZE = 20

    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramBot(config)
        self.solana = SolanaConnector(config)
        self.risk = RiskManager(config)
        self.scanner = SolanaScanner(
            min_liquidity_usd=50_000.0,
            min_volume_24h=10_000.0,
            max_pairs_per_scan=20,
            microcap_enabled=True,
            microcap_max_mcap=500_000.0,
            microcap_min_liquidity_usd=10_000.0,
            microcap_min_volume_24h=5_000.0,
            microcap_max_pairs=10,
            discovery_enabled=True,
            discovery_max_age_hours=48.0,
            discovery_min_score=20.0,
            max_workers=2,
        )
        # Pump.fun sniper: buy brand-new launches, exit mechanically at +20%/-10%.
        # Enabled via SNIPER_ENABLED; tunables via env. Position sizing reuses the
        # engine's percentage-of-capital sizing (risk.compute_position_size_usd).
        self.sniper_config = SniperConfig(
            take_profit_pct=config.sniper_take_profit_pct,
            stop_loss_pct=config.sniper_stop_loss_pct,
            max_token_age_minutes=config.sniper_max_age_minutes,
            min_liquidity_usd=config.sniper_min_liquidity_usd,
        )
        self.sniper_enabled = config.sniper_enabled
        self.feed = PriceFeed(
            trappist_api_url=config.trappist_api_url,
            trappist_api_key=config.trappist_api_key,
        )
        self.watchlist_store = WatchlistStore()
        self.watchlist: list[TokenPair] = self.watchlist_store.load()
        self.strategies: dict[str, SolanaScalper] = {
            pair.mint: SolanaScalper(symbol=f"{pair.symbol}/{pair.quote_symbol}")
            for pair in self.watchlist
        }
        self._removed_mints_path = Path("data/removed_mints.json")
        self._removed_mints: set[str] = self._load_removed_mints()
        self.running = False
        self.recent_evaluations: dict[str, dict[str, Any]] = {}
        self.logs: list[dict[str, Any]] = []
        self._cycle_count: int = 0
        self.telegram_messages: list[dict[str, str]] = []
        self._paper_test_done = False
        self._running_cycle = False
        self._last_alert: dict[str, datetime] = postgres_store.load_alert_cooldowns()
        self._last_sol_balance: float = 0.0
        self._last_usdc_balance: float = 0.0
        self._cached_sol_price_usd: float = 77.0
        self.groq_reporter = GroqReporter(api_key=config.groq_api_key)
        self.piranewz = PiranewzClient(api_url=config.piranewz_api_url, api_key=config.piranewz_api_key)
        # The Telegram channel reader uses the SAME bot token as the main bot,
        # which causes getUpdates conflicts (only one poller per token allowed).
        # It is opt-in via PIRANEWZ_TG_READER=true and requires a separate token.
        # Default: scrape the public channel page t.me/s/<channel> — no token,
        # no conflict, works even if the bot is not in the channel.
        self.piranewz_reader = PiranewzTelegramReader(config)
        if not config.piranewz_tg_reader_enabled:
            self.piranewz_reader.enabled = False
        self.piranewz_scraper = PiranewzScraper(config)
        self.multi_asset_feed = MultiAssetPriceFeed(
            coingecko_api_key=config.coingecko_api_key,
            cmc_api_key=config.coinmarketcap_api_key,
        )
        self.multi_asset_reporter = MultiAssetReporter(
            api_key=config.groq_api_key,
            price_feed=self.multi_asset_feed,
        )
        self._last_groq_report_cycle: int = 0
        self.groq_reports: list[dict[str, Any]] = []
        self.manual_signals: list[dict[str, Any]] = []
        self._sampler_task: asyncio.Task | None = None
        self._next_cycle_at: float = 0.0  # unix ts (time.time) of next trading cycle

    async def start(self) -> None:
        """Start Telegram bot and initialize watchlist in the background."""
        self.telegram._set_engine(self)
        await self.telegram.start()
        if self.piranewz_reader.enabled:
            await self.piranewz_reader.start()
        else:
            logger.info("Piranewz Telegram reader disabled; using HTTP client only.")
        self.running = True
        # Start watchlist refresh in background so Telegram/dashboard stay responsive.
        if self.watchlist:
            logger.info("Loaded %s persisted watchlist pairs", len(self.watchlist))
        asyncio.create_task(self._refresh_watchlist())
        self._sampler_task = asyncio.create_task(self._price_sampler_loop())
        await self.telegram.send_alert(
            f"⚡ *AIOud Trader* démarré.\nMode: *{'PAPER' if self.config.paper_trading else 'REAL'}*\n"
            "Scan multi-actifs Solana actif."
        )
        # Generate an initial Groq market report so the dashboard/context is populated.
        if self.config.groq_api_key:
            asyncio.create_task(self._run_groq_report())

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        self.running = False
        if self._sampler_task:
            self._sampler_task.cancel()
            try:
                await self._sampler_task
            except asyncio.CancelledError:
                pass
        if self.piranewz_reader.enabled:
            await self.piranewz_reader.stop()
        # Message rassurant: en pratique un 'stop' sur Render = redeploiement,
        # la nouvelle instance reprend immediatement le relais.
        await self.telegram.send_alert(
            "🔄 *AIOud Trader* redémarre (déploiement).\n"
            "La nouvelle instance prend le relais dans quelques secondes."
        )
        await self.telegram.stop()

    async def _refresh_watchlist(self) -> None:
        """Refresh the list of tracked tokens in a background thread."""
        logger.info("Refreshing watchlist...")
        try:
            new_tokens: list[str] = []

            def _capture_alert(text: str) -> None:
                new_tokens.append(text)

            # DexScreener is synchronous and can be slow; run in thread.
            scanned = await asyncio.to_thread(
                self.scanner.build_watchlist, alert_callback=_capture_alert
            )
            # Merge persisted + scanned, preserve manually added pairs, honor removals,
            # and refresh metadata for tokens that are rediscovered.
            merged: dict[str, TokenPair] = {p.mint: p for p in self.watchlist if p.mint not in self._removed_mints}
            for pair in scanned:
                if pair.mint in self._removed_mints:
                    continue
                existing = merged.get(pair.mint)
                if existing is not None:
                    # Refresh price/volume metadata but keep the manually added pair.
                    existing.price_usd = pair.price_usd
                    existing.liquidity_usd = pair.liquidity_usd
                    existing.volume_24h = pair.volume_24h
                    existing.market_cap = pair.market_cap
                    existing.price_change_24h = pair.price_change_24h
                    existing.txn_count_24h = pair.txn_count_24h
                    existing.source_endpoints = pair.source_endpoints or existing.source_endpoints
                    existing.boost_amount = pair.boost_amount
                    existing.total_boost_amount = pair.total_boost_amount
                    existing.profile_updated_at = pair.profile_updated_at
                else:
                    merged[pair.mint] = pair
            self.watchlist = self._sort_and_cap_watchlist(merged.values())
            self.strategies = {
                pair.mint: SolanaScalper(symbol=f"{pair.symbol}/{pair.quote_symbol}")
                for pair in self.watchlist
            }
            self.watchlist_store.save(self.watchlist)
            symbols = ", ".join(p.symbol for p in self.watchlist[:10])
            new_count = len(self.scanner.new_tokens[-10:])
            # Send one batched alert for new tokens instead of one per token.
            if new_tokens and self._can_alert("new_tokens"):
                await self.telegram.send_alert(
                    "🆕 *Nouveaux tokens détectés*\n\n" + "\n\n".join(new_tokens[:5])
                )
            await self.telegram.send_alert(
                f"📋 *Watchlist mise à jour* ({len(self.watchlist)} paires)\n"
                f"Top: {symbols}\n"
                f"🆕 Nouveaux tokens récents: {new_count}"
            )
            logger.info("Watchlist updated: %s tokens", len(self.watchlist))
        except Exception as exc:
            logger.error("Watchlist refresh failed: %s", exc)
            await self.telegram.send_alert(f"⚠️ Erreur refresh watchlist: {exc}")

    async def _price_sampler_loop(self) -> None:
        """Snapshot watchlist prices every minute so candles build in real time.

        Without this, a new token needs ~2h (23 cycles x 5 min) before the
        strategy has enough 1-minute candles to evaluate it. With this sampler
        it needs ~25 minutes, and indicators use true 1m spacing.
        """
        interval = max(15.0, self.config.price_sampler_interval_seconds)
        while self.running:
            try:
                mints = [p.mint for p in self.watchlist]
                if mints:
                    await asyncio.to_thread(self.feed.snapshot_batch, mints)
            except Exception as exc:
                logger.warning("Price sampler failed: %s", exc)
            await asyncio.sleep(interval)

    def _sort_and_cap_watchlist(self, pairs) -> list[TokenPair]:
        """Sort watchlist by score/volume and enforce the max size."""
        sorted_pairs = sorted(
            pairs,
            key=lambda p: (self.scanner._score_token(p), p.volume_24h),
            reverse=True,
        )
        capped = sorted_pairs[: self.MAX_WATCHLIST_SIZE]
        if len(sorted_pairs) > self.MAX_WATCHLIST_SIZE:
            dropped = [p.symbol for p in sorted_pairs[self.MAX_WATCHLIST_SIZE :]]
            logger.warning(
                "Watchlist capped at %s tokens; dropped: %s",
                self.MAX_WATCHLIST_SIZE,
                ", ".join(dropped[:10]),
            )
        return capped

    async def _run_groq_report(self) -> None:
        """Generate a Groq situation report every 10 minutes and act on buy ideas."""
        try:
            open_positions = []
            for pos in self.risk.positions.values():
                try:
                    price_data = await asyncio.to_thread(self.feed.fetch_price, pos.mint)
                    current_price = price_data["price"]
                except Exception:
                    current_price = pos.entry_price
                open_positions.append(
                    {
                        "symbol": pos.symbol,
                        "mint": pos.mint,
                        "entry": pos.entry_price,
                        "current_price": current_price,
                        "amount": pos.amount,
                        "tp1": pos.tp1,
                        "tp2": pos.tp2,
                        "tp3": pos.tp3,
                        "sl": pos.sl,
                        "unrealized_pnl_pct": pos.pnl_pct(current_price),
                    }
                )

            # Enrich prompt with Piranewz news for tracked symbols.
            symbols = [p.symbol for p in self.watchlist[:30]]
            try:
                news_articles = await asyncio.to_thread(
                    self.piranewz.fetch_news, symbols=symbols, limit=10, hours=24
                )
            except Exception:
                news_articles = []

            if self.piranewz_reader.enabled:
                piranewz_sentiment = self.piranewz_reader.get_sentiment_summary(hours=24)
            else:
                # Default path: scrape the public channel page (no token needed).
                # Falls back to HTTP API headlines if the scrape returns nothing.
                piranewz_sentiment = await asyncio.to_thread(
                    self.piranewz_scraper.get_sentiment_summary, 24
                )
                if not piranewz_sentiment.get("signals") and news_articles:
                    piranewz_sentiment = self.piranewz.summarize_sentiment(news_articles)

            watchlist_state = [
                {
                    "symbol": p.symbol,
                    "mint": p.mint,
                    "price": p.price_usd,
                    "market_cap": p.market_cap,
                    "liquidity": p.liquidity_usd,
                    "volume_24h": p.volume_24h,
                    "score": self.scanner._score_token(p),
                }
                for p in self.watchlist
            ]

            balances = {
                "sol_balance": self._last_sol_balance,
                "usdc_balance": self._last_usdc_balance,
                "sol_price_usd": self._cached_sol_price_usd,
            }

            report = await asyncio.to_thread(
                self.groq_reporter.generate_report,
                balances=balances,
                open_positions=open_positions,
                watchlist=watchlist_state,
                recent_evaluations=self.recent_evaluations,
                trade_history=self.risk.trade_history,
                news_articles=news_articles,
                piranewz_sentiment=piranewz_sentiment,
            )

            report_entry = {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "summary": report.summary,
                "reasoning": report.reasoning,
                "buy_suggestions": report.buy_suggestions,
                "hold_suggestions": report.hold_suggestions,
                "sell_suggestions": report.sell_suggestions,
                "news_count": len(news_articles),
                "piranewz_sentiment": piranewz_sentiment,
            }
            self.groq_reports.append(report_entry)
            if len(self.groq_reports) > 20:
                self.groq_reports = self.groq_reports[-20:]

            # Generate manual CEX signals for non-Solana tokens mentioned by Piranewz.
            await self._generate_manual_signals(piranewz_sentiment)

            # Build and send Telegram alert.
            buy_lines = [
                f"• *{s['symbol']}* (conf {s.get('confidence', 0)}%)\n_{s.get('reason', '')}_"
                for s in report.buy_suggestions[:5]
            ]
            sell_lines = [
                f"• *{s['symbol']}* (conf {s.get('confidence', 0)}%)\n_{s.get('reason', '')}_"
                for s in report.sell_suggestions[:5]
            ]
            hold_lines = [
                f"• *{s['symbol']}* (conf {s.get('confidence', 0)}%)\n_{s.get('reason', '')}_"
                for s in report.hold_suggestions[:5]
            ]
            message = (
                f"📊 *Rapport IA — {report.summary}*\n\n"
                f"💡 *Raisonnement:*\n{report.reasoning[:400]}\n\n"
            )
            if buy_lines:
                message += f"🟢 *Achats suggérés*\n" + "\n".join(buy_lines) + "\n\n"
            if sell_lines:
                message += f"🔴 *Ventes suggérées*\n" + "\n".join(sell_lines) + "\n\n"
            if hold_lines:
                message += f"🟡 *Hold*\n" + "\n".join(hold_lines) + "\n\n"

            await self.telegram.send_alert(message)
            logger.info("Groq report sent: %s", report.summary)

            # Act on Groq buy suggestions already in the watchlist and Solana-compatible.
            # We no longer require a double technical confirmation (buy signal AND
            # score>=40): the AI's fundamental/sentiment read is itself the edge.
            # We only SKIP when the technicals scream SELL (strong downtrend).
            watchlist_symbols = {p.symbol.upper(): p for p in self.watchlist}
            for suggestion in report.buy_suggestions:
                symbol = suggestion.get("symbol", "").upper()
                confidence = suggestion.get("confidence", 0)
                if confidence < self.config.groq_auto_buy_min_confidence:
                    continue
                pair = watchlist_symbols.get(symbol)
                if not pair or pair.mint in self.risk.positions:
                    continue
                evaluation = await self._evaluate_pair(pair)
                if not evaluation:
                    continue
                # Skip only on a clearly bearish technical read; otherwise trust the AI.
                if evaluation["signal"] == "sell" or evaluation["score"] <= -40:
                    logger.info("Groq buy %s skipped: technicals bearish (score %s)", symbol, evaluation["score"])
                    continue
                reason = f"🤖 IA ({confidence}%): {suggestion.get('reason', 'signal IA')}"
                await self._open_position(
                    pair, evaluation["price"], max(evaluation["score"], confidence), reason,
                    atr=evaluation["details"].get("atr", 0.0),
                )
                await self.telegram.send_alert(
                    f"🤖 *Achat IA auto — {pair.symbol}*\n"
                    f"Confiance: `{confidence}%`\n"
                    f"_{suggestion.get('reason', '')}_\n"
                    f"Stratégie TP/SL envoyée ci-dessus ⬆️"
                )
                logger.info("Groq-triggered buy for %s (conf %s)", pair.symbol, confidence)
        except Exception as exc:
            logger.warning("Groq report cycle failed: %s", exc)
            await self.telegram.send_alert(f"⚠️ Rapport IA indisponible: {exc}")

    async def _generate_manual_signals(self, piranewz_sentiment: dict[str, Any]) -> None:
        """Generate and broadcast manual CEX signals for non-Solana tokens."""
        if not self.config.manual_signal_enabled:
            return
        try:
            symbol_sentiment = piranewz_sentiment.get("symbol_sentiment", {})
            recent_signals = piranewz_sentiment.get("signals", [])
            if not symbol_sentiment:
                return
            signals = await asyncio.to_thread(
                self.multi_asset_reporter.generate_signals,
                symbol_sentiment=symbol_sentiment,
                recent_signals=recent_signals,
                min_confidence=self.config.manual_signal_min_confidence,
            )
            self.manual_signals = [s.__dict__ for s in signals]
            for signal in signals[:5]:
                await self.telegram.send_manual_signal(signal)
                logger.info(
                    "Manual signal %s: buy_limit=%.8f TP1=%.8f SL=%.8f conf=%s",
                    signal.symbol,
                    signal.buy_limit,
                    signal.tp1,
                    signal.sl,
                    signal.confidence,
                )
        except Exception as exc:
            logger.warning("Manual signal generation failed: %s", exc)

    def _load_removed_mints(self) -> set[str]:
        """Load the persisted set of manually removed mints."""
        try:
            if postgres_store._is_postgres_available():
                return postgres_store.load_removed_mints()
        except Exception as exc:
            logger.warning("Failed to load removed mints from Postgres: %s", exc)
        if not self._removed_mints_path.exists():
            return set()
        try:
            with self._removed_mints_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data) if isinstance(data, list) else set()
        except Exception as exc:
            logger.warning("Failed to load removed mints: %s", exc)
            return set()

    def _save_removed_mints(self) -> None:
        """Persist the set of manually removed mints."""
        try:
            self._removed_mints_path.parent.mkdir(parents=True, exist_ok=True)
            with self._removed_mints_path.open("w", encoding="utf-8") as f:
                json.dump(sorted(self._removed_mints), f, indent=2)
            postgres_store.save_removed_mints(self._removed_mints)
        except Exception as exc:
            logger.warning("Failed to save removed mints: %s", exc)

    def _log(self, level: str, message: str) -> None:
        """Store a recent log entry for the dashboard."""
        self.logs.append(
            {
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            }
        )
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

    def add_pair(self, mint: str) -> TokenPair | None:
        """Add a token by mint address (sync, can be called from Telegram)."""
        pair = self.scanner._lookup_token(mint)
        if not pair:
            return None
        if pair.mint in self._removed_mints:
            self._removed_mints.discard(pair.mint)
        if pair.mint not in {p.mint for p in self.watchlist}:
            self.watchlist.append(pair)
            self.strategies[pair.mint] = SolanaScalper(symbol=f"{pair.symbol}/{pair.quote_symbol}")
            self.watchlist = self._sort_and_cap_watchlist(self.watchlist)
            self.watchlist_store.save(self.watchlist)
            logger.info("Manually added pair: %s (%s)", pair.symbol, pair.mint)
        return pair

    def remove_pair(self, mint: str) -> bool:
        """Remove a token by mint address."""
        before = len(self.watchlist)
        self.watchlist = [p for p in self.watchlist if p.mint != mint]
        self.strategies.pop(mint, None)
        self._removed_mints.add(mint)
        self._save_removed_mints()
        self.watchlist_store.save(self.watchlist)
        removed = len(self.watchlist) < before or self.watchlist_store.remove(mint)
        if removed:
            logger.info("Removed pair: %s", mint)
        return removed

    async def _evaluate_pair(self, pair: TokenPair, candles: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """Update candles and check for a trading signal."""
        try:
            if candles is None:
                candles = await asyncio.to_thread(self.feed.snapshot_and_build, pair.mint)
            min_candles = SolanaScalper.MIN_CANDLES
            if len(candles) < min_candles and self.config.candle_warmup_enabled:
                # Seed missing history with synthetic flat candles at current price
                # so a fresh token becomes evaluable immediately instead of after
                # ~2h of sampling. Synthetic volume is 0, so volume-based signals
                # stay neutral until real volume accumulates.
                missing = min_candles - len(candles)
                last_price = candles[-1]["close"] if candles else (pair.price_usd or 0.0)
                last_ts = int(candles[-1]["timestamp"]) if candles else int(time.time())
                warmup = [
                    {
                        "timestamp": last_ts - (missing - i) * 60,
                        "open": last_price,
                        "high": last_price,
                        "low": last_price,
                        "close": last_price,
                        "volume": 0.0,
                    }
                    for i in range(missing)
                ]
                candles = warmup + candles
            if len(candles) < min_candles:
                msg = f"Evaluation {pair.symbol}: pas assez de bougies ({len(candles)}/{min_candles})"
                self._log("info", msg)
                logger.info(msg)
                return None

            strategy = self.strategies.get(pair.mint)
            if not strategy:
                return None

            result = strategy.analyze(candles)
            price = candles[-1]["close"]
            eval_data = {
                "pair": pair,
                "signal": result.signal,
                "score": result.score,
                "details": result.details,
                "price": price,
                "candles": candles[-40:],
            }
            self.recent_evaluations[pair.mint] = {
                "symbol": pair.symbol,
                "signal": result.signal,
                "score": result.score,
                "price": price,
                "reason": result.details.get("reason", ""),
                "rsi": result.details.get("rsi"),
                "adx": result.details.get("adx", {}).get("adx"),
                "volume_spike": result.details.get("volume_spike"),
                "candles": candles[-40:],
            }
            self._log(
                "buy" if result.signal == "buy" else "sell" if result.signal == "sell" else "info",
                f"{pair.symbol}: {result.signal.upper()} score={result.score}",
            )
            return eval_data
        except Exception as exc:
            msg = f"Evaluation failed for {pair.symbol}: {exc}"
            self._log("warn", msg)
            logger.warning(msg)
            return None

    async def _snipe_new_tokens(self) -> None:
        """Detect freshly-launched tokens in the watchlist and snipe them.

        Unlike the indicator-based scalper, the sniper does NOT wait for RSI/MACD
        confirmation — on a fresh pump.fun launch there are no candles yet, and
        speed matters more than confirmation. We buy immediately and exit at a
        fixed +20% (protective -10% stop), handled by the risk manager's sniper
        branch.
        """
        if not self.sniper_enabled:
            return
        for pair in self.watchlist:
            if pair.mint in self.risk.positions:
                continue  # already holding
            if pair.mint in self.risk.cooldown_until:
                continue  # recently closed, let it cool
            try:
                fresh = is_fresh_pumpfun_token(
                    pair,
                    max_age_minutes=self.sniper_config.max_token_age_minutes,
                    min_liquidity_usd=self.sniper_config.min_liquidity_usd,
                )
            except Exception:
                fresh = False
            if not fresh:
                continue
            try:
                data = await asyncio.to_thread(self.feed.fetch_price, pair.mint)
                price = data["price"]
            except Exception:
                price = pair.price_usd or 0.0
            if price <= 0:
                continue
            reason = (
                f"🎯 Snipe pump.fun — lancement récent "
                f"(liq ${pair.liquidity_usd:,.0f})"
            )
            await self._open_position(
                pair, price, score=100, reason=reason, atr=0.0, is_sniper=True
            )
            logger.info("Sniper entered %s @ %.8f", pair.symbol, price)
            await self.telegram.send_alert(
                f"🎯 *SNIPE — {pair.symbol}*\n"
                f"Nouveau lancement détecté, entrée immédiate.\n"
                f"Objectif: *+20%* | Stop: *-10%*"
            )

    async def _open_position(self, pair: TokenPair, price: float, score: int, reason: str, atr: float = 0.0, is_sniper: bool = False) -> None:
        """Open a new position after risk checks.

        ATR (Average True Range) is passed through so stops/targets adapt to the
        token's actual volatility instead of using one-size-fits-all percentages.
        """
        sol_balance = self._last_sol_balance
        usdc_balance = self._last_usdc_balance
        if not self.risk.can_open_position(
            mint=pair.mint,
            symbol=pair.symbol,
            capital_sol=sol_balance,
            sol_price=price if pair.symbol == "SOL" else self._sol_price_usd,
            open_positions=len(self.risk.positions),
            usdc_balance=usdc_balance,
        ):
            reason = self.risk.last_reject_reason or "risk check failed"
            await self.telegram.send_alert(
                f"⛔ *Signal rejeté — {pair.symbol}*\n{reason}"
            )
            self._log("warn", f"Signal rejeté {pair.symbol}: {reason}")
            return

        levels = self.risk.compute_exit_levels(price, atr=atr)
        # Percentage-based sizing: position = max_position_pct of total capital,
        # capped by TRADING_AMOUNT_USD (now a max-per-trade ceiling).
        amount_usd = self.risk.compute_position_size_usd(
            capital_sol=sol_balance,
            sol_price=price if pair.symbol == "SOL" else self._sol_price_usd,
            usdc_balance=usdc_balance,
        )

        result = await asyncio.to_thread(self.solana.buy_token, pair.mint, amount_usd)
        if not result.success:
            await self.telegram.send_alert(
                f"❌ *Échec achat {pair.symbol}*\n{result.error}"
            )
            return

        # Use actual amount received for position size when available.
        position_amount = result.amount_out if result.amount_out > 0 else amount_usd / price
        self.risk.add_position(
            mint=pair.mint,
            symbol=pair.symbol,
            entry_price=price,
            amount=position_amount,
            atr=atr,
            is_sniper=is_sniper,
        )

        await self.telegram.send_trade_open(
            symbol=pair.symbol,
            side="buy",
            entry_price=price,
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            sl=levels["sl"],
            amount_usd=amount_usd,
            reason=reason,
            score=score,
        )
        # Sniper positions use a single +20%/-10% exit, not the scalper ladder —
        # send a clear strategy message so the TP/SL shown match reality.
        if is_sniper:
            await self.telegram.send_alert(
                f"🎯 *Stratégie Sniper — {pair.symbol}*\n"
                f"Entrée: `{price:.8f}`\n"
                f"🎯 Take-profit: *+{self.sniper_config.take_profit_pct*100:.0f}%* "
                f"(`{price * (1 + self.sniper_config.take_profit_pct):.8f}`)\n"
                f"🛡️ Stop-loss: *-{self.sniper_config.stop_loss_pct*100:.0f}%* "
                f"(`{price * (1 - self.sniper_config.stop_loss_pct):.8f}`)\n"
                f"💵 Montant: `${amount_usd:.2f}`"
            )
        msg = (
            f"Opened {pair.symbol} @ {price:.6f} | score={score} | "
            f"paper={self.config.paper_trading}"
        )
        logger.info(msg)
        self._log("buy", msg)
        self.telegram_messages.append(
            {"type": "buy", "text": f"🟢 Achat {pair.symbol} @ ${price:.6f} | score {score}"}
        )

    @property
    def _sol_price_usd(self) -> float:
        """Cached SOL price in USD; updated during trading cycles only."""
        return self._cached_sol_price_usd

    def _update_cached_sol_price(self) -> None:
        """Refresh cached SOL price from the feed (call only in cycles/threads)."""
        price = self.feed.fetch_sol_price()
        if price:
            self._cached_sol_price_usd = float(price)

    async def _manage_open_positions(self) -> None:
        """Check TP/SL for existing positions."""
        for mint, position in list(self.risk.positions.items()):
            try:
                pair = next(p for p in self.watchlist if p.mint == mint)
            except StopIteration:
                continue

            try:
                data = await asyncio.to_thread(self.feed.fetch_price, mint)
            except Exception as exc:
                logger.warning("Cannot fetch price for %s: %s", position.symbol, exc)
                continue

            current_price = data["price"]
            decision = self.risk.check_exit(mint, current_price)

            if decision["action"] == "close":
                result = await asyncio.to_thread(
                    self.solana.sell_token, mint, position.amount * position.entry_price
                )
                pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
                pnl_usd = decision["pnl_usd"]
                duration = int((datetime.now(timezone.utc) - position.opened_at).total_seconds() / 60)
                await self.telegram.send_trade_close(
                    symbol=position.symbol,
                    entry_price=position.entry_price,
                    exit_price=current_price,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                    reason=decision["reason"],
                    duration_min=duration,
                )
                if not result.success:
                    await self.telegram.send_alert(
                        f"⚠️ Vente {position.symbol} échouée: {result.error}"
                    )

            elif decision["action"] == "partial":
                portion = decision.get("portion", 0.3)
                sell_amount_usd = position.amount * position.entry_price * portion
                await asyncio.to_thread(self.solana.sell_token, mint, sell_amount_usd)
                await self.telegram.send_alert(
                    f"🔒 *Partiel {position.symbol}* {decision['level'].upper()}\n"
                    f"Prix: `{current_price:.6f}` | Portion: {portion*100:.0f}%"
                )

    async def _check_balances(self) -> None:
        """Alert on low balances and stop trading if critical.

        The two RPC calls (SOL + USDC) run in parallel to halve latency.
        Also updates cached balance/PnL values for the dashboard.
        """
        sol_balance, usdc_balance = await asyncio.gather(
            asyncio.to_thread(self.solana.get_balance),
            asyncio.to_thread(self.solana.get_token_balance, self.solana.USDC_MINT),
            return_exceptions=True,
        )
        if isinstance(sol_balance, Exception):
            logger.warning("Balance check SOL failed: %s", sol_balance)
            sol_balance = self._last_sol_balance
        else:
            self._last_sol_balance = sol_balance
        if isinstance(usdc_balance, Exception):
            logger.warning("Balance check USDC failed: %s", usdc_balance)
            usdc_balance = self._last_usdc_balance
        else:
            self._last_usdc_balance = usdc_balance
        await asyncio.to_thread(self._update_cached_sol_price)

        if sol_balance < self.config.min_sol_balance_for_fees:
            if self._can_alert("low_sol"):
                await self.telegram.send_alert(
                    f"🚨 *SOL très faible*\n"
                    f"Solde: `{sol_balance:.4f} SOL`\n"
                    f"Minimum requis: `{self.config.min_sol_balance_for_fees} SOL`\n"
                    f"Recharge rapidement ou les swaps vont échouer."
                )

        if usdc_balance < self.config.min_usdc_balance_to_trade:
            if self._can_alert("low_usdc"):
                await self.telegram.send_alert(
                    f"🚨 *USDC insuffisant*\n"
                    f"Solde: `${usdc_balance:.2f}`\n"
                    f"Minimum pour trader: `${self.config.min_usdc_balance_to_trade}`\n"
                    f"Le bot ne peut plus ouvrir de positions."
                )

    async def _send_status_report(self) -> None:
        """Send a full status snapshot to Telegram: balances, positions, PnL, watchlist.

        Used as a debug/monitoring aid: gives a complete picture of the bot state
        on every cycle so you can watch it live without opening the dashboard.
        """
        perf = self.risk.performance_stats()
        day_pnl = -self.risk.daily_loss_usd

        # Open positions with live unrealized PnL.
        pos_lines: list[str] = []
        for pos in self.risk.positions.values():
            try:
                data = await asyncio.to_thread(self.feed.fetch_price, pos.mint)
                cur = data["price"]
            except Exception:
                cur = pos.entry_price
            pnl = pos.pnl_pct(cur)
            pos_lines.append(
                f"• *{pos.symbol}* entry `{pos.entry_price:.6f}` now `{cur:.6f}` "
                f"({pnl:+.1f}%)"
            )
        positions_txt = "\n".join(pos_lines) if pos_lines else "_Aucune position ouverte_"

        # Watchlist with latest signals.
        wl_lines: list[str] = []
        for p in self.watchlist[:10]:
            ev = self.recent_evaluations.get(p.mint, {})
            sig = ev.get("signal", "hold").upper()
            score = ev.get("score", 0)
            wl_lines.append(f"• {p.symbol}: {sig} ({score:+d})")
        watchlist_txt = "\n".join(wl_lines) if wl_lines else "_Watchlist vide_"

        msg = (
            f"📟 *Rapport d'état — cycle #{self._cycle_count}*\n\n"
            f"💰 *Soldes*\n"
            f"SOL: `{self._last_sol_balance:.4f}` | USDC: `{self._last_usdc_balance:.2f}$`\n"
            f"SOL/USD: `{self._cached_sol_price_usd:.2f}$`\n\n"
            f"📊 *PnL*\n"
            f"Jour: `{day_pnl:+.2f}$` | Total: `{perf['total_pnl_usd']:+.2f}$`\n"
            f"Trades: `{perf['trades']}` | Win rate: `{perf['win_rate']:.0f}%` | "
            f"PF: `{perf['profit_factor']}`\n\n"
            f"📂 *Positions ({len(self.risk.positions)}/5)*\n{positions_txt}\n\n"
            f"🔭 *Watchlist ({len(self.watchlist)})*\n{watchlist_txt}"
        )
        await self.telegram.send_alert(msg)

    def _can_alert(self, alert_type: str) -> bool:
        """Throttle repeated Telegram alerts."""
        now = datetime.now(timezone.utc)
        last = self._last_alert.get(alert_type)
        cooldown = timedelta(minutes=self.config.balance_alert_cooldown_minutes)
        if last is None or now - last >= cooldown:
            self._last_alert[alert_type] = now
            postgres_store.save_alert_cooldown(alert_type, now)
            return True
        return False

    async def run_cycle(self) -> None:
        """Run one full scan/evaluate/trade cycle."""
        if self._running_cycle:
            logger.warning("Previous cycle still running, skipping this tick")
            return
        self._running_cycle = True
        evaluated = 0
        cycle_start = time.monotonic()
        try:
            logger.info("--- Starting trading cycle ---")
            self._log("info", "Cycle de trading démarré")
            await self._check_balances()
            await self._manage_open_positions()
            # Snipe any freshly-launched pump.fun tokens before the slower
            # indicator-based evaluation — launch momentum is time-sensitive.
            try:
                await self._snipe_new_tokens()
            except Exception as exc:
                logger.warning("Sniper pass failed: %s", exc)

            # Optional paper test: force one fake trade on the very first cycle
            # to validate the full pipeline (signal -> execution -> Telegram -> dashboard).
            if self.config.force_paper_test_trade and not self._paper_test_done:
                self._paper_test_done = True
                first = next((p for p in self.watchlist if p.symbol == "SOL"), self.watchlist[0])
                if first.mint not in self.risk.positions:
                    try:
                        data = await asyncio.to_thread(self.feed.fetch_price, first.mint)
                        price = data["price"]
                    except Exception as exc:
                        logger.warning("Force paper test fetch failed: %s", exc)
                        price = first.price_usd or 0.01
                    await self._open_position(first, price, 999, "TEST PAPER — pipeline validation")
                    self._log("buy", f"TEST PAPER: opened {first.symbol} @ {price}")
                    logger.info("Force paper test trade executed for %s", first.symbol)

            # Evaluate at most N pairs per cycle (round-robin) so a slow price
            # fetch on one token cannot stall the whole cycle / dashboard.
            max_per_cycle = max(1, self.config.max_pairs_per_cycle)
            start_idx = self._cycle_count % max(1, len(self.watchlist))
            ordered = self.watchlist[start_idx:] + self.watchlist[:start_idx]
            to_eval = [p for p in ordered if p.mint not in self.risk.positions][:max_per_cycle]

            # Snapshot ONLY the pairs we're about to evaluate. The background
            # 60s sampler already keeps all watchlist candles fresh, so there's
            # no need to fetch every mint here — that's what made cycles take 250s.
            eval_mints = [p.mint for p in to_eval]
            if eval_mints:
                await asyncio.to_thread(self.feed.snapshot_batch, eval_mints)

            for pair in to_eval:
                if pair.mint in self.risk.positions:
                    continue  # Already in a position.

                candles = self.feed.get_candles(pair.mint)
                evaluation = await self._evaluate_pair(pair, candles)
                evaluated += 1
                if not evaluation:
                    continue

                signal = evaluation["signal"]
                score = evaluation["score"]
                details = evaluation["details"]
                price = evaluation["price"]

                # Buy threshold lowered to 40: in a bearish/neutral market a 50+
                # score is rare, so the bot almost never traded. 40 still requires
                # multiple confirming indicators but catches real opportunities.
                if signal == "buy" and score >= 40:
                    reason = details.get("reason", "signal technique")
                    await self._open_position(pair, price, score, reason, atr=details.get("atr", 0.0))

            logger.info("--- Trading cycle complete (%s pairs evaluated) ---", evaluated)
            self._log("info", f"Cycle terminé ({evaluated} paires évaluées)")
        finally:
            self._running_cycle = False
            elapsed = time.monotonic() - cycle_start
            logger.info("Trading cycle elapsed %.2fs", elapsed)
            if elapsed > 60:
                logger.warning("Trading cycle was slow (%.2fs); dashboard/health may have stalled", elapsed)

    def _clean_state_value(self, value: Any) -> Any:
        """Replace NaN/inf with None so JSON serialization works."""
        import math

        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return value
        if isinstance(value, dict):
            return {k: self._clean_state_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clean_state_value(v) for v in value]
        return value

    def get_state(self) -> dict[str, Any]:
        """Return current engine state for the dashboard API.

        This method must stay non-blocking. All live RPC/price calls happen
        inside the trading cycle and their latest values are cached. Falling
        back to cached values avoids freezing the dashboard when Solana/RPC is
        slow or DexScreener times out.
        """
        # Use cached balances from _check_balances; do not call RPC here.
        sol_balance = self._last_sol_balance
        usdc_balance = self._last_usdc_balance

        state = {
            "paper_trading": self.config.paper_trading,
            "sol_balance": sol_balance,
            "usdc_balance": usdc_balance,
            "sol_price_usd": self._sol_price_usd,
            "watchlist_count": len(self.watchlist),
            "open_positions": len(self.risk.positions),
            "daily_pnl": -self.risk.daily_loss_usd,
            "unrealized_pnl": 0.0,
            # Absolute unix timestamp so the dashboard can compute the real
            # remaining time at every poll (no drifting local countdown).
            "next_scan_ts": int(self._next_cycle_at),
            "next_scan_seconds": max(0, int(self._next_cycle_at - time.time())),
            "new_tokens": getattr(self.scanner, "new_tokens", [])[-10:],
            "scanner": {
                "discovery_enabled": self.scanner.discovery_enabled,
                "microcap_enabled": self.scanner.microcap_enabled,
                "discovery_max_age_hours": self.scanner.discovery_max_age_hours,
                "discovery_min_score": self.scanner.discovery_min_score,
            },
            "watchlist": [
                {
                    "symbol": p.symbol,
                    "mint": p.mint,
                    "price": p.price_usd,
                    "market_cap": p.market_cap,
                    "liquidity": p.liquidity_usd,
                    "volume_24h": p.volume_24h,
                    "price_change_24h": p.price_change_24h,
                    "txn_count_24h": p.txn_count_24h,
                    "sources": p.source_endpoints,
                    "boost_amount": p.boost_amount,
                    "discovery_score": self.scanner._score_token(p),
                    "signal": self.recent_evaluations.get(p.mint, {}).get("signal", "hold"),
                    "score": self.recent_evaluations.get(p.mint, {}).get("score", 0),
                    "adx": self.recent_evaluations.get(p.mint, {}).get("adx"),
                    "volume_spike": self.recent_evaluations.get(p.mint, {}).get("volume_spike"),
                }
                for p in self.watchlist
            ],
            "positions": [
                {
                    "symbol": p.symbol,
                    "entry": p.entry_price,
                    "amount": p.amount,
                    "tp1": p.tp1,
                    "tp2": p.tp2,
                    "tp3": p.tp3,
                    "sl": p.sl,
                    "trailing_stop": p.trailing_stop,
                }
                for p in self.risk.positions.values()
            ],
            "evaluations": self.recent_evaluations,
            "trade_history": self.risk.trade_history[-20:],
            "performance": self.risk.performance_stats(),
            "logs": self.logs[-50:],
            "telegram": self.telegram_messages[-20:],
            "groq_reports": self.groq_reports[-5:],
            "piranewz_configured": bool(self.config.piranewz_api_url) or self.piranewz_reader.enabled or True,
            "piranewz_sentiment": (
                self.piranewz_reader.get_sentiment_summary(hours=24)
                if self.piranewz_reader.enabled
                else self.piranewz_scraper.get_sentiment_summary(hours=24)
            ),
            "manual_signals_enabled": self.config.manual_signal_enabled,
            "manual_signals": self.manual_signals[-10:],
        }
        return self._clean_state_value(state)

    async def loop(self, scan_interval_minutes: int = 2) -> None:
        """Run trading cycles until stopped."""
        while self.running:
            self._cycle_count += 1
            logger.info("Loop iteration #%s", self._cycle_count)
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.exception("Trading cycle error: %s", exc)
                try:
                    await self.telegram.send_alert(f"⚠️ Erreur cycle: {exc}")
                except Exception:
                    pass
            # Every 10 cycles (10 minutes at 1m interval), generate a Groq report.
            if self._cycle_count % 10 == 0 and self._cycle_count != self._last_groq_report_cycle:
                self._last_groq_report_cycle = self._cycle_count
                await self._run_groq_report()

            # Heartbeat every 6 cycles (~30 min): confirm the bot is alive even
            # when no trade/report fires, so Telegram never looks 'dead'.
            if self._cycle_count % 6 == 0 and self._can_alert("heartbeat"):
                perf = self.risk.performance_stats()
                await self.telegram.send_alert(
                    f"💓 *Bot actif* — cycle #{self._cycle_count}\n"
                    f"Positions: {len(self.risk.positions)}/5 | "
                    f"Trades: {perf['trades']} | Win rate: {perf['win_rate']:.0f}%\n"
                    f"PnL jour: {'+' if -self.risk.daily_loss_usd >= 0 else ''}{-self.risk.daily_loss_usd:.2f}$"
                )

            # Full status report every cycle when debug monitoring is enabled.
            if self.config.status_report_every_cycle:
                await self._send_status_report()

            # Sleep in small chunks so stop_event remains responsive and we log liveness.
            total_sleep = scan_interval_minutes * 60
            self._next_cycle_at = time.time() + total_sleep
            chunk = 10
            for _ in range(total_sleep // chunk):
                if not self.running:
                    break
                await asyncio.sleep(chunk)

            # Refresh watchlist every 30 minutes to reduce DexScreener calls.
            if self._cycle_count % 30 == 0:
                await self._refresh_watchlist()
                postgres_store.prune_snapshots(max_age_days=3)
