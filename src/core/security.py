"""Controles de securite executes au demarrage, avant le moindre ordre.

Le code ne peut pas t'empecher de creer une cle API dangereuse chez Binance,
mais il peut refuser de s'en servir. Ces controles echouent bruyamment plutot
que de laisser tourner un bot mal configure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.logger import get_logger
from src.models.config import BotConfig

logger = get_logger("security")


class SecurityError(RuntimeError):
    """Un controle de securite a echoue. Le bot ne doit pas demarrer."""


@dataclass(frozen=True, slots=True)
class ApiPermissions:
    """Permissions declarees par Binance pour la cle utilisee."""

    can_withdraw: bool
    can_trade_futures: bool
    is_readable: bool

    @classmethod
    def unknown(cls) -> ApiPermissions:
        """Permissions non verifiables (testnet, endpoint indisponible)."""
        return cls(can_withdraw=False, can_trade_futures=True, is_readable=False)


def parse_permissions(raw: dict[str, Any]) -> ApiPermissions:
    """Traduit la reponse `/sapi/v1/account/apiRestrictions` de Binance."""
    return ApiPermissions(
        can_withdraw=bool(raw.get("enableWithdrawals", False)),
        can_trade_futures=bool(raw.get("enableFutures", True)),
        is_readable=True,
    )


def check_permissions(permissions: ApiPermissions, config: BotConfig) -> None:
    """Verifie que la cle API n'en fait pas trop, ni trop peu."""
    if not permissions.is_readable:
        logger.warning(
            "Permissions de la cle API non verifiables (normal sur le testnet). "
            "Verifie toi-meme sur Binance que la cle n'autorise PAS les retraits."
        )
        return

    if config.security.require_no_withdrawal and permissions.can_withdraw:
        raise SecurityError(
            "REFUS DE DEMARRER : cette cle API autorise les RETRAITS.\n"
            "Un bot de trading n'a jamais besoin de retirer des fonds. Si la cle "
            "fuite, ton compte peut etre vide.\n"
            "Corrige sur https://www.binance.com/en/my/settings/api-management : "
            "decoche 'Enable Withdrawals', ou cree une nouvelle cle sans ce droit."
        )

    if config.security.require_futures_permission and not permissions.can_trade_futures:
        raise SecurityError(
            "REFUS DE DEMARRER : cette cle API n'a pas la permission Futures.\n"
            "Active 'Enable Futures' sur la page de gestion des cles API Binance, "
            "ou cree une nouvelle cle avec cette permission."
        )

    logger.info("Controle des permissions API : OK (pas de retrait, Futures autorise).")


def check_testnet_coherence(config: BotConfig, balance: float) -> None:
    """Detecte une incoherence entre le type de cle et le mode configure.

    Un compte testnet est credite d'environ 15 000 USDT fictifs. Un solde
    exactement egal au capital configure sur un compte 'reel' est suspect.
    """
    if config.exchange.testnet:
        logger.info("Mode TESTNET : les fonds sont fictifs, aucun risque financier.")
        return

    logger.warning(
        "Mode MAINNET : les ordres engagent de VRAIS fonds. Solde detecte : %.2f USDT.",
        balance,
    )
    if balance < config.strategy.capital_usdt:
        raise SecurityError(
            f"REFUS DE DEMARRER : tu as configure un capital de "
            f"{config.strategy.capital_usdt:,.2f} USDT mais le compte n'en contient "
            f"que {balance:,.2f}.\n"
            "Reduis strategy.capital_usdt, ou alimente le compte."
        )


def confirm_live_trading(config: BotConfig, balance: float, *, input_fn=input) -> None:
    """Demande une confirmation clavier avant le premier ordre reel.

    Ce garde-fou n'existe qu'en mainnet : sur le testnet il n'y a rien a perdre.
    Il est desactivable pour les lancements automatises (systemd, Docker) via
    security.confirm_before_live.
    """
    if config.dry_run or config.exchange.testnet or not config.security.confirm_before_live:
        return

    print("\n" + "=" * 70)
    print("  ATTENTION : TRADING REEL SUR FONDS REELS")
    print("=" * 70)
    print(f"  Symbole        : {config.symbol}")
    print(f"  Capital engage : {config.strategy.capital_usdt:,.2f} USDT")
    print(f"  Solde du compte: {balance:,.2f} USDT")
    print(f"  Perte max/jour : {config.risk.max_daily_drawdown_pct}%")
    print(f"  Perte max totale: {config.risk.stop_loss_pct}%")
    print("=" * 70)
    answer = input_fn("  Tape OUI en majuscules pour confirmer : ").strip()
    if answer != "OUI":
        raise SecurityError("Lancement annule : confirmation non donnee.")
    logger.warning("Trading reel confirme par l'utilisateur.")


def audit_environment(config: BotConfig) -> list[str]:
    """Liste les points de vigilance detectables sans appel reseau."""
    warnings: list[str] = []
    if not config.dry_run and not config.exchange.testnet:
        warnings.append("Trading REEL sur MAINNET : les pertes sont definitives.")
    if config.strategy.leverage > 1:
        warnings.append(
            f"Levier x{config.strategy.leverage} : une variation de "
            f"{100 / config.strategy.leverage:.0f}% liquide la position."
        )
    if not config.alerts.enabled and not config.dry_run:
        warnings.append(
            "Alertes desactivees en mode reel : tu ne seras pas prevenu d'un kill-switch."
        )
    if config.risk.max_daily_drawdown_pct > 5:
        warnings.append(
            f"Drawdown journalier autorise eleve ({config.risk.max_daily_drawdown_pct}%)."
        )
    for warning in warnings:
        logger.warning("VIGILANCE : %s", warning)
    return warnings
