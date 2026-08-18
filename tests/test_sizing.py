"""Tests du dimensionnement automatique de la grille."""

from __future__ import annotations

import pytest

from src.core.sizing import GridPlan, SizingError, average_true_range, plan_grid
from src.models.domain import Candle, SymbolFilters

BTC_FILTERS = SymbolFilters(tick_size=0.1, step_size=0.001, min_notional=100.0, min_quantity=0.001)
SOL_FILTERS = SymbolFilters(tick_size=0.01, step_size=0.1, min_notional=5.0, min_quantity=0.1)


def _candles(high: float, low: float, count: int = 14) -> list[Candle]:
    return [Candle(high=high, low=low, close=(high + low) / 2) for _ in range(count)]


class TestAtr:
    def test_atr_is_the_mean_range(self):
        assert average_true_range(_candles(110.0, 100.0)) == pytest.approx(10.0)

    def test_only_the_last_period_counts(self):
        candles = _candles(200.0, 100.0, count=20) + _candles(110.0, 100.0, count=14)
        assert average_true_range(candles, period=14) == pytest.approx(10.0)

    def test_empty_history_is_rejected(self):
        with pytest.raises(SizingError):
            average_true_range([])

    def test_invalid_period_is_rejected(self):
        with pytest.raises(ValueError):
            average_true_range(_candles(110.0, 100.0), period=0)


class TestPlanGrid:
    def test_range_is_centred_on_the_current_price(self):
        plan = plan_grid(
            price=100_000.0,
            candles=_candles(102_000.0, 98_000.0),
            capital_usdt=10_000.0,
            leverage=1,
            filters=BTC_FILTERS,
            atr_multiple=3.0,
            target_grids=10,
        )
        midpoint = (plan.lower + plan.upper) / 2
        assert midpoint == pytest.approx(100_000.0, rel=1e-4)

    def test_wider_atr_multiple_gives_a_wider_range(self):
        common = dict(
            price=100_000.0,
            candles=_candles(102_000.0, 98_000.0),
            capital_usdt=10_000.0,
            leverage=1,
            filters=BTC_FILTERS,
            target_grids=10,
        )
        narrow = plan_grid(**common, atr_multiple=2.0)
        wide = plan_grid(**common, atr_multiple=4.0)
        assert (wide.upper - wide.lower) > (narrow.upper - narrow.lower)

    def test_grid_count_is_reduced_when_capital_is_tight(self):
        """1000 USDT sur BTC ne permet pas 20 niveaux a 100 USDT minimum."""
        plan = plan_grid(
            price=100_000.0,
            candles=_candles(102_000.0, 98_000.0),
            capital_usdt=1_000.0,
            leverage=1,
            filters=BTC_FILTERS,
            atr_multiple=3.0,
            target_grids=20,
        )
        assert plan.num_grids < 20
        assert plan.capital_per_grid >= BTC_FILTERS.min_notional

    def test_every_order_of_the_plan_is_executable(self):
        plan = plan_grid(
            price=100_000.0,
            candles=_candles(102_000.0, 98_000.0),
            capital_usdt=5_000.0,
            leverage=1,
            filters=BTC_FILTERS,
            atr_multiple=3.0,
            target_grids=15,
        )
        # Cas le plus contraignant : le niveau le plus haut, quantite arrondie bas.
        quantity = int(plan.capital_per_grid / plan.upper / BTC_FILTERS.step_size)
        notional = quantity * BTC_FILTERS.step_size * plan.upper
        assert notional >= BTC_FILTERS.min_notional

    def test_cheap_asset_allows_a_finer_grid(self):
        """Un step size plus fin permet plus de niveaux a capital egal."""
        common = dict(
            candles=_candles(210.0, 190.0),
            capital_usdt=500.0,
            leverage=1,
            atr_multiple=3.0,
            target_grids=20,
        )
        on_sol = plan_grid(price=200.0, filters=SOL_FILTERS, **common)
        btc_common = dict(common, candles=_candles(102_000.0, 98_000.0))
        on_btc = plan_grid(price=100_000.0, filters=BTC_FILTERS, **btc_common)
        assert on_sol.num_grids > on_btc.num_grids

    def test_capital_too_small_raises_an_actionable_error(self):
        with pytest.raises(SizingError, match="ne permet aucune grille"):
            plan_grid(
                price=100_000.0,
                candles=_candles(102_000.0, 98_000.0),
                capital_usdt=50.0,
                leverage=1,
                filters=BTC_FILTERS,
                atr_multiple=3.0,
                target_grids=10,
            )

    def test_flat_market_falls_back_to_a_volatility_floor(self):
        """Un ATR quasi nul ne doit pas produire une plage microscopique."""
        plan = plan_grid(
            price=100_000.0,
            candles=_candles(100_001.0, 100_000.0),
            capital_usdt=10_000.0,
            leverage=1,
            filters=BTC_FILTERS,
            atr_multiple=3.0,
            target_grids=10,
        )
        assert (plan.upper / plan.lower - 1) > 0.05  # au moins 5% de large

    def test_lower_bound_stays_positive(self):
        plan = plan_grid(
            price=100.0,
            candles=_candles(200.0, 0.0),  # volatilite enorme
            capital_usdt=10_000.0,
            leverage=1,
            filters=SOL_FILTERS,
            atr_multiple=10.0,
            target_grids=5,
        )
        assert plan.lower > 0

    @pytest.mark.parametrize(("price", "capital"), [(0.0, 1000.0), (100.0, 0.0)])
    def test_invalid_inputs_are_rejected(self, price, capital):
        with pytest.raises(ValueError):
            plan_grid(
                price=price,
                candles=_candles(110.0, 90.0),
                capital_usdt=capital,
                leverage=1,
                filters=SOL_FILTERS,
                atr_multiple=3.0,
                target_grids=5,
            )

    def test_describe_is_human_readable(self):
        plan = GridPlan(
            lower=90_000.0, upper=110_000.0, num_grids=10, atr=2_000.0, capital_per_grid=300.0
        )
        text = plan.describe()
        assert "10 niveaux" in text and "300" in text
