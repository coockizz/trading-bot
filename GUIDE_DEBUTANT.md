# Guide du débutant

Ce guide explique **ce que fait le bot et pourquoi**, sans jargon. Lis-le en
entier avant d'engager le moindre euro réel. Compte 20 minutes.

---

## Sommaire

1. [Le grid trading en 5 minutes](#1-le-grid-trading-en-5-minutes)
2. [Quand ça gagne, quand ça perd](#2-quand-ça-gagne-quand-ça-perd)
3. [Choisir sa plage, ses niveaux, son capital](#3-choisir-sa-plage-ses-niveaux-son-capital)
4. [Les garde-fous du bot](#4-les-garde-fous-du-bot)
5. [Lire les logs](#5-lire-les-logs)
6. [Lire les statistiques](#6-lire-les-statistiques)
7. [Le bot s'est arrêté : que faire ?](#7-le-bot-sest-arrêté-que-faire-)
8. [Erreurs de débutant à éviter](#8-erreurs-de-débutant-à-éviter)
9. [Ton parcours en 4 étapes](#9-ton-parcours-en-4-étapes)

---

## 1. Le grid trading en 5 minutes

### L'idée

Le prix d'une crypto monte et descend sans arrêt. Le grid trading consiste à
**acheter à chaque baisse et revendre à chaque hausse**, mécaniquement, sans
essayer de deviner la direction.

Tu découpes une plage de prix en niveaux réguliers. Sous le prix actuel, tu
poses des ordres d'achat. Dès qu'un achat s'exécute, tu poses immédiatement un
ordre de vente au niveau juste au-dessus.

```
  102 000 ──────────────────────  niveau 6
                                     ▲ vente posée automatiquement
  100 600 ──── ACHAT exécuté ────  niveau 5
                                     
   99 200 ──── achat en attente ─  niveau 4
   97 800 ──── achat en attente ─  niveau 3
   96 400 ──── achat en attente ─  niveau 2
```

Quand le prix remonte à 102 000, la vente part : tu as gagné 1 400 € par unité
achetée. Et le niveau 5 redevient un achat en attente, prêt pour le prochain
cycle. C'est répétitif, et c'est voulu.

### Pourquoi ça marche (parfois)

Le prix ne monte ni ne descend en ligne droite : il oscille. Chaque oscillation
qui traverse deux de tes niveaux te rapporte l'écart entre les deux, **moins les
frais**. Plus le prix bouge dans ta plage, plus tu gagnes.

### Le calcul qui compte

Avec 12 niveaux entre 90 000 et 110 000, l'écart entre deux niveaux est d'environ
**1,84 %**. Les frais Binance sont de 0,02 % à l'achat et 0,02 % à la vente, soit
**0,04 % l'aller-retour**.

```
Profit net par cycle = 1,84 % − 0,04 % = 1,80 %
```

Le bot **refuse de démarrer** si l'écart entre tes niveaux ne couvre pas les
frais. C'est un piège classique : une grille très fine semble générer beaucoup de
trades, mais chaque trade perd de l'argent.

---

## 2. Quand ça gagne, quand ça perd

C'est la partie la plus importante du guide.

### Marché latéral : le bot gagne

Le prix oscille entre 95 000 et 105 000 pendant deux semaines. Le bot encaisse
un cycle à chaque aller-retour. C'est le scénario idéal.

### Marché en hausse forte : le bot gagne peu

Le prix monte de 100 000 à 130 000 sans redescendre. Le bot vend tout au fur et
à mesure, puis reste sans rien faire au-dessus de sa plage. Il a gagné, mais
**beaucoup moins que si tu avais simplement acheté et gardé**.

### Marché en baisse forte : le bot perd

Le prix tombe de 100 000 à 70 000. Le bot achète à chaque niveau en descendant.
Arrivé en bas de la plage, il détient tout son inventaire, acheté en moyenne bien
au-dessus du prix actuel. **C'est le risque principal du grid trading.**

C'est exactement là qu'intervient le kill-switch : il coupe les pertes avant
qu'elles ne deviennent graves.

### Ce qu'il faut retenir

> Le grid trading n'est pas un revenu passif garanti. C'est une stratégie qui
> convertit la volatilité en petits gains réguliers, et qui perd quand le marché
> part durablement dans une direction. Aucun réglage ne change ça.

---

## 3. Choisir sa plage, ses niveaux, son capital

### Le plus simple : laisse le bot décider

Si tu ne mets ni `price_range` ni `num_grids` dans ta config, le bot calcule tout
au démarrage :

- il lit le **prix actuel** et centre la plage dessus ;
- il mesure la **volatilité des 14 derniers jours** (l'ATR) et fixe la largeur à
  3 fois cette valeur — assez large pour que le prix reste dedans, assez serrée
  pour que les cycles arrivent ;
- il calcule le **nombre maximum de niveaux** compatible avec ton capital et avec
  le minimum imposé par Binance.

C'est le mode recommandé. Tu ne renseignes que `capital_usdt` et un `profile`.

### La contrainte que tout le monde découvre trop tard

Binance impose un montant minimum par ordre : **environ 100 USDT sur BTCUSDT**.

```
capital ÷ nombre de niveaux ≥ 100 USDT
```

Conséquence directe :

| Capital | Niveaux possibles sur BTCUSDT |
|---|---|
| 200 € | 2 |
| 500 € | 5 |
| 1 000 € | 10 |
| 3 000 € | 12 (largement suffisant) |

**Avec moins de 1 000 €, BTCUSDT est un mauvais choix.** Prends `SOLUSDT` ou
`DOGEUSDT` : leur pas de quantité est bien plus fin, donc tu peux faire une
grille de 15-20 niveaux avec 200 €. Il suffit de changer `symbol` dans la config.

### Les trois profils

| | `prudent` | `equilibre` | `agressif` |
|---|---|---|---|
| Largeur de plage | 4 × ATR (très large) | 3 × ATR | 2 × ATR (serrée) |
| Niveaux visés | 8 | 12 | 20 |
| Perte max / jour | 1 % | 2 % | 3 % |
| Perte max totale | 3 % | 5 % | 8 % |
| Exposition max | 30 % | 40 % | 60 % |

**Commence par `prudent` en réel**, même si le testnet t'a donné de bons
résultats. Une plage large et peu de niveaux, c'est moins de trades mais beaucoup
moins de chances de te retrouver coincé en bas de la grille.

---

## 4. Les garde-fous du bot

### Le kill-switch

Après chaque mouvement de prix, le bot recalcule sa perte. Si elle dépasse une
des deux limites, il **annule tous ses ordres, ferme sa position au marché, et
s'arrête**.

- `max_daily_drawdown_pct` : perte depuis minuit UTC (remise à zéro chaque jour)
- `stop_loss_pct` : perte depuis le lancement (jamais remise à zéro)

L'arrêt est **définitif**. Même si le marché se retourne dans la seconde, le bot
ne repart pas seul. C'est voulu : un kill-switch qui se relance n'est pas un
kill-switch, c'est une boucle qui répète la même perte.

### L'alerte anticipée

Tu es prévenu sur Discord **avant** le kill-switch, quand la perte atteint 75 %
de la limite. Ça te laisse le temps de regarder ce qui se passe.

### La taille de position maximale

Le bot ne dépassera jamais `max_position_size_pct` du capital. Un ordre qui
*réduit* l'exposition est toujours accepté — sinon le bot ne pourrait plus sortir
d'une position trop grosse.

### Les contrôles au démarrage

Le bot refuse de démarrer si :

- ta clé API autorise les **retraits** (danger majeur si elle fuite) ;
- la permission **Futures** manque ;
- le solde du compte est inférieur au capital configuré ;
- ta grille ne peut pas produire d'ordres valides.

Chaque refus explique quoi corriger, avec les chiffres.

---

## 5. Lire les logs

Les logs sont dans `logs/bot.log` et s'affichent aussi à l'écran.

```
00:12:03 | INFO | bot.sizing        | Grille calculée automatiquement : plage
                                      90 885 - 108 862 (19.8% de large), 12
                                      niveaux, 250.00 USDT par niveau
```
→ Le bot a calculé sa grille. Vérifie que la plage encadre bien le prix actuel.

```
00:12:08 | INFO | bot.main          | FILL buy 0.003000 @ 98395.70 |
                                      frais 0.0590 | P&L réalisé +0.0000 USDT
```
→ Un achat s'est exécuté. Le P&L réalisé est à 0 : normal, un achat n'a encore
rien gagné. Le gain arrivera à la revente.

```
00:14:22 | INFO | bot.stats         | STATS | P&L total +4.59 USDT
                                      (réalisé +4.37 / latent +0.22) | jour
                                      +4.59 | fills 4 | trades 1 |
                                      win rate 100.0% | DD max 0.27%
```
→ Résumé périodique. **Réalisé** = encaissé pour de bon. **Latent** = valeur
actuelle de ce que tu détiens encore, qui bouge avec le prix.

```
00:20:11 | WARNING | bot.websocket  | Flux de prix interrompu (). Reconnexion
                                      dans 2.6s (essai 1).
```
→ Coupure réseau. Le bot se reconnecte tout seul. Pas d'action de ta part.

```
00:31:45 | ERROR | bot.risk         | KILL-SWITCH DÉCLENCHÉ : Drawdown
                                      journalier atteint : 2.14% de perte
```
→ Arrêt d'urgence. Va lire la section 7.

### Les niveaux

| Niveau | Signification |
|---|---|
| `INFO` | fonctionnement normal : ordres, exécutions, résumés |
| `WARNING` | quelque chose mérite ton attention, mais le bot continue |
| `ERROR` | le bot s'arrête |

---

## 6. Lire les statistiques

Le fichier `state/stats.json` est réécrit en continu :

```json
{
  "total_pnl": 4.59,          // gain/perte total, frais déduits
  "realized_pnl": 4.37,       // encaissé pour de bon
  "unrealized_pnl": 0.22,     // valeur de ce que tu détiens encore
  "daily_pnl": 4.59,          // depuis minuit UTC
  "total_fees": 0.16,         // frais payés à Binance
  "num_closed_trades": 1,     // cycles achat/vente terminés
  "win_rate_pct": 100.0,      // part de cycles gagnants
  "max_drawdown_pct": 0.27,   // pire baisse depuis le sommet
  "average_exposure_usdt": 263.62
}
```

Et `state/trades.csv` contient chaque exécution, ouvrable dans Excel ou LibreOffice.

### Un point qui surprend : le win rate

Le bot compte son P&L comme Binance : en **prix moyen pondéré**. Si tu as acheté
à trois niveaux différents, ton prix moyen d'entrée est la moyenne des trois. Une
vente qui te fait gagner par rapport à *son* niveau d'achat peut donc apparaître
comme perdante par rapport à *la moyenne*.

Conséquence : **ne juge pas la performance sur le win rate**, regarde
`total_pnl`. Le win rate est indicatif ; le P&L total est la vérité, et il
correspond exactement à ce que t'affiche Binance.

---

## 7. Le bot s'est arrêté : que faire ?

Regarde d'abord le **code de sortie** ou la dernière ligne des logs.

### Code 2 — kill-switch

**Ce n'est pas un bug.** Le bot a fait exactement son travail : limiter la perte.

1. Ouvre `state/stats.json` : combien as-tu perdu, exactement ?
2. Ouvre `logs/bot.log` et remonte jusqu'à la ligne `KILL-SWITCH`.
3. Demande-toi : le prix est-il sorti de ma plage ?
   - **Oui** → ta plage était mal placée ou trop serrée. Passe en profil
     `prudent`, ou laisse le bot recalculer sa grille (mode automatique).
   - **Non** → le marché a chuté violemment dans ta plage. Ton profil est
     peut-être trop agressif pour ce marché.
4. **Ne relance pas immédiatement.** Attends de comprendre. Relancer sans rien
   changer, c'est reproduire la même perte.

### Code 1 — erreur

Le message d'erreur dit quoi faire. Les plus fréquents sont dans la FAQ du
README.

### Code 0 — arrêt normal

Tu as fait Ctrl-C, ou la simulation est terminée. Rien à signaler.

---

## 8. Erreurs de débutant à éviter

**Passer en réel trop vite.** Le testnet est gratuit et identique au réel. Fais-y
tourner le bot au moins une semaine complète, en incluant un week-end.

**Mettre trop d'argent.** Commence avec une somme dont la perte totale ne
changerait rien à ta vie. Tu pourras toujours augmenter.

**Utiliser du levier.** Le bot le refuse sous 1 000 € de capital, et ce n'est pas
une contrainte arbitraire : avec un levier x10, une baisse de 10 % liquide tout.

**Désactiver le kill-switch parce qu'il « se déclenche trop souvent ».** S'il se
déclenche souvent, c'est ta configuration qu'il faut changer, pas la limite.

**Créer une clé API avec droit de retrait.** Le bot refuse de démarrer avec une
telle clé. Ne cherche pas à contourner ce contrôle.

**Regarder le bot en permanence.** C'est le contraire du but recherché. Active
les alertes Discord et va faire autre chose.

**Croire que plus de niveaux = plus de gains.** Plus de niveaux signifie des
écarts plus serrés, donc moins de profit par cycle et une part plus grande mangée
par les frais.

---

## 9. Ton parcours en 4 étapes

### Étape 1 — Simulation (aujourd'hui, 5 minutes)

```bash
./run.sh --simulate 500
```

Aucune clé, aucun réseau, aucun risque. Tu vois le bot poser ses ordres,
encaisser des cycles, et calculer son P&L. Lis les logs, familiarise-toi.

### Étape 2 — Dry-run testnet (cette semaine)

Crée un compte sur https://testnet.binancefuture.com, mets tes clés dans `.env`,
puis :

```bash
./run.sh
```

Le bot suit les vrais prix du marché mais n'envoie aucun ordre. Laisse-le tourner
plusieurs jours et regarde ce que ça donne.

### Étape 3 — Testnet réel (2 à 4 semaines)

Dans `config/testnet.yaml`, passe `dry_run: false`, puis :

```bash
./run.sh --live
```

De vrais ordres, avec de l'argent fictif. C'est ici que tu apprends vraiment :
tu verras des exécutions partielles, des reconnexions, peut-être un kill-switch.
**Ne passe pas à l'étape suivante avant d'avoir vu le bot survivre à une semaine
complète sans intervention.**

### Étape 4 — Réel (quand tu es prêt, pas avant)

```bash
cp config/prod.example.yaml config/prod.yaml
```

Édite le fichier : `profile: prudent`, ton capital réel, `dry_run: false`. Active
les alertes Discord. Puis :

```bash
./run.sh --config config/prod.yaml --live
```

Le bot te demandera de taper `OUI` pour confirmer.

---

## En cas de doute

- Le README contient une FAQ des erreurs courantes.
- Les logs disent presque toujours quoi corriger, en français, avec les chiffres.
- Dans le doute : reste en dry-run. Ça ne coûte rien.
