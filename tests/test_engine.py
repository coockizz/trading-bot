"""Tests du moteur : dimensionnement auto, alertes, resume, supervision."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.alerts import AlertDispatcher, AlertLevel, MemoryNotifier
from src.core.exchange import DryRunExchange, ExchangeError
from src.core.websocket import ReplayPriceFeed
from src.main import TradingEngine, _supervise, load_config_with_profile, resolve_mode
from src.models.config import AlertsConfig


def _engine(bot_config, test_filters, series, *, strategy=None):
    exchange = DryRunExchange(bot_config, filters=test_filters)
    notifier = MemoryNotifier()
    alerts = AlertDispatcher(notifier, AlertsConfig(enabled=True), symbol=bot_config.symbol)
    engine = TradingEngine(
        bot_config,
        exchange,
        strategy,
        ReplayPriceFeed(series or [1.0]),
        reconcile_every_tick=True,
        alerts=alerts,
    )
    return engine, notifier


@pytest.fixture
def auto_config(bot_config):
    """Configuration sans plage ni nombre de niveaux : le bot doit les calculer."""
    return bot_config.model_copy(
        update={
            "strategy": bot_config.strategy.model_copy(
                update={"price_range": None, "num_grids": None, "target_grids": 10}
            )
        }
    )


class TestAutoSizing:
    async def test_grid_is_built_on_the_first_price(self, auto_config, test_filters):
        engine, _ = _engine(auto_config, test_filters, [])
        await engine.setup()
        assert engine.strategy is None

        await engine._on_price(100_000.0)
        assert engine.strategy is not None
        assert engine.config.strategy.num_grids is not None
        await engine.shutdown()

    async def test_generated_range_contains_the_current_price(self, auto_config, test_filters):
        engine, _ = _engine(auto_config, test_filters, [])
        await engine.setup()
        await engine._on_price(100_000.0)

        price_range = engine.config.strategy.resolved_range
        assert price_range.lower < 100_000.0 < price_range.upper
        await engine.shutdown()

    async def test_orders_are_placed_after_auto_sizing(self, auto_config, test_filters):
        engine, _ = _engine(auto_config, test_filters, [])
        await engine.setup()
        await engine._on_price(100_000.0)
        assert await engine.exchange.get_open_orders()
        await engine.shutdown()

    async def test_startup_alert_reports_the_computed_grid(self, auto_config, test_filters):
        engine, notifier = _engine(auto_config, test_filters, [])
        await engine.setup()
        await engine._on_price(100_000.0)

        started = [a for a in notifier.sent if "demarre" in a.title]
        assert len(started) == 1
        assert "niveaux" in started[0].body
        await engine.shutdown()


class TestEngineAlerts:
    async def test_closed_trade_triggers_one_alert(self, bot_config, test_filters, strategy_config):
        from src.strategy.grid import GridStrategy

        engine, notifier = _engine(
            bot_config, test_filters, [], strategy=GridStrategy(strategy_config)
        )
        await engine.setup()
        for price in (100_000.0, 94_000.0, 100_000.0):
            await engine._on_price(price)

        trade_alerts = [a for a in notifier.sent if "Gain" in a.title or "Perte" in a.title]
        assert len(trade_alerts) == len(engine.stats.closed_trades)
        await engine.shutdown()

    async def test_kill_switch_sends_a_critical_alert(self, bot_config, test_filters):
        from src.strategy.grid import GridStrategy

        engine, notifier = _engine(
            bot_config, test_filters, [], strategy=GridStrategy(bot_config.strategy)
        )
        await engine.setup()
        for price in (100_000.0, 91_000.0, 60_000.0):
            await engine._on_price(price)

        assert engine.risk.is_halted
        critical = [a for a in notifier.sent if a.level is AlertLevel.CRITICAL]
        assert len(critical) == 1
        await engine.shutdown()

    async def test_summary_is_written_and_notified(self, bot_config, test_filters):
        from src.strategy.grid import GridStrategy

        engine, notifier = _engine(
            bot_config, test_filters, [], strategy=GridStrategy(bot_config.strategy)
        )
        await engine.setup()
        await engine._on_price(100_000.0)
        text = await engine.send_summary()

        assert "P&L total" in text
        assert Path(bot_config.monitoring.summary_file).read_text(encoding="utf-8") == text
        assert any("Resume" in a.title for a in notifier.sent)
        await engine.shutdown()


class TestSupervision:
    async def test_transient_error_is_retried_then_gives_up(self, bot_config, monkeypatch):
        """Une erreur reseau relance le moteur, sans boucler indefiniment."""
        attempts = 0

        async def failing(config, ticks):  # noqa: ARG001 - signature imposee par _run_once
            nonlocal attempts
            attempts += 1
            raise ExchangeError("coupure reseau simulee")

        monkeypatch.setattr("src.main._run_once", failing)
        monkeypatch.setattr("asyncio.sleep", _instant_sleep)
        config = bot_config.model_copy(
            update={"runtime": bot_config.runtime.model_copy(update={"max_restarts": 3})}
        )

        assert await _supervise(config, None) == 1
        assert attempts == 4  # 1 tentative initiale + 3 redemarrages

    async def test_clean_exit_is_not_retried(self, bot_config, monkeypatch):
        calls = 0

        async def succeeding(config, ticks):  # noqa: ARG001 - signature imposee
            nonlocal calls
            calls += 1
            return 0

        monkeypatch.setattr("src.main._run_once", succeeding)
        assert await _supervise(bot_config, None) == 0
        assert calls == 1


class TestCliModes:
    def test_simulate_forces_dry_run(self, bot_config):
        args = _args(simulate=100, dry_run=False, live=False)
        assert resolve_mode(bot_config.model_copy(update={"dry_run": False}), args).dry_run

    def test_live_refuses_a_dry_run_config(self, bot_config):
        from src.models.config import ConfigError

        with pytest.raises(ConfigError, match="dry_run"):
            resolve_mode(bot_config, _args(live=True))

    def test_live_and_simulate_are_incompatible(self, bot_config):
        from src.models.config import ConfigError

        with pytest.raises(ConfigError, match="incompatibles"):
            resolve_mode(bot_config, _args(live=True, simulate=10))

    def test_profile_override_replaces_risk_values(self):
        equilibre = load_config_with_profile("config/testnet.yaml", "equilibre")
        prudent = load_config_with_profile("config/testnet.yaml", "prudent")
        assert prudent.risk.max_daily_drawdown_pct < equilibre.risk.max_daily_drawdown_pct


async def _instant_sleep(_delay: float) -> None:
    """Remplace asyncio.sleep dans les tests de supervision."""


def _args(**overrides):
    from argparse import Namespace

    defaults = dict(dry_run=False, live=False, simulate=None, profile=None, test_alerts=False)
    return Namespace(**{**defaults, **overrides})
