"""Fixtures partagees par la suite de tests."""

from __future__ import annotations

import pytest

from src.models.config import (
    BotConfig,
    ExchangeConfig,
    LoggingConfig,
    PriceRange,
    RiskConfig,
    RuntimeConfig,
    StrategyConfig,
)
from src.models.domain import SymbolFilters


@pytest.fixture
def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        price_range=PriceRange(lower=90_000, upper=110_000),
        num_grids=11,
        capital_usdt=3_000,
        leverage=1,
        spacing="geometric",
        active_orders_per_side=3,
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(max_daily_drawdown_pct=2.0, stop_loss_pct=5.0, max_position_size_pct=20.0)


@pytest.fixture
def bot_config(strategy_config: StrategyConfig, risk_config: RiskConfig, tmp_path) -> BotConfig:
    return BotConfig(
        exchange=ExchangeConfig(testnet=True),
        symbol="BTCUSDT",
        dry_run=True,
        strategy=strategy_config,
        risk=risk_config,
        logging=LoggingConfig(file=str(tmp_path / "bot.log")),
        runtime=RuntimeConfig(
            state_file=str(tmp_path / "state.json"), stats_file=str(tmp_path / "stats.json")
        ),
    )


@pytest.fixture
def test_filters() -> SymbolFilters:
    """Filtres permissifs : les tests portent sur la logique, pas sur les minimums."""
    return SymbolFilters(tick_size=0.1, step_size=0.000001, min_notional=1.0, min_quantity=0.0)
