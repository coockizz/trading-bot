# Bot de grid trading — Binance Futures

Bot de trading automatique en Python. Il achète bas, revend haut, en boucle, dans
une plage de prix qu'il calcule lui-même. Il tourne seul, te prévient sur Discord,
et coupe tout si les pertes dépassent la limite que tu as fixée.

**Débutant ?** Lis d'abord le **[GUIDE_DEBUTANT.md](GUIDE_DEBUTANT.md)**.

> ### Avertissement
> Le trading de crypto-monnaies peut faire perdre **la totalité** du capital
> engagé. Le grid trading n'est **pas** un revenu passif garanti : il gagne quand
> le prix oscille, il perd quand le marché chute durablement. Ce logiciel est
> fourni sans garantie. N'engage que ce que tu peux perdre, et commence par le
> testnet.

---

## Démarrer en 5 minutes

### 1. Installer

**Linux / macOS**
```bash
git clone <url-du-depot> trading-bot
cd trading-bot
bash install.sh
```

**Windows** — double-clique sur `install.bat`.

Le script crée un environnement isolé, installe les dépendances, prépare ton
fichier `.env` et vérifie que tout fonctionne. Il **n'installe pas Python** : si
tu ne l'as pas, il t'affiche la commande exacte pour ton système.

### 2. Essayer sans risque, sans compte

```bash
./run.sh --simulate 500
```

Ni clé API, ni connexion internet. Le bot rejoue 500 prix simulés, place ses
ordres, encaisse des cycles et affiche son résultat. **Si cette commande
fonctionne, l'installation est bonne.**

### 3. Brancher un compte testnet (argent fictif)

1. Va sur **https://testnet.binancefuture.com**
2. Connecte-toi avec GitHub ou Google — le compte est crédité de ~15 000 USDT fictifs
3. En bas de page, onglet **API Key** : copie la clé et le secret
4. Colle-les dans le fichier `.env` :

```bash
BINANCE_API_KEY=ta_cle
BINANCE_API_SECRET=ton_secret
```

5. Lance :

```bash
./run.sh
```

Le bot suit les vrais prix du marché mais **n'envoie aucun ordre**. Laisse-le
tourner quelques jours.

---

## Les trois modes

| Commande | Réseau | Ordres réels | Pour quoi faire |
|---|---|---|---|
| `./run.sh --simulate 500` | non | non | vérifier l'installation, comprendre |
| `./run.sh` | prix réels | non | observer sur le marché vivant |
| `./run.sh --live` | complet | **oui** | trader (testnet, puis réel) |

Pour trader réellement, **deux verrous** doivent être levés volontairement :
`dry_run: false` dans le fichier de config **et** l'option `--live`. En mainnet,
le bot demande en plus de taper `OUI` au clavier.

---

## Configurer

Le fichier `config/testnet.yaml` suffit pour commencer :

```yaml
profile: equilibre        # prudent | equilibre | agressif
symbol: BTCUSDT
dry_run: true
strategy:
  capital_usdt: 3000
```

C'est tout. **Tu ne choisis ni la plage de prix ni le nombre de niveaux** : le
bot les calcule au démarrage à partir du prix courant, de la volatilité récente
et de ton capital.

### Les trois profils

| | `prudent` | `equilibre` | `agressif` |
|---|---|---|---|
| Largeur de plage | très large | moyenne | serrée |
| Niveaux visés | 8 | 12 | 20 |
| Perte max / jour | 1 % | 2 % | 3 % |
| Perte max totale | 3 % | 5 % | 8 % |
| Exposition max | 30 % | 40 % | 60 % |

```bash
./run.sh --profile prudent      # surcharge ponctuelle
```

Toutes les options existantes sont documentées dans
[`config.example.yaml`](config.example.yaml).

---

## Alertes Discord

Pour être prévenu des trades, des pertes et des arrêts sans surveiller un écran.

1. Sur ton serveur Discord : roue dentée d'un salon → **Intégrations** →
   **Webhooks** → **Nouveau webhook** → copier l'URL
2. Colle l'URL dans `.env` :
   ```bash
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
3. Dans ta config, mets `alerts: { enabled: true }`
4. Vérifie :
   ```bash
   ./run.sh --test-alerts
   ```

Tu recevras : chaque cycle terminé (gain ou perte), une alerte quand la perte
atteint 75 % de la limite, le déclenchement du kill-switch, et un résumé toutes
les 4 heures (configurable).

---

## Faire tourner le bot en permanence

### Sur un serveur Linux (recommandé)

```bash
sudo cp deploy/trading-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot.service    # adapte les chemins
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
```

```bash
sudo systemctl status trading-bot     # état
sudo journalctl -u trading-bot -f     # logs en direct
```

Le service redémarre automatiquement après un plantage ou un reboot, **mais
jamais après un kill-switch** : le code de sortie 2 est un arrêt volontaire.

### Avec Docker

```bash
docker build -t trading-bot .
docker run -d --name trading-bot --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/logs:/app/logs" -v "$(pwd)/state:/app/state" \
  trading-bot --config config/prod.yaml --live
