"""Tests d'integration sur exchange simule : placement, fills, reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exchange import DryRunExchange, _level_from_client_id, _to_ccxt_symbol
from src.core.websocket import ReplayPriceFeed, _parse_mark_price
from src.main import TradingEngine
from src.models.domain import OrderIntent, Side
from src.strategy.grid import GridStrategy
from src.utils.helpers import round_to_step, round_to_tick


@pytest.fixture
async def exchange(bot_config, test_filters) -> DryRunExchange:
    ex = DryRunExchange(bot_config, filters=test_filters)
    await ex.connect()
    return ex


class TestRounding:
    def test_price_is_snapped_to_tick(self):
        assert round_to_tick(100_000.07, 0.1) == pytest.approx(100_000.1)

    def test_quantity_is_rounded_down_to_step(self):
        # Arrondir vers le haut depasserait le capital disponible.
        assert round_to_step(0.0019, 0.001) == pytest.approx(0.001)

    def test_invalid_tick_or_step_is_rejected(self):
        with pytest.raises(ValueError):
            round_to_tick(100.0, 0)
        with pytest.raises(ValueError):
            round_to_step(1.0, -1)


class TestDryRunExchange:
    async def test_place_order_registers_it(self, exchange):
        order = await exchange.place_order(_intent(Side.BUY, 95_000.0, 0.01))
        assert order is not None
        assert [o.client_id for o in await exchange.get_open_orders()] == ["grid-b-0"]

    async def test_order_below_min_notional_is_skipped(self, bot_config, test_filters):
        strict = test_filters.__class__(
            tick_size=0.1, step_size=0.001, min_notional=100.0, min_quantity=0.001
        )
        ex = DryRunExchange(bot_config, filters=strict)
        await ex.connect()
        assert await ex.place_order(_intent(Side.BUY, 95_000.0, 0.0001)) is None
        assert await ex.get_open_orders() == []

    async def test_buy_fills_when_price_drops_to_it(self, exchange):
        await exchange.place_order(_intent(Side.BUY, 95_000.0, 0.01))

        assert await exchange.poll_fills(96_000.0) == []
        fills = await exchange.poll_fills(94_900.0)
        assert len(fills) == 1
        assert fills[0].side is Side.BUY
        assert fills[0].price == pytest.approx(95_000.0)
        assert fills[0].fee > 0
        assert await exchange.get_open_orders() == []

    async def test_sell_fills_when_price_rises_to_it(self, exchange):
        await exchange.place_order(_intent(Side.SELL, 105_000.0, 0.01, level=5))
        fills = await exchange.poll_fills(105_100.0)
        assert len(fills) == 1
        assert fills[0].level_index == 5

    async def test_position_tracks_fills(self, exchange):
        await exchange.place_order(_intent(Side.BUY, 95_000.0, 0.01))
        await exchange.poll_fills(94_000.0)
        position = await exchange.get_position()
        assert position.quantity == pytest.approx(0.01)
        assert position.entry_price == pytest.approx(95_000.0)

    async def test_cancel_removes_the_order(self, exchange):
        order = await exchange.place_order(_intent(Side.BUY, 95_000.0, 0.01))
        await exchange.cancel_order(order)
        assert await exchange.get_open_orders() == []
        assert await exchange.poll_fills(90_000.0) == []

    async def test_close_all_positions_flattens_inventory(self, exchange):
        await exchange.place_order(_intent(Side.BUY, 95_000.0, 0.01))
        await exchange.poll_fills(94_000.0)
        await exchange.close_all_positions()
        assert (await exchange.get_position()).is_flat


class TestEngineIntegration:
    async def test_engine_places_a_book_on_first_reconcile(self, bot_config, test_filters):
        engine = await _engine(bot_config, test_filters, [100_000.0])
        await engine.setup()
        await engine._on_price(100_000.0)

        orders = await engine.exchange.get_open_orders()
        assert orders, "le moteur doit poser des ordres au premier tick"
        assert all(o.side is Side.BUY for o in orders)
        await engine.shutdown()

    async def test_price_drop_fills_buys_and_creates_exits(self, bot_config, test_filters):
        engine = await _engine(bot_config, test_filters, [])
        await engine.setup()
        await engine._on_price(100_000.0)  # pose les achats

        engine._last_reconcile = 0.0
        await engine._on_price(94_000.0)  # traverse plusieurs niveaux vers le bas

        assert engine.strategy.held_levels, "des niveaux doivent etre detenus apres les fills"
        sells = [o for o in await engine.exchange.get_open_orders() if o.side is Side.SELL]
        assert sells, "chaque niveau detenu doit avoir un ordre de sortie"
        await engine.shutdown()

    async def test_round_trip_produces_positive_pnl(self, bot_config, test_filters):
        engine = await _engine(bot_config, test_filters, [])
        await engine.setup()
        for price in (100_000.0, 94_000.0, 100_000.0):
            engine._last_reconcile = 0.0
            await engine._on_price(price)

        assert engine.stats.num_fills >= 2
        assert engine.stats.gross_realized_pnl > 0
        await engine.shutdown()

    async def test_kill_switch_closes_everything(self, bot_config, test_filters):
        # Drawdown journalier a 2% : une chute violente doit arreter le bot.
        engine = await _engine(bot_config, test_filters, [])
        await engine.setup()
        await engine._on_price(100_000.0)
        engine._last_reconcile = 0.0
        await engine._on_price(91_000.0)
        engine._last_reconcile = 0.0
        await engine._on_price(60_000.0)

        assert engine.risk.is_halted
        assert engine._halted_by_risk
        assert (await engine.exchange.get_position()).is_flat
        await engine.shutdown()

    async def test_shutdown_cancels_open_orders(self, bot_config, test_filters):
        engine = await _engine(bot_config, test_filters, [])
        await engine.setup()
        await engine._on_price(100_000.0)
        await engine.shutdown()
        assert await engine.exchange.get_open_orders() == []

    async def test_run_consumes_the_feed_and_persists_state(self, bot_config, test_filters):
        engine = await _engine(bot_config, test_filters, [100_000.0, 99_000.0, 98_000.0])
        await engine.setup()
        exit_code = await engine.run()
        await engine.shutdown()

        assert exit_code == 0
        assert Path(bot_config.runtime.state_file).exists()
        assert Path(bot_config.runtime.stats_file).exists()


class TestParsingHelpers:
    def test_mark_price_message_is_parsed(self):
        assert _parse_mark_price('{"e":"markPriceUpdate","p":"100123.45"}') == pytest.approx(
            100_123.45
        )

    @pytest.mark.parametrize("raw", ['{"e":"other"}', "pas du json", '{"p":"abc"}', '{"p":"-1"}'])
    def test_unusable_messages_return_none(self, raw):
        assert _parse_mark_price(raw) is None

    def test_symbol_conversion_to_ccxt(self):
        assert _to_ccxt_symbol("BTCUSDT") == "BTC/USDT:USDT"

    def test_unknown_quote_currency_is_rejected(self):
        with pytest.raises(Exception, match="non reconnu"):
            _to_ccxt_symbol("BTCEUR")

    def test_level_is_decoded_from_client_id(self):
        assert _level_from_client_id("grid-b-12") == 12
        assert _level_from_client_id("manuel") == -1


async def _engine(bot_config, test_filters, series: list[float]) -> TradingEngine:
    strategy = GridStrategy(bot_config.strategy, taker_fee_pct=bot_config.exchange.taker_fee_pct)
    exchange = DryRunExchange(bot_config, filters=test_filters)
    feed = ReplayPriceFeed(series or [1.0])
    return TradingEngine(bot_config, exchange, strategy, feed)


def _intent(side: Side, price: float, quantity: float, level: int = 0) -> OrderIntent:
    prefix = "grid-b" if side is Side.BUY else "grid-s"
    return OrderIntent(
        client_id=f"{prefix}-{level}",
        side=side,
        price=price,
        quantity=quantity,
        level_index=level,
    )
