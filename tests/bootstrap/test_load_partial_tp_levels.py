"""Unit tests for Application._load_partial_tp_levels.

Standalone from test_app.py's full-Application-construction tests since
this method reads configs/strategy.yaml directly (same pattern
_load_track_funding_pnl was later modeled on) and touches no other
instance state, so it's cheaper to exercise via Application.__new__ +
monkeypatch.chdir than by constructing a full Application.

Flagged as a coverage gap in [1.3.20] (this method predates
_load_track_funding_pnl and was the one left without a dedicated test
file); closed here with the same test shape.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from src.bootstrap.app import Application


@pytest.fixture
def app() -> Application:
    return Application.__new__(Application)


class TestLoadPartialTpLevels:
    def test_missing_file_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert app._load_partial_tp_levels() is None

    def test_missing_partial_take_profit_section_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text("symbols: [BTCUSDT]\n")
        assert app._load_partial_tp_levels() is None

    def test_enabled_key_absent_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n  levels:\n    - [1.0, 0.5]\n"
        )
        assert app._load_partial_tp_levels() is None

    def test_explicit_disabled_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n  enabled: false\n  levels:\n    - [1.0, 0.5]\n"
        )
        assert app._load_partial_tp_levels() is None

    def test_enabled_with_valid_levels_returns_decimal_tuples(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n"
            "  enabled: true\n"
            "  levels:\n"
            "    - [1.0, 0.5]\n"
            "    - [2.0, 0.5]\n"
        )
        result = app._load_partial_tp_levels()
        assert result == [
            (Decimal("1.0"), Decimal("0.5")),
            (Decimal("2.0"), Decimal("0.5")),
        ]

    def test_enabled_with_no_levels_key_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n  enabled: true\n"
        )
        assert app._load_partial_tp_levels() is None

    def test_enabled_with_empty_levels_list_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n  enabled: true\n  levels: []\n"
        )
        assert app._load_partial_tp_levels() is None

    def test_malformed_level_entry_skipped_valid_kept(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n"
            "  enabled: true\n"
            "  levels:\n"
            "    - [1.0, 0.5]\n"
            "    - not_a_pair\n"
            "    - [2.0, 0.5]\n"
        )
        result = app._load_partial_tp_levels()
        assert result == [
            (Decimal("1.0"), Decimal("0.5")),
            (Decimal("2.0"), Decimal("0.5")),
        ]

    def test_all_levels_malformed_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "partial_take_profit:\n"
            "  enabled: true\n"
            "  levels:\n"
            "    - not_a_pair\n"
            "    - also_bad\n"
        )
        assert app._load_partial_tp_levels() is None

    def test_malformed_yaml_returns_none(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text("partial_take_profit: [1, 2\n")
        assert app._load_partial_tp_levels() is None
