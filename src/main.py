"""Point d'entree du bot : CLI, moteur d'execution, arret propre.

python -m src.main --config config/binance_testnet.yaml
python -m src.main --config config/binance_testnet.yaml --live
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import signal
import sys
import time
from pathlib import Path

from src.core.exchange import BaseExchange, ExchangeError, build_exchange
from src.core.logger import get_logger, setup_logging
from src.core.stats import StatsTracker
from src.core.websocket import BinanceMarkPriceFeed, PriceFeed, ReplayPriceFeed
from src.models.config import BotConfig, ConfigError, load_config
from src.models.domain import OrderIntent
from src.risk.manager import RiskManager
from src.strategy.grid import GridStrategy
from src.utils.helpers import clamp

logger = get_logger("main")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_KILL_SWITCH = 2


class TradingEngine:
    """Boucle principale : prix -> fills -> stats -> risque -> reconciliation."""

    def __init__(
        self,
        config: BotConfig,
        exchange: BaseExchange,
        strategy: GridStrategy,
        feed: PriceFeed,
        *,
        reconcile_every_tick: bool = False,
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.feed = feed
        # En simulation les ticks arrivent sans delai reel : attendre
        # reconcile_interval_s ne poserait le carnet qu'une seule fois.
        self._reconcile_every_tick = reconcile_every_tick
        self.stats: StatsTracker | None = None
        self.risk: RiskManager | None = None
        self._stop_event = asyncio.Event()
        self._last_reconcile = 0.0
        self._halted_by_risk = False

    # ------------------------------------------------------------ cycle de vie

    async def setup(self) -> None:
        await self.exchange.connect()
        equity = await self.exchange.get_balance()
        if equity <= 0:
            raise ExchangeError(
                "Solde USDT nul sur le compte. Alimente le compte testnet avant de lancer le bot."
            )

        trades_csv = Path(self.config.runtime.stats_file).with_name("trades.csv")
        self.stats = StatsTracker(equity, trades_csv=trades_csv)
        self.risk = RiskManager(self.config.risk, equity)
        self.risk.validate_against(self.exchange.filters.min_notional)
        self._validate_grid_notional()
        self._restore_state()

        logger.info(
            "Bot pret | %s | mode %s | capital %.2f USDT | levier x%d",
            self.config.symbol,
            "DRY-RUN" if self.config.dry_run else "REEL",
            equity,
            self.config.strategy.leverage,
        )

    def _validate_grid_notional(self) -> None:
        """Verifie qu'un ordre de grille survit a l'arrondi au step size.

        La quantite est arrondie vers le bas au step size : sur BTCUSDT
        (step 0.001), un notional theorique de 150 USDT peut retomber a 95 USDT
        et se faire rejeter. Sans ce controle, le bot tourne sans jamais poser
        un seul ordre, ce qui ressemble a une panne silencieuse.
        """
        worst_level = max(self.strategy.levels)  # prix le plus haut = quantite la plus petite
        quantity = self.config.strategy.capital_per_grid / worst_level
        price, rounded = self.exchange.normalize(worst_level, quantity)
        if not self.exchange.is_tradable(price, rounded):
            raise ExchangeError(
                f"Chaque niveau dispose de {self.config.strategy.capital_per_grid:.2f} USDT, "
                f"soit {rounded:.6f} apres arrondi au step de "
                f"{self.exchange.filters.step_size}, donc {price * rounded:.2f} USDT de notional "
                f"pour un minimum exchange de {self.exchange.filters.min_notional:.2f} USDT. "
                "Reduis num_grids ou augmente capital_usdt."
            )

    async def run(self) -> int:
        """Consomme le flux de prix jusqu'a l'arret. Retourne le code de sortie."""
        try:
            async for price in self.feed.prices():
                if self._stop_event.is_set():
                    break
                await self._on_price(price)
                if self._halted_by_risk:
                    break
        except asyncio.CancelledError:
            logger.info("Boucle interrompue.")
            raise
        return EXIT_KILL_SWITCH if self._halted_by_risk else EXIT_OK

    async def _on_price(self, price: float) -> None:
        assert self.stats is not None and self.risk is not None  # garanti par setup()

        for fill in await self.exchange.poll_fills(price):
            realized = self.stats.record_fill(fill)
            self.strategy.on_fill(fill)
            logger.info(
                "FILL %s %.6f @ %.2f | frais %.4f | P&L realise %+.4f USDT",
                fill.side,
                fill.quantity,
                fill.price,
                fill.fee,
                realized,
            )

        self.stats.update_market(price)

        verdict = self.risk.evaluate(self.stats)
        if verdict.should_halt:
            await self._trigger_kill_switch(verdict.reason)
            return

        now = time.monotonic()
        due = now - self._last_reconcile >= self.config.runtime.reconcile_interval_s
        if self._reconcile_every_tick or due:
            self._last_reconcile = now
            await self._reconcile(price)
            self.stats.log_summary()
            self._persist()

    async def _reconcile(self, price: float) -> None:
        """Aligne le carnet reel sur le carnet voulu par la strategie."""
        assert self.risk is not None
        position = await self.exchange.get_position()
        wanted: dict[str, OrderIntent] = {
            intent.client_id: intent
            for intent in self.strategy.desired_orders(price)
            if self.risk.allows_order(intent, position, price)
        }
        existing = {order.client_id: order for order in await self.exchange.get_open_orders()}

        for client_id, order in existing.items():
            intent = wanted.get(client_id)
            if intent is None or not _same_price(intent.price, order.price, self.exchange):
                await self.exchange.cancel_order(order)

        for client_id, intent in wanted.items():
            if client_id in existing and _same_price(
                intent.price, existing[client_id].price, self.exchange
            ):
                continue
            await self.exchange.place_order(intent)

    async def _trigger_kill_switch(self, reason: str) -> None:
        self._halted_by_risk = True
        logger.error("Arret d'urgence : %s", reason)
        with contextlib.suppress(ExchangeError):
            await self.exchange.close_all_positions()
        self.strategy.reset_inventory()
        self._persist()

    async def shutdown(self) -> None:
        """Annule les ordres, sauvegarde l'etat, ferme les connexions."""
        logger.info("Arret en cours : annulation des ordres ouverts...")
        with contextlib.suppress(ExchangeError):
            await self.exchange.cancel_all_orders()
        self._persist()
        if self.stats is not None:
            self.stats.log_summary()
        await self.feed.close()
        await self.exchange.close()
        logger.info("Bot arrete proprement.")

    def request_stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------ persistance

    def _persist(self) -> None:
        state_path = Path(self.config.runtime.state_file)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(self.strategy.snapshot_state(), indent=2), encoding="utf-8"
        )
        if self.stats is not None:
            self.stats.write_json(self.config.runtime.stats_file)

    def _restore_state(self) -> None:
        state_path = Path(self.config.runtime.state_file)
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("Etat sauvegarde illisible (%s), demarrage a vide.", exc)
            return
        self.strategy.restore_state(state)


