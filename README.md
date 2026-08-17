# Bot de grid trading — Binance Futures

Bot de trading automatise en Python 3.11, concu pour la strategie **grid trading**
sur Binance USDT-M Futures. Architecture modulaire (donnees / strategie /
execution / risque), mode simulation integre, kill-switch sur drawdown.

> **AVERTISSEMENT**
> Le trading de crypto-actifs a effet de levier peut entrainer la perte totale
> du capital engage. Le grid trading **n'est pas un revenu passif garanti** : il
> est rentable quand le prix oscille dans une plage, et perdant quand le prix
> sort de la plage en tendance soutenue. Ce logiciel est fourni sans garantie.
> N'engage que des sommes que tu peux perdre entierement, et commence par le
> testnet.

---

## Sommaire

1. [Ce que fait le bot](#ce-que-fait-le-bot)
2. [Prerequis](#prerequis)
3. [Installation](#installation)
4. [Creer un compte Binance Futures testnet](#creer-un-compte-binance-futures-testnet)
5. [Configurer les cles API](#configurer-les-cles-api)
6. [Lancer le bot](#lancer-le-bot)
7. [La strategie grid en detail](#la-strategie-grid-en-detail)
8. [Les parametres de risque](#les-parametres-de-risque)
9. [Configuration complete](#configuration-complete)
10. [Logs et statistiques](#logs-et-statistiques)
11. [Tests](#tests)
12. [Deploiement Docker](#deploiement-docker)
13. [Architecture du code](#architecture-du-code)
14. [Etendre le bot](#etendre-le-bot)
15. [Depannage](#depannage)

---

## Ce que fait le bot

- Decoupe une plage de prix en N niveaux, achete en bas, revend au niveau
  superieur, et recommence.
- Ne poste qu'une **fenetre glissante** d'ordres autour du prix courant, pour ne
  pas saturer les limites d'ordres ouverts de l'exchange.
- Reconcilie en continu le carnet reel avec le carnet voulu : un ordre disparu,
  annule ou decale est retabli au cycle suivant.
- Coupe tout (`kill-switch`) si la perte journaliere ou totale depasse les seuils
  configures, puis s'arrete avec le code de sortie `2`.
- Reprend son inventaire apres un redemarrage, via un fichier d'etat.

Trois modes d'execution :

| Mode | Reseau | Ordres reels | Usage |
|---|---|---|---|
| `--simulate N` | aucun | non | valider une config hors ligne, en quelques secondes |
| dry-run (defaut) | prix reels en lecture | non | observer la strategie sur le marche vivant |
| `--live` | complet | **oui** | trading reel, testnet puis mainnet |

---

## Prerequis

- **Python 3.11 ou plus** (`python3 --version`)
- Un compte Binance Futures **testnet** (gratuit, sans KYC) pour commencer
- Environ 50 Mo d'espace disque

---

## Installation

```bash
git clone <url-du-depot> trading-bot
cd trading-bot

python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate

pip install -r requirements.txt
```

Pour developper ou lancer les tests :

```bash
pip install -r requirements-dev.txt
```

**Verification immediate, sans cle API ni reseau :**

```bash
python -m src.main --config config/binance_testnet.yaml --simulate 500
```

Le bot rejoue 500 prix simules dans la plage configuree, place ses ordres,
encaisse des fills, et ecrit ses statistiques. Si cette commande fonctionne,
l'installation est bonne.

---

## Creer un compte Binance Futures testnet

Le testnet utilise de l'argent fictif. Aucune verification d'identite, aucun
risque financier.

1. Va sur **https://testnet.binancefuture.com**.
2. Clique sur **Log In** puis authentifie-toi avec un compte GitHub ou Google.
   Le compte est credite automatiquement (environ 15 000 USDT fictifs).
3. En bas de la page, ouvre l'onglet **API Key**.
4. Note la **API Key** et la **API Secret**. Le secret n'est affiche qu'une
   fois : si tu le perds, regenere la paire.

> Les cles testnet ne fonctionnent **que** sur le testnet, et les cles mainnet
> **que** sur le mainnet. Le champ `exchange.testnet` de la configuration doit
> correspondre au type de cle, sinon Binance repond `Authentification refusee`.

Pour le passage en reel plus tard : cree les cles sur
https://www.binance.com/en/my/settings/api-management, **active la permission
Futures**, restreins l'acces a l'IP de ta machine ou de ton VPS, et **n'active
jamais la permission de retrait**.

---

## Configurer les cles API

Les cles ne sont jamais ecrites dans le code ni dans les fichiers YAML. Le YAML
ne contient que le **nom** des variables d'environnement a lire.

```bash
cp .env.example .env
```

Edite `.env` :

```
BINANCE_API_KEY=ta_cle_ici
BINANCE_API_SECRET=ton_secret_ici
```

Charge les variables dans ton shell :

```bash
# Linux / macOS
set -a && source .env && set +a

# Windows PowerShell
$env:BINANCE_API_KEY="ta_cle_ici"
$env:BINANCE_API_SECRET="ton_secret_ici"
```

`.env` est exclu par `.gitignore`. **Ne le commite jamais.** Les logs passent par
un filtre qui remplace toute chaine ressemblant a une cle ou une signature par
`[REDACTED]`.

En mode `--simulate` et en dry-run local, aucune cle n'est necessaire : seul le
mode `--live` interroge ton compte.

---

## Lancer le bot

### 1. Simulation hors ligne (aucune cle, aucun reseau)

```bash
python -m src.main --config config/binance_testnet.yaml --simulate 500
```

### 2. Dry-run sur les prix reels du marche

```bash
python -m src.main --config config/binance_testnet.yaml
```

Le bot se connecte au flux de mark price public, calcule ses ordres, simule les
executions et tient un P&L virtuel **frais compris**. Aucun ordre n'est envoye.
Laisse-le tourner plusieurs heures avant d'aller plus loin.

### 3. Trading reel sur le testnet

Deux verrous doivent etre leves, volontairement :

1. Dans le fichier de config, passe `dry_run: true` a `dry_run: false`.
2. Ajoute `--live` a la commande.

```bash
python -m src.main --config config/binance_testnet.yaml --live
```

Verifie ensuite tes ordres sur https://testnet.binancefuture.com.

### 4. Trading reel sur le mainnet

Ne fais cette etape qu'apres plusieurs jours concluants sur le testnet.

```bash
cp config/binance_testnet.yaml config/mainnet.yaml
```

Dans `config/mainnet.yaml` :

- `exchange.testnet: false`
- `dry_run: false`
- `strategy.capital_usdt` : ton capital reel
- `strategy.price_range` : une plage centree sur le prix actuel du marche
- `strategy.num_grids` : voir la contrainte de notional minimum ci-dessous

```bash
set -a && source .env && set +a     # avec des cles MAINNET
python -m src.main --config config/mainnet.yaml --live
```

### Codes de sortie

| Code | Signification |
|---|---|
| `0` | arret propre (Ctrl-C, SIGTERM, fin du flux simule) |
| `1` | erreur de configuration ou erreur exchange fatale |
| `2` | **kill-switch declenche** : positions fermees, intervention requise |

Un code `2` est deliberement definitif : le bot ne se relance pas seul. Analyse
ce qui s'est passe avant de le redemarrer, sinon la meme perte recommencera.

---

## La strategie grid en detail

### Principe

La plage `[lower, upper]` est decoupee en `num_grids` niveaux de prix. Sous le
prix courant, le bot place des **achats**. Chaque achat execute au niveau `i`
declenche automatiquement une **vente** au niveau `i+1`.

```
110 000  ─────────────────────  niveau 9
                                   ...
102 883  ── VENTE ────────────  niveau 6   <- sortie de l'achat du niveau 5
100 614  ── achat execute ────  niveau 5
 98 396  ── ACHAT en attente ─  niveau 4
 96 226  ── ACHAT en attente ─  niveau 3
 90 000  ─────────────────────  niveau 0
```

Le profit d'un cycle est l'ecart entre deux niveaux, **moins les frais aller-
retour**. Avec la configuration testnet livree (10 niveaux sur 90k–110k) :
2,255 % d'ecart − 0,10 % de frais = **2,155 % net par cycle**.

### Espacement geometrique ou arithmetique

- `spacing: geometric` (defaut) : chaque niveau est un multiple constant du
  precedent. Le profit en pourcentage est identique partout dans la plage.
- `spacing: arithmetic` : ecart absolu constant. Sur une large plage, les cycles
  du bas rapportent proportionnellement plus que ceux du haut.

Le bot **refuse de demarrer** si l'ecart entre deux niveaux ne couvre pas les
frais aller-retour : une grille trop serree perd de l'argent a chaque cycle.

### Fenetre d'ordres actifs

`active_orders_per_side` limite le nombre d'ordres reellement en carnet de chaque
cote du prix. Les autres niveaux existent en memoire et seront pourvus quand le
prix s'en approchera. Cela evite de saturer la limite d'ordres ouverts de
Binance et reduit le nombre d'appels API.

### Contrainte de notional minimum

Binance impose un notional minimum par ordre (environ **100 USDT** sur BTCUSDT)
et une quantite multiple du **step size** (0,001 BTC sur BTCUSDT). La quantite
est toujours arrondie **vers le bas**, pour ne jamais depasser le capital alloue.

Consequence : `capital_usdt / num_grids` doit rester confortablement au-dessus de
100 USDT. Le bot verifie cela au demarrage et refuse de tourner avec un message
chiffre plutot que de rester silencieux sans jamais placer d'ordre.

```
capital_usdt = 3000, num_grids = 10  ->  300 USDT/niveau  ->  OK
capital_usdt = 500,  num_grids = 50  ->   10 USDT/niveau  ->  REFUSE
```

Avec un capital de 100–500 €, BTCUSDT n'autorise que **2 a 5 niveaux**. Pour une
grille plus fine a petit capital, prends un actif au step size plus fin
(par exemple `SOLUSDT` ou `DOGEUSDT`) : change simplement `symbol`, le bot lit
les filtres du symbole aupres de l'exchange.

### Quand le prix sort de la plage

- **Au-dessus de `upper`** : tout a ete vendu, le bot n'a plus d'inventaire et
  attend un repli. Aucune perte, mais aucun gain non plus.
- **En dessous de `lower`** : le bot detient l'inventaire de tous les niveaux
  achetes, en perte latente. C'est le scenario de risque principal du grid
  trading, et c'est la que le kill-switch intervient.

---

## Les parametres de risque

| Parametre | Effet | Defaut |
|---|---|---|
| `max_daily_drawdown_pct` | perte maximale depuis 00:00 UTC avant kill-switch | 2,0 % |
| `stop_loss_pct` | perte maximale depuis le lancement avant kill-switch | 5,0 % |
| `max_position_size_pct` | part du capital engageable sur la position | voir ci-dessous |

### Kill-switch

Apres chaque tick de prix, le bot recalcule son P&L (realise + latent, frais
inclus) et le compare aux deux seuils. Au premier depassement, il **annule tous
les ordres, ferme la position au marche, ecrit les statistiques et s'arrete**.
L'arret est definitif pour le processus : meme si le marche se retourne
favorablement dans la seconde, le bot reste arrete.

Le P&L journalier repart de zero a minuit UTC. Le stop-loss global, lui, ne se
remet jamais a zero : il protege contre une serie de petites pertes quotidiennes
qui ne declenchent jamais le seuil journalier.

La configuration impose `max_daily_drawdown_pct <= stop_loss_pct`. Dans le cas
inverse, le stop-loss global serait inatteignable.

### `max_position_size_pct` — attention a l'arithmetique

Ce parametre est un **pourcentage du capital**. La valeur de 0,5 % de la
specification initiale donne, sur 500 USDT de capital, une position maximale de
**2,50 USDT** — soit bien moins que le notional minimum de Binance. Le bot
refuserait alors chaque ordre.

`config/default_config.yaml` conserve cette valeur (0,5) pour rester fidele a la
specification, et le bot s'arrete au demarrage avec l'explication chiffree.
`config/binance_testnet.yaml` utilise **40 %**, ce qui couvre les 3 niveaux
actifs a 300 USDT chacun. C'est le fichier a utiliser pour un lancement reel.

Regle pratique : `max_position_size_pct >= active_orders_per_side x
capital_par_niveau / capital_total x 100`.

Un ordre qui **reduit** l'exposition est toujours accepte, meme si la position
depasse deja la limite — sinon le bot ne pourrait plus sortir.

---

## Configuration complete

```yaml
exchange:
  name: binance
  api_key_env: BINANCE_API_KEY     # nom de la variable d'env, pas la cle
  api_secret_env: BINANCE_API_SECRET
  testnet: true                    # doit correspondre au type de cle
  maker_fee_pct: 0.02              # Binance Futures VIP0, ordres limites
  taker_fee_pct: 0.05              # Binance Futures VIP0, ordres au marche
  recv_window_ms: 5000

symbol: BTCUSDT                    # ETHUSDT, SOLUSDT... les filtres sont lus
                                   # automatiquement aupres de l'exchange

dry_run: true                      # false = ordres reels (avec --live)

strategy:
  type: grid
  price_range:
    lower: 90000
    upper: 110000
  num_grids: 10
  capital_usdt: 3000
  leverage: 1                      # > 1 refuse sous 1000 USDT de capital
  spacing: geometric               # ou arithmetic
  active_orders_per_side: 3

risk:
  max_daily_drawdown_pct: 2.0
  stop_loss_pct: 5.0
  max_position_size_pct: 40.0

logging:
  level: INFO                      # DEBUG pour voir chaque ordre simule
  file: logs/bot.log
  max_bytes: 10485760              # rotation a 10 Mo
  backup_count: 5

runtime:
  reconcile_interval_s: 5.0        # frequence d'alignement du carnet
  state_file: state/bot_state.json
  stats_file: state/stats.json
```

Toute cle inconnue dans le YAML provoque une erreur explicite : une faute de
frappe ne peut pas passer inapercue en laissant une valeur par defaut en place.

### Le levier est bride

`leverage > 1` est refuse quand `capital_usdt < 1000`. Avec un petit capital, une
meche de quelques pourcents suffit a liquider une position a levier. Si tu veux
vraiment du levier, augmente d'abord le capital.

---

## Logs et statistiques

### Logs

- Console + `logs/bot.log`, avec rotation (10 Mo, 5 fichiers d'archive).
- `INFO` : ordres, fills, P&L, resume periodique.
- `WARNING` : reconnexions, ordres refuses par le risk manager.
- `ERROR` : kill-switch, erreurs exchange fatales.
- Un filtre remplace toute chaine ressemblant a une cle ou signature par
  `[REDACTED]`, y compris dans les traces d'exception de la librairie exchange.

### `state/stats.json`

Reecrit a chaque reconciliation, pret a etre lu par un dashboard :

```json
{
  "generated_at": "2026-08-17T23:21:30+00:00",
  "total_pnl": 4.594264,
  "realized_pnl": 4.374402,
  "unrealized_pnl": 0.219862,
  "daily_pnl": 4.594264,
  "total_fees": 0.162798,
  "num_fills": 4,
  "num_closed_trades": 1,
  "win_rate_pct": 100.0,
  "max_drawdown_pct": 0.2674,
  "current_drawdown_pct": 0.0659,
  "average_exposure_usdt": 263.62,
  "inventory_qty": 0.004,
  "average_entry_price": 101748.5
}
```

`realized_pnl` est **net de tous les frais payes**, ouvertures comprises.
`win_rate_pct` porte sur les cycles fermes, pas sur les executions.

### `state/trades.csv`

Journal append-only de chaque execution, exploitable dans un tableur :

```csv
timestamp,side,price,quantity,fee,realized_pnl,level
2026-08-17T23:21:30+00:00,buy,100614.20000000,0.00200000,0.04024568,0.00000000,5
2026-08-17T23:21:30+00:00,sell,102882.80000000,0.00200000,0.04115312,4.49604688,5
```

### `state/bot_state.json`

Niveaux detenus, pour reprendre apres un redemarrage. Si la configuration de
grille a change depuis la derniere execution, l'etat est **ignore** avec un
avertissement : des index de niveaux obsoletes feraient vendre au mauvais prix.

---

## Tests

```bash
pytest                # 64 tests, environ 0,2 s
pytest -v             # detail par test
ruff check . && ruff format --check .
```

Couverture : calcul des niveaux de grille, cycle achat/vente, persistance de
l'etat, arrondis tick/step, comptabilite du P&L et des frais, win rate,
drawdown, les trois limites de risque, et des tests d'integration de bout en
bout sur exchange simule (placement, fills, kill-switch, arret propre).

Aucun test n'appelle le reseau.

---

## Deploiement Docker

```bash
docker build -t trading-bot .

# Dry-run sur les prix reels
docker run --rm \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/state:/app/state" \
  trading-bot --config config/binance_testnet.yaml

# Trading reel (dry_run: false dans le YAML requis)
docker run -d --name trading-bot --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/state:/app/state" \
  trading-bot --config config/mainnet.yaml --live
```

L'image tourne sous un utilisateur non privilegie et ne contient aucun secret :
les cles sont injectees a l'execution. `--restart unless-stopped` relance le
conteneur apres un crash ou un redemarrage du VPS, mais **pas** apres un
kill-switch : le code de sortie `2` est un arret volontaire.

---

## Architecture du code

```
config/          configurations YAML
src/
  main.py        CLI, moteur (prix -> fills -> stats -> risque -> carnet)
  core/
    exchange.py  interface commune + Binance Futures (ccxt) + dry-run local
    websocket.py flux de mark price, reconnexion avec backoff
    logger.py    handlers console/fichier + filtre anti-secrets
    stats.py     P&L en cout moyen pondere, win rate, drawdown, exports
  strategy/
    base.py      interface Strategy
    grid.py      niveaux, fenetre d'ordres, cycle achat/vente
  risk/
    manager.py   drawdown journalier, stop-loss global, taille max, kill-switch
  models/
    config.py    modeles pydantic + chargement YAML + resolution des env vars
    domain.py    Order, Fill, Position, OrderIntent, SymbolFilters
  utils/
    helpers.py   arrondis exchange, niveaux, retry backoff, temps UTC
tests/           64 tests, sans reseau
```

Le flux de donnees est unidirectionnel :

```
websocket ──prix──> moteur ──> strategie ──ordres voulus──> reconciliation
                       │                                          │
                       ├──fills──> stats ──> risk manager ──> kill-switch
                       └──────────────> exchange <─────────────────┘
```

La strategie ne parle jamais a l'exchange. Elle declare le carnet qu'elle
souhaite (`desired_orders`), et le moteur se charge de l'aligner sur la realite.
C'est ce qui la rend entierement testable sans reseau.

---

## Etendre le bot

**Ajouter une strategie** : herite de `src/strategy/base.Strategy` et implemente
`desired_orders`, `on_fill`, `snapshot_state`, `restore_state`. Aucune
modification du moteur, du risk manager ou de la couche exchange n'est
necessaire.

**Ajouter un exchange** : herite de `src/core/exchange.BaseExchange` et implemente
les methodes abstraites. Le reste du bot ne connait que cette interface.

**Ajouter une paire** : change `symbol`. Les tick size, step size et notional
minimum sont lus aupres de l'exchange au demarrage. Pour trader plusieurs paires
en parallele, lance un processus par paire, avec un `state_file` distinct.

**Brancher un dashboard** : lis `state/stats.json` (etat courant) et
`state/trades.csv` (historique). Les deux sont reecrits/completes a chaque
reconciliation.

---

## Depannage

| Symptome | Cause et solution |
|---|---|
| `Authentification refusee par Binance` | Le type de cle ne correspond pas a `exchange.testnet`, ou la permission Futures n'est pas activee sur la cle. |
| `Variables d'environnement manquantes` | Les variables ne sont pas chargees dans le shell courant : `set -a && source .env && set +a`. |
| `Grille non rentable` | `num_grids` trop eleve pour la plage : les niveaux sont plus serres que les frais aller-retour. Reduis `num_grids` ou elargis `price_range`. |
| `Chaque niveau dispose de X USDT` | `capital_usdt / num_grids` est sous le notional minimum de l'exchange. Reduis `num_grids` ou augmente le capital. |
| `max_position_size_pct=... autorise au plus X USDT` | La limite de taille de position est plus basse que le notional minimum. Voir la section Risque. |
| `leverage > 1 ... est refuse` | Garde-fou volontaire sous 1000 USDT de capital. Mets `leverage: 1`. |
| Le bot tourne mais ne place aucun ordre | Le prix est probablement hors de `price_range`. Verifie le prix courant et recentre la plage. |
| `Flux de prix interrompu` en boucle | Reseau, pare-feu ou proxy bloquant le websocket. Le bot se reconnecte seul avec un backoff exponentiel. |
| Sortie avec le code `2` | Kill-switch. Lis `logs/bot.log` et `state/stats.json` avant de relancer. |
| `Etat sauvegarde ignore` | La configuration de grille a change. Comportement voulu : l'inventaire repart a vide. |

---

## Licence et responsabilite

Code fourni tel quel, sans garantie d'aucune sorte. L'utilisateur est seul
responsable de ses decisions de trading et des pertes eventuelles.
