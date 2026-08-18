"""Acces exchange : interface commune, implementation Binance Futures (ccxt)
et implementation dry-run entierement locale.

Toute la strategie parle a `BaseExchange`. Ajouter un exchange revient a ecrire
une nouvelle sous-classe, sans toucher a la strategie ni au risk manager.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.core.logger import get_logger
from src.core.security import ApiPermissions, parse_permissions
from src.models.config import BotConfig, resolve_credentials
from src.models.domain import Candle, Fill, Order, OrderIntent, Position, Side, SymbolFilters
from src.utils.helpers import fee_for, mask_secret, retry_async, round_to_step, round_to_tick

logger = get_logger("exchange")


class ExchangeError(RuntimeError):
    """Erreur exchange non recuperable (parametres invalides, refus definitif)."""


class BaseExchange(ABC):
    """Contrat minimal attendu par la strategie et le risk manager."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.symbol = config.symbol
        self._filters: SymbolFilters | None = None

    @property
    def filters(self) -> SymbolFilters:
        if self._filters is None:
            raise ExchangeError("Filtres du symbole non charges : appelle connect() d'abord.")
        return self._filters

    def normalize(self, price: float, quantity: float) -> tuple[float, float]:
        """Aligne prix et quantite sur le tick size / step size du symbole."""
        return (
            round_to_tick(price, self.filters.tick_size),
            round_to_step(quantity, self.filters.step_size),
        )

    def is_tradable(self, price: float, quantity: float) -> bool:
        """Vrai si l'ordre respecte les minimums de l'exchange."""
        return (
            quantity >= self.filters.min_quantity and price * quantity >= self.filters.min_notional
        )

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_balance(self) -> float: ...

    @abstractmethod
    async def get_position(self) -> Position: ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def place_order(self, intent: OrderIntent) -> Order | None: ...

    @abstractmethod
    async def cancel_order(self, order: Order) -> None: ...

    @abstractmethod
    async def close_all_positions(self) -> None: ...

    @abstractmethod
    async def poll_fills(self, mark_price: float) -> list[Fill]: ...

    @abstractmethod
    async def fetch_candles(self, limit: int = 14) -> list[Candle]:
        """Bougies journalieres recentes, pour mesurer la volatilite."""

    async def fetch_permissions(self) -> ApiPermissions:
        """Permissions de la cle API. Inconnues par defaut."""
        return ApiPermissions.unknown()

    async def cancel_all_orders(self) -> None:
        for order in await self.get_open_orders():
            await self.cancel_order(order)


# --------------------------------------------------------------------- dry-run


