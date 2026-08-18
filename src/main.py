"""Point d'entree du bot : CLI, moteur d'execution, supervision, arret propre.

python -m src.main --config config/testnet.yaml --simulate 500   # hors ligne
python -m src.main --config config/testnet.yaml                  # dry-run
python -m src.main --config config/prod.yaml --live              # reel
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

from src.core.alerts import Alert, AlertDispatcher, AlertLevel, MemoryNotifier, build_notifier
from src.core.exchange import BaseExchange, DryRunExchange, ExchangeError, build_exchange
from src.core.logger import get_logger, setup_logging
from src.core.security import (
    SecurityError,
    audit_environment,
    check_permissions,
    check_testnet_coherence,
    confirm_live_trading,
)
from src.core.sizing import GridPlan, SizingError, plan_grid
from src.core.stats import StatsTracker
from src.core.websocket import BinanceMarkPriceFeed, PriceFeed, ReplayPriceFeed
from src.models.config import BotConfig, ConfigError, PriceRange, load_config
from src.models.domain import OrderIntent
from src.risk.manager import RiskManager
from src.strategy.grid import GridStrategy
from src.utils.helpers import clamp

logger = get_logger("main")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_KILL_SWITCH = 2

# Erreurs qui justifient un redemarrage automatique : elles viennent du reseau
# ou de l'exchange, pas de la configuration, et disparaissent souvent seules.
TRANSIENT_ERRORS = (ExchangeError, OSError, asyncio.TimeoutError)


class TradingEngine:
    """Boucle principale : prix -> fills -> stats -> risque -> reconciliation."""

    def __init__(
        self,
        config: BotConfig,
        exchange: BaseExchange,
        strategy: GridStrategy | None,
        feed: PriceFeed,
        *,
        reconcile_every_tick: bool = False,
        alerts: AlertDispatcher | None = None,
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.strategy = strategy
        self.feed = feed
        self.alerts = alerts
        self.stats: StatsTracker | None = None
        self.risk: RiskManager | None = None
        # En simulation les ticks arrivent sans delai reel : attendre
        # reconcile_interval_s ne poserait le carnet qu'une seule fois.
        self._reconcile_every_tick = reconcile_every_tick
        self._stop_event = asyncio.Event()
        self._last_reconcile = 0.0
        self._last_summary = time.monotonic()
        self._reported_trades = 0
        self._halted_by_risk = False
        self._equity = 0.0

    # ------------------------------------------------------------ cycle de vie

    async def setup(self) -> None:
        await self.exchange.connect()
        await self._run_security_checks()

        self._equity = await self.exchange.get_balance()
        if self._equity <= 0:
            raise ExchangeError(
                "Le compte ne contient aucun USDT. Alimente-le avant de lancer le bot "
                "(sur le testnet, le solde est credite automatiquement a la creation)."
            )

        trades_csv = Path(self.config.runtime.stats_file).with_name("trades.csv")
        self.stats = StatsTracker(self._equity, trades_csv=trades_csv)
        self.risk = RiskManager(self.config.risk, self._equity)
        self.risk.validate_against(self.exchange.filters.min_notional)

        if self.strategy is not None:
            self._validate_grid_notional()
            self._restore_state()

        logger.info(
            "Bot pret | %s | mode %s | capital %.2f USDT | levier x%d",
            self.config.symbol,
            self._mode_label(),
            self._equity,
            self.config.strategy.leverage,
        )

    async def _run_security_checks(self) -> None:
        """Refuse de demarrer sur une cle API dangereuse ou un compte incoherent."""
        permissions = await self.exchange.fetch_permissions()
        check_permissions(permissions, self.config)
        balance = await self.exchange.get_balance()
        check_testnet_coherence(self.config, balance)
        audit_environment(self.config)
        confirm_live_trading(self.config, balance)

    def _mode_label(self) -> str:
        if self.config.dry_run:
            return "DRY-RUN (aucun ordre reel)"
        return "REEL TESTNET" if self.config.exchange.testnet else "REEL MAINNET"

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
        await self._ensure_strategy(price)

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
        await self._announce_closed_trades()

        self.stats.update_market(price)
        await self._warn_on_drawdown()

        verdict = self.risk.evaluate(self.stats)
        if verdict.should_halt:
            await self._trigger_kill_switch(verdict.reason)
            return

        now = time.monotonic()
        if self._reconcile_every_tick or (
            now - self._last_reconcile >= self.config.runtime.reconcile_interval_s
        ):
            self._last_reconcile = now
            await self._reconcile(price)
            self.stats.log_summary()
            self._persist()
        await self._maybe_send_summary()

    async def _ensure_strategy(self, price: float) -> None:
        """Construit la grille au premier prix recu, si elle est automatique.

        Le dimensionnement automatique a besoin du prix courant : il ne peut pas
        se faire avant l'arrivee du premier tick.
        """
        if self.strategy is not None:
            return

        plan = await self._plan_grid(price)
        resolved = self.config.strategy.model_copy(
            update={
                "price_range": PriceRange(lower=plan.lower, upper=plan.upper),
                "num_grids": plan.num_grids,
            }
        )
        # BotConfig est immuable : on remplace l'objet plutot que de le muter,
        # pour que le reste du moteur lise une configuration coherente.
        self.config = self.config.model_copy(update={"strategy": resolved})
        self.strategy = GridStrategy(resolved, taker_fee_pct=self.config.exchange.taker_fee_pct)
        self._validate_grid_notional()
        self._restore_state()

        if self.alerts is not None:
            await self.alerts.bot_started(
                mode=self._mode_label(), capital=self._equity, grid=plan.describe()
            )

    async def _plan_grid(self, price: float) -> GridPlan:
        if isinstance(self.exchange, DryRunExchange):
            self.exchange.set_reference_price(price)
        candles = await self.exchange.fetch_candles()
        return plan_grid(
            price=price,
            candles=candles,
            capital_usdt=self.config.strategy.capital_usdt,
            leverage=self.config.strategy.leverage,
            filters=self.exchange.filters,
            atr_multiple=self.config.strategy.atr_multiple,
            target_grids=self.config.strategy.target_grids,
        )

    def _validate_grid_notional(self) -> None:
        """Verifie qu'un ordre de grille survit a l'arrondi au step size.

        La quantite est arrondie vers le bas : sur BTCUSDT (step 0.001), un
        notional theorique de 150 USDT peut retomber a 95 USDT et se faire
        rejeter. Sans ce controle, le bot tourne sans jamais poser un ordre, ce
        qui ressemble a une panne silencieuse.
        """
        assert self.strategy is not None
        worst_level = max(self.strategy.levels)  # prix le plus haut = quantite la plus petite
        quantity = self.config.strategy.capital_per_grid / worst_level
        price, rounded = self.exchange.normalize(worst_level, quantity)
        if not self.exchange.is_tradable(price, rounded):
            raise ExchangeError(
                f"Chaque niveau dispose de {self.config.strategy.capital_per_grid:.2f} USDT, "
                f"soit {price * rounded:.2f} USDT de notional apres arrondi, pour un minimum "
                f"exchange de {self.exchange.filters.min_notional:.2f} USDT.\n"
                "Solution : laisse price_range et num_grids vides pour que le bot calcule "
                "lui-meme une grille compatible avec ton capital."
            )

    async def _reconcile(self, price: float) -> None:
        """Aligne le carnet reel sur le carnet voulu par la strategie."""
        assert self.risk is not None and self.strategy is not None
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

    # ----------------------------------------------------------------- alertes

    async def _announce_closed_trades(self) -> None:
        """Notifie chaque cycle achat/vente termine depuis le dernier appel."""
        assert self.stats is not None
        if self.alerts is None:
            return
        for trade in self.stats.closed_trades[self._reported_trades :]:
            await self.alerts.trade_closed(
                entry=trade.entry_price, exit_price=trade.exit_price, net_pnl=trade.net_pnl
            )
        self._reported_trades = len(self.stats.closed_trades)

    async def _warn_on_drawdown(self) -> None:
        """Previent quand la perte du jour approche la limite du kill-switch."""
        assert self.stats is not None
        if self.alerts is None:
            return
        daily_loss_pct = max(0.0, -self.stats.daily_pnl) / self._equity * 100.0
        await self.alerts.drawdown_warning(
            current_pct=daily_loss_pct, limit_pct=self.config.risk.max_daily_drawdown_pct
        )

    async def _maybe_send_summary(self) -> None:
        """Envoie le resume periodique quand l'intervalle est ecoule."""
        if not self.config.monitoring.summary_enabled:
            return
        interval_s = self.config.monitoring.summary_interval_hours * 3600
        if time.monotonic() - self._last_summary < interval_s:
            return
        self._last_summary = time.monotonic()
        await self.send_summary()

    async def send_summary(self) -> str:
        """Ecrit et notifie un resume de l'activite. Retourne le texte envoye."""
        assert self.stats is not None
        position = await self.exchange.get_position()
        held = len(self.strategy.held_levels) if self.strategy else 0
        text = self.stats.summary_text(position_qty=position.quantity, held_levels=held)
        logger.info("Resume periodique :\n%s", text)

        path = Path(self.config.monitoring.summary_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if self.alerts is not None:
            await self.alerts.summary(text)
        return text

    async def _trigger_kill_switch(self, reason: str) -> None:
        self._halted_by_risk = True
        logger.error("Arret d'urgence : %s", reason)
        with contextlib.suppress(ExchangeError):
            await self.exchange.close_all_positions()
        if self.strategy is not None:
            self.strategy.reset_inventory()
        self._persist()
        if self.alerts is not None:
            await self.alerts.kill_switch(reason=reason)

    async def shutdown(self, *, reason: str = "arret demande") -> None:
        """Annule les ordres, sauvegarde l'etat, ferme les connexions."""
        logger.info("Arret en cours : annulation des ordres ouverts...")
        with contextlib.suppress(ExchangeError):
            await self.exchange.cancel_all_orders()
        self._persist()
        if self.stats is not None:
            self.stats.log_summary()
            if self.alerts is not None and not self._halted_by_risk:
                await self.alerts.bot_stopped(reason=reason, summary=self.stats.summary_text())
        if self.alerts is not None:
            await self.alerts.close()
        await self.feed.close()
        await self.exchange.close()
        logger.info("Bot arrete proprement.")

    def request_stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------ persistance

    def _persist(self) -> None:
        if self.strategy is None:
            return
        state_path = Path(self.config.runtime.state_file)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(self.strategy.snapshot_state(), indent=2), encoding="utf-8"
        )
        if self.stats is not None:
            self.stats.write_json(self.config.runtime.stats_file)

    def _restore_state(self) -> None:
        assert self.strategy is not None
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
    return abs(wanted - existing) < exchange.filters.tick_size


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
            "Rejoue une marche aleatoire de TICKS prix, sans reseau ni cle API. "
            "Implique --dry-run. Sert a valider une configuration hors ligne."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Envoie de VRAIS ordres. Necessite dry_run: false dans la configuration.",
    )
    parser.add_argument(
        "--profile",
        choices=("prudent", "equilibre", "agressif"),
        help="Surcharge le profil de risque du fichier de configuration.",
    )
    parser.add_argument(
        "--test-alerts",
        action="store_true",
        help="Envoie une alerte de test sur Discord et quitte. Ne trade pas.",
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
                "--live demande mais la configuration impose dry_run: true.\n"
                "Ouvre ton fichier de config et remplace 'dry_run: true' par "
                "'dry_run: false' pour confirmer que tu veux trader reellement."
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
    """Marche aleatoire bornee, pour valider une configuration hors ligne."""
    if ticks < 1:
        raise ConfigError("--simulate attend un nombre de ticks >= 1.")
    price_range = config.strategy.price_range
    lower = price_range.lower if price_range else 90_000.0
    upper = price_range.upper if price_range else 110_000.0
    price = (lower + upper) / 2
    step = (upper - lower) / 100
    series = []
    for _ in range(ticks):
        price = clamp(price + random.uniform(-step, step), lower, upper)  # noqa: S311
        series.append(price)
    return series


async def _test_alerts(config: BotConfig) -> int:
    """Envoie une alerte de test et rend compte du resultat."""
    if not config.alerts.enabled:
        print(
            "Les alertes sont desactivees dans ta configuration.\n"
            "Mets 'alerts: { enabled: true }' puis relance cette commande."
        )
        return EXIT_ERROR

    try:
        notifier = build_notifier(config.alerts)
    except ConfigError as exc:
        print(f"Configuration des alertes incomplete :\n{exc}")
        return EXIT_ERROR

    alert = Alert(
        title=f"Test des alertes - {config.symbol}",
        body=(
            "Si tu lis ce message dans Discord, les alertes fonctionnent.\n"
            "Tu recevras : trades fermes, approche du drawdown, kill-switch, "
            "resumes periodiques."
        ),
        level=AlertLevel.INFO,
    )
    ok = await notifier.send(alert)
    await notifier.close()

    if ok:
        print("Alerte envoyee. Verifie ton salon Discord.")
        return EXIT_OK
    print(
        "L'envoi a echoue. Verifie que DISCORD_WEBHOOK_URL contient bien l'URL "
        "complete du webhook, et que ta connexion sortante n'est pas bloquee."
    )
    return EXIT_ERROR


async def _run_once(config: BotConfig, simulate_ticks: int | None) -> int:
    """Un cycle de vie complet du moteur. Les erreurs remontent a la supervision."""
    exchange = build_exchange(config)
    feed = _build_feed(config, simulate_ticks)
    notifier = build_notifier(config.alerts) if not simulate_ticks else MemoryNotifier()
    alerts = AlertDispatcher(notifier, config.alerts, symbol=config.symbol)

    strategy = (
        None
        if config.strategy.is_auto
        else GridStrategy(config.strategy, taker_fee_pct=config.exchange.taker_fee_pct)
    )
    engine = TradingEngine(
        config,
        exchange,
        strategy,
        feed,
        reconcile_every_tick=bool(simulate_ticks),
        alerts=alerts,
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


async def _supervise(config: BotConfig, simulate_ticks: int | None) -> int:
    """Relance le moteur apres une erreur transitoire.

    Une coupure reseau ne doit pas mettre fin a la journee du bot. En revanche
    le kill-switch et les erreurs de configuration sont definitifs : les
    rejouer ne ferait que repeter la meme perte ou la meme erreur.
    """
    attempts = 0
    while True:
        try:
            return await _run_once(config, simulate_ticks)
        except TRANSIENT_ERRORS as exc:
            attempts += 1
            if attempts > config.runtime.max_restarts:
                logger.error("Arret definitif apres %d redemarrages : %s", attempts - 1, exc)
                return EXIT_ERROR
            delay = config.runtime.restart_delay_s
            logger.warning(
                "Erreur transitoire (%s). Redemarrage %d/%d dans %.0fs.",
                exc,
                attempts,
                config.runtime.max_restarts,
                delay,
            )
            await asyncio.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.profile:
            config = load_config_with_profile(args.config, args.profile)
        config = resolve_mode(config, args)
    except ConfigError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return EXIT_ERROR

    setup_logging(config.logging)

    if args.test_alerts:
        return asyncio.run(_test_alerts(config))

    if not config.dry_run:
        logger.warning("MODE REEL ACTIF : des ordres vont etre envoyes avec de vrais fonds.")

    try:
        return asyncio.run(_supervise(config, args.simulate))
    except (SecurityError, ConfigError, SizingError, ValueError) as exc:
        logger.error("Arret sur erreur : %s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        logger.info("Interruption clavier.")
        return EXIT_OK


def load_config_with_profile(path: str, profile: str) -> BotConfig:
    """Recharge la configuration en forcant un profil de risque."""
    import yaml

    from src.models.profiles import apply_profile

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw["profile"] = profile
    # Le profil doit primer sur les anciennes valeurs du fichier : on retire
    # les champs qu'il pilote avant de le reappliquer.
    for section, keys in (
        ("strategy", ("atr_multiple", "target_grids", "active_orders_per_side", "leverage")),
        ("risk", ("max_daily_drawdown_pct", "stop_loss_pct", "max_position_size_pct")),
    ):
        if isinstance(raw.get(section), dict):
            for key in keys:
                raw[section].pop(key, None)
    try:
        return BotConfig.model_validate(apply_profile(raw))
    except Exception as exc:  # noqa: BLE001 - remonte comme erreur de config lisible
        raise ConfigError(f"Profil '{profile}' inapplicable a {path} : {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
