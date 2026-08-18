#!/usr/bin/env bash
# Lance le bot avec l'environnement virtuel et les variables de .env.
#
#   ./run.sh                      -> dry-run sur le testnet (aucun ordre reel)
#   ./run.sh --simulate 500       -> simulation hors ligne
#   ./run.sh --test-alerts        -> verifie les notifications Discord
#   ./run.sh --config config/prod.yaml --live   -> trading reel

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "Environnement virtuel absent. Lance d'abord : bash install.sh" >&2
  exit 1
fi

# Charge .env sans l'afficher (les cles ne doivent jamais passer dans les logs).
if [ -f ".env" ]; then
  set -a; . ./.env; set +a
fi

# Config par defaut si l'utilisateur n'en precise pas.
if [[ "$*" != *"--config"* ]]; then
  set -- --config config/testnet.yaml "$@"
fi

exec ./.venv/bin/python -m src.main "$@"