```

### Coupures réseau

Le bot se reconnecte seul au flux de prix, et redémarre son moteur jusqu'à
`max_restarts` fois après une erreur réseau ou une indisponibilité de Binance.
Seuls le kill-switch et les erreurs de configuration sont définitifs.

---

## Passer en réel

Ne fais cette étape qu'après **plusieurs semaines concluantes sur le testnet**.

1. Crée des clés **mainnet** sur
   https://www.binance.com/en/my/settings/api-management
   - active **Enable Futures**
   - **n'active PAS Enable Withdrawals** — le bot refusera de démarrer
   - restreins l'accès à l'IP de ta machine
2. Prépare la configuration :
   ```bash
   cp config/prod.example.yaml config/prod.yaml
   ```
   Édite : `profile: prudent`, ton capital réel, `dry_run: false`.
3. Lance :
   ```bash
   ./run.sh --config config/prod.yaml --live
   ```

Le bot affiche un récapitulatif et attend que tu tapes `OUI`.

**Avec moins de 1 000 €, ne trade pas BTCUSDT** : Binance impose ~100 USDT
minimum par ordre, ce qui ne laisse que 2 à 5 niveaux. Utilise `SOLUSDT` ou
`DOGEUSDT`, dont le pas de quantité est plus fin.

---

## Sécurité

- Les clés API ne sont **jamais** dans le code ni dans les fichiers YAML : le
  YAML ne contient que le *nom* de la variable d'environnement à lire.
- `.env` est exclu de git et créé avec des permissions restreintes.
- Les logs passent par un filtre qui remplace toute clé ou signature par
  `[REDACTED]`, y compris dans les traces d'erreur de la librairie exchange.
- Au démarrage, le bot **refuse de tourner** si la clé autorise les retraits, si
  la permission Futures manque, ou si le solde est inférieur au capital
  configuré.
- L'image Docker tourne sous un utilisateur non privilégié et ne contient aucun
  secret.

---

## Où regarder ce que fait le bot

| Fichier | Contenu |
|---|---|
| `logs/bot.log` | tout ce que fait le bot (rotation à 10 Mo) |
| `state/stats.json` | P&L, win rate, drawdown, exposition — mis à jour en continu |
| `state/trades.csv` | chaque exécution, ouvrable dans Excel |
| `state/summary.json` | dernier résumé périodique |
| `state/bot_state.json` | niveaux détenus, pour reprendre après redémarrage |

---

## Codes de sortie

| Code | Signification | Action |
|---|---|---|
| `0` | arrêt propre | rien |
| `1` | erreur de configuration ou erreur fatale | lis le message, corrige |
| `2` | **kill-switch** : positions fermées | comprends avant de relancer |

---

## FAQ — problèmes courants

**« Variables d'environnement manquantes »**
Tes clés ne sont pas chargées. Vérifie que `.env` existe et contient
`BINANCE_API_KEY=...`. Avec `./run.sh`, le chargement est automatique.

**« Authentification refusée par Binance »**
Trois causes possibles : clé testnet utilisée en mainnet (ou l'inverse — vérifie
`exchange.testnet`), permission Futures non activée, ou horloge système
décalée de plus de 5 secondes.

**« REFUS DE DÉMARRER : cette clé API autorise les RETRAITS »**
Volontaire. Un bot n'a jamais besoin de retirer des fonds. Crée une nouvelle clé
sans ce droit.

**« Ton capital ne permet aucune grille sur ce symbole »**
Ton capital divisé par le nombre de niveaux tombe sous le minimum de Binance.
Augmente le capital, ou passe sur `SOLUSDT` / `DOGEUSDT`.

**« Grille non rentable »**
Tes niveaux sont plus serrés que les frais aller-retour : chaque cycle perdrait
de l'argent. Réduis `target_grids` ou élargis `atr_multiple`.

**Le bot tourne mais ne place aucun ordre**
Le prix est probablement sorti de la plage. Regarde la ligne « Grille calculée »
dans les logs et compare au prix actuel. En mode automatique, redémarre le bot
pour qu'il recalcule sa plage.

**« Flux de prix interrompu » en boucle**
Réseau, pare-feu ou proxy. Le bot se reconnecte seul avec un délai croissant.

**Le bot s'est arrêté avec le code 2**
Le kill-switch a fait son travail. Voir la section 7 du
[GUIDE_DEBUTANT.md](GUIDE_DEBUTANT.md).

**Mon win rate est bas alors que je gagne de l'argent**
Normal : le P&L est calculé en prix moyen pondéré, comme chez Binance. Fie-toi à
`total_pnl`, pas au win rate. Explication détaillée dans le guide, section 6.

---

## Pour les développeurs

```
config/            configurations YAML
src/
  main.py          CLI, moteur, supervision
  core/
    exchange.py    interface commune + Binance (ccxt) + dry-run local
    websocket.py   flux de prix, reconnexion automatique
    sizing.py      calcul automatique de la grille (ATR, capital, minimums)
    alerts.py      notifications Discord
    security.py    contrôles au démarrage
    stats.py       P&L, win rate, drawdown, exports JSON/CSV
    logger.py      logs + filtre anti-secrets
  strategy/        interface Strategy + grid.py
  risk/manager.py  drawdown, stop-loss, taille max, kill-switch
  models/          config pydantic, profils, types de domaine
  utils/helpers.py arrondis exchange, retry, calculs
tests/             138 tests, sans réseau
deploy/            unité systemd
```

```bash
pytest                              # 138 tests, ~0,3 s
ruff check . && ruff format --check .
```

Le flux est unidirectionnel : `prix → stratégie → ordres voulus → réconciliation`,
avec `fills → stats → risque → kill-switch` en parallèle. La stratégie ne parle
jamais à l'exchange, ce qui la rend testable sans réseau.

**Ajouter une stratégie** : hérite de `src/strategy/base.Strategy`.
**Ajouter un exchange** : hérite de `src/core/exchange.BaseExchange`.
**Ajouter un canal d'alerte** : hérite de `src/core/alerts.Notifier`.

---

## Licence

Fourni tel quel, sans garantie. L'utilisateur est seul responsable de ses
décisions de trading et de ses pertes éventuelles.