class DryRunExchange(BaseExchange):
    """Exchange simule : aucun appel reseau, aucun ordre reel.

    Les ordres limites sont remplis des que le prix simule traverse leur niveau.
    Les frais maker sont appliques, la marge n'est pas modelisee (leverage 1
    impose par la config quand le capital est faible).
    """

    def __init__(self, config: BotConfig, *, filters: SymbolFilters | None = None) -> None:
        super().__init__(config)
        self._orders: dict[str, Order] = {}
        self._balance = config.strategy.capital_usdt
        self._position_qty = 0.0
        self._position_entry = 0.0
        self._last_price: float | None = None
        self._preset_filters = filters
        self._next_id = 0

    async def connect(self) -> None:
        # BTCUSDT sur Binance Futures : tick 0.10, step 0.001, notional mini 100.
        self._filters = self._preset_filters or SymbolFilters(
            tick_size=0.1, step_size=0.001, min_notional=100.0, min_quantity=0.001
        )
        logger.info("Mode DRY-RUN : aucun ordre ne sera envoye a %s", self.config.exchange.name)

    async def close(self) -> None:
        self._orders.clear()

    async def get_balance(self) -> float:
        return self._balance

    async def get_position(self) -> Position:
        return Position(quantity=self._position_qty, entry_price=self._position_entry)

    async def get_open_orders(self) -> list[Order]:
        return list(self._orders.values())

    async def place_order(self, intent: OrderIntent) -> Order | None:
        price, quantity = self.normalize(intent.price, intent.quantity)
        if not self.is_tradable(price, quantity):
            logger.debug(
                "Ordre ignore (sous les minimums) : %s %.6f @ %.2f", intent.side, quantity, price
            )
            return None
        self._next_id += 1
        order = Order(
            order_id=f"dry-{self._next_id}",
            client_id=intent.client_id,
            side=intent.side,
            price=price,
            quantity=quantity,
        )
        self._orders[order.client_id] = order
        logger.debug(
            "DRY placement %s %.6f @ %.2f (%s)", order.side, quantity, price, order.client_id
        )
        return order

    async def cancel_order(self, order: Order) -> None:
        self._orders.pop(order.client_id, None)

    async def close_all_positions(self) -> None:
        if self._last_price is None or abs(self._position_qty) < 1e-12:
            self._position_qty = 0.0
            return
        logger.info("DRY fermeture de position : %.6f @ %.2f", self._position_qty, self._last_price)
        self._apply_trade(
            Side.SELL if self._position_qty > 0 else Side.BUY,
            abs(self._position_qty),
            self._last_price,
        )

    async def poll_fills(self, mark_price: float) -> list[Fill]:
        """Remplit les ordres traverses par le prix. A appeler a chaque tick."""
        self._last_price = mark_price
        fills: list[Fill] = []
        for client_id, order in list(self._orders.items()):
            crossed = (order.side is Side.BUY and mark_price <= order.price) or (
                order.side is Side.SELL and mark_price >= order.price
            )
            if not crossed:
                continue
            del self._orders[client_id]
            fee = fee_for(order.price * order.quantity, self.config.exchange.maker_fee_pct)
            self._apply_trade(order.side, order.quantity, order.price)
            fills.append(
                Fill(
                    client_id=client_id,
                    side=order.side,
                    price=order.price,
                    quantity=order.quantity,
                    fee=fee,
                    level_index=_level_from_client_id(client_id),
                )
            )
        return fills

    async def fetch_candles(self, limit: int = 14) -> list[Candle]:
        """Bougies synthetiques autour du dernier prix connu.

        En dry-run il n'y a pas d'historique reel : on simule une amplitude
        journaliere de 3%, proche de la volatilite habituelle de BTC. Cela
        suffit a dimensionner une grille de demonstration ; en mode reel les
        vraies bougies sont utilisees.
        """
        reference = self._last_price or self.config.strategy.capital_usdt
        if self._last_price is None and self.config.strategy.price_range is not None:
            price_range = self.config.strategy.price_range
            reference = (price_range.lower + price_range.upper) / 2
        half = reference * 0.015
        return [
            Candle(high=reference + half, low=reference - half, close=reference)
            for _ in range(limit)
        ]

    def set_reference_price(self, price: float) -> None:
        """Fixe le prix de reference avant tout fill (dimensionnement automatique)."""
        if price <= 0:
            raise ValueError(f"price doit etre > 0, recu {price}")
        self._last_price = price

    def _apply_trade(self, side: Side, quantity: float, price: float) -> None:
        signed = quantity * side.sign
        new_qty = self._position_qty + signed
        if self._position_qty == 0 or (self._position_qty > 0) == (signed > 0):
            cost = self._position_entry * abs(self._position_qty) + price * quantity
            self._position_entry = cost / abs(new_qty) if new_qty else 0.0
        elif abs(new_qty) < 1e-12:
            self._position_entry = 0.0
        elif (new_qty > 0) != (self._position_qty > 0):
            self._position_entry = price
        self._position_qty = 0.0 if abs(new_qty) < 1e-12 else new_qty


# --------------------------------------------------------------------- binance


