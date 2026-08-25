"""Unit tests for Application._load_track_funding_pnl.

Standalone from test_app.py's full-Application-construction tests since
this method reads configs/strategy.yaml directly (mirrors
_load_partial_tp_levels) and touches no other instance state, so it's
cheaper to exercise via Application.__new__ + monkeypatch.chdir than by
constructing a full Application.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.bootstrap.app import Application


@pytest.fixture
def app() -> Application:
    return Application.__new__(Application)


class TestLoadTrackFundingPnl:
    def test_missing_file_returns_false(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert app._load_track_funding_pnl() is False

    def test_missing_execution_section_returns_false(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text("symbols: [BTCUSDT]\n")
        assert app._load_track_funding_pnl() is False

    def test_explicit_false_returns_false(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "execution:\n  track_funding_pnl: false\n"
        )
        assert app._load_track_funding_pnl() is False

    def test_explicit_true_returns_true(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "execution:\n  track_funding_pnl: true\n"
        )
        assert app._load_track_funding_pnl() is True

    def test_non_bool_value_returns_false(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text(
            "execution:\n  track_funding_pnl: 'yes please'\n"
        )
        assert app._load_track_funding_pnl() is False

    def test_malformed_yaml_returns_false(
        self, app: Application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "configs").mkdir()
        (tmp_path / "configs" / "strategy.yaml").write_text("execution: [1, 2\n")
        assert app._load_track_funding_pnl() is False
