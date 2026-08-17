"""Fonctions utilitaires : arrondis exchange, retry, temps UTC, formatage."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


def round_to_tick(price: float, tick_size: float) -> float:
    """Arrondit un prix au multiple de tick_size le plus proche.

    Passe par Decimal : 0.1 + 0.2 en float donne 0.30000000000000004, ce que
    Binance rejette comme prix invalide.
    """
    if tick_size <= 0:
        raise ValueError(f"tick_size doit etre > 0, recu {tick_size}")
    tick = Decimal(str(tick_size))
    quantized = (Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    return float(quantized)


def round_to_step(quantity: float, step_size: float) -> float:
    """Arrondit une quantite au multiple inferieur de step_size.

    Toujours vers le bas : arrondir a la hausse peut depasser le capital
    disponible et faire rejeter l'ordre pour marge insuffisante.
    """
    if step_size <= 0:
        raise ValueError(f"step_size doit etre > 0, recu {step_size}")
    step = Decimal(str(step_size))
    quantized = (Decimal(str(quantity)) / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
    return float(quantized)


def geometric_levels(lower: float, upper: float, count: int) -> list[float]:
    """`count` prix espaces geometriquement, bornes incluses.

    L'espacement geometrique donne un profit constant en pourcentage par
    niveau, contrairement a l'espacement arithmetique qui avantage le bas de
    la plage.
    """
    _validate_range(lower, upper, count)
    ratio = (upper / lower) ** (1 / (count - 1))
    return [lower * ratio**i for i in range(count)]


def arithmetic_levels(lower: float, upper: float, count: int) -> list[float]:
    """`count` prix espaces lineairement, bornes incluses."""
    _validate_range(lower, upper, count)
    step = (upper - lower) / (count - 1)
    return [lower + step * i for i in range(count)]


def _validate_range(lower: float, upper: float, count: int) -> None:
    if lower <= 0:
        raise ValueError(f"lower doit etre > 0, recu {lower}")
    if upper <= lower:
        raise ValueError(f"upper ({upper}) doit etre > lower ({lower})")
    if count < 2:
        raise ValueError(f"count doit etre >= 2, recu {count}")


def utc_day_start(moment: datetime | None = None) -> datetime:
    """Minuit UTC du jour de `moment` (par defaut : maintenant)."""
    now = moment or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("moment doit etre timezone-aware")
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def pct_change(current: float, reference: float) -> float:
    """Variation de `current` par rapport a `reference`, en pourcentage."""
    if reference == 0:
        raise ValueError("reference ne peut pas etre nulle")
    return (current - reference) / abs(reference) * 100.0


def fee_for(notional: float, fee_pct: float) -> float:
    """Frais absolus pour un notional donne."""
    if notional < 0:
        raise ValueError(f"notional doit etre >= 0, recu {notional}")
    return notional * fee_pct / 100.0


def format_usdt(value: float) -> str:
    return f"{value:,.2f} USDT"


def mask_secret(value: str, visible: int = 4) -> str:
    """Masque un secret pour les logs : 'abcd...wxyz' -> 'abcd****'."""
    if not value:
        return "<vide>"
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "*" * (len(value) - visible)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    retryable: tuple[type[Exception], ...],
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    description: str = "operation",
) -> T:
    """Rejoue `operation` avec un backoff exponentiel et un jitter.

    Seules les exceptions listees dans `retryable` sont rejouees : une erreur
    de parametre d'ordre ne s'ameliore pas en la repetant, elle doit remonter
    immediatement.
    """
    if attempts < 1:
        raise ValueError(f"attempts doit etre >= 1, recu {attempts}")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retryable as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            delay *= 0.5 + random.random()  # noqa: S311 - jitter, pas de la crypto
            logger.warning(
                "%s a echoue (tentative %d/%d) : %s. Nouvel essai dans %.1fs",
                description,
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"{description} a echoue apres {attempts} tentatives") from last_error


def clamp(value: float, low: float, high: float) -> float:
    if low > high:
        raise ValueError(f"low ({low}) doit etre <= high ({high})")
    return max(low, min(high, value))


def is_close(a: float, b: float, tolerance: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance)
