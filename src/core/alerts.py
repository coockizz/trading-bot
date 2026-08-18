"""Notifications Discord.

Un bot autonome doit prevenir : sans alerte, tu devrais le surveiller, et
surveiller un bot autonome n'a aucun interet.

Le transport est isole derriere `Notifier` pour que les tests n'aient jamais
besoin du reseau, et pour qu'un autre canal (Telegram, email) se branche plus
tard sans toucher au moteur.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from src.core.logger import get_logger
from src.models.config import AlertsConfig

logger = get_logger("alerts")

_DISCORD_MESSAGE_LIMIT = 1900  # la vraie limite est 2000, on garde une marge


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def emoji(self) -> str:
        return {
            "info": "\N{LARGE BLUE CIRCLE}",
            "warning": "\N{WARNING SIGN}",
            "critical": "\N{OCTAGONAL SIGN}",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Alert:
    title: str
    body: str
    level: AlertLevel = AlertLevel.INFO

    def render(self) -> str:
        text = f"{self.level.emoji} **{self.title}**\n{self.body}".strip()
        if len(text) <= _DISCORD_MESSAGE_LIMIT:
            return text
        return text[: _DISCORD_MESSAGE_LIMIT - 3] + "..."


class Notifier(ABC):
    """Canal de notification."""

    @abstractmethod
    async def send(self, alert: Alert) -> bool:
        """Envoie une alerte. Retourne False si l'envoi a echoue."""

    async def close(self) -> None:  # noqa: B027 - facultatif : un canal simple n'a rien a fermer
        """Libere les ressources du canal."""


class NullNotifier(Notifier):
    """Canal inerte, utilise quand les alertes sont desactivees."""

    async def send(self, alert: Alert) -> bool:
        logger.debug("Alerte non envoyee (canal desactive) : %s", alert.title)
        return True


class MemoryNotifier(Notifier):
    """Canal en memoire, pour les tests et pour `--test-alerts` hors ligne."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


class DiscordNotifier(Notifier):
    """Envoi via un webhook Discord.

    Un echec de notification ne doit JAMAIS arreter le bot : Discord
    indisponible n'est pas une raison de cesser de gerer des positions. Les
    erreurs sont donc journalisees, pas propagees.
    """

    def __init__(self, webhook_url: str, config: AlertsConfig) -> None:
        self._url = webhook_url
        self._config = config
        self._timestamps: list[float] = []

    async def send(self, alert: Alert) -> bool:
        if not self._allow_now():
            logger.warning("Alerte '%s' supprimee : limite de debit atteinte.", alert.title)
            return False
        payload = json.dumps({"content": alert.render()}).encode("utf-8")
        try:
            # urllib est bloquant : execute dans un thread pour ne pas figer la
            # boucle asyncio pendant que le bot gere ses ordres.
            return await asyncio.to_thread(self._post, payload)
        except Exception as exc:  # noqa: BLE001 - aucune alerte ne doit tuer le bot
            logger.warning("Echec d'envoi de l'alerte '%s' : %s", alert.title, exc)
            return False

    def _post(self, payload: bytes) -> bool:
        request = urllib.request.Request(  # noqa: S310 - URL validee a la configuration
            self._url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "trading-bot"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_s) as response:  # noqa: S310
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            logger.warning("Discord a repondu %s : %s", exc.code, exc.reason)
            return False
        except urllib.error.URLError as exc:
            logger.warning("Discord injoignable : %s", exc.reason)
            return False

    def _allow_now(self) -> bool:
        """Fenetre glissante d'une minute, pour ne pas se faire limiter."""
        now = time.monotonic()
        self._timestamps = [t for t in self._timestamps if now - t < 60.0]
        if len(self._timestamps) >= self._config.max_messages_per_minute:
            return False
        self._timestamps.append(now)
        return True


class AlertDispatcher:
    """Traduit les evenements du bot en alertes, selon la configuration.

    Le moteur appelle des methodes metier (`trade_closed`, `kill_switch`...)
    sans savoir quel canal est branche ni quels evenements sont actives.
    """

    def __init__(self, notifier: Notifier, config: AlertsConfig, *, symbol: str) -> None:
        self._notifier = notifier
        self._config = config
        self._symbol = symbol
        self._drawdown_warned = False

    async def _emit(self, enabled: bool, alert: Alert) -> None:
        if enabled:
            await self._notifier.send(alert)

    async def bot_started(self, *, mode: str, capital: float, grid: str) -> None:
        await self._emit(
            self._config.on_start_stop,
            Alert(
                title=f"Bot demarre - {self._symbol}",
                body=f"Mode : {mode}\nCapital : {capital:,.2f} USDT\nGrille : {grid}",
                level=AlertLevel.INFO,
            ),
        )

    async def bot_stopped(self, *, reason: str, summary: str) -> None:
        await self._emit(
            self._config.on_start_stop,
            Alert(
                title=f"Bot arrete - {self._symbol}",
                body=f"Raison : {reason}\n\n{summary}",
                level=AlertLevel.WARNING,
            ),
        )

    async def trade_closed(self, *, entry: float, exit_price: float, net_pnl: float) -> None:
        verdict = "Gain" if net_pnl >= 0 else "Perte"
        await self._emit(
            self._config.on_trade_closed,
            Alert(
                title=f"{verdict} - {self._symbol}",
                body=(
                    f"Achat {entry:,.2f} -> vente {exit_price:,.2f}\n"
                    f"Resultat net : {net_pnl:+,.2f} USDT"
                ),
                level=AlertLevel.INFO,
            ),
        )

    async def drawdown_warning(self, *, current_pct: float, limit_pct: float) -> None:
        """Prevenu AVANT le kill-switch, pour pouvoir reagir."""
        threshold = limit_pct * self._config.drawdown_warning_pct_of_limit / 100.0
        if current_pct < threshold:
            self._drawdown_warned = False
            return
        if self._drawdown_warned:  # une seule alerte par franchissement
            return
        self._drawdown_warned = True
        await self._emit(
            self._config.on_drawdown_warning,
            Alert(
                title=f"Perte en approche de la limite - {self._symbol}",
                body=(
                    f"Perte du jour : {current_pct:.2f}%\nLe bot coupera tout a {limit_pct:.2f}%."
                ),
                level=AlertLevel.WARNING,
            ),
        )

    async def kill_switch(self, *, reason: str) -> None:
        await self._emit(
            self._config.on_kill_switch,
            Alert(
                title=f"KILL-SWITCH - {self._symbol}",
                body=(
                    f"{reason}\n\nToutes les positions ont ete fermees et le bot "
                    "est arrete. Il ne redemarrera pas seul."
                ),
                level=AlertLevel.CRITICAL,
            ),
        )

    async def summary(self, text: str) -> None:
        await self._emit(
            True,
            Alert(title=f"Resume - {self._symbol}", body=text, level=AlertLevel.INFO),
        )

    async def critical_error(self, *, message: str) -> None:
        await self._emit(
            True,
            Alert(
                title=f"Erreur critique - {self._symbol}",
                body=message,
                level=AlertLevel.CRITICAL,
            ),
        )

    async def close(self) -> None:
        await self._notifier.close()


def build_notifier(config: AlertsConfig) -> Notifier:
    """Fabrique le canal correspondant a la configuration."""
    if not config.enabled:
        return NullNotifier()
    from src.models.config import resolve_webhook_url

    return DiscordNotifier(resolve_webhook_url(config), config)
