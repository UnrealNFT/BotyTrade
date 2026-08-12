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
            # Default: no HTTP Piranewz API configured, Telegram listener stays disabled.
            coingecko_api_key=os.environ.get("COINGECKO_API_KEY", ""),
            coinmarketcap_api_key=os.environ.get("COINMARKETCAP_API_KEY", ""),
            manual_signal_min_confidence=int(os.environ.get("MANUAL_SIGNAL_MIN_CONFIDENCE", "50")),
            manual_signal_enabled=os.environ.get("MANUAL_SIGNAL_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            force_paper_test_trade=os.environ.get("FORCE_PAPER_TEST_TRADE", "false").lower()
            in ("1", "true", "yes"),
            trappist_api_url=os.environ.get("TRAPPIST_API_URL", ""),
            trappist_api_key=os.environ.get("TRAPPIST_API_KEY", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
