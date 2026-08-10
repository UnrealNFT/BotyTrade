"""Main trading loop: scan, sample prices, generate signals, execute trades."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from aioud.connectors.solana import SolanaConnector
from aioud.data.prices import PriceFeed
from aioud.data.scanner import SolanaScanner, TokenPair
from aioud.data.watchlist_store import WatchlistStore
from aioud.risk.manager import RiskManager
from aioud.strategies.scalper import SolanaScalper
from aioud.telegram.bot import TelegramBot
from aioud.utils.config import Config
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


class TradingEngine:
    """Autonomous trading engine for Solana tokens."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.telegram = TelegramBot(config)
        self.solana = SolanaConnector(config)
        self.risk = RiskManager(config)
        self.scanner = SolanaScanner(
            min_liquidity_usd=50_000.0,
            min_volume_24h=10_000.0,
            max_pairs_per_scan=30,
            microcap_enabled=True,
            microcap_max_mcap=500_000.0,
            microcap_min_liquidity_usd=10_000.0,
            microcap_min_volume_24h=5_000.0,
            microcap_max_pairs=20,
            discovery_enabled=True,
            discovery_max_age_hours=48.0,
            discovery_min_score=10.0,
            max_workers=8,
        )
        self.feed = PriceFeed()
        self.watchlist_store = WatchlistStore()
        self.watchlist: list[TokenPair] = self.watchlist_store.load()
        self.strategies: dict[str, SolanaScalper] = {
            pair.mint: SolanaScalper(symbol=f"{pair.symbol}/{pair.quote_symbol}")
            for pair in self.watchlist
        }
        self._removed_mints: set[str] = set()
        self.running = False
        self.recent_evaluations: dict[str, dict[str, Any]] = {}
        self.logs: list[dict[str, Any]] = []
        self.telegram_messages: list[dict[str, str]] = []
        self._paper_test_done = False
        self._last_alert: dict[str, datetime] = {}
        self._last_sol_balance: float = 0.0
        self._last_usdc_balance: float = 0.0
        self._cached_sol_price_usd: float = 77.0

    async def start(self) -> None:
        """Start Telegram bot and initialize watchlist in the background."""
        self.telegram._set_engine(self)
        await self.telegram.start()
        self.running = True
        # Start watchlist refresh in background so Telegram/dashboard stay responsive.
        if self.watchlist:
            logger.info("Loaded %s persisted watchlist pairs", len(self.watchlist))
        asyncio.create_task(self._refresh_watchlist())
        await self.telegram.send_alert(
            f"⚡ *AIOud Trader* démarré.\nMode: *{'PAPER' if self.config.paper_trading else 'REAL'}*\n"
            "Scan multi-actifs Solana actif."
        )

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        self.running = False
        await self.telegram.send_alert("🛑 *AIOud Trader* arrêté.")
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
            # Merge persisted + scanned, preserve manually added pairs, honor removals.
            merged: dict[str, TokenPair] = {p.mint: p for p in self.watchlist if p.mint not in self._removed_mints}
            for pair in scanned:
                if pair.mint not in self._removed_mints and pair.mint not in merged:
                    merged[pair.mint] = pair
            self.watchlist = sorted(
                merged.values(),
                key=lambda p: (self.scanner._score_token(p), p.volume_24h),
                reverse=True,
            )[: self.scanner.max_pairs_per_scan]
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
            self.watchlist = sorted(
                self.watchlist,
                key=lambda p: (self.scanner._score_token(p), p.volume_24h),
                reverse=True,
            )[: self.scanner.max_pairs_per_scan]
            self.watchlist_store.save(self.watchlist)
            logger.info("Manually added pair: %s (%s)", pair.symbol, pair.mint)
        return pair

    def remove_pair(self, mint: str) -> bool:
        """Remove a token by mint address."""
        before = len(self.watchlist)
        self.watchlist = [p for p in self.watchlist if p.mint != mint]
        self.strategies.pop(mint, None)
        self._removed_mints.add(mint)
        self.watchlist_store.save(self.watchlist)
        removed = len(self.watchlist) < before or self.watchlist_store.remove(mint)
        if removed:
            logger.info("Removed pair: %s", mint)
        return removed

    async def _evaluate_pair(self, pair: TokenPair) -> dict[str, Any] | None:
        """Update candles and check for a trading signal."""
        try:
            candles = await asyncio.to_thread(self.feed.snapshot_and_build, pair.mint)
            min_candles = SolanaScalper.MIN_CANDLES
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

    async def _open_position(self, pair: TokenPair, price: float, score: int, reason: str) -> None:
        """Open a new position after risk checks."""
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
            await self.telegram.send_alert(
                f"⛔ *Signal rejeté — {pair.symbol}*\nRisk check failed (voir logs)."
            )
            return

        levels = self.risk.compute_exit_levels(price)
        amount_usd = self.config.trading_amount_usd

        result = self.solana.buy_token(pair.mint, amount_usd)
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
        sol_mint = "So11111111111111111111111111111111111111112"
        try:
            data = self.feed.fetch_price(sol_mint)
            price = data.get("price")
            if price:
                self._cached_sol_price_usd = float(price)
        except Exception:
            pass

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
                result = self.solana.sell_token(mint, position.amount * position.entry_price)
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
                self.solana.sell_token(mint, sell_amount_usd)
                await self.telegram.send_alert(
                    f"🔒 *Partiel {position.symbol}* {decision['level'].upper()}\n"
                    f"Prix: `{current_price:.6f}` | Portion: {portion*100:.0f}%"
                )

    async def _check_balances(self) -> None:
        """Alert on low balances and stop trading if critical.

        Also updates cached balance/PnL values for the dashboard.
        """
        now = datetime.now(timezone.utc)
        try:
            sol_balance = await asyncio.to_thread(self.solana.get_balance)
            self._last_sol_balance = sol_balance
        except Exception as exc:
            logger.warning("Balance check SOL failed: %s", exc)
            sol_balance = self._last_sol_balance
        try:
            usdc_balance = await asyncio.to_thread(self.solana.get_token_balance, self.solana.USDC_MINT)
            self._last_usdc_balance = usdc_balance
        except Exception as exc:
            logger.warning("Balance check USDC failed: %s", exc)
            usdc_balance = self._last_usdc_balance
        self._update_cached_sol_price()

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

    def _can_alert(self, alert_type: str) -> bool:
        """Throttle repeated Telegram alerts."""
        now = datetime.now(timezone.utc)
        last = self._last_alert.get(alert_type)
        cooldown = timedelta(minutes=self.config.balance_alert_cooldown_minutes)
        if last is None or now - last >= cooldown:
            self._last_alert[alert_type] = now
            return True
        return False

    async def run_cycle(self) -> None:
        """Run one full scan/evaluate/trade cycle."""
        logger.info("--- Starting trading cycle ---")
        self._log("info", "Cycle de trading démarré")
        await self._check_balances()
        await self._manage_open_positions()

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

        evaluated = 0
        for pair in self.watchlist:
            if pair.mint in self.risk.positions:
                continue  # Already in a position.

            evaluation = await self._evaluate_pair(pair)
            evaluated += 1
            if not evaluation:
                continue

            signal = evaluation["signal"]
            score = evaluation["score"]
            details = evaluation["details"]
            price = evaluation["price"]

            if signal == "buy" and score >= 50:
                reason = details.get("reason", "signal technique")
                await self._open_position(pair, price, score, reason)

        logger.info("--- Trading cycle complete (%s pairs evaluated) ---", evaluated)
        self._log("info", f"Cycle terminé ({evaluated} paires évaluées)")

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
            "next_scan_seconds": 120,
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
            "logs": self.logs[-50:],
            "telegram": self.telegram_messages[-20:],
        }
        return self._clean_state_value(state)

    async def loop(self, scan_interval_minutes: int = 2) -> None:
        """Run trading cycles until stopped."""
        cycle_count = 0
        while self.running:
            cycle_count += 1
            logger.info("Loop iteration #%s", cycle_count)
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.exception("Trading cycle error: %s", exc)
                await self.telegram.send_alert(f"⚠️ Erreur cycle: {exc}")

            # Sleep in small chunks so stop_event remains responsive and we log liveness.
            total_sleep = scan_interval_minutes * 60
            chunk = 10
            for _ in range(total_sleep // chunk):
                if not self.running:
                    break
                await asyncio.sleep(chunk)

            # Refresh watchlist every 12 minutes to discover new tokens faster.
            if cycle_count % 6 == 0:
                await self._refresh_watchlist()