def _same_price(wanted: float, existing: float, exchange: BaseExchange) -> bool:
    """Deux prix sont equivalents s'ils tombent sur le meme tick."""
    tick = exchange.filters.tick_size
    return abs(wanted - existing) < tick


# ------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Bot de grid trading pour Binance Futures.",
    )
    parser.add_argument("--config", required=True, help="Chemin du fichier YAML de configuration.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force la simulation, meme si le fichier de config dit le contraire.",
    )
    parser.add_argument(
        "--simulate",
        type=int,
        metavar="TICKS",
        help=(
            "Rejoue une marche aleatoire de TICKS prix dans la plage configuree, sans reseau. "
            "Implique --dry-run. Sert a valider une configuration hors ligne."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Envoie de VRAIS ordres. Necessite dry_run: false dans la configuration.",
    )
    return parser


def resolve_mode(config: BotConfig, args: argparse.Namespace) -> BotConfig:
    """Determine le mode final. La simulation gagne toujours en cas de doute."""
    if args.dry_run and args.live:
        raise ConfigError("--dry-run et --live sont incompatibles.")
    if args.simulate and args.live:
        raise ConfigError("--simulate et --live sont incompatibles.")
    if args.dry_run or args.simulate:
        return config.model_copy(update={"dry_run": True})
    if args.live:
        if config.dry_run:
            raise ConfigError(
                "--live demande mais la configuration impose dry_run: true. "
                "Passe dry_run a false dans le YAML pour confirmer le trading reel."
            )
        return config
    return config


def _build_feed(config: BotConfig, simulate_ticks: int | None) -> PriceFeed:
    if simulate_ticks:
        series = _random_walk(config, simulate_ticks)
        logger.info("Mode SIMULATION : %d prix rejoues, aucun appel reseau.", len(series))
        return ReplayPriceFeed(series)
    return BinanceMarkPriceFeed(config.symbol, testnet=config.exchange.testnet)


def _random_walk(config: BotConfig, ticks: int) -> list[float]:
    """Marche aleatoire bornee a la plage de la grille, pour valider hors ligne."""
    if ticks < 1:
        raise ConfigError("--simulate attend un nombre de ticks >= 1.")
    lower = config.strategy.price_range.lower
    upper = config.strategy.price_range.upper
    price = (lower + upper) / 2
    step = (upper - lower) / 100
    series = []
    for _ in range(ticks):
        price = clamp(price + random.uniform(-step, step), lower, upper)
        series.append(price)
    return series


async def _run(config: BotConfig, simulate_ticks: int | None = None) -> int:
    strategy = GridStrategy(config.strategy, taker_fee_pct=config.exchange.taker_fee_pct)
    exchange = build_exchange(config)
    feed = _build_feed(config, simulate_ticks)
    engine = TradingEngine(
        config, exchange, strategy, feed, reconcile_every_tick=bool(simulate_ticks)
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows n'expose pas add_signal_handler
            loop.add_signal_handler(sig, engine.request_stop)

    try:
        await engine.setup()
        return await engine.run()
    finally:
        await engine.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = resolve_mode(load_config(args.config), args)
    except ConfigError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return EXIT_ERROR

    setup_logging(config.logging)
    if not config.dry_run:
        logger.warning("MODE REEL ACTIF : des ordres vont etre envoyes avec de vrais fonds.")

    try:
        return asyncio.run(_run(config, args.simulate))
    except (ExchangeError, ConfigError, ValueError) as exc:
        logger.error("Arret sur erreur : %s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        logger.info("Interruption clavier.")
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
