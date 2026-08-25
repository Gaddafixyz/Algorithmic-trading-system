"""
Tests for Settings.load_yaml_overrides' risk.yaml / symbols.yaml /
monitoring.yaml / exchanges.yaml merging.

Context: risk.yaml and symbols.yaml previously weren't read by any code
path at all -- RiskConfig only ever picked up RISK_*-prefixed env vars
and field defaults. These tests cover the new _load_risk_yaml /
_load_symbols_yaml methods, in particular that:
  - yaml values apply when no env override exists,
  - explicit env vars still win over yaml (documented precedence order),
  - a per-symbol leverage exceeding the account-wide ceiling is a hard
    config error rather than a silent clamp,
  - disabled symbols are still parsed (callers decide what to do with
    `enabled`).

TestExchangesYamlLoading covers the same gap for exchanges.yaml, found
in a later audit pass: BinanceConfig/BybitConfig's *_base_url properties
picked between hardcoded literals keyed only on `testnet`, and
max_orders_per_second/max_orders_per_minute weren't fields at all.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from src.core.config import RiskConfig, Settings, get_settings, override_settings, reset_settings
from src.core.exceptions import ValidationError


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "configs"
    d.mkdir()
    return d


class TestRiskYamlLoading:
    def test_yaml_values_apply_when_no_env_override(self, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RISK_MAX_PORTFOLIO_RISK_PCT", raising=False)
        _write_yaml(
            config_dir / "risk.yaml",
            {
                "risk": {
                    "max_portfolio_risk_pct": 0.75,
                    "max_open_positions": 4,
                },
                "circuit_breaker": {"cooldown_minutes": 45},
            },
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.risk.max_portfolio_risk_pct == Decimal("0.75")
        assert settings.risk.max_open_positions == 4
        assert settings.risk.circuit_breaker_cooldown_minutes == 45

    def test_env_var_still_wins_over_yaml(self, config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RISK_MAX_PORTFOLIO_RISK_PCT", "2.0")
        _write_yaml(config_dir / "risk.yaml", {"risk": {"max_portfolio_risk_pct": 0.75}})

        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        # Documented precedence: env vars > yaml > field defaults.
        assert settings.risk.max_portfolio_risk_pct == Decimal("2.0")

    def test_missing_risk_yaml_is_a_silent_noop(self, config_dir: Path) -> None:
        settings = Settings(_env_file=None)
        default_risk_pct = settings.risk.max_portfolio_risk_pct
        settings.load_yaml_overrides(config_dir=config_dir)
        assert settings.risk.max_portfolio_risk_pct == default_risk_pct


class TestSymbolsYamlLoading:
    def test_per_symbol_leverage_loaded(self, config_dir: Path) -> None:
        _write_yaml(
            config_dir / "symbols.yaml",
            {
                "symbols": {
                    "BTCUSDT": {"leverage": 5, "margin_type": "ISOLATED", "enabled": True},
                    "SOLUSDT": {"leverage": 3, "margin_type": "ISOLATED", "enabled": False},
                }
            },
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.symbols["BTCUSDT"].leverage == 5
        assert settings.symbols["BTCUSDT"].enabled is True
        assert settings.symbols["SOLUSDT"].enabled is False

    def test_symbol_leverage_exceeding_account_ceiling_raises(self, config_dir: Path) -> None:
        _write_yaml(config_dir / "risk.yaml", {"risk": {"max_leverage": 10.0}})
        _write_yaml(
            config_dir / "symbols.yaml",
            {"symbols": {"BTCUSDT": {"leverage": 20, "enabled": True}}},
        )
        settings = Settings(_env_file=None)
        with pytest.raises(ValidationError, match="exceeds risk.max_leverage"):
            settings.load_yaml_overrides(config_dir=config_dir)

    def test_missing_symbols_yaml_leaves_empty_dict(self, config_dir: Path) -> None:
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)
        assert settings.symbols == {}


class TestMonitoringYamlLoading:
    """
    configs/monitoring.yaml previously wasn't read by any code path either
    -- alert thresholds/cooldowns were hardcoded in
    monitoring/alerts.py's build_standard_alerts(), and health-check/
    heartbeat/watchdog intervals were hardcoded constructor defaults in
    bootstrap/app.py. These tests cover the new _load_monitoring_yaml
    method using the same precedence contract as risk.yaml/symbols.yaml.
    """

    def test_yaml_values_apply_when_no_env_override(self, config_dir: Path) -> None:
        _write_yaml(
            config_dir / "monitoring.yaml",
            {
                "alerts": {
                    "daily_loss_warning_pct": 2.5,
                    "drawdown_warning_pct": 8.0,
                    "cooldown_minutes": 15,
                },
                "health_check": {"interval_seconds": 45},
                "heartbeat": {"interval_seconds": 90},
                "watchdog": {"timeout_seconds": 20, "check_interval_seconds": 2},
            },
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.monitoring.daily_loss_warning_pct == 2.5
        assert settings.monitoring.drawdown_warning_pct == 8.0
        assert settings.monitoring.alert_cooldown_minutes == 15
        assert settings.monitoring.health_check_interval_seconds == 45
        assert settings.monitoring.heartbeat_interval_seconds == 90
        assert settings.monitoring.watchdog_timeout_seconds == 20
        assert settings.monitoring.watchdog_check_interval_seconds == 2
        assert settings._monitoring_yaml_overrides == {
            "daily_loss_warning_pct",
            "drawdown_warning_pct",
            "alert_cooldown_minutes",
            "health_check_interval_seconds",
            "heartbeat_interval_seconds",
            "watchdog_timeout_seconds",
            "watchdog_check_interval_seconds",
        }

    def test_missing_monitoring_yaml_is_a_silent_noop(self, config_dir: Path) -> None:
        settings = Settings(_env_file=None)
        default_pct = settings.monitoring.daily_loss_warning_pct
        settings.load_yaml_overrides(config_dir=config_dir)
        assert settings.monitoring.daily_loss_warning_pct == default_pct
        assert settings._monitoring_yaml_overrides == set()

    def test_dashboard_and_prometheus_block_is_ignored(self, config_dir: Path) -> None:
        # The `monitoring:` sub-block (enable_prometheus/prometheus_port/
        # enable_dashboard/dashboard_port) intentionally stays env-only --
        # this file has no effect on those fields even when present.
        _write_yaml(
            config_dir / "monitoring.yaml",
            {"monitoring": {"prometheus_port": 12345, "enable_dashboard": False}},
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.monitoring.prometheus_port == 9090  # unchanged default
        assert settings.monitoring.enable_dashboard is True  # unchanged default


class TestLoggingYamlLoading:
    """
    configs/logging.yaml previously held a full logging.config dictConfig()
    tree that nothing ever passed to dictConfig() -- the live logging setup
    is entirely structlog-based (monitoring/telemetry.py:configure_logging,
    wired from bootstrap/app.py). Repurposed as rotation-only parameters for
    the RotatingFileHandler that configure_logging() attaches, using the
    same env > yaml > default precedence as risk.yaml/monitoring.yaml.
    """

    def test_yaml_values_apply_when_no_env_override(self, config_dir: Path) -> None:
        _write_yaml(
            config_dir / "logging.yaml",
            {
                "rotation": {
                    "enabled": False,
                    "filename": "custom.log",
                    "max_bytes": 1000,
                    "backup_count": 2,
                }
            },
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.logging.file_enabled is False
        assert settings.logging.file_name == "custom.log"
        assert settings.logging.max_bytes == 1000
        assert settings.logging.backup_count == 2

    def test_missing_logging_yaml_is_a_silent_noop(self, config_dir: Path) -> None:
        settings = Settings(_env_file=None)
        default_file_name = settings.logging.file_name
        settings.load_yaml_overrides(config_dir=config_dir)
        assert settings.logging.file_name == default_file_name
        assert settings.logging.file_enabled is True

    def test_partial_rotation_section_only_overrides_given_keys(self, config_dir: Path) -> None:
        _write_yaml(config_dir / "logging.yaml", {"rotation": {"backup_count": 7}})
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.logging.backup_count == 7
        assert settings.logging.file_name == "trading.log"  # unchanged default
        assert settings.logging.max_bytes == 52_428_800  # unchanged default

    def test_default_logger_levels_suppress_the_four_noisy_loggers(
        self, config_dir: Path
    ) -> None:
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.logging.logger_levels == {
            "asyncio": "WARNING",
            "websockets": "WARNING",
            "aiohttp": "WARNING",
            "sqlalchemy.engine": "WARNING",
        }

    def test_loggers_section_merges_over_default_without_dropping_others(
        self, config_dir: Path
    ) -> None:
        _write_yaml(
            config_dir / "logging.yaml",
            {"loggers": {"websockets": {"level": "DEBUG"}}},
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.logging.logger_levels["websockets"] == "DEBUG"
        # The other three defaults stay suppressed -- listing one logger
        # doesn't reset the rest.
        assert settings.logging.logger_levels["asyncio"] == "WARNING"
        assert settings.logging.logger_levels["aiohttp"] == "WARNING"
        assert settings.logging.logger_levels["sqlalchemy.engine"] == "WARNING"

    def test_loggers_section_can_add_a_new_logger(self, config_dir: Path) -> None:
        _write_yaml(
            config_dir / "logging.yaml",
            {"loggers": {"src.exchange.binance.ws": {"level": "INFO"}}},
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.logging.logger_levels["src.exchange.binance.ws"] == "INFO"
        assert settings.logging.logger_levels["asyncio"] == "WARNING"

    def test_invalid_log_level_in_loggers_section_is_ignored(self, config_dir: Path) -> None:
        _write_yaml(
            config_dir / "logging.yaml",
            {"loggers": {"asyncio": {"level": "NOT_A_LEVEL"}}},
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        # Falls back to the hardcoded default rather than accepting garbage.
        assert settings.logging.logger_levels["asyncio"] == "WARNING"


class TestExchangesYamlLoading:
    """
    configs/exchanges.yaml previously wasn't read by any code path either:
    BinanceConfig.rest_base_url/ws_base_url and BybitConfig.rest_base_url
    picked between hardcoded literals keyed only on `testnet` (module
    constants for Binance, inline string literals for Bybit), and
    max_orders_per_second/max_orders_per_minute weren't even fields --
    RateLimiter's constructor call in BinanceFuturesRESTClient/
    BybitFuturesClient hardcoded them directly. These cover the new
    _load_exchanges_yaml method using the same precedence contract as
    risk.yaml/symbols.yaml/monitoring.yaml.
    """

    def test_binance_yaml_values_apply_when_no_env_override(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "binance")
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        _write_yaml(
            config_dir / "exchanges.yaml",
            {
                "binance": {
                    "rest_mainnet": "https://custom-mainnet.example",
                    "rest_testnet": "https://custom-testnet.example",
                    "ws_mainnet": "wss://custom-mainnet-ws.example",
                    "ws_testnet": "wss://custom-testnet-ws.example",
                    "max_orders_per_second": 4,
                    "max_orders_per_minute": 200,
                }
            },
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.binance is not None
        assert settings.binance.rest_mainnet == "https://custom-mainnet.example"
        assert settings.binance.rest_testnet == "https://custom-testnet.example"
        assert settings.binance.ws_mainnet == "wss://custom-mainnet-ws.example"
        assert settings.binance.ws_testnet == "wss://custom-testnet-ws.example"
        assert settings.binance.max_orders_per_second == 4
        assert settings.binance.max_orders_per_minute == 200
        # testnet defaults True -> the *_base_url properties resolve to the
        # testnet entries, proving the properties actually read the merged
        # fields rather than the old hardcoded module constants.
        assert settings.binance.rest_base_url == "https://custom-testnet.example"
        assert settings.binance.ws_base_url == "wss://custom-testnet-ws.example"

    def test_env_var_still_wins_over_yaml_for_binance(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "binance")
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        monkeypatch.setenv("BINANCE_MAX_ORDERS_PER_SECOND", "7")
        _write_yaml(config_dir / "exchanges.yaml", {"binance": {"max_orders_per_second": 4}})

        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        # Documented precedence: env vars > yaml > field defaults.
        assert settings.binance.max_orders_per_second == 7

    def test_bybit_yaml_values_apply_when_no_env_override(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "bybit")
        monkeypatch.setenv("BYBIT_API_KEY", "k")
        monkeypatch.setenv("BYBIT_API_SECRET", "s")
        _write_yaml(
            config_dir / "exchanges.yaml",
            {
                "bybit": {
                    "rest_mainnet": "https://custom-bybit-mainnet.example",
                    "rest_testnet": "https://custom-bybit-testnet.example",
                    "max_orders_per_second": 5,
                    "max_orders_per_minute": 300,
                }
            },
        )
        settings = Settings(_env_file=None)
        settings.load_yaml_overrides(config_dir=config_dir)

        assert settings.bybit is not None
        assert settings.bybit.rest_mainnet == "https://custom-bybit-mainnet.example"
        assert settings.bybit.rest_testnet == "https://custom-bybit-testnet.example"
        assert settings.bybit.max_orders_per_second == 5
        assert settings.bybit.max_orders_per_minute == 300
        assert settings.bybit.rest_base_url == "https://custom-bybit-testnet.example"

    def test_missing_exchanges_yaml_is_a_silent_noop(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "binance")
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        settings = Settings(_env_file=None)
        default_rest_mainnet = settings.binance.rest_mainnet
        settings.load_yaml_overrides(config_dir=config_dir)
        assert settings.binance.rest_mainnet == default_rest_mainnet

    def test_noop_when_the_other_providers_config_is_none(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # exchange_provider=binance -> settings.bybit stays None. The
        # bybit section of exchanges.yaml must not raise trying to merge
        # into a config object that was never constructed.
        monkeypatch.setenv("EXCHANGE_PROVIDER", "binance")
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        _write_yaml(config_dir / "exchanges.yaml", {"bybit": {"max_orders_per_second": 99}})

        settings = Settings(_env_file=None)
        assert settings.bybit is None
        settings.load_yaml_overrides(config_dir=config_dir)  # must not raise
        assert settings.bybit is None
    """
    settings.binance/settings.bybit were declared as optional fields that
    nothing ever instantiated in the real get_settings() path -- only
    tests built them explicitly. That left the field None even when the
    documented BINANCE_*/BYBIT_* env vars (see .env.example) were actually
    set, so the app could never start against a real exchange.
    """

    def test_binance_env_vars_populate_settings_binance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "binance")
        monkeypatch.setenv("BINANCE_API_KEY", "real_key")
        monkeypatch.setenv("BINANCE_API_SECRET", "real_secret")
        monkeypatch.setenv("BINANCE_TESTNET", "true")

        settings = Settings(_env_file=None)

        assert settings.binance is not None
        assert settings.binance.api_key.get_secret_value() == "real_key"
        assert settings.bybit is None

    def test_bybit_env_vars_populate_settings_bybit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "bybit")
        monkeypatch.setenv("BYBIT_API_KEY", "real_key")
        monkeypatch.setenv("BYBIT_API_SECRET", "real_secret")
        monkeypatch.setenv("BYBIT_TESTNET", "true")

        settings = Settings(_env_file=None)

        assert settings.bybit is not None
        assert settings.bybit.api_key.get_secret_value() == "real_key"
        assert settings.binance is None

    def test_missing_credentials_leaves_field_none_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXCHANGE_PROVIDER", "binance")
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

        settings = Settings(_env_file=None)  # must not raise

        assert settings.binance is None

    def test_explicitly_passed_binance_config_is_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.core.config import BinanceConfig

        monkeypatch.setenv("BINANCE_API_KEY", "env_key")
        monkeypatch.setenv("BINANCE_API_SECRET", "env_secret")
        explicit = BinanceConfig(api_key="explicit_key", api_secret="explicit_secret")

        settings = Settings(_env_file=None, exchange_provider="binance", binance=explicit)

        assert settings.binance is explicit
        assert settings.binance.api_key.get_secret_value() == "explicit_key"


class TestSettingsSingleton:
    """
    Covers get_settings()/override_settings()/reset_settings() -- the
    module-global Settings singleton and its two test-only lifecycle
    helpers. None of the three had any test coverage before this: every
    other test in this file builds a Settings() instance directly
    (bypassing the singleton entirely via the make_settings fixture or a
    direct constructor call), so this singleton path was completely
    unexercised. override_settings/reset_settings exist specifically to
    let tests control this singleton without leaking state between tests
    -- this class is that intended usage, and also guards against a real
    regression risk: without an explicit reset, a Settings singleton
    created by an earlier test in the suite could otherwise leak into a
    later one via get_settings()'s global.
    """

    def teardown_method(self) -> None:
        # Always leave the singleton clean for whichever test runs next,
        # regardless of whether this test's own body already reset it.
        reset_settings()

    def test_get_settings_returns_singleton_on_repeated_calls(self) -> None:
        reset_settings()
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_reset_settings_forces_reinitialisation(self) -> None:
        reset_settings()
        first = get_settings()
        reset_settings()
        second = get_settings()
        assert first is not second

    def test_override_settings_replaces_singleton(self) -> None:
        reset_settings()
        get_settings()  # establish an initial singleton
        custom = Settings(_env_file=None)

        override_settings(custom)

        assert get_settings() is custom

    def test_override_settings_survives_until_next_reset(self) -> None:
        custom = Settings(_env_file=None)
        override_settings(custom)

        assert get_settings() is custom
        assert get_settings() is custom  # repeated calls don't reinitialise
