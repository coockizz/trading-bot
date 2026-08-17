"""Configuration du logging : console + fichier tournant, sans secrets."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.models.config import LoggingConfig

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Motifs de secrets a neutraliser avant ecriture. Filet de securite : le code ne
# doit deja jamais logger de cle, mais une trace d'exception ccxt peut en
# contenir une dans une URL signee.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(api[_-]?key|api[_-]?secret|signature|token)\b\s*[=:]\s*\S+"),
    re.compile(r"(?i)([?&](?:signature|api[_-]?key)=)[^&\s]+"),
    re.compile(r"\b[A-Za-z0-9]{48,}\b"),  # cles Binance : 64 caracteres
)


class SecretRedactingFilter(logging.Filter):
    """Remplace tout ce qui ressemble a un secret par [REDACTED]."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.getMessage())
        record.args = ()
        return True

    @staticmethod
    def _redact(message: str) -> str:
        for pattern in _SECRET_PATTERNS:
            message = pattern.sub(
                lambda m: (m.group(1) + "[REDACTED]") if m.groups() else "[REDACTED]", message
            )
        return message


def setup_logging(cfg: LoggingConfig) -> logging.Logger:
    """Installe les handlers console + fichier et retourne le logger racine du bot."""
    log_path = Path(cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("bot")
    root.setLevel(cfg.level)
    root.propagate = False
    for handler in list(root.handlers):  # idempotent : utile en tests et au reload
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    redactor = SecretRedactingFilter()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # ccxt logge les requetes HTTP en DEBUG, URLs signees comprises.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    """Logger enfant du logger racine du bot."""
    return logging.getLogger(f"bot.{name}")
