"""Gestion du risque : drawdown journalier, stop-loss global, taille maximale.

Le risk manager a le dernier mot : il peut refuser un ordre, ou declencher le
kill-switch qui ferme tout et arrete le bot. Il ne place jamais d'ordre
lui-meme, il rend un verdict que le moteur applique.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.core.logger import get_logger
from src.core.stats import StatsTracker
from src.models.config import RiskConfig
from src.models.domain import OrderIntent, Position
from src.utils.helpers import format_usdt

logger = get_logger("risk")


class RiskAction(StrEnum):
    CONTINUE = "continue"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    action: RiskAction
    reason: str = ""

    @property
    def should_halt(self) -> bool:
        return self.action is RiskAction.HALT


class RiskManager:
    """Applique les limites de perte et de taille definies en configuration."""

    def __init__(self, config: RiskConfig, initial_equity: float) -> None:
        if initial_equity <= 0:
            raise ValueError(f"initial_equity doit etre > 0, recu {initial_equity}")
        self.config = config
        self.initial_equity = initial_equity
        self._halted = False
        self._halt_reason = ""

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def max_position_notional(self) -> float:
        """Notional maximal autorise sur la position, en USDT."""
        return self.initial_equity * self.config.max_position_size_pct / 100.0

    def validate_against(self, min_notional: float) -> None:
        """Verifie que les limites de risque laissent passer au moins un ordre.

        Sans ce controle, un `max_position_size_pct` trop bas fait tourner le
        bot indefiniment en rejetant chaque ordre, sans erreur visible.
        """
        if self.max_position_notional < min_notional:
            raise ValueError(
                f"max_position_size_pct={self.config.max_position_size_pct}% de "
                f"{format_usdt(self.initial_equity)} autorise au plus "
                f"{format_usdt(self.max_position_notional)} de position, alors que "
                f"l'exchange impose {format_usdt(min_notional)} par ordre. "
                "Augmente max_position_size_pct ou le capital."
            )

    # ---------------------------------------------------------- pre-execution

    def allows_order(self, intent: OrderIntent, position: Position, mark_price: float) -> bool:
        """Vrai si l'ordre peut etre place sans depasser la taille maximale."""
        if self._halted:
            return False
        if mark_price <= 0:
            raise ValueError(f"mark_price doit etre > 0, recu {mark_price}")

        projected_qty = position.quantity + intent.quantity * intent.side.sign
        projected_notional = abs(projected_qty) * mark_price
        # Une vente qui reduit l'exposition est toujours acceptee : la refuser
        # empecherait de sortir d'une position deja trop grosse.
        if abs(projected_qty) <= abs(position.quantity):
            return True

        if projected_notional > self.max_position_notional:
            logger.warning(
                "Ordre refuse (taille max) : position projetee %s > limite %s",
                format_usdt(projected_notional),
                format_usdt(self.max_position_notional),
            )
            return False
        return True

    # --------------------------------------------------------- post-execution

    def evaluate(self, stats: StatsTracker) -> RiskVerdict:
        """Verdict apres mise a jour des stats. Declenche le kill-switch si besoin."""
        if self._halted:
            return RiskVerdict(RiskAction.HALT, self._halt_reason)

        daily_loss_pct = self._loss_pct(stats.daily_pnl, self.initial_equity)
        if daily_loss_pct >= self.config.max_daily_drawdown_pct:
            return self._halt(
                f"Drawdown journalier atteint : {daily_loss_pct:.2f}% de perte "
                f"({format_usdt(stats.daily_pnl)}) >= limite "
                f"{self.config.max_daily_drawdown_pct}%"
            )

        total_loss_pct = self._loss_pct(stats.total_pnl, self.initial_equity)
        if total_loss_pct >= self.config.stop_loss_pct:
            return self._halt(
                f"Stop-loss global atteint : {total_loss_pct:.2f}% de perte depuis le "
                f"lancement ({format_usdt(stats.total_pnl)}) >= limite "
                f"{self.config.stop_loss_pct}%"
            )

        return RiskVerdict(RiskAction.CONTINUE)

    @staticmethod
    def _loss_pct(pnl: float, reference: float) -> float:
        """Perte en pourcentage du capital de reference. 0 si le P&L est positif."""
        if pnl >= 0:
            return 0.0
        return abs(pnl) / reference * 100.0

    def _halt(self, reason: str) -> RiskVerdict:
        self._halted = True
        self._halt_reason = reason
        logger.error("KILL-SWITCH DECLENCHE : %s", reason)
        return RiskVerdict(RiskAction.HALT, reason)

    def force_halt(self, reason: str) -> RiskVerdict:
        """Arret demande par un evenement externe (signal, erreur fatale)."""
        return self._halt(reason)
