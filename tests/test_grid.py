"""Tests de la strategie grid : niveaux, ordres souhaites, cycle achat/vente."""

from __future__ import annotations

import pytest

from src.models.config import PriceRange, StrategyConfig
from src.models.domain import Fill, Side
from src.strategy.grid import BUY_PREFIX, SELL_PREFIX, GridStrategy
from src.utils.helpers import arithmetic_levels, geometric_levels


class TestLevelComputation:
    def test_geometric_levels_span_the_range(self):
        levels = geometric_levels(100.0, 200.0, 5)
        assert len(levels) == 5
        assert levels[0] == pytest.approx(100.0)
        assert levels[-1] == pytest.approx(200.0)

    def test_geometric_spacing_is_constant_in_percent(self):
        levels = geometric_levels(90_000, 110_000, 11)
        ratios = [levels[i + 1] / levels[i] for i in range(len(levels) - 1)]
        assert all(r == pytest.approx(ratios[0]) for r in ratios)

    def test_arithmetic_spacing_is_constant_in_absolute(self):
        levels = arithmetic_levels(100.0, 200.0, 5)
        gaps = [levels[i + 1] - levels[i] for i in range(len(levels) - 1)]
        assert all(g == pytest.approx(25.0) for g in gaps)

    def test_levels_are_strictly_increasing(self):
        levels = geometric_levels(90_000, 110_000, 50)
        assert all(levels[i] < levels[i + 1] for i in range(len(levels) - 1))

    @pytest.mark.parametrize(
        ("lower", "upper", "count"),
        [(0, 100, 5), (-10, 100, 5), (200, 100, 5), (100, 200, 1)],
    )
    def test_invalid_ranges_are_rejected(self, lower, upper, count):
        with pytest.raises(ValueError):
            geometric_levels(lower, upper, count)


class TestGridStrategy:
    def test_builds_expected_number_of_levels(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        assert len(strategy.levels) == strategy_config.num_grids

    def test_rejects_grid_too_tight_to_cover_fees(self):
        config = StrategyConfig(
            price_range=PriceRange(lower=100_000, upper=100_100),
            num_grids=200,
            capital_usdt=100_000,
        )
        with pytest.raises(ValueError, match="non rentable"):
            GridStrategy(config, taker_fee_pct=0.05)

    def test_capital_is_split_evenly_across_levels(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        notional = strategy.quantity_at(0) * strategy.levels[0]
        assert notional == pytest.approx(strategy_config.capital_per_grid)

    def test_buy_orders_sit_below_price_sell_orders_above(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        price = 100_000.0
        intents = strategy.desired_orders(price)
        assert intents, "la strategie doit proposer des ordres"
        assert all(i.side is Side.BUY for i in intents)
        assert all(i.price < price for i in intents)

    def test_active_orders_per_side_caps_the_book(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        intents = strategy.desired_orders(110_000.0)
        buys = [i for i in intents if i.side is Side.BUY]
        assert len(buys) == strategy_config.active_orders_per_side

    def test_buy_fill_creates_a_sell_one_level_above(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        level = 3
        strategy.on_fill(_fill(Side.BUY, strategy.levels[level], level))

        sells = [i for i in strategy.desired_orders(100_000.0) if i.side is Side.SELL]
        assert len(sells) == 1
        assert sells[0].price == pytest.approx(strategy.levels[level + 1])
        assert sells[0].client_id == f"{SELL_PREFIX}-{level}"

    def test_filled_level_is_not_re_bought(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        level = 4  # dans la fenetre active au prix de 100 000
        strategy.on_fill(_fill(Side.BUY, strategy.levels[level], level))
        buy_ids = {i.client_id for i in strategy.desired_orders(100_000.0) if i.side is Side.BUY}
        assert f"{BUY_PREFIX}-{level}" not in buy_ids

    def test_sell_fill_frees_the_level_again(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        level = 4  # dans la fenetre active au prix de 100 000
        strategy.on_fill(_fill(Side.BUY, strategy.levels[level], level))
        strategy.on_fill(_fill(Side.SELL, strategy.levels[level + 1], level))

        assert strategy.held_levels == frozenset()
        buy_ids = {i.client_id for i in strategy.desired_orders(100_000.0) if i.side is Side.BUY}
        assert f"{BUY_PREFIX}-{level}" in buy_ids

    def test_top_level_purchase_has_no_exit_order(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        top = len(strategy.levels) - 1
        strategy.on_fill(_fill(Side.BUY, strategy.levels[top], top))
        sells = [i for i in strategy.desired_orders(111_000.0) if i.side is Side.SELL]
        assert sells == []

    def test_out_of_range_fill_is_ignored(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        strategy.on_fill(_fill(Side.BUY, 100_000.0, 999))
        assert strategy.held_levels == frozenset()

    def test_rejects_non_positive_price(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        with pytest.raises(ValueError):
            strategy.desired_orders(0.0)


class TestGridPersistence:
    def test_state_round_trip_restores_held_levels(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        strategy.on_fill(_fill(Side.BUY, strategy.levels[4], 4))
        state = strategy.snapshot_state()

        restored = GridStrategy(strategy_config)
        restored.restore_state(state)
        assert restored.held_levels == frozenset({4})

    def test_state_from_a_different_grid_is_discarded(self, strategy_config):
        strategy = GridStrategy(strategy_config)
        strategy.on_fill(_fill(Side.BUY, strategy.levels[4], 4))
        state = strategy.snapshot_state()

        other = GridStrategy(strategy_config.model_copy(update={"num_grids": 15}))
        other.restore_state(state)
        assert other.held_levels == frozenset()


def _fill(side: Side, price: float, level: int) -> Fill:
    return Fill(
        client_id=f"grid-{'b' if side is Side.BUY else 's'}-{level}",
        side=side,
        price=price,
        quantity=0.01,
        fee=0.0,
        level_index=level,
    )
