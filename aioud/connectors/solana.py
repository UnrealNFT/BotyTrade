"""Solana trading connector via Jupiter API.

Supports paper trading. Real swaps are executed as versioned transactions
with a fresh recent blockhash to maximize confirmation reliability.
"""

import base64
import time
from dataclasses import dataclass

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts, TxOpts

from aioud.utils.config import Config
from aioud.utils.logger import get_logger

logger = get_logger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"


class _MintConstants:
    """Namespace so module-level mint constants are accessible as attributes."""

    USDC_MINT = USDC_MINT
    SOL_MINT = SOL_MINT

CONFIRMATION_TIMEOUT_SECONDS = 60


@dataclass
class SwapResult:
    success: bool
    signature: str = ""
    error: str = ""
    amount_in: float = 0.0
    amount_out: float = 0.0
    price: float = 0.0


class SolanaConnector(_MintConstants):
    """Trade tokens on Solana using Jupiter aggregator."""

    # Fallback public RPC endpoints used when the primary RPC is slow/failing.
    # The primary is the private Helius endpoint from env; we keep Solana public
    # RPC as a last-resort fallback.
    FALLBACK_RPC_URLS = [
        "https://api.mainnet-beta.solana.com",
    ]

    def __init__(self, config: Config) -> None:
        self.config = config
        self.paper = config.paper_trading
        self.rpc_url = config.solana_rpc_url
        self.rpc = Client(self.rpc_url)
        self._fallback_index = 0
        self.keypair: Keypair | None = None
        if config.solana_private_key and config.solana_private_key not in (
            "your_base58_private_key",
            "",
        ):
            try:
                self.keypair = Keypair.from_base58_string(config.solana_private_key)
            except Exception as exc:
                logger.error("Invalid SOLANA_PRIVATE_KEY: %s", exc)
        else:
            logger.warning("SOLANA_PRIVATE_KEY not set; real swaps disabled.")
        self.jupiter_url = config.jupiter_api_url.rstrip("/")
        logger.info(
            "SolanaConnector initialized | paper=%s | pubkey=%s | rpc=%s",
            self.paper,
            self.keypair.pubkey() if self.keypair else "NONE",
            self.rpc_url,
        )

    def _owner_pubkey(self) -> Pubkey:
        """Return the owner's public key or raise if no keypair is configured."""
        if self.keypair is None:
            raise RuntimeError("No SOLANA_PRIVATE_KEY configured")
        return self.keypair.pubkey()

    def _switch_rpc(self) -> None:
        """Rotate to the next fallback RPC endpoint."""
        urls = [self.rpc_url] + self.FALLBACK_RPC_URLS
        self._fallback_index = (self._fallback_index + 1) % len(urls)
        new_url = urls[self._fallback_index]
        self.rpc = Client(new_url)
        logger.warning("Switched RPC to %s", new_url)

    def _get_token_decimals(self, mint: str) -> int:
        """Return SPL token decimals via on-chain metadata.

        Defaults to 6 (USDC / most memecoins). SOL is 9.
        """
        if mint == SOL_MINT:
            return 9
        try:
            resp = self.rpc.get_token_supply(Pubkey.from_string(mint))
            return resp.value.decimals
        except Exception as exc:
            logger.warning("Failed to get decimals for %s: %s; defaulting to 6", mint, exc)
            return 6

    def get_price(self, input_mint: str, output_mint: str, amount_lamports: int) -> dict:
        """Fetch a Jupiter quote for a swap."""
        url = (
            f"{self.jupiter_url}/quote"
            f"?inputMint={input_mint}"
            f"&outputMint={output_mint}"
            f"&amount={amount_lamports}"
            f"&slippageBps=500"
        )
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_usd: float,
        input_decimals: int | None = None,
        slippage_bps: int = 500,
    ) -> SwapResult:
        """Execute or simulate a swap.

        In paper trading mode no transaction is broadcast.
        """
        if input_decimals is None:
            input_decimals = self._get_token_decimals(input_mint)
        output_decimals = self._get_token_decimals(output_mint)

        if self.paper:
            logger.info(
                "PAPER TRADE: swap %.2f USD of %s -> %s",
                amount_usd,
                input_mint,
                output_mint,
            )
            return SwapResult(
                success=True,
                signature="paper",
                amount_in=amount_usd,
                amount_out=amount_usd * 0.98,
                price=1.0,
            )

        if self.keypair is None:
            logger.error("Cannot execute real swap: no valid SOLANA_PRIVATE_KEY")
            return SwapResult(success=False, error="missing private key")

        amount_lamports = int(amount_usd * (10**input_decimals))
        quote = self.get_price(input_mint, output_mint, amount_lamports)
        logger.info("Jupiter quote received: outAmount=%s", quote.get("outAmount", "unknown"))

        owner_pubkey = self._owner_pubkey()
        swap_request = {
            "quoteResponse": quote,
            "userPublicKey": str(owner_pubkey),
            "wrapAndUnwrapSol": True,
            "priorityFeeMicroLamports": self.config.priority_fee_micro_lamports,
        }
        response = requests.post(
            f"{self.jupiter_url}/swap",
            json=swap_request,
            timeout=60,
        )
        response.raise_for_status()
        swap_data = response.json()

        raw_tx = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(raw_tx)

        # Re-sign the versioned transaction with the owner keypair.
        # Jupiter's response contains placeholder signatures that must be replaced.
        signed_tx = VersionedTransaction(tx.message, [self.keypair])

        def _send_with_fallback(tx_to_send) -> str:
            try:
                sig = self.rpc.send_transaction(
                    tx_to_send,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
                )
                return str(sig.value)
            except Exception as exc:
                logger.warning("RPC send_transaction failed on %s: %s", self.rpc._provider.endpoint_uri, exc)
                self._switch_rpc()
                sig = self.rpc.send_transaction(
                    tx_to_send,
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
                )
                return str(sig.value)

        signature = _send_with_fallback(signed_tx)
        logger.info("Swap broadcast: %s", signature)

        # Wait briefly for on-chain confirmation; mark failed if not confirmed.
        confirmed = self._await_confirmation(signature)
        if not confirmed:
            logger.error("Swap not confirmed after %ss: %s", CONFIRMATION_TIMEOUT_SECONDS, signature)
            return SwapResult(
                success=False,
                signature=signature,
                error="Transaction not confirmed (dropped)",
                amount_in=amount_usd,
            )

        out_amount = float(quote.get("outAmount", 0))
        return SwapResult(
            success=True,
            signature=signature,
            amount_in=amount_usd,
            amount_out=out_amount / 10**output_decimals,
            price=out_amount / amount_lamports if amount_lamports else 0.0,
        )

    def _await_confirmation(self, signature: str) -> bool:
        """Poll for on-chain confirmation of a transaction signature."""
        deadline = time.time() + CONFIRMATION_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                status = self.rpc.get_signature_statuses([Signature.from_string(signature)])
                entry = status.value[0] if status.value else None
                if entry and entry.confirmations is not None:
                    if entry.err:
                        logger.error("Transaction failed: %s", entry.err)
                        return False
                    return True
            except Exception as exc:
                logger.warning("Confirmation check failed on %s: %s", self.rpc._provider.endpoint_uri, exc)
                self._switch_rpc()
            time.sleep(1)
        return False

    def buy_token(self, output_mint: str, amount_usd: float) -> SwapResult:
        """Buy a token with USDC."""
        return self.swap(USDC_MINT, output_mint, amount_usd)

    def sell_token(self, input_mint: str, amount_usd: float) -> SwapResult:
        """Sell a token for USDC."""
        return self.swap(input_mint, USDC_MINT, amount_usd)

    def get_balance(self) -> float:
        """Return SOL balance in lamports."""
        try:
            owner_pubkey = self._owner_pubkey()
            resp = self.rpc.get_balance(owner_pubkey)
            return float(resp.value) / 10**9
        except Exception as exc:
            logger.warning("RPC get_balance failed on %s: %s", self.rpc._provider.endpoint_uri, exc)
            self._switch_rpc()
            raise

    def get_token_balance(self, mint: str) -> float:
        """Return UI token balance for a given SPL mint."""
        try:
            owner_pubkey = self._owner_pubkey()
            accounts = self.rpc.get_token_accounts_by_owner_json_parsed(
                owner_pubkey,
                TokenAccountOpts(mint=Pubkey.from_string(mint)),
            ).value
            if not accounts:
                return 0.0
            total = 0.0
            for account in accounts:
                parsed = account.account.data.parsed
                if not isinstance(parsed, dict):
                    continue
                info = parsed.get("info", {})
                token_amount = info.get("tokenAmount", {}) if isinstance(info, dict) else {}
                if not isinstance(token_amount, dict):
                    continue
                amt = token_amount.get("uiAmount")
                decimals = int(token_amount.get("decimals", 6) or 6)
                if amt is None:
                    raw = int(token_amount.get("amount", 0) or 0)
                    amt = raw / (10 ** decimals)
                total += float(amt)
            return total
        except Exception as exc:
            logger.warning("RPC get_token_balance failed on %s for %s: %s", self.rpc._provider.endpoint_uri, mint, exc)
            self._switch_rpc()
            raise
