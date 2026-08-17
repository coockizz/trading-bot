"""Grid trading.

Principe : on decoupe une plage de prix en N niveaux. Sous le prix courant on
pose des achats, et chaque achat execute au niveau i declenche une vente au
niveau i+1. La difference entre les deux niveaux, moins les frais aller-retour,
est le profit d'un cycle.

Le carnet n'est pas pose en entier : seuls `active_orders_per_side` niveaux de
chaque cote du prix sont reellement en carnet, ce qui evite de saturer les
limites d'ordres ouverts de l'exchange tout en couvrant les mouvements utiles.
"""

from __future__ import annotations

from src.core.logger import get_logger
from src.models.config import StrategyConfig
from src.models.domain import Fill, OrderIntent, Side
from src.strategy.base import Strategy
from src.utils.helpers import arithmetic_levels, geometric_levels

logger = get_logger("strategy.grid")

BUY_PREFIX = "grid-b"
SELL_PREFIX = "grid-s"


class GridStrategy(Strategy):
    """Grid statique sur une plage de prix bornee."""

    name = "grid"

    def __init__(self, config: StrategyConfig, *, taker_fee_pct: float = 0.05) -> None:
        self.config = config
        self.levels = self._build_levels(config)
        self._taker_fee_pct = taker_fee_pct
        # Niveaux achetes dont la vente n'a pas encore ete executee.
        self._held_levels: set[int] = set()
        self._warn_if_unprofitable()

    @staticmethod
    def _build_levels(config: StrategyConfig) -> list[float]:
        builder = geometric_levels if config.spacing == "geometric" else arithmetic_levels
        return builder(config.price_range.lower, config.price_range.upper, config.num_grids)

    def _warn_if_unprofitable(self) -> None:
        """Verifie qu'un cycle couvre les frais aller-retour.

        Une grille trop serree perd de l'argent a chaque cycle, silencieusement.
        """
        spread_pct = min(
            (self.levels[i + 1] - self.levels[i]) / self.levels[i] * 100.0
            for i in range(len(self.levels) - 1)
        )
        round_trip_fee_pct = 2 * self._taker_fee_pct
        if spread_pct <= round_trip_fee_pct:
            raise ValueError(
                f"Grille non rentable : ecart minimal entre niveaux {spread_pct:.4f}% "
                f"<= frais aller-retour {round_trip_fee_pct:.4f}%. "
                "Reduis num_grids ou elargis price_range."
            )
        logger.info(
            "Grille : %d niveaux de %.2f a %.2f (%s), ecart mini %.3f%%, "
            "profit net estime par cycle %.3f%%",
            len(self.levels),
            self.levels[0],
            self.levels[-1],
            self.config.spacing,
            spread_pct,
            spread_pct - round_trip_fee_pct,
        )

    # ------------------------------------------------------------------ etat

    @property
    def held_levels(self) -> frozenset[int]:
        return frozenset(self._held_levels)

    def quantity_at(self, level_index: int) -> float:
        """Quantite de base a traiter pour un niveau donne."""
        return self.config.capital_per_grid / self.levels[level_index]

    def nearest_level_below(self, price: float) -> int:
        """Index du plus haut niveau strictement sous `price` (-1 si aucun)."""
        for index in range(len(self.levels) - 1, -1, -1):
            if self.levels[index] < price:
                return index
        return -1

    # --------------------------------------------------------------- ordres

    def desired_orders(self, mark_price: float) -> list[OrderIntent]:
        if mark_price <= 0:
            raise ValueError(f"mark_price doit etre > 0, recu {mark_price}")
        return self._buy_intents(mark_price) + self._sell_intents()

    def _buy_intents(self, mark_price: float) -> list[OrderIntent]:
        """Achats sur les niveaux libres immediatement sous le prix."""
        pivot = self.nearest_level_below(mark_price)
        intents: list[OrderIntent] = []
        for index in range(pivot, -1, -1):
            if len(intents) >= self.config.active_orders_per_side:
                break
            if index in self._held_levels:
                continue
            intents.append(
                OrderIntent(
                    client_id=f"{BUY_PREFIX}-{index}",
                    side=Side.BUY,
                    price=self.levels[index],
                    quantity=self.quantity_at(index),
                    level_index=index,
                )
            )
        return intents

    def _sell_intents(self) -> list[OrderIntent]:
        """Ventes de sortie pour chaque niveau detenu, au niveau juste au-dessus."""
        intents: list[OrderIntent] = []
        for index in sorted(self._held_levels):
            target = index + 1
            if target >= len(self.levels):
                # Niveau achete au sommet de la plage : pas de sortie possible
                # dans la grille, on garde l'inventaire jusqu'a un repli.
                continue
            if len(intents) >= self.config.active_orders_per_side:
                break
            intents.append(
                OrderIntent(
                    client_id=f"{SELL_PREFIX}-{index}",
                    side=Side.SELL,
                    price=self.levels[target],
                    quantity=self.quantity_at(index),
                    level_index=index,
                )
            )
        return intents

    def on_fill(self, fill: Fill) -> None:
        level = fill.level_index
        if not 0 <= level < len(self.levels):
            logger.warning("Fill ignore : niveau %d hors grille (%s)", level, fill.client_id)
            return

        if fill.side is Side.BUY:
            self._held_levels.add(level)
            logger.info(
                "Achat execute au niveau %d @ %.2f | niveaux detenus : %d",
                level,
                fill.price,
                len(self._held_levels),
            )
        else:
            self._held_levels.discard(level)
            logger.info(
                "Vente executee pour le niveau %d @ %.2f | niveaux detenus : %d",
                level,
                fill.price,
                len(self._held_levels),
            )

    def reset_inventory(self) -> None:
        """Oublie l'inventaire, apres une fermeture forcee par le risk manager."""
        self._held_levels.clear()

    # ------------------------------------------------------------ persistance

    def snapshot_state(self) -> dict[str, object]:
        return {
            "strategy": self.name,
            "held_levels": sorted(self._held_levels),
            "num_grids": self.config.num_grids,
            "lower": self.config.price_range.lower,
            "upper": self.config.price_range.upper,
        }

    def restore_state(self, state: dict[str, object]) -> None:
        """Restaure les niveaux detenus si la grille n'a pas change.

        Une grille redimensionnee rend les index precedents faux : mieux vaut
        repartir a vide que de vendre au mauvais niveau.
        """
        same_grid = (
            state.get("num_grids") == self.config.num_grids
            and state.get("lower") == self.config.price_range.lower
            and state.get("upper") == self.config.price_range.upper
        )
        if not same_grid:
            logger.warning(
                "Etat sauvegarde ignore : la configuration de grille a change depuis la "
                "derniere execution. Le bot repart avec un inventaire vide."
            )
            return
        levels = state.get("held_levels") or []
        if not isinstance(levels, list):
            logger.warning("Etat sauvegarde invalide (held_levels), ignore.")
            return
        self._held_levels = {int(index) for index in levels if 0 <= int(index) < len(self.levels)}
        logger.info("Etat restaure : %d niveaux detenus.", len(self._held_levels))
