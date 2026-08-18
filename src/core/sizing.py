"""Dimensionnement automatique de la grille.

Choisir `lower`, `upper` et `num_grids` a la main est la premiere source
d'erreurs : une plage mal placee et le bot ne trade jamais, trop de niveaux et
chaque ordre passe sous le minimum de l'exchange.

Ce module calcule ces trois valeurs a partir de trois choses connues :
le prix courant, la volatilite recente (ATR) et le capital disponible.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.logger import get_logger
from src.models.domain import Candle, SymbolFilters
from src.utils.helpers import round_to_step, round_to_tick

logger = get_logger("sizing")

ATR_PERIOD = 14
# Plancher de volatilite : si l'ATR mesure est absurdement bas (marche fige,
# historique incomplet), on retombe sur 2% du prix pour ne pas produire une
# plage microscopique dans laquelle aucun ordre ne serait rentable.
MIN_ATR_PCT_OF_PRICE = 2.0


class SizingError(ValueError):
    """Le capital ne permet aucune grille valide sur ce symbole."""


@dataclass(frozen=True, slots=True)
class GridPlan:
    """Resultat du dimensionnement : une grille concrete et executable."""

    lower: float
    upper: float
    num_grids: int
    atr: float
    capital_per_grid: float

    def describe(self) -> str:
        width_pct = (self.upper / self.lower - 1) * 100
        return (
            f"plage {self.lower:,.2f} - {self.upper:,.2f} ({width_pct:.1f}% de large), "
            f"{self.num_grids} niveaux, {self.capital_per_grid:,.2f} USDT par niveau"
        )


def average_true_range(candles: list[Candle], period: int = ATR_PERIOD) -> float:
    """ATR simplifie : moyenne des amplitudes (haut - bas) des N dernieres bougies.

    Mesure de combien le prix bouge sur une periode typique. Sert a dimensionner
    la plage : trop etroite, le prix en sort sans arret ; trop large, le bot ne
    trade jamais.
    """
    if not candles:
        raise SizingError("Aucune bougie disponible pour mesurer la volatilite.")
    if period < 1:
        raise ValueError(f"period doit etre >= 1, recu {period}")

    recent = candles[-period:]
    ranges = [candle.high - candle.low for candle in recent if candle.high >= candle.low]
    if not ranges:
        raise SizingError("Bougies invalides : aucune amplitude exploitable.")
    return sum(ranges) / len(ranges)


def plan_grid(
    *,
    price: float,
    candles: list[Candle],
    capital_usdt: float,
    leverage: int,
    filters: SymbolFilters,
    atr_multiple: float,
    target_grids: int,
) -> GridPlan:
    """Calcule une grille centree sur le prix, compatible avec le capital.

    La plage vaut `atr_multiple` x ATR de chaque cote du prix. Le nombre de
    niveaux part de `target_grids` puis est reduit jusqu'a ce que chaque ordre
    depasse le notional minimum de l'exchange, arrondi compris.
    """
    if price <= 0:
        raise ValueError(f"price doit etre > 0, recu {price}")
    if capital_usdt <= 0:
        raise ValueError(f"capital_usdt doit etre > 0, recu {capital_usdt}")

    atr = _effective_atr(candles, price)
    half_width = atr * atr_multiple
    lower = round_to_tick(max(price - half_width, price * 0.05), filters.tick_size)
    upper = round_to_tick(price + half_width, filters.tick_size)

    num_grids = _largest_viable_grid_count(
        upper=upper,
        capital=capital_usdt * leverage,
        filters=filters,
        target=target_grids,
    )
    plan = GridPlan(
        lower=lower,
        upper=upper,
        num_grids=num_grids,
        atr=atr,
        capital_per_grid=capital_usdt * leverage / num_grids,
    )
    logger.info("Grille calculee automatiquement : %s", plan.describe())
    if num_grids < target_grids:
        logger.warning(
            "Nombre de niveaux reduit de %d a %d : ton capital ne permet pas plus "
            "sans passer sous le minimum de %.0f USDT par ordre impose par l'exchange.",
            target_grids,
            num_grids,
            filters.min_notional,
        )
    return plan


def _effective_atr(candles: list[Candle], price: float) -> float:
    atr = average_true_range(candles)
    floor = price * MIN_ATR_PCT_OF_PRICE / 100.0
    if atr < floor:
        logger.warning(
            "Volatilite mesuree tres faible (%.2f). Plancher de %.2f applique "
            "pour eviter une plage trop etroite.",
            atr,
            floor,
        )
        return floor
    return atr


def _largest_viable_grid_count(
    *, upper: float, capital: float, filters: SymbolFilters, target: int
) -> int:
    """Plus grand nombre de niveaux <= target dont chaque ordre reste executable.

    On teste au prix le plus haut de la plage : c'est la que la quantite est la
    plus petite, donc le cas le plus contraignant apres arrondi au step size.
    """
    for count in range(target, 1, -1):
        if _order_is_viable(capital / count, upper, filters):
            return count

    required = _minimum_capital(upper, filters)
    raise SizingError(
        f"Ton capital de {capital:,.2f} USDT ne permet aucune grille sur ce symbole : "
        f"il faut au moins {required:,.2f} USDT pour placer 2 ordres respectant le "
        f"minimum de {filters.min_notional:,.0f} USDT par ordre. "
        "Augmente le capital, ou choisis un symbole moins cher par unite "
        "(SOLUSDT, DOGEUSDT) dont le pas de quantite est plus fin."
    )


def _order_is_viable(capital_per_grid: float, price: float, filters: SymbolFilters) -> bool:
    quantity = round_to_step(capital_per_grid / price, filters.step_size)
    return quantity >= filters.min_quantity and quantity * price >= filters.min_notional


def _minimum_capital(price: float, filters: SymbolFilters) -> float:
    """Capital minimal pour deux ordres viables, arrondi au step pres."""
    step_notional = filters.step_size * price
    per_order = max(filters.min_notional, step_notional)
    # Une quantite arrondie vers le bas peut perdre jusqu'a un step complet.
    return 2 * (per_order + step_notional)
