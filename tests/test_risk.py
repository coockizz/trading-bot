"""Tests du risk manager et du calcul de P&L / drawdown."""

from __future__ import annotations

import pytest

from src.core.stats import StatsTracker
from src.models.config import RiskConfig
from src.models.domain import Fill, OrderIntent, Position, Side
from src.risk.manager import RiskManager
from src.utils.helpers import fee_for, pct_change


def _fill(side: Side, price: float, qty: float, fee: float = 0.0, level: int = 0) -> Fill:
    return Fill(
        client_id=f"t-{level}", side=side, price=price, quantity=qty, fee=fee, level_index=level
    )


class TestStatsPnl:
    def test_round_trip_profit_is_net_of_fees(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0, fee=0.02))
        stats.record_fill(_fill(Side.SELL, 110.0, 1.0, fee=0.022))

        assert stats.gross_realized_pnl == pytest.approx(10.0)
        assert stats.total_fees == pytest.approx(0.042)
        assert stats.realized_pnl == pytest.approx(10.0 - 0.042)

    def test_losing_round_trip_is_negative(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.record_fill(_fill(Side.SELL, 90.0, 1.0))
        assert stats.realized_pnl == pytest.approx(-10.0)

    def test_average_entry_price_is_weighted(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.record_fill(_fill(Side.BUY, 200.0, 3.0))
        assert stats.average_entry_price == pytest.approx(175.0)
        assert stats.inventory_qty == pytest.approx(4.0)

    def test_partial_close_keeps_remaining_inventory(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 2.0))
        stats.record_fill(_fill(Side.SELL, 110.0, 1.0))
        assert stats.inventory_qty == pytest.approx(1.0)
        assert stats.gross_realized_pnl == pytest.approx(10.0)

    def test_unrealized_pnl_follows_mark_price(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 2.0))
        stats.update_market(120.0)
        assert stats.unrealized_pnl == pytest.approx(40.0)

    def test_win_rate_counts_closed_trades_only(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.record_fill(_fill(Side.SELL, 110.0, 1.0))  # gagnant
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.record_fill(_fill(Side.SELL, 90.0, 1.0))  # perdant
        assert stats.win_rate_pct == pytest.approx(50.0)

    def test_max_drawdown_records_the_worst_dip(self):
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.update_market(200.0)  # equity 1100, nouveau sommet
        stats.update_market(50.0)  # equity 950 -> DD depuis le sommet
        stats.update_market(200.0)  # remontee : le max reste memorise
        assert stats.max_drawdown_pct == pytest.approx((1_100 - 950) / 1_100 * 100, rel=1e-6)

    def test_fee_helper_matches_binance_rate(self):
        assert fee_for(1_000.0, 0.02) == pytest.approx(0.2)

    def test_pct_change_needs_a_reference(self):
        assert pct_change(110.0, 100.0) == pytest.approx(10.0)
        with pytest.raises(ValueError):
            pct_change(1.0, 0.0)


class TestRiskLimits:
    def test_no_halt_while_within_limits(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.update_market(101.0)
        assert not risk.evaluate(stats).should_halt

    def test_daily_drawdown_triggers_kill_switch(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.update_market(75.0)  # -25 USDT = 2.5% de 1000 > 2%

        verdict = risk.evaluate(stats)
        assert verdict.should_halt
        assert "journalier" in verdict.reason.lower()

    def test_halt_is_sticky(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.update_market(50.0)
        risk.evaluate(stats)

        stats.update_market(500.0)  # meme redevenu profitable, le bot reste arrete
        assert risk.evaluate(stats).should_halt
        assert risk.is_halted

    def test_global_stop_loss_triggers_on_losses_carried_over_days(self, risk_config):
        """Une perte accumulee la veille doit declencher le stop-loss global.

        Le P&L journalier est repartu de zero (perte anterieure a aujourd'hui),
        donc seul le stop-loss global peut encore arreter le bot.
        """
        risk = RiskManager(risk_config, 1_000.0)
        stats = StatsTracker(1_000.0)
        stats.record_fill(_fill(Side.BUY, 100.0, 1.0))
        stats.update_market(40.0)  # -60 USDT = 6% du capital, > stop_loss 5%
        # Simule le passage de minuit UTC : la baseline journaliere absorbe la perte.
        stats._realized_at_day_start = stats.realized_pnl
        stats._unrealized_at_day_start = stats.unrealized_pnl

        assert stats.daily_pnl == pytest.approx(0.0)
        verdict = risk.evaluate(stats)
        assert verdict.should_halt
        assert "stop-loss" in verdict.reason.lower()

    def test_daily_drawdown_must_not_exceed_stop_loss(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            RiskConfig(max_daily_drawdown_pct=10.0, stop_loss_pct=5.0)


class TestPositionSizing:
    def test_order_within_limit_is_accepted(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)  # limite = 200 USDT
        intent = OrderIntent("b-0", Side.BUY, price=100.0, quantity=1.0, level_index=0)
        assert risk.allows_order(intent, Position(0.0, 0.0), 100.0)

    def test_order_exceeding_limit_is_refused(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)
        intent = OrderIntent("b-0", Side.BUY, price=100.0, quantity=3.0, level_index=0)
        assert not risk.allows_order(intent, Position(0.0, 0.0), 100.0)

    def test_reducing_order_is_always_allowed(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)
        oversized = Position(quantity=10.0, entry_price=100.0)
        intent = OrderIntent("s-0", Side.SELL, price=100.0, quantity=5.0, level_index=0)
        assert risk.allows_order(intent, oversized, 100.0)

    def test_nothing_is_allowed_after_halt(self, risk_config):
        risk = RiskManager(risk_config, 1_000.0)
        risk.force_halt("test")
        intent = OrderIntent("b-0", Side.BUY, price=100.0, quantity=0.001, level_index=0)
        assert not risk.allows_order(intent, Position(0.0, 0.0), 100.0)

    def test_limit_below_exchange_minimum_is_rejected_at_startup(self):
        config = RiskConfig(max_position_size_pct=0.5)
        risk = RiskManager(config, 500.0)  # 0.5% de 500 = 2.50 USDT
        with pytest.raises(ValueError, match="max_position_size_pct"):
            risk.validate_against(min_notional=100.0)
