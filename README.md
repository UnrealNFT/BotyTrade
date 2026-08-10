# BotyTrade (AIOud Trader) 🤖⚡

Bot de trading crypto autonome piloté par Telegram ([@AIOud_bot](https://t.me/AIOud_bot)).

## Phase 1 : Solana (en cours)
- Sniping / scalping de tokens sur Solana
- Indicateurs : RSI, MACD, Heikin-Ashi
- Gestion automatique des TP/SL
- Notifications en temps réel sur Telegram

## Phase 2 : MEXC Futures
- Trading futures sur MEXC (CSPR, etc.)
- Intégration API MEXC

## Phase 3 : News + LLM
- Flux RSS crypto comme signal de sentiment
- Couche décisionnelle LLM via Groq

---

## Stack
- Python 3.10+
- Telegram Bot API
- Solana (Helius RPC / Jupiter)
- FastAPI dashboard API
- Pandas/TA-Lib pour indicateurs
- SQLite pour persistence

## Démarrage local

```bash
cd aioud-trader
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # puis remplis les variables
python main.py
```

## Déploiement

| Composant | Hébergement | Notes |
|-----------|-------------|-------|
| Backend API + bot Telegram | Render | Variables d'env configurées dans le dashboard Render |
| Frontend dashboard | Netlify | Fichier `mockup.html` statique, à pointer sur le repo |
