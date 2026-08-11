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
from aioud.utils.logger import get_logger

logger = get_logger(__name__)


class TelegramBot:
    """Wraps python-telegram-bot Application for AIOud Trader."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.enabled = bool(config.telegram_bot_token)
        self.application: Application | None = None
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
            "watchlist": self._cmd_watchlist,
        }.items():
            self.application.add_handler(CommandHandler(cmd, handler))

    async def _deny(self, update: Update) -> None:
        user = update.effective_user
        name = f"@{user.username}" if user and user.username else "inconnu"
        logger.warning("Unauthorized Telegram access from %s", name)
        await update.message.reply_text(
            "🚫 Accès refusé. Ce bot est privé.",
            parse_mode="Markdown",
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        await update.message.reply_text(
            "⚡ *AIOud Trader* est en ligne.\n"
            "Commandes: /status /balance /pnl /positions /panic /add /remove /watchlist",
            parse_mode="Markdown",
        )

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
        await update.message.reply_text(
            f"🤖 Bot actif.\n"
            f"Mode: *{mode} TRADING*\n"
            f"Positions ouvertes: *{open_count}*\n"
            f"Scan en cours sur Solana.",
            parse_mode="Markdown",
        )

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await update.message.reply_text("📊 PnL non disponible pour le moment.")
            return
        state = engine.get_state()
        daily_pnl = state.get("daily_pnl", 0.0) or 0.0
        unrealized = state.get("unrealized_pnl", 0.0) or 0.0
        await update.message.reply_text(
            f"📊 *PnL*\n"
            f"Journalier: `${daily_pnl:+.2f}`\n"
            f"Latent: `${unrealized:+.2f}`",
            parse_mode="Markdown",
        )

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine or not engine.risk.positions:
            await update.message.reply_text("📈 Aucune position ouverte.")
            return
        lines = ["📈 *Positions ouvertes*"]
        for mint, pos in engine.risk.positions.items():
            try:
                price = engine.feed.fetch_price(mint)["price"]
                pnl = pos.pnl_pct(price)
            except Exception:
                price = pos.entry_price
                pnl = 0.0
            lines.append(
                f"• *{pos.symbol}* @ `{pos.entry_price:.8f}` | "
                f"actuel `{price:.8f}` | PnL `{pnl:+.2f}%`"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _cmd_panic(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        await update.message.reply_text(
            "🛑 *PANIC* — fermeture de toutes les positions demandée.\n"
            "(implémentation manuelle via dashboard ou redémarrage)",
            parse_mode="Markdown",
        )

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await update.message.reply_text("💰 Solde non disponible pour le moment.")
            return
        sol = engine._last_sol_balance
        usdc = engine._last_usdc_balance
        await update.message.reply_text(
            f"💰 *Soldes wallet*\n"
            f"SOL: `{sol:.4f}`\n"
            f"USDC: `${usdc:.2f}`",
            parse_mode="Markdown",
        )

    async def _cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await update.message.reply_text("🤖 Bot pas encore prêt.")
            return
        args = context.args
        if not args:
            await update.message.reply_text("Usage: `/add <mint_address>`", parse_mode="Markdown")
            return
        mint = args[0].strip()
        pair = await asyncio.to_thread(engine.add_pair, mint)
        if not pair:
            await update.message.reply_text(f"❌ Impossible de trouver le token: `{mint}`", parse_mode="Markdown")
            return
        await update.message.reply_text(
            f"✅ *{pair.symbol}* ajouté à la watchlist\n"
            f"Mint: `{mint}`\n"
            f"Prix: `${pair.price_usd:.8f}`",
            parse_mode="Markdown",
        )

    async def _cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await update.message.reply_text("🤖 Bot pas encore prêt.")
            return
        args = context.args
        if not args:
            await update.message.reply_text("Usage: `/remove <mint_address>`", parse_mode="Markdown")
            return
        mint = args[0].strip()
        if engine.remove_pair(mint):
            await update.message.reply_text(f"✅ Token retiré de la watchlist: `{mint}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"ℹ️ Token non trouvé dans la watchlist: `{mint}`", parse_mode="Markdown")

    async def _cmd_watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._deny(update)
            return
        engine = getattr(self, "engine", None)
        if not engine:
            await update.message.reply_text("🤖 Bot pas encore prêt.")
            return
        pairs = engine.watchlist[:20]
        if not pairs:
            await update.message.reply_text("📋 Watchlist vide.")
            return
        lines = [f"📋 *Watchlist ({len(pairs)})*"]
        for p in pairs:
            lines.append(f"• *{p.symbol}* `{p.mint}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

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
        if not self.enabled or not self.application:
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
        """Start the bot in the background."""
        if not self.enabled or not self.application:
            logger.info("Telegram bot is disabled, skipping start.")
            return
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (authorized: @%s)", self.config.telegram_authorized_username or "anyone")

    async def stop(self) -> None:
        """Stop the bot."""
        if not self.enabled or not self.application:
            return
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
        logger.info("Telegram bot stopped")
