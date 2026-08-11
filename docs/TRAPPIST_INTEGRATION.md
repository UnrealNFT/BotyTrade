# Intégration TrappistAI — Backend de prix pour BotyTrade

## Objectif

Éviter les rate-limits DexScreener sur Render free en faisant passer toutes les requêtes de prix par ton propre backend TrappistAI. Cela permet à BotyTrade de tourner 24/7 sans être tué par Render.

## 1. Déployer le backend TrappistAI

Assure-toi que ton backend TrappistAI est déployé et accessible publiquement, par exemple :

```
https://trappistai-api.onrender.com
```

## 2. Ajouter l'endpoint `/api/prices/batch` dans TrappistAI

Dans le fichier `backend/main.py` de TrappistAI, ajoute les imports et le modèle suivants :

```python
import requests
from pydantic import BaseModel
from typing import Any

# Mapping des mints Solana connus vers les IDs CoinGecko
TRAPPIST_MINT_TO_COINGECKO = {
    "So11111111111111111111111111111111111111112": "solana",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "usd-coin",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "bonk",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "jupiter-exchange-solana",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "raydium",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "dogwifcoin",
}

TRAPPIST_DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{}"

class PricesBatchRequest(BaseModel):
    mints: list[str]
```

Puis ajoute l'endpoint (idéalement après les endpoints `/health`) :

```python
@app.post("/api/prices/batch")
@limiter.limit("200/minute")
async def prices_batch(request: Request, data: PricesBatchRequest):
    """Return USD price/volume/market cap for a list of Solana mint addresses."""
    results: dict[str, dict[str, Any]] = {}

    coingecko_ids: list[str] = []
    id_to_mint: dict[str, str] = {}
    dex_mints: list[str] = []

    for mint in data.mints:
        cid = TRAPPIST_MINT_TO_COINGECKO.get(mint)
        if cid:
            coingecko_ids.append(cid)
            id_to_mint[cid] = mint
        else:
            dex_mints.append(mint)

    # 1) CoinGecko pour les blue chips connus
    if coingecko_ids:
        try:
            prices = get_prices(coingecko_ids)
            for cid, info in prices.items():
                mint = id_to_mint.get(cid)
                if mint and info.get("usd") is not None:
                    results[mint] = {
                        "price": float(info["usd"]),
                        "volume_24h": float(info.get("volume_24h") or 0),
                        "market_cap": float(info.get("market_cap") or 0),
                        "change_24h": info.get("change_24h"),
                        "source": "coingecko",
                    }
        except Exception as e:
            logger.warning("TrappistAI CoinGecko batch failed: %s", e)

    # 2) DexScreener pour les mints inconnus / meme tokens
    if dex_mints:
        for i in range(0, len(dex_mints), 30):
            batch = dex_mints[i : i + 30]
            try:
                r = requests.get(
                    TRAPPIST_DEXSCREENER_TOKENS.format(",".join(batch)),
                    timeout=(5, 15),
                )
                r.raise_for_status()
                pairs_by_mint: dict[str, list[dict[str, Any]]] = {}
                for pair in r.json().get("pairs", []):
                    if pair.get("chainId") != "solana":
                        continue
                    mint = pair.get("baseToken", {}).get("address")
                    if not mint or mint not in batch:
                        continue
                    pairs_by_mint.setdefault(mint, []).append(pair)

                for mint, pairs in pairs_by_mint.items():
                    if mint in results:
                        continue
                    stable = [p for p in pairs if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")]
                    candidates = stable or pairs
                    best = max(candidates, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
                    if best.get("priceUsd"):
                        results[mint] = {
                            "price": float(best["priceUsd"]),
                            "volume_24h": float(best.get("volume", {}).get("h24") or 0),
                            "liquidity_usd": float(best.get("liquidity", {}).get("usd") or 0),
                            "market_cap": float(best.get("marketCap") or 0),
                            "source": "dexscreener",
                        }
            except Exception as e:
                logger.warning("TrappistAI DexScreener batch failed: %s", e)

    return results
```

> **Note** : `get_prices` vient déjà de `backend/prices.py` dans TrappistAI. Vérifie qu'il est importé dans `main.py` (`from prices import get_prices`).

Redéploie TrappistAI après avoir ajouté ce code.

## 3. Configurer BotyTrade sur Render

Dans les variables d'environnement de ton service `botytrade-api` sur Render, ajoute :

```
TRAPPIST_API_URL=https://trappistai-api.onrender.com
TRAPPIST_API_KEY=  # laisser vide si pas d'auth sur /api/prices/batch
```

Remplace l'URL par celle de ton backend TrappistAI.

## 4. Vérifier que ça marche

Teste l'endpoint depuis ton navigateur ou curl :

```bash
curl -X POST https://trappistai-api.onrender.com/api/prices/batch \
  -H "Content-Type: application/json" \
  -d '{"mints":["So11111111111111111111111111111111111111112"]}'
```

Tu devrais recevoir :

```json
{
  "So11111111111111111111111111111111111111112": {
    "price": 76.15,
    "volume_24h": 64511099,
    "market_cap": 0,
    "change_24h": 2.5,
    "source": "coingecko"
  }
}
```

## 5. Sécurité (optionnel)

Pour éviter que n'importe qui n'utilise ton endpoint, ajoute une clé API :

```python
import os
from fastapi import Header, HTTPException

TRAPPIST_PRICE_API_KEY = os.getenv("TRAPPIST_PRICE_API_KEY", "")

@app.post("/api/prices/batch")
@limiter.limit("200/minute")
async def prices_batch(
    request: Request,
    data: PricesBatchRequest,
    x_api_key: str = Header(default=""),
):
    if TRAPPIST_PRICE_API_KEY and x_api_key != TRAPPIST_PRICE_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    # ... reste du code
```

Et dans BotyTrade, renseigne `TRAPPIST_API_KEY` avec la même valeur.

## 6. Pourquoi ça résout le problème

- **Un seul backend fait les appels DexScreener** : ton IP TrappistAI n'est pas partagée avec d'autres services Render.
- **Cache possible côté TrappistAI** : tu peux ajouter un cache mémoire sur `/api/prices/batch` pour réduire encore les appels.
- **BotyTrade devient un simple client HTTP** : son health-check `/health` reste instantané, plus de blocage Render.
