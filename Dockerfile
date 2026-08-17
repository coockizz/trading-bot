# Image de deploiement VPS. Le bot ne tourne pas en root et ne contient
# aucun secret : les cles sont injectees a l'execution via -e ou --env-file.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Couche de dependances separee : le cache Docker survit aux changements de code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/

# Utilisateur non privilegie, proprietaire des repertoires ecrits a l'execution.
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/logs /app/state \
    && chown -R bot:bot /app
USER bot

VOLUME ["/app/logs", "/app/state"]

# Dry-run par defaut : demarrer l'image ne peut pas engager de fonds reels.
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--config", "config/binance_testnet.yaml"]
