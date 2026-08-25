"""Unit tests for src.utils.files."""

from __future__ import annotations

from pathlib import Path

from src.utils.files import ensure_dir, read_csv, read_json, write_csv, write_json


class TestWriteAndReadJson:
    def test_round_trips_a_simple_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        write_json(path, {"a": 1, "b": "two"})
        assert read_json(path) == {"a": 1, "b": "two"}

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "data.json"
        write_json(path, {"x": 1})
        assert path.exists()
        assert read_json(path) == {"x": 1}

    def test_non_serializable_values_are_stringified_via_default_str(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "data.json"

        class Thing:
            def __str__(self) -> str:
                return "thing-repr"

        write_json(path, {"obj": Thing()})
        assert read_json(path) == {"obj": "thing-repr"}

    def test_writes_with_the_requested_indent(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        write_json(path, {"a": 1}, indent=4)
        text = path.read_text(encoding="utf-8")
        # 4-space indent means the nested key line starts with 4 spaces.
        assert '\n    "a"' in text

    def test_utf8_content_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        write_json(path, {"name": "café \u2603"})
        assert read_json(path) == {"name": "café \u2603"}


class TestWriteAndReadCsv:
    def test_round_trips_rows_with_string_values(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        rows = [{"symbol": "BTCUSDT", "side": "BUY"}, {"symbol": "ETHUSDT", "side": "SELL"}]
        write_csv(path, rows)
        result = read_csv(path)
        assert result == rows

    def test_empty_rows_list_writes_nothing_and_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "empty.csv"
        write_csv(path, [])
        assert not path.exists()

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "data.csv"
        write_csv(path, [{"a": "1"}])
        assert path.exists()

    def test_uses_keys_of_first_row_as_header(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        write_csv(path, [{"first": "1", "second": "2"}])
        text = path.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "first,second"


class TestEnsureDir:
    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "new_dir"
        result = ensure_dir(target)
        assert target.is_dir()
        assert result == target

    def test_is_idempotent_when_directory_already_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "existing"
        target.mkdir()
        result = ensure_dir(target)
        assert result == target
        assert target.is_dir()

    def test_creates_nested_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        ensure_dir(target)
        assert target.is_dir()
