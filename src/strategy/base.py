"""Interface commune a toutes les strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.models.domain import Fill, OrderIntent


class Strategy(ABC):
    """Une strategie transforme un prix et des executions en ordres souhaites.

    Elle ne parle jamais a l'exchange : elle declare le carnet qu'elle veut voir
    (`desired_orders`), et le moteur se charge de reconcilier avec le carnet
    reel. Cette separation rend la strategie testable sans reseau.
    """

    name: str = "base"

    @abstractmethod
    def desired_orders(self, mark_price: float) -> list[OrderIntent]:
        """Carnet souhaite au prix courant."""

    @abstractmethod
    def on_fill(self, fill: Fill) -> None:
        """Met a jour l'etat interne apres l'execution d'un ordre."""

    @abstractmethod
    def snapshot_state(self) -> dict[str, object]:
        """Etat serialisable, pour reprise apres redemarrage."""

    @abstractmethod
    def restore_state(self, state: dict[str, object]) -> None:
        """Restaure l'etat produit par `snapshot_state`."""
