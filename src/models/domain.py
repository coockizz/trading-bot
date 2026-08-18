"""Types de domaine partages entre exchange, strategie et risk manager."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 pour un achat, -1 pour une vente (variation d'inventaire)."""
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Ordre que la strategie souhaite voir en carnet.

    `client_id` est deterministe (derive du niveau de grille), ce qui permet de
    reconcilier les ordres en place avec les ordres voulus sans etat cache.
    """

    client_id: str
    side: Side
    price: float
    quantity: float
    level_index: int


@dataclass(frozen=True, slots=True)
class Order:
    """Ordre reellement present sur l'exchange."""

    order_id: str
    client_id: str
    side: Side
    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class Fill:
    """Execution d'un ordre."""

    client_id: str
    side: Side
    price: float
    quantity: float
    fee: float
    level_index: int
    timestamp: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class Position:
    """Position nette sur le symbole. `quantity` > 0 = long, < 0 = short."""

    quantity: float
    entry_price: float

    @property
    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-12

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.is_flat:
            return 0.0
        return (mark_price - self.entry_price) * self.quantity


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """Contraintes de l'exchange sur le symbole."""

    tick_size: float
    step_size: float
    min_notional: float
    min_quantity: float


@dataclass(frozen=True, slots=True)
class Candle:
    """Bougie OHLC, utilisee pour mesurer la volatilite recente."""

    high: float
    low: float
    close: float
