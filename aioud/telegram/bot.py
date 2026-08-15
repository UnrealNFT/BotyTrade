"""Telegram bot for AIOud Trader."""

import asyncio
import os
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from aioud.utils.config import Config
from aioud.utils import postgres_store
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


class TelegramBot:
    """Wraps python-telegram-bot Application for AIOud Trader."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.enabled = bool(config.telegram_bot_token)
        self.application: Application | None = None
        self._lock_conn: Any = None          # Postgres advisory lock connection
        self._polling_active: bool = False
        self._leadership_task: asyncio.Task | None = None
        if self.enabled:
            # Safety check: never post AIOud alerts into the Piranewz channel.
            piranewz_channel = (config.piranewz_channel or "").lstrip("@").lower()
            chat_id = str(config.telegram_chat_id or "").lstrip("@").lower()
            if piranewz_channel and chat_id == piranewz_channel:
                logger.error(
                    "TELEGRAM_CHAT_ID (%s) is the same as PIRANEWZ_CHANNEL. "
                    "AIOud alerts must go to a separate chat/group, not Piranewz.",
                    config.telegram_chat_id,
                )
                self.enabled = False
            else:
                self.application = Application.builder().token(config.telegram_bot_token).build()
                self._register_handlers()
                logger.info(
                    "Telegram bot configured to post to chat_id=%s",
                    config.telegram_chat_id,
                )
                if config.piranewz_channel:
                    logger.info(
                        "Piranewz reader channel=%s (must differ from TELEGRAM_CHAT_ID)",
                        config.piranewz_channel,
                    )
        else:
            logger.warning("TELEGRAM_BOT_TOKEN not set; Telegram bot is disabled.")

    def _is_authorized(self, update: Update) -> bool:
        """Only allow the configured Telegram username to control the bot."""
        if not self.config.telegram_authorized_username:
            return True
        user = update.effective_user
        if not user or not user.username:
            return False
        return user.username.lower() == self.config.telegram_authorized_username.lower()

    def _register_handlers(self) -> None:
        for cmd, handler in {
            "start": self._cmd_start,
            "status": self._cmd_status,
            "pnl": self._cmd_pnl,
            "positions": self._cmd_positions,
            "panic": self._cmd_panic,
            "balance": self._cmd_balance,
            "add": self._cmd_add,
            "remove": self._cmd_remove,
            "close": self._cmd_close,
            "mode1": self._cmd_mode1,
            "mode2": self._cmd_mode2,
            "buy": self._cmd_buy,
            "sweep": self._cmd_sweep,
            "watchlist": self._cmd_watchlist,
            "chatid": self._cmd_chatid,
        }.items():
            if self.application:
                self.application.add_handler(CommandHandler(cmd, handler))

    async def _reply(self, update: Update, text: str, **kwargs) -> None:
        """Safely reply to a Telegram message.

        Falls back to plain text if Markdown parsing fails (error messages often
        contain raw RPC/program logs with unescaped Markdown chars like _ or [).
        """
        message = update.message
        if not message:
            logger.warning("Cannot reply: update.message is None")
            return
        try:
            await message.reply_text(text, parse_mode="Markdown", **kwargs)
        except Exception as exc:
            logger.warning("Markdown reply failed (%s); retrying as plain text", exc)
            try:
                await message.reply_text(text, **kwargs)
            except Exception as exc2:
                logger.warning("Plain reply also failed: %s", exc2)

    async def _deny(self, update: Update) -> None:
        user = update.effective_user
        name = f"@{user.username}" if user and user.username else "inconnu"
        logger.warning("Unauthorized Telegram access from %s", name)
        await self._reply(update, "🚫 Accès refusé. Ce bot est privé.")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        await self._reply(
            update,
            "⚡ *AIOud Trader* est en ligne.\n"
            "Commandes: /status /balance /pnl /positions /close /panic\n"
            "/mode1 /mode2 /buy /sweep /add /remove /watchlist /chatid",
        )

    async def _cmd_mode1(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch to Mode 1: multi-TP ladder, ATR stops, percentage sizing."""
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        engine.trading_mode = 1
        engine.risk.trading_mode = 1
        await self._reply(
            update,
            "📈 *Mode 1 activé*\n"
            "Sizing: % du capital | Sorties: ladder TP1/2/3 + break-even + trailing",
        )

    async def _cmd_mode2(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch to Mode 2: flat $5 positions, single +5% TP / -$0.10 SL, 1h max."""
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        engine.trading_mode = 2
        engine.risk.trading_mode = 2
        c = engine.config
        await self._reply(
            update,
            f"⚡ *Mode 2 activé*\n"
            f"Position: `${c.mode2_position_usd:.0f}` fixe | "
            f"TP: `+{c.mode2_tp_pct*100:.0f}%` | SL: `-${c.mode2_sl_usd:.2f}`\n"
            f"⏱️ Time-stop: `{c.mode2_max_hold_minutes:.0f}min` max par position (auto-close)",
        )

    async def _cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Buy a token by contract address: /buy <mint>"""
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        args = (context.args or [])
        if not args:
            await self._reply(update, "Usage: `/buy <adresse_contrat>`")
            return
        mint = args[0].strip()
        await self._reply(update, f"🔍 Recherche du token `{mint[:12]}...`")
        try:
            ok = await engine.buy_by_contract(mint)
            if not ok:
                await self._reply(update, "❌ Token introuvable ou achat impossible.")
        except Exception as exc:
            await self._reply(update, f"⚠️ Erreur achat: {str(exc)[:200]}")

    async def _cmd_sweep(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Sweep token profits to SOL, keep only SOL + USDC in the wallet."""
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        await self._reply(update, "🧹 Sweep des profits vers SOL en cours...")
        try:
            n = await engine.sweep_profits_to_sol()
            await self._reply(update, f"✅ Sweep terminé: {n} token(s) convertis en SOL.")
        except Exception as exc:
            await self._reply(update, f"⚠️ Erreur sweep: {str(exc)[:200]}")

    def _set_engine(self, engine) -> None:
        """Allow commands to query the live trading engine."""
        self.engine = engine

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        mode = "PAPER" if self.config.paper_trading else "REAL"
        engine = getattr(self, "engine", None)
        open_count = len(engine.risk.positions) if engine else 0
        await self._reply(
            update,
            f"🤖 Bot actif.\n"
            f"Mode: *{mode} TRADING*\n"
            f"Positions ouvertes: *{open_count}*\n"
            f"Scan en cours sur Solana.",
        )

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "📊 PnL non disponible pour le moment.")
            return
        state = engine.get_state()
        daily_pnl = state.get("daily_pnl", 0.0) or 0.0
        unrealized = state.get("unrealized_pnl", 0.0) or 0.0
        await self._reply(
            update,
            f"📊 *PnL*\n"
            f"Journalier: `${daily_pnl:+.2f}`\n"
            f"Latent: `${unrealized:+.2f}`",
        )

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine or not engine.risk.positions:
            await self._reply(update, "📈 Aucune position ouverte.")
            return
        lines = ["📈 *Positions ouvertes*"]
        for mint, pos in engine.risk.positions.items():
            try:
                price_data = await asyncio.to_thread(engine.feed.fetch_price, mint)
                price = price_data["price"]
                pnl = pos.pnl_pct(price)
            except Exception:
                price = pos.entry_price
                pnl = 0.0
            lines.append(
                f"• *{pos.symbol}* @ `{pos.entry_price:.8f}` | "
                f"actuel `{price:.8f}` | PnL `{pnl:+.2f}%`"
            )
        await self._reply(update, "\n".join(lines))

    async def _cmd_panic(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        count = len(engine.risk.positions)
        if count == 0:
            await self._reply(update, "ℹ️ Aucune position ouverte à fermer.")
            return
        await self._reply(update, f"🛑 *PANIC* — fermeture de {count} position(s) en cours...")
        try:
            closed = await engine.close_all_positions()
            await self._reply(
                update,
                f"✅ *Panic terminé* — {closed}/{count} position(s) fermée(s).\n"
                f"🖥️ [Dashboard]({self._dashboard_url()})",
            )
        except Exception as exc:
            await self._reply(update, f"⚠️ Erreur panic: {exc}")

    async def _cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Close a single position by symbol: /close HAIRINU"""
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        args = (context.args or [])
        if not args:
            open_syms = ", ".join(p.symbol for p in engine.risk.positions.values()) or "aucune"
            await self._reply(
                update,
                f"Usage: `/close SYMBOLE`\nPositions ouvertes: {open_syms}",
            )
            return
        symbol = args[0].upper()
        await self._reply(update, f"🔒 Fermeture de *{symbol}* en cours...")
        try:
            trade = await engine.close_position_by_symbol(symbol)
            if trade:
                # send_trade_close already fired inside close_position_by_symbol.
                pass
            else:
                await self._reply(update, f"ℹ️ Aucune position ouverte sur *{symbol}*.")
        except Exception as exc:
            # Truncate + plain text: error payloads (RPC logs) break Markdown.
            short = str(exc)[:300]
            await self._reply(update, f"⚠️ Erreur fermeture {symbol}: {short}")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "💰 Solde non disponible pour le moment.")
            return
        sol = engine._last_sol_balance
        usdc = engine._last_usdc_balance
        await self._reply(
            update,
            f"💰 *Soldes wallet*\n"
            f"SOL: `{sol:.4f}`\n"
            f"USDC: `${usdc:.2f}`",
        )

    async def _cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        args = context.args
        if not args:
            await self._reply(update, "Usage: `/add <mint_address>`")
            return
        mint = args[0].strip()
        pair = await asyncio.to_thread(engine.add_pair, mint)
        if not pair:
            await self._reply(update, f"❌ Impossible de trouver le token: `{mint}`")
            return
        await self._reply(
            update,
            f"✅ *{pair.symbol}* ajouté à la watchlist\n"
            f"Mint: `{mint}`\n"
            f"Prix: `${pair.price_usd:.8f}`",
        )

    async def _cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        args = context.args
        if not args:
            await self._reply(update, "Usage: `/remove <mint_address>`")
            return
        mint = args[0].strip()
        if engine.remove_pair(mint):
            await self._reply(update, f"✅ Token retiré de la watchlist: `{mint}`")
        else:
            await self._reply(update, f"ℹ️ Token non trouvé dans la watchlist: `{mint}`")

    async def _cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await self._reply(update, "🤖 Bot pas encore prêt.")
            return
        pairs = engine.watchlist[:20]
        if not pairs:
            await self._reply(update, "📋 Watchlist vide.")
            return
        lines = [f"📋 *Watchlist ({len(pairs)})*"]
        for p in pairs:
            lines.append(f"• *{p.symbol}* `{p.mint}`")
        await self._reply(update, "\n".join(lines))

    async def _cmd_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Return the current chat ID so the user can configure TELEGRAM_CHAT_ID."""
        if not self._is_authorized(update):
            await self._deny(update)
            return
        chat_id = update.effective_chat.id if update.effective_chat else "inconnu"
        await self._reply(
            update,
            f"🆔 *Chat ID*: `{chat_id}`\n\n"
            f"Utilise cette valeur pour `TELEGRAM_CHAT_ID` dans Render.",
        )

    def _dashboard_url(self) -> str:
        return os.environ.get("DASHBOARD_URL", "http://localhost:8080/mockup.html")

    async def send_trade_open(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        tp1: float,
        tp2: float,
        tp3: float,
        sl: float,
        amount_usd: float,
        reason: str,
        score: int,
    ) -> None:
        """Notify the group that a trade was opened."""
        if not self.enabled or not self.application:
            return
        emoji = "🟢" if side == "buy" else "🔴"
        text = (
            f"{emoji} *NOUVEAU TRADE — {side.upper()} {symbol}*\n\n"
            f"📊 Signal: {reason}\n"
            f"⭐ Score: {score}/100\n"
            f"💰 Entrée: `{entry_price:.6f}`\n"
            f"🎯 TP1: +{((tp1 / entry_price - 1) * 100):.1f}%\n"
            f"🎯 TP2: +{((tp2 / entry_price - 1) * 100):.1f}%\n"
            f"🎯 TP3: +{((tp3 / entry_price - 1) * 100):.1f}%\n"
            f"🛡️ SL: -{((1 - sl / entry_price) * 100):.1f}%\n"
            f"💵 Montant: ${amount_usd:.2f}\n"
            f"⏱️ Stratégie: Scalping 5m\n"
            f"🖥️ [Dashboard]({self._dashboard_url()})"
        )
        try:
            await self.application.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to send Telegram trade open: %s", exc)

    async def send_trade_close(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        pnl_pct: float,
        pnl_usd: float,
        reason: str,
        duration_min: int = 0,
    ) -> None:
        """Notify the group that a trade was closed."""
        if not self.enabled or not self.application:
            return
        emoji = "🟢" if pnl_usd >= 0 else "🔴"
        text = (
            f"{emoji} *TRADE CLOS — {symbol}*\n\n"
            f"💰 Entrée: `{entry_price:.6f}`\n"
            f"💰 Sortie: `{exit_price:.6f}`\n"
            f"📊 PnL: `{pnl_pct:+.2f}%` / `${pnl_usd:+.2f}`\n"
            f"⏱️ Durée: `{duration_min} min`\n"
            f"📝 Raison: `{reason}`"
        )
        try:
            await self.application.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to send Telegram trade close: %s", exc)

    async def send_alert(self, message: str) -> None:
        """Send a generic alert, logging failures instead of crashing."""
        if not self.enabled or not self.application or not self.application.bot:
            logger.info("Telegram alert (bot disabled): %s", message)
            return
        text = message + f"\n🖥️ [Dashboard]({self._dashboard_url()})"
        try:
            await self.application.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to send Telegram alert: %s", exc)

    async def send_manual_signal(self, signal) -> None:
        """Broadcast a manual CEX signal to Telegram."""
        if not self.enabled or not self.application:
            return
        emoji = "🟢" if signal.confidence >= 70 else "🟡"
        text = (
            f"{emoji} *SIGNAL MANUEL — {signal.symbol}*\n\n"
            f"💰 Prix actuel: `{signal.current_price:.8f}`\n"
            f"🎯 Buy limit: `{signal.buy_limit:.8f}`\n"
            f"🎯 TP1: `{signal.tp1:.8f}`\n"
            f"🎯 TP2: `{signal.tp2:.8f}`\n"
            f"🎯 TP3: `{signal.tp3:.8f}`\n"
            f"🛡️ SL: `{signal.sl:.8f}`\n"
            f"⭐ Confiance: `{signal.confidence}%`\n"
            f"📊 Sentiment: `{signal.sentiment}`\n"
            f"🏦 Exchange suggéré: `{signal.exchange_hint}`\n"
            f"💡 Raison: _{signal.reason}_\n\n"
            f"⚠️ Signal manuel uniquement — exécute toi-même si tu veux."
        )
        try:
            await self.application.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to send Telegram manual signal: %s", exc)

    async def _send_safe(self, text: str) -> None:
        """Send a message to the configured chat, logging failures."""
        if not self.enabled or not self.application:
            return
        try:
            await self.application.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning("Failed to send Telegram message: %s", exc)

    async def start(self) -> None:
        """Start the bot in the background.

        Leadership: only ONE instance across all Render deploys may poll
        Telegram (one getUpdates poller per token). We take a Postgres
        advisory lock; if another instance holds it, this one becomes a
        standby (no polling, no crash) and retries leadership every 30s.
        """
        if not self.enabled or not self.application:
            logger.info("Telegram bot is disabled, skipping start.")
            return
        await self.application.initialize()
        await self.application.start()
        if not self.application.updater:
            return
        try:
            await self.application.bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:
            logger.warning("delete_webhook failed: %s", exc)
        # Start with or without the lock; a background task keeps retrying.
        self._lock_conn = postgres_store.acquire_instance_lock()
        if self._lock_conn is not None:
            await self._start_polling_locked()
        else:
            logger.warning(
                "Another instance holds the Telegram lock; standby mode "
                "(no polling). Will retry leadership every 30s."
            )
        self._leadership_task = asyncio.create_task(self._leadership_loop())

    async def _start_polling_locked(self) -> None:
        """Start getUpdates polling (called only when we hold the lock)."""
        if self._polling_active or not self.application or not self.application.updater:
            return
        try:
            await self.application.updater.start_polling(
                drop_pending_updates=True,
                poll_interval=3.0,
                timeout=30,
            )
            self._polling_active = True
            logger.info(
                "Telegram bot started (authorized: @%s)",
                self.config.telegram_authorized_username or "anyone",
            )
        except Exception as exc:
            logger.error("Failed to start Telegram polling: %s", exc)

    async def _leadership_loop(self) -> None:
        """Keep/retake the instance lock and start/stop polling accordingly."""
        import psycopg2  # local import, optional dep
        while self.enabled and self.application:
            await asyncio.sleep(30)
            if self._lock_conn is not None:
                # Verify the lock connection is still alive; reconnect if lost.
                try:
                    cur = self._lock_conn.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                    if not self._polling_active:
                        await self._start_polling_locked()
                    continue
                except Exception:
                    logger.warning("Instance lock lost; re-acquiring.")
                    try:
                        self._lock_conn.close()
                    except Exception:
                        pass
                    self._lock_conn = None
                    self._polling_active = False
            # Try to (re)acquire leadership.
            self._lock_conn = postgres_store.acquire_instance_lock()
            if self._lock_conn is not None and not self._polling_active:
                logger.info("Acquired Telegram leadership; starting polling.")
                await self._start_polling_locked()

    async def stop(self) -> None:
        """Stop the bot."""
        if not self.enabled or not self.application:
            return
        self.enabled = False
        if self._leadership_task:
            self._leadership_task.cancel()
            try:
                await self._leadership_task
            except asyncio.CancelledError:
                pass
        if self.application.updater and self._polling_active:
            await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()  # releases the advisory lock
            except Exception:
                pass
            self._lock_conn = None
        logger.info("Telegram bot stopped")
