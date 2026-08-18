"""Tests des controles de securite au demarrage."""

from __future__ import annotations

import pytest

from src.core.security import (
    ApiPermissions,
    SecurityError,
    audit_environment,
    check_permissions,
    check_testnet_coherence,
    confirm_live_trading,
    parse_permissions,
)


class TestPermissionParsing:
    def test_binance_response_is_translated(self):
        permissions = parse_permissions({"enableWithdrawals": True, "enableFutures": False})
        assert permissions.can_withdraw is True
        assert permissions.can_trade_futures is False
        assert permissions.is_readable is True

    def test_unknown_permissions_are_not_readable(self):
        assert ApiPermissions.unknown().is_readable is False


class TestPermissionChecks:
    def test_withdrawal_permission_blocks_startup(self, bot_config):
        permissions = ApiPermissions(can_withdraw=True, can_trade_futures=True, is_readable=True)
        with pytest.raises(SecurityError, match="RETRAITS"):
            check_permissions(permissions, bot_config)

    def test_missing_futures_permission_blocks_startup(self, bot_config):
        permissions = ApiPermissions(can_withdraw=False, can_trade_futures=False, is_readable=True)
        with pytest.raises(SecurityError, match="Futures"):
            check_permissions(permissions, bot_config)

    def test_safe_key_passes(self, bot_config):
        permissions = ApiPermissions(can_withdraw=False, can_trade_futures=True, is_readable=True)
        check_permissions(permissions, bot_config)  # ne leve pas

    def test_unverifiable_permissions_only_warn(self, bot_config):
        """Sur le testnet l'endpoint n'existe pas : on avertit sans bloquer."""
        check_permissions(ApiPermissions.unknown(), bot_config)

    def test_check_can_be_disabled_explicitly(self, bot_config):
        security = bot_config.security.model_copy(update={"require_no_withdrawal": False})
        relaxed = bot_config.model_copy(update={"security": security})
        permissions = ApiPermissions(can_withdraw=True, can_trade_futures=True, is_readable=True)
        check_permissions(permissions, relaxed)  # ne leve pas


class TestTestnetCoherence:
    def test_testnet_is_always_accepted(self, bot_config):
        check_testnet_coherence(bot_config, balance=15_000.0)

    def test_mainnet_with_insufficient_balance_is_refused(self, bot_config):
        mainnet = bot_config.model_copy(
            update={"exchange": bot_config.exchange.model_copy(update={"testnet": False})}
        )
        with pytest.raises(SecurityError, match="capital"):
            check_testnet_coherence(mainnet, balance=10.0)

    def test_mainnet_with_sufficient_balance_passes(self, bot_config):
        mainnet = bot_config.model_copy(
            update={"exchange": bot_config.exchange.model_copy(update={"testnet": False})}
        )
        check_testnet_coherence(mainnet, balance=99_999.0)


class TestLiveConfirmation:
    def _mainnet_live(self, bot_config):
        return bot_config.model_copy(
            update={
                "dry_run": False,
                "exchange": bot_config.exchange.model_copy(update={"testnet": False}),
            }
        )

    def test_dry_run_needs_no_confirmation(self, bot_config):
        confirm_live_trading(bot_config, 1_000.0, input_fn=lambda _: "non")

    def test_testnet_needs_no_confirmation(self, bot_config):
        live_testnet = bot_config.model_copy(update={"dry_run": False})
        confirm_live_trading(live_testnet, 1_000.0, input_fn=lambda _: "non")

    def test_mainnet_requires_explicit_yes(self, bot_config):
        with pytest.raises(SecurityError, match="annule"):
            confirm_live_trading(self._mainnet_live(bot_config), 1_000.0, input_fn=lambda _: "oui")

    def test_uppercase_oui_confirms(self, bot_config):
        confirm_live_trading(self._mainnet_live(bot_config), 1_000.0, input_fn=lambda _: "OUI")


class TestEnvironmentAudit:
    def test_dry_run_testnet_raises_no_flag(self, bot_config):
        assert audit_environment(bot_config) == []

    def test_mainnet_live_is_flagged(self, bot_config):
        mainnet = bot_config.model_copy(
            update={
                "dry_run": False,
                "exchange": bot_config.exchange.model_copy(update={"testnet": False}),
            }
        )
        warnings = audit_environment(mainnet)
        assert any("REEL" in w for w in warnings)
        assert any("Alertes desactivees" in w for w in warnings)
