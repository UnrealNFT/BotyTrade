"""Runtime configuration loaded from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")


@dataclass(frozen=True)
class Config:
    """Application configuration."""

    # Telegram
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_authorized_username: str

    # Solana
    solana_private_key: str
    solana_public_key: str
    solana_rpc_url: str

    # Trading
    paper_trading: bool
    max_daily_loss_usd: float
    max_position_pct: float
    default_sl_pct: float
    default_tp1_pct: float
    default_tp2_pct: float
    default_tp3_pct: float
    trading_amount_usd: float
    priority_fee_micro_lamports: int
    price_sampler_interval_seconds: float
    max_pairs_per_cycle: int
    candle_warmup_enabled: bool
    min_sol_balance_for_fees: float
    min_usdc_balance_to_trade: float
    balance_alert_cooldown_minutes: float

    # Data
    jupiter_api_url: str
    moralis_api_key: str

    # Groq
    groq_api_key: str

    # Piranewz
    piranewz_api_url: str
    piranewz_api_key: str
    piranewz_channel: str
    piranewz_extra_tickers: str
    piranewz_tg_reader_enabled: bool

    # Multi-asset / manual signals (CEX prices)
    coingecko_api_key: str
    coinmarketcap_api_key: str
    manual_signal_min_confidence: int
    manual_signal_enabled: bool

    # TrappistAI backend (optional price source to avoid DexScreener rate limits)
    trappist_api_url: str
    trappist_api_key: str

    # PostgreSQL persistence (Render free survives restarts with external DB)
    database_url: str

    # Testing
    force_paper_test_trade: bool
    # Debug: send a full status report (balances/positions/PnL/watchlist) to
    # Telegram on every trading cycle (~5 min). Enable for live monitoring.
    status_report_every_cycle: bool

    # Pump.fun sniper: buy brand-new launches, exit at a fixed +20% target.
    sniper_enabled: bool
    sniper_take_profit_pct: float
    sniper_stop_loss_pct: float
    sniper_max_age_minutes: float
    sniper_min_liquidity_usd: float

    # Groq auto-buy: minimum AI confidence to auto-execute a buy suggestion.
    groq_auto_buy_min_confidence: int

    # Logging
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            telegram_authorized_username=os.environ.get(
                "TELEGRAM_AUTHORIZED_USERNAME", ""
            ),
            solana_private_key=os.environ.get("SOLANA_PRIVATE_KEY", ""),
            solana_public_key=os.environ.get("SOLANA_PUBLIC_KEY", ""),
            solana_rpc_url=os.environ.get(
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ),
            paper_trading=os.environ.get("PAPER_TRADING", "true").lower()
            in ("1", "true", "yes"),
            max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", "50")),
            max_position_pct=float(os.environ.get("MAX_POSITION_PCT", "0.1")),
            default_sl_pct=float(os.environ.get("DEFAULT_SL_PCT", "0.05")),
            default_tp1_pct=float(os.environ.get("DEFAULT_TP1_PCT", "0.03")),
            default_tp2_pct=float(os.environ.get("DEFAULT_TP2_PCT", "0.06")),
            default_tp3_pct=float(os.environ.get("DEFAULT_TP3_PCT", "0.12")),
            trading_amount_usd=float(os.environ.get("TRADING_AMOUNT_USD", "10")),
            priority_fee_micro_lamports=int(
                os.environ.get("PRIORITY_FEE_MICRO_LAMPORTS", "0")
            ),
            # Price sampler: how often (s) to snapshot watchlist prices to build
            # real 1-minute candles, and how many pairs to evaluate per cycle.
            price_sampler_interval_seconds=float(
                os.environ.get("PRICE_SAMPLER_INTERVAL_SECONDS", "60")
            ),
            max_pairs_per_cycle=int(os.environ.get("MAX_PAIRS_PER_CYCLE", "5")),
            # Candle warmup: seed a new token with synthetic candles when it has
            # less history than the strategy needs, so it becomes tradable fast.
            candle_warmup_enabled=os.environ.get("CANDLE_WARMUP_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            min_sol_balance_for_fees=float(
                os.environ.get("MIN_SOL_BALANCE_FOR_FEES", "0.02")
            ),
            min_usdc_balance_to_trade=float(
                os.environ.get("MIN_USDC_BALANCE_TO_TRADE", "0.50")
            ),
            balance_alert_cooldown_minutes=float(
                os.environ.get("BALANCE_ALERT_COOLDOWN_MINUTES", "30")
            ),
            jupiter_api_url=os.environ.get(
                "JUPITER_API_URL", "https://quote-api.jup.ag/v6"
            ),
            moralis_api_key=os.environ.get("MORALIS_API_KEY", ""),
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            piranewz_api_url=os.environ.get("PIRANEWZ_API_URL", ""),
            piranewz_api_key=os.environ.get("PIRANEWZ_API_KEY", ""),
            piranewz_channel=os.environ.get("PIRANEWZ_CHANNEL", "@PIRANEWZ"),
            piranewz_extra_tickers=os.environ.get("PIRANEWZ_EXTRA_TICKERS", ""),
            # Off by default: the TG reader uses the same bot token as the main
            # bot, which causes getUpdates conflicts. The web scraper is used
            # instead unless this is explicitly enabled with a separate token.
            piranewz_tg_reader_enabled=os.environ.get(
                "PIRANEWZ_TG_READER", "false"
            ).lower() in ("1", "true", "yes"),
            # Default: no HTTP Piranewz API configured, Telegram listener stays disabled.
            coingecko_api_key=os.environ.get("COINGECKO_API_KEY", ""),
            coinmarketcap_api_key=os.environ.get("COINMARKETCAP_API_KEY", ""),
            manual_signal_min_confidence=int(os.environ.get("MANUAL_SIGNAL_MIN_CONFIDENCE", "50")),
            manual_signal_enabled=os.environ.get("MANUAL_SIGNAL_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            force_paper_test_trade=os.environ.get("FORCE_PAPER_TEST_TRADE", "false").lower()
            in ("1", "true", "yes"),
            status_report_every_cycle=os.environ.get(
                "STATUS_REPORT_EVERY_CYCLE", "false"
            ).lower() in ("1", "true", "yes"),
            sniper_enabled=os.environ.get("SNIPER_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            sniper_take_profit_pct=float(os.environ.get("SNIPER_TAKE_PROFIT_PCT", "0.20")),
            sniper_stop_loss_pct=float(os.environ.get("SNIPER_STOP_LOSS_PCT", "0.10")),
            sniper_max_age_minutes=float(os.environ.get("SNIPER_MAX_AGE_MINUTES", "15")),
            sniper_min_liquidity_usd=float(os.environ.get("SNIPER_MIN_LIQUIDITY_USD", "3000")),
            groq_auto_buy_min_confidence=int(
                os.environ.get("GROQ_AUTO_BUY_MIN_CONFIDENCE", "40")
            ),
            trappist_api_url=os.environ.get("TRAPPIST_API_URL", ""),
            trappist_api_key=os.environ.get("TRAPPIST_API_KEY", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
