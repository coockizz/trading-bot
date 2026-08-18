"""Tests des profils de risque et de leur fusion avec la config utilisateur."""

from __future__ import annotations

import pytest

from src.models.config import BotConfig, ConfigError, load_config
from src.models.profiles import PROFILES, apply_profile, describe


class TestProfiles:
    @pytest.mark.parametrize("name", sorted(PROFILES))
    def test_every_profile_produces_a_valid_config(self, name):
        raw = apply_profile({"profile": name, "strategy": {"capital_usdt": 3_000}})
        config = BotConfig.model_validate(raw)
        assert config.risk.max_daily_drawdown_pct <= config.risk.stop_loss_pct

    def test_prudent_is_stricter_than_agressif(self):
        prudent = BotConfig.model_validate(
            apply_profile({"profile": "prudent", "strategy": {"capital_usdt": 3_000}})
        )
        agressif = BotConfig.model_validate(
            apply_profile({"profile": "agressif", "strategy": {"capital_usdt": 3_000}})
        )
        assert prudent.risk.max_daily_drawdown_pct < agressif.risk.max_daily_drawdown_pct
        assert prudent.risk.max_position_size_pct < agressif.risk.max_position_size_pct
        assert prudent.strategy.atr_multiple > agressif.strategy.atr_multiple  # plage plus large
        assert prudent.strategy.target_grids < agressif.strategy.target_grids

    def test_explicit_values_override_the_profile(self):
        raw = apply_profile(
            {
                "profile": "agressif",
                "strategy": {"capital_usdt": 3_000},
                "risk": {"stop_loss_pct": 12.0},
            }
        )
        config = BotConfig.model_validate(raw)
        assert config.risk.stop_loss_pct == 12.0
        # Le reste du profil agressif est conserve.
        assert (
            config.risk.max_daily_drawdown_pct
            == PROFILES["agressif"]["risk"]["max_daily_drawdown_pct"]
        )

    def test_no_profile_leaves_the_config_untouched(self):
        raw = {"strategy": {"capital_usdt": 3_000}}
        assert apply_profile(raw) == raw

    def test_unknown_profile_lists_the_valid_ones(self):
        with pytest.raises(ValueError, match="prudent"):
            apply_profile({"profile": "turbo"})

    def test_every_profile_is_described(self):
        assert all(describe(name) != "profil inconnu" for name in PROFILES)


class TestConfigLoading:
    def test_shipped_configs_are_valid(self):
        for path in ("config/testnet.yaml", "config/prod.example.yaml"):
            config = load_config(path)
            assert config.strategy.capital_usdt > 0

    def test_missing_file_gives_a_clear_error(self):
        with pytest.raises(ConfigError, match="introuvable"):
            load_config("config/inexistant.yaml")

    def test_unknown_key_is_rejected(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("strategy:\n  capital_usdt: 100\n  typo_field: 3\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_auto_grid_config_needs_no_range(self, tmp_path):
        path = tmp_path / "auto.yaml"
        path.write_text("profile: equilibre\nstrategy:\n  capital_usdt: 3000\n", encoding="utf-8")
        config = load_config(path)
        assert config.strategy.is_auto
