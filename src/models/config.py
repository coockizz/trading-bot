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

from src.models.profiles import PROFILES, apply_profile

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
    # price_range et num_grids peuvent rester vides : le bot les calcule alors
    # au demarrage a partir du prix courant, de la volatilite recente et de ton
    # capital (voir src/core/sizing.py). C'est le mode recommande.
    price_range: PriceRange | None = None
    num_grids: int | None = Field(default=None, ge=2, le=200)
    # Largeur de la plage automatique, en multiples de l'ATR journalier.
    atr_multiple: float = Field(default=3.0, gt=0, le=20)
    # Nombre de niveaux vise en mode automatique. Le bot le reduit si le capital
    # ne permet pas de respecter le notional minimum de l'exchange.
    target_grids: int = Field(default=12, ge=2, le=200)
    capital_usdt: PositiveFloat
    leverage: int = Field(default=1, ge=1, le=20)
    spacing: Literal["geometric", "arithmetic"] = "geometric"
    # Nombre d'ordres reellement postes de chaque cote du prix courant. Poster
    # les 200 niveaux d'un coup sature les limites d'ordres ouverts de Binance.
    active_orders_per_side: int = Field(default=10, ge=1, le=50)

    @property
    def is_auto(self) -> bool:
        """Vrai si la plage ou le nombre de niveaux doit etre calcule au demarrage."""
        return self.price_range is None or self.num_grids is None

    @property
    def resolved_range(self) -> PriceRange:
        if self.price_range is None:
            raise ConfigError("price_range n'est pas encore resolu (mode automatique).")
        return self.price_range

    @property
    def resolved_grids(self) -> int:
        if self.num_grids is None:
            raise ConfigError("num_grids n'est pas encore resolu (mode automatique).")
        return self.num_grids

    @property
    def capital_per_grid(self) -> float:
        return self.capital_usdt * self.leverage / self.resolved_grids

    @model_validator(mode="after")
    def _check_capital_per_grid(self) -> StrategyConfig:
        if self.num_grids is None:
            return self  # le dimensionnement automatique garantit deja la contrainte
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


class AlertsConfig(_Base):
    """Notifications Discord. Le webhook est lu depuis l'environnement."""

    enabled: bool = False
    webhook_url_env: str = "DISCORD_WEBHOOK_URL"
    # Evenements notifies. Desactive ce qui te sature.
    on_trade_closed: bool = True
    on_drawdown_warning: bool = True
    on_kill_switch: bool = True
    on_start_stop: bool = True
    # Seuil d'alerte anticipee : 75 = prevenu quand la perte atteint 75% de la
    # limite de drawdown, donc avant que le kill-switch ne coupe tout.
    drawdown_warning_pct_of_limit: float = Field(default=75.0, gt=0, le=100)
    # Nombre maximal de messages par minute, pour ne pas se faire limiter
    # par Discord ni noyer le canal pendant une chute rapide.
    max_messages_per_minute: int = Field(default=10, ge=1, le=60)
    timeout_s: float = Field(default=10.0, gt=0, le=60)


class MonitoringConfig(_Base):
    """Resume periodique de l'activite du bot."""

    summary_enabled: bool = True
    summary_interval_hours: float = Field(default=4.0, ge=0.25, le=168.0)
    summary_file: str = "state/summary.json"


class SecurityConfig(_Base):
    """Controles effectues au demarrage, avant tout ordre."""

    # Refuse de demarrer si la cle API autorise les retraits.
    require_no_withdrawal: bool = True
    # Refuse de demarrer si la permission Futures manque.
    require_futures_permission: bool = True
    # Demande une confirmation clavier avant le premier ordre reel.
    confirm_before_live: bool = True


class RuntimeConfig(_Base):
    # Periode de reconciliation carnet voulu / carnet reel, en secondes.
    reconcile_interval_s: float = Field(default=5.0, ge=0.5, le=300.0)
    state_file: str = "state/bot_state.json"
    stats_file: str = "state/stats.json"
    # Nombre de redemarrages automatiques apres une erreur transitoire
    # (reseau, API indisponible). 0 = arret des la premiere erreur.
    max_restarts: int = Field(default=10, ge=0, le=1000)
    restart_delay_s: float = Field(default=30.0, ge=1.0, le=3600.0)


class BotConfig(_Base):
    exchange: ExchangeConfig = ExchangeConfig()
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Z0-9]{4,20}$")
    dry_run: bool = True
    # Profil de risque pre-regle : prudent, equilibre ou agressif. Les valeurs
    # ecrites explicitement plus bas ont toujours la priorite.
    profile: str | None = None
    strategy: StrategyConfig
    risk: RiskConfig = RiskConfig()
    alerts: AlertsConfig = AlertsConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    security: SecurityConfig = SecurityConfig()
    logging: LoggingConfig = LoggingConfig()
    runtime: RuntimeConfig = RuntimeConfig()

    @model_validator(mode="after")
    def _check_profile_name(self) -> BotConfig:
        if self.profile is not None and self.profile not in PROFILES:
            valid = ", ".join(sorted(PROFILES))
            raise ValueError(f"profil inconnu : '{self.profile}'. Valeurs possibles : {valid}.")
        return self


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
        raw = apply_profile(raw)
    except ValueError as exc:
        raise ConfigError(f"Configuration invalide dans {path} : {exc}") from exc

    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Configuration invalide dans {path} :\n{exc}") from exc


def resolve_webhook_url(cfg: AlertsConfig) -> str:
    """Lit l'URL du webhook Discord depuis l'environnement."""
    url = os.environ.get(cfg.webhook_url_env, "").strip()
    if not url:
        raise ConfigError(
            f"Les alertes sont activees mais la variable {cfg.webhook_url_env} est vide. "
            "Colle l'URL de ton webhook Discord dans le fichier .env, ou mets "
            "alerts.enabled a false."
        )
    if not url.startswith("https://"):
        raise ConfigError(
            f"{cfg.webhook_url_env} ne ressemble pas a une URL de webhook Discord "
            "(elle doit commencer par https://)."
        )
    return url


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
