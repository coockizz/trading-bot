"""Flux de prix temps reel.

Binance diffuse le mark price sur un websocket public : aucune cle API n'est
necessaire, y compris en dry-run, ce qui permet de tester la strategie sur de
vrais prix sans compte.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from src.core.logger import get_logger

logger = get_logger("websocket")

MAINNET_WS = "wss://fstream.binance.com/ws"
TESTNET_WS = "wss://fstream.binancefuture.com/ws"

_MAX_BACKOFF_S = 60.0
_STALE_AFTER_S = 30.0


class PriceFeed(ABC):
    """Source de prix consommee par le moteur."""

    @abstractmethod
    def prices(self) -> AsyncIterator[float]:
        """Emet un prix a chaque mise a jour."""

    async def close(self) -> None:  # noqa: B027 - facultatif : un flux en memoire n'a rien a fermer
        """Libere les ressources. Sans effet par defaut."""


class BinanceMarkPriceFeed(PriceFeed):
    """Mark price Binance Futures, avec reconnexion automatique.

    Le flux est considere mort si aucun message n'arrive pendant
    `_STALE_AFTER_S` : Binance emet toutes les secondes, un silence plus long
    signifie une connexion fantome qu'il faut recycler plutot qu'attendre.
    """

    def __init__(self, symbol: str, *, testnet: bool = True) -> None:
        self._url = f"{TESTNET_WS if testnet else MAINNET_WS}/{symbol.lower()}@markPrice@1s"
        self._symbol = symbol
        self._closed = False

    async def prices(self) -> AsyncIterator[float]:
        import websockets
        from websockets.exceptions import WebSocketException

        attempt = 0
        while not self._closed:
            try:
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Flux de prix connecte : %s", self._symbol)
                    attempt = 0
                    async for price in self._read(ws):
                        yield price
            except (WebSocketException, OSError, TimeoutError) as exc:
                if self._closed:
                    break
                attempt += 1
                delay = min(2**attempt, _MAX_BACKOFF_S) * (0.5 + random.random())  # noqa: S311
                logger.warning(
                    "Flux de prix interrompu (%s). Reconnexion dans %.1fs (essai %d).",
                    exc,
                    delay,
                    attempt,
                )
                await asyncio.sleep(delay)

    async def _read(self, ws) -> AsyncIterator[float]:
        while not self._closed:
            raw = await asyncio.wait_for(ws.recv(), timeout=_STALE_AFTER_S)
            price = _parse_mark_price(raw)
            if price is not None:
                yield price

    async def close(self) -> None:
        self._closed = True


class ReplayPriceFeed(PriceFeed):
    """Rejoue une serie de prix. Utilise par les tests et le backtest rapide."""

    def __init__(self, series: list[float], *, interval_s: float = 0.0) -> None:
        if not series:
            raise ValueError("La serie de prix ne peut pas etre vide.")
        self._series = series
        self._interval_s = interval_s

    async def prices(self) -> AsyncIterator[float]:
        for price in self._series:
            if self._interval_s:
                await asyncio.sleep(self._interval_s)
            yield price


def _parse_mark_price(raw: str | bytes) -> float | None:
    """Extrait le champ `p` d'un message markPrice. None si le message est autre."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Message websocket illisible, ignore.")
        return None
    value = payload.get("p") if isinstance(payload, dict) else None
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        logger.warning("Prix websocket non numerique, ignore.")
        return None
    return price if price > 0 else None


@contextlib.asynccontextmanager
async def price_feed(symbol: str, *, testnet: bool):
    """Ouvre un flux et garantit sa fermeture."""
    feed = BinanceMarkPriceFeed(symbol, testnet=testnet)
    try:
        yield feed
    finally:
        await feed.close()
