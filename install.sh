#!/usr/bin/env bash
# Installation du bot de trading. A lancer depuis le dossier du projet :
#     bash install.sh
#
# Ce script ne modifie RIEN en dehors du dossier du projet : il ne touche pas
# a ton systeme, n'installe pas Python globalement et ne demande pas les
# droits administrateur.

set -euo pipefail

VERT='\033[0;32m'; ROUGE='\033[0;31m'; JAUNE='\033[1;33m'; RESET='\033[0m'
info()    { printf "${VERT}==>${RESET} %s\n" "$1"; }
attention(){ printf "${JAUNE}[ATTENTION]${RESET} %s\n" "$1"; }
erreur()  { printf "${ROUGE}ERREUR${RESET} %s\n" "$1" >&2; }

PYTHON_MIN="3.11"

trouver_python() {
  for candidat in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidat" >/dev/null 2>&1; then
      if "$candidat" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
        echo "$candidat"; return 0
      fi
    fi
  done
  return 1
}

echo
info "Installation du bot de trading"
echo

# --- 1. Python -------------------------------------------------------------
# Le script n'installe PAS Python : modifier le runtime systeme sans prevenir
# peut casser d'autres logiciels. Il affiche la commande exacte a lancer.
if ! PYTHON=$(trouver_python); then
  erreur "Python ${PYTHON_MIN} ou plus recent est introuvable."
  echo
  echo "  Installe-le avec la commande correspondant a ton systeme :"
  echo "    Ubuntu / Debian : sudo apt update && sudo apt install python3.11 python3.11-venv"
  echo "    macOS (Homebrew): brew install python@3.11"
  echo "    Fedora          : sudo dnf install python3.11"
  echo
  echo "  Puis relance : bash install.sh"
  exit 1
fi
info "Python trouve : $($PYTHON --version)"

# --- 2. Environnement virtuel ---------------------------------------------
# Un venv isole les dependances du bot du reste de ta machine.
if [ -d ".venv" ]; then
  info "Environnement virtuel deja present, reutilise."
else
  info "Creation de l'environnement virtuel (.venv)..."
  "$PYTHON" -m venv .venv
fi

# --- 3. Dependances --------------------------------------------------------
info "Installation des dependances (peut prendre 1 a 2 minutes)..."
./.venv/bin/python -m pip install --upgrade pip --quiet
./.venv/bin/python -m pip install -r requirements.txt --quiet
# requirements-dev ajoute pytest, qui sert a verifier l'installation juste apres.
./.venv/bin/python -m pip install -r requirements-dev.txt --quiet
info "Dependances installees."

# --- 4. Fichier .env -------------------------------------------------------
if [ -f ".env" ]; then
  info "Fichier .env deja present, conserve."
else
  cp .env.example .env
  info "Fichier .env cree a partir du modele."
  attention "Ouvre .env et colle tes cles API avant de lancer le bot en reel."
fi
chmod 600 .env 2>/dev/null || true

# --- 5. Dossiers de travail ------------------------------------------------
mkdir -p logs state

# --- 6. Verification -------------------------------------------------------
info "Verification de l'installation (tests automatiques)..."
if ./.venv/bin/python -m pytest -q >/dev/null 2>&1; then
  info "Tests : OK"
else
  attention "Les tests n'ont pas tous reussi. Lance './.venv/bin/python -m pytest' pour voir pourquoi."
fi

info "Test du bot en simulation (sans reseau ni cle API)..."
if ./.venv/bin/python -m src.main --config config/testnet.yaml --simulate 200 >/dev/null 2>&1; then
  info "Simulation : OK"
else
  erreur "La simulation a echoue. Lance la commande ci-dessous pour voir le detail :"
  echo "    ./.venv/bin/python -m src.main --config config/testnet.yaml --simulate 200"
  exit 1
fi

cat <<'FIN'

  Installation terminee.

  ETAPES SUIVANTES
  ----------------
  1. Essayer le bot sans rien risquer (aucune cle necessaire) :
         ./run.sh --simulate 500

  2. Creer un compte Binance testnet et coller les cles dans .env :
         https://testnet.binancefuture.com
     puis lancer sur les vrais prix du marche, sans envoyer d'ordre :
         ./run.sh

  3. Lire le guide debutant avant d'aller plus loin :
         GUIDE_DEBUTANT.md

FIN