class BinanceFuturesExchange(BaseExchange):
    """Wrapper ccxt autour de Binance USDT-M Futures.

    Les fills sont detectes en comparant deux instantanes du carnet d'ordres
    ouverts : un ordre limite qui disparait sans avoir ete annule par le bot a
    ete execute a son prix limite (donc en maker).
    """

    def __init__(self, config: BotConfig) -> None:
        super().__init__(config)
        self._client: Any = None
        self._market_symbol = _to_ccxt_symbol(config.symbol)
        self._tracked: dict[str, Order] = {}
        self._cancelled: set[str] = set()

    async def connect(self) -> None:
        import ccxt.async_support as ccxt  # import tardif : ccxt est lourd a charger

        creds = resolve_credentials(self.config.exchange)
        logger.info(
            "Connexion Binance Futures (testnet=%s) avec la cle %s",
            self.config.exchange.testnet,
            mask_secret(creds.api_key),
        )
        self._client = ccxt.binanceusdm(
            {
                "apiKey": creds.api_key,
                "secret": creds.api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "recvWindow": self.config.exchange.recv_window_ms,
                },
            }
        )
        self._client.set_sandbox_mode(self.config.exchange.testnet)

        markets = await self._call(self._client.load_markets, description="load_markets")
        market = markets.get(self._market_symbol)
        if market is None:
            raise ExchangeError(f"Symbole inconnu sur Binance Futures : {self.config.symbol}")
        self._filters = _filters_from_market(market)
        await self._set_leverage()

    async def _set_leverage(self) -> None:
        try:
            await self._call(
                lambda: self._client.set_leverage(
                    self.config.strategy.leverage, self._market_symbol
                ),
                description="set_leverage",
            )
        except Exception as exc:  # noqa: BLE001 - levier deja correct = erreur benigne
            logger.warning("Impossible de fixer le levier (%s). Valeur du compte conservee.", exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def get_balance(self) -> float:
        balance = await self._call(self._client.fetch_balance, description="fetch_balance")
        return float(balance.get("USDT", {}).get("total", 0.0))

    async def get_position(self) -> Position:
        positions = await self._call(
            lambda: self._client.fetch_positions([self._market_symbol]),
            description="fetch_positions",
        )
        for raw in positions:
            contracts = float(raw.get("contracts") or 0.0)
            if contracts == 0:
                continue
            sign = -1 if str(raw.get("side", "long")).lower() == "short" else 1
            return Position(
                quantity=contracts * sign, entry_price=float(raw.get("entryPrice") or 0.0)
            )
        return Position(quantity=0.0, entry_price=0.0)

    async def get_open_orders(self) -> list[Order]:
        raw_orders = await self._call(
            lambda: self._client.fetch_open_orders(self._market_symbol),
            description="fetch_open_orders",
        )
        orders = [
            Order(
                order_id=str(raw["id"]),
                client_id=str(raw.get("clientOrderId") or raw["id"]),
                side=Side(str(raw["side"]).lower()),
                price=float(raw["price"]),
                quantity=float(raw["amount"]),
            )
            for raw in raw_orders
        ]
        self._tracked = {order.client_id: order for order in orders}
        return orders

    async def place_order(self, intent: OrderIntent) -> Order | None:
        price, quantity = self.normalize(intent.price, intent.quantity)
        if not self.is_tradable(price, quantity):
            logger.debug("Ordre ignore (sous les minimums) : %.6f @ %.2f", quantity, price)
            return None

        raw = await self._call(
            lambda: self._client.create_order(
                self._market_symbol,
                "limit",
                intent.side.value,
                quantity,
                price,
                {"clientOrderId": intent.client_id, "timeInForce": "GTX"},  # GTX = maker only
            ),
            description=f"create_order {intent.side} @ {price}",
        )
        order = Order(
            order_id=str(raw["id"]),
            client_id=intent.client_id,
            side=intent.side,
            price=price,
            quantity=quantity,
        )
        self._tracked[order.client_id] = order
        logger.info("Ordre place : %s %.6f %s @ %.2f", order.side, quantity, self.symbol, price)
        return order

    async def cancel_order(self, order: Order) -> None:
        await self._call(
            lambda: self._client.cancel_order(order.order_id, self._market_symbol),
            description=f"cancel_order {order.order_id}",
        )
        self._tracked.pop(order.client_id, None)
        self._cancelled.add(order.client_id)
        logger.info("Ordre annule : %s @ %.2f", order.client_id, order.price)

    async def close_all_positions(self) -> None:
        await self.cancel_all_orders()
        position = await self.get_position()
        if position.is_flat:
            logger.info("Aucune position a fermer.")
            return
        side = Side.SELL if position.quantity > 0 else Side.BUY
        quantity = round_to_step(abs(position.quantity), self.filters.step_size)
        logger.warning("Fermeture au marche de %.6f %s (%s)", quantity, self.symbol, side)
        await self._call(
            lambda: self._client.create_order(
                self._market_symbol, "market", side.value, quantity, None, {"reduceOnly": True}
            ),
            description="close_position",
        )

    async def poll_fills(self, mark_price: float) -> list[Fill]:  # noqa: ARG002 - prix inutile ici
        """Detecte les fills par difference de carnet (le prix n'est pas requis)."""
        previous = dict(self._tracked)
        current = {order.client_id: order for order in await self.get_open_orders()}
        fills: list[Fill] = []
        for client_id, order in previous.items():
            if client_id in current or client_id in self._cancelled:
                continue
            fills.append(
                Fill(
                    client_id=client_id,
                    side=order.side,
                    price=order.price,
                    quantity=order.quantity,
                    fee=fee_for(order.price * order.quantity, self.config.exchange.maker_fee_pct),
                    level_index=_level_from_client_id(client_id),
                )
            )
        self._cancelled.clear()
        return fills

    async def fetch_candles(self, limit: int = 14) -> list[Candle]:
        raw = await self._call(
            lambda: self._client.fetch_ohlcv(self._market_symbol, "1d", None, limit),
            description="fetch_ohlcv",
        )
        candles = [
            Candle(high=float(row[2]), low=float(row[3]), close=float(row[4]))
            for row in raw
            if row and len(row) >= 5
        ]
        if not candles:
            raise ExchangeError("Binance n'a retourne aucune bougie pour ce symbole.")
        return candles

    async def fetch_permissions(self) -> ApiPermissions:
        """Interroge l'endpoint des restrictions de cle API.

        Cet endpoint appartient a l'API Spot et n'existe pas sur le testnet
        Futures : son absence n'est pas une erreur, seulement une verification
        impossible, signalee comme telle.
        """
        try:
            raw = await self._call(
                self._client.sapi_get_account_apirestrictions,
                description="fetch_api_restrictions",
            )
        except (ExchangeError, AttributeError) as exc:
            logger.warning("Permissions de la cle non verifiables : %s", exc)
            return ApiPermissions.unknown()
        return parse_permissions(raw)

    async def _call(self, operation, *, description: str):
        """Execute un appel ccxt avec retry sur les erreurs transitoires."""
        import ccxt

        retryable = (
            ccxt.NetworkError,
            ccxt.RequestTimeout,
            ccxt.ExchangeNotAvailable,
            ccxt.DDoSProtection,
        )
        try:
            return await retry_async(
                lambda: _as_awaitable(operation),
                retryable=retryable,
                description=description,
            )
        except ccxt.AuthenticationError as exc:
            raise ExchangeError(
                "Authentification refusee par Binance. Verifie tes cles API, leurs "
                "permissions Futures, et que testnet correspond bien au type de cle."
            ) from exc
        except ccxt.InsufficientFunds as exc:
            raise ExchangeError(f"Fonds insuffisants pour {description} : {exc}") from exc
        except ccxt.ExchangeError as exc:
            raise ExchangeError(f"Binance a refuse {description} : {exc}") from exc


async def _as_awaitable(operation):
    result = operation()
    return await result if hasattr(result, "__await__") else result


def _filters_from_market(market: dict[str, Any]) -> SymbolFilters:
    limits = market.get("limits", {})
    precision = market.get("precision", {})
    return SymbolFilters(
        tick_size=float(precision.get("price") or 0.1),
        step_size=float(precision.get("amount") or 0.001),
        min_notional=float((limits.get("cost") or {}).get("min") or 5.0),
        min_quantity=float((limits.get("amount") or {}).get("min") or 0.0),
    )


def _to_ccxt_symbol(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC/USDT:USDT' (notation des perpetuels ccxt)."""
    for quote in ("USDT", "USDC", "BUSD"):
        if symbol.endswith(quote):
            return f"{symbol[: -len(quote)]}/{quote}:{quote}"
    raise ExchangeError(f"Symbole non reconnu : {symbol} (quote attendue : USDT/USDC/BUSD)")


def _level_from_client_id(client_id: str) -> int:
    """Extrait l'index de niveau encode dans le client id ('grid-b-12' -> 12)."""
    tail = client_id.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else -1


def build_exchange(config: BotConfig) -> BaseExchange:
    """Fabrique l'implementation correspondant au mode configure."""
    return DryRunExchange(config) if config.dry_run else BinanceFuturesExchange(config)
