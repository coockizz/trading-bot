"""Modeles pydantic de la configuration + chargement YAML.

Les secrets ne sont jamais stockes dans le YAML : celui-ci ne contient que le
*nom* des variables d'environnement a lire (`api_key_env`), resolues au
demarrage par `resolve_credentials()`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PositiveFloat = Annotated[float, Field(gt=0)]
Percent = Annotated[float, Field(gt=0, le=100)]


class ConfigError(RuntimeError):
    """Configuration invalide ou illisible."""


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExchangeConfig(_Base):
    name: Literal["binance"] = "binance"
    api_key_env: str = "BINANCE_API_KEY"
    api_secret_env: str = "BINANCE_API_SECRET"
    testnet: bool = True
    # Frais Binance Futures VIP0. Utilises pour le P&L simule en dry-run et pour
    # verifier que l'ecart entre deux niveaux couvre bien l'aller-retour.
    maker_fee_pct: float = Field(default=0.02, ge=0, le=1)
    taker_fee_pct: float = Field(default=0.05, ge=0, le=1)
    recv_window_ms: int = Field(default=5_000, ge=1_000, le=60_000)


class PriceRange(_Base):
    lower: PositiveFloat
    upper: PositiveFloat

    @model_validator(mode="after")
    def _check_order(self) -> PriceRange:
        if self.upper <= self.lower:
            raise ValueError(f"upper ({self.upper}) doit etre > lower ({self.lower})")
        return self


class StrategyConfig(_Base):
    type: Literal["grid"] = "grid"
    price_range: PriceRange
    num_grids: int = Field(default=20, ge=2, le=200)
    capital_usdt: PositiveFloat
    leverage: int = Field(default=1, ge=1, le=20)
    spacing: Literal["geometric", "arithmetic"] = "geometric"
    # Nombre d'ordres reellement postes de chaque cote du prix courant. Poster
    # les 200 niveaux d'un coup sature les limites d'ordres ouverts de Binance.
    active_orders_per_side: int = Field(default=10, ge=1, le=50)

    @property
    def capital_per_grid(self) -> float:
        return self.capital_usdt * self.leverage / self.num_grids

    @model_validator(mode="after")
    def _check_capital_per_grid(self) -> StrategyConfig:
        # Binance Futures impose ~100 USDT de notional minimum par ordre sur les
        # paires majeures. Sans ce garde-fou, le bot demarre puis se fait
        # rejeter chaque ordre, ce qui est bien plus difficile a diagnostiquer.
        if self.capital_per_grid < 5.0:
            raise ValueError(
                f"capital_usdt={self.capital_usdt} reparti sur num_grids={self.num_grids} "
                f"ne laisse que {self.capital_per_grid:.2f} USDT par niveau. "
                "Reduis num_grids ou augmente capital_usdt/leverage."
            )
        return self

    @model_validator(mode="after")
    def _check_leverage_warning_free(self) -> StrategyConfig:
        if self.leverage > 1 and self.capital_usdt < 1_000:
            raise ValueError(
                "leverage > 1 avec moins de 1000 USDT de capital est refuse par defaut : "
                "une meche suffit a liquider la position. Mets leverage: 1."
            )
        return self


class RiskConfig(_Base):
    max_daily_drawdown_pct: Percent = 2.0
    stop_loss_pct: Percent = 5.0
    # Part maximale du capital engagee sur une seule position, en pourcentage.
    max_position_size_pct: Percent = 0.5

    @model_validator(mode="after")
    def _check_ordering(self) -> RiskConfig:
        if self.max_daily_drawdown_pct > self.stop_loss_pct:
            raise ValueError(
                "max_daily_drawdown_pct doit etre <= stop_loss_pct, sinon le stop-loss "
                "global ne peut jamais se declencher avant le kill-switch journalier."
            )
        return self


class LoggingConfig(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = "logs/bot.log"
    max_bytes: int = Field(default=10_485_760, ge=1_024)
    backup_count: int = Field(default=5, ge=0, le=50)


class RuntimeConfig(_Base):
    # Periode de reconciliation carnet voulu / carnet reel, en secondes.
    reconcile_interval_s: float = Field(default=5.0, ge=0.5, le=300.0)
    state_file: str = "state/bot_state.json"
    stats_file: str = "state/stats.json"


class BotConfig(_Base):
    exchange: ExchangeConfig = ExchangeConfig()
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Z0-9]{4,20}$")
    dry_run: bool = True
    strategy: StrategyConfig
    risk: RiskConfig = RiskConfig()
    logging: LoggingConfig = LoggingConfig()
    runtime: RuntimeConfig = RuntimeConfig()


class Credentials(NamedTuple):
    api_key: str
    api_secret: str


def load_config(path: str | Path) -> BotConfig:
    """Charge et valide un fichier de configuration YAML."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Fichier de configuration introuvable : {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} doit contenir un mapping YAML a la racine.")

    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Configuration invalide dans {path} :\n{exc}") from exc


def resolve_credentials(cfg: ExchangeConfig) -> Credentials:
    """Lit les cles API depuis l'environnement.

    Leve ConfigError plutot que de retourner des chaines vides : un bot qui
    demarre sans cles echoue de toute facon, autant echouer clairement.
    """
    key = os.environ.get(cfg.api_key_env, "").strip()
    secret = os.environ.get(cfg.api_secret_env, "").strip()
    provided = ((cfg.api_key_env, key), (cfg.api_secret_env, secret))
    missing = [name for name, value in provided if not value]
    if missing:
        raise ConfigError(
            f"Variables d'environnement manquantes : {', '.join(missing)}. "
            "Copie .env.example vers .env et renseigne tes cles."
        )
    return Credentials(api_key=key, api_secret=secret)
