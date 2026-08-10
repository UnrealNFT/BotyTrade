# Déploiement BotyTrade — Guide Render + Netlify

## 1. Prérequis

- Repo GitHub privé : `UnrealNFT/BotyTrade`
- Compte Render : https://render.com
- Compte Netlify : https://netlify.com

## 2. Backend sur Render (Web Service)

### 2.1 Donner l'accès à Render sur le repo privé
1. Aller sur https://github.com/settings/installations
2. Cliquer sur **Render**
3. Cliquer **Configure**
4. Dans **Repository access**, choisir **Select repositories**
5. Ajouter `UnrealNFT/BotyTrade`
6. Cliquer **Save**

### 2.2 Créer le Web Service
1. Aller sur https://dashboard.render.com/
2. Cliquer **New +** → **Web Service**
3. Sélectionner `UnrealNFT/BotyTrade`
4. Configurer :

| Champ | Valeur |
|-------|--------|
| Name | `botytrade-api` |
| Region | `Frankfurt (EU Central)` |
| Runtime | `Python 3` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python main.py` |
| Plan | `Free` (ou Starter pour 24/7) |

5. Cliquer **Create Web Service**

### 2.3 Variables d'environnement (onglet Environment)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-1003379273264
TELEGRAM_AUTHORIZED_USERNAME=djaf77
SOLANA_PRIVATE_KEY=...
SOLANA_PUBLIC_KEY=...
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...
PAPER_TRADING=true
MAX_DAILY_LOSS_USD=50
MAX_POSITION_PCT=0.10
DEFAULT_SL_PCT=0.05
DEFAULT_TP1_PCT=0.03
DEFAULT_TP2_PCT=0.06
DEFAULT_TP3_PCT=0.12
TRADING_AMOUNT_USD=10
PRIORITY_FEE_MICRO_LAMPORTS=0
MIN_SOL_BALANCE_FOR_FEES=0.02
MIN_USDC_BALANCE_TO_TRADE=0.50
BALANCE_ALERT_COOLDOWN_MINUTES=30
JUPITER_API_URL=https://quote-api.jup.ag/v6
MORALIS_API_KEY=...
GROQ_API_KEY=...
LOG_LEVEL=INFO
DASHBOARD_URL=https://botytrade.netlify.app/mockup.html
```

Remplacer `DASHBOARD_URL` par l'URL Netlify réelle une fois créée.

### 2.4 Récupérer l'URL du backend
Une fois déployé, Render affiche une URL du type :
```
https://botytrade-api.onrender.com
```
La noter pour Netlify.

## 3. Frontend sur Netlify

1. Aller sur https://app.netlify.com/
2. **Add new site** → **Import an existing project**
3. Choisir GitHub → `UnrealNFT/BotyTrade`
4. Configurer :
   - **Build command** : laisser vide
   - **Publish directory** : `.`
5. Dans **Site settings → Environment variables**, ajouter :
   ```env
   API_BASE_URL=https://botytrade-api.onrender.com
   ```
6. Déployer. Le dashboard sera accessible sur :
   ```
   https://<nom-du-site>.netlify.app/mockup.html
   ```

## 4. Vérification

- Backend Render : `https://botytrade-api.onrender.com/api/state` doit retourner du JSON.
- Frontend Netlify : le dashboard doit afficher les données du backend.
- Telegram : envoyer `/watchlist` au bot, il doit répondre.

## 5. Notes importantes

- Le bot Telegram et l'API tournent dans le **même processus Render**.
- Le plan Free de Render s'endort après 15 min d'inactivité : le bot se réveille au premier appel API, mais les scans continus ne fonctionneront pas bien. Passer au plan Starter pour du 24/7.
- Ne jamais committer `.env` ou les clés privées dans GitHub.
