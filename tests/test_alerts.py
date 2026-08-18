"""Tests des alertes : rendu, limitation de debit, declenchement."""

from __future__ import annotations

import pytest

from src.core.alerts import (
    Alert,
    AlertDispatcher,
    AlertLevel,
    DiscordNotifier,
    MemoryNotifier,
    NullNotifier,
    build_notifier,
)
from src.models.config import AlertsConfig, ConfigError


@pytest.fixture
def alerts_config() -> AlertsConfig:
    return AlertsConfig(enabled=True)


@pytest.fixture
def dispatcher(alerts_config) -> tuple[AlertDispatcher, MemoryNotifier]:
    notifier = MemoryNotifier()
    return AlertDispatcher(notifier, alerts_config, symbol="BTCUSDT"), notifier


class TestAlertRendering:
    def test_message_contains_title_and_body(self):
        rendered = Alert("Titre", "Corps", AlertLevel.INFO).render()
        assert "Titre" in rendered and "Corps" in rendered

    def test_long_message_is_truncated_for_discord(self):
        rendered = Alert("T", "x" * 5_000).render()
        assert len(rendered) <= 1_900
        assert rendered.endswith("...")

    @pytest.mark.parametrize("level", list(AlertLevel))
    def test_every_level_has_an_emoji(self, level):
        assert level.emoji


class TestDispatcher:
    async def test_closed_trade_reports_the_net_result(self, dispatcher):
        alerts, notifier = dispatcher
        await alerts.trade_closed(entry=100.0, exit_price=110.0, net_pnl=9.5)
        assert len(notifier.sent) == 1
        assert "Gain" in notifier.sent[0].title
        assert "+9.50" in notifier.sent[0].body

    async def test_losing_trade_is_labelled_as_a_loss(self, dispatcher):
        alerts, notifier = dispatcher
        await alerts.trade_closed(entry=100.0, exit_price=90.0, net_pnl=-10.0)
        assert "Perte" in notifier.sent[0].title

    async def test_kill_switch_is_critical(self, dispatcher):
        alerts, notifier = dispatcher
        await alerts.kill_switch(reason="drawdown atteint")
        assert notifier.sent[0].level is AlertLevel.CRITICAL

    async def test_drawdown_warning_fires_before_the_limit(self, dispatcher):
        alerts, notifier = dispatcher
        # Seuil par defaut : 75% de la limite, soit 1.5% pour une limite de 2%.
        await alerts.drawdown_warning(current_pct=1.6, limit_pct=2.0)
        assert len(notifier.sent) == 1
        assert notifier.sent[0].level is AlertLevel.WARNING

    async def test_drawdown_warning_stays_silent_below_the_threshold(self, dispatcher):
        alerts, notifier = dispatcher
        await alerts.drawdown_warning(current_pct=0.5, limit_pct=2.0)
        assert notifier.sent == []

    async def test_drawdown_warning_is_sent_once_per_crossing(self, dispatcher):
        alerts, notifier = dispatcher
        await alerts.drawdown_warning(current_pct=1.8, limit_pct=2.0)
        await alerts.drawdown_warning(current_pct=1.9, limit_pct=2.0)
        assert len(notifier.sent) == 1

        await alerts.drawdown_warning(current_pct=0.1, limit_pct=2.0)  # retour au calme
        await alerts.drawdown_warning(current_pct=1.8, limit_pct=2.0)  # nouveau franchissement
        assert len(notifier.sent) == 2

    async def test_disabled_events_are_not_sent(self):
        notifier = MemoryNotifier()
        config = AlertsConfig(enabled=True, on_trade_closed=False)
        alerts = AlertDispatcher(notifier, config, symbol="BTCUSDT")
        await alerts.trade_closed(entry=100.0, exit_price=110.0, net_pnl=1.0)
        assert notifier.sent == []


class TestRateLimit:
    def test_burst_is_capped_per_minute(self):
        config = AlertsConfig(enabled=True, max_messages_per_minute=3)
        notifier = DiscordNotifier("https://example.invalid/webhook", config)
        assert [notifier._allow_now() for _ in range(5)] == [True, True, True, False, False]


class TestNotifierFactory:
    def test_disabled_alerts_give_an_inert_channel(self):
        assert isinstance(build_notifier(AlertsConfig(enabled=False)), NullNotifier)

    def test_enabled_alerts_without_webhook_fail_clearly(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
            build_notifier(AlertsConfig(enabled=True))

    def test_non_https_webhook_is_rejected(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://exemple.invalide/hook")
        with pytest.raises(ConfigError, match="https://"):
            build_notifier(AlertsConfig(enabled=True))

    def test_valid_webhook_builds_a_discord_channel(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
        assert isinstance(build_notifier(AlertsConfig(enabled=True)), DiscordNotifier)

    async def test_null_channel_accepts_everything(self):
        assert await NullNotifier().send(Alert("t", "b")) is True
