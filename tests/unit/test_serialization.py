"""Unit tests for src.core.serialization and the src.utils.json shim."""

from __future__ import annotations

import json as stdlib_json
from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest
from pydantic import BaseModel

from src.core.serialization import (
    DecimalEncoder,
    dumps,
    dumps_str,
    from_dict,
    loads,
    to_dict,
)
from src.utils.json import dumps as json_shim_dumps
from src.utils.json import loads as json_shim_loads


class TestDumpsAndLoads:
    def test_round_trips_a_simple_dict(self) -> None:
        data = {"a": 1, "b": "two", "c": [1, 2, 3]}
        result = loads(dumps(data))
        assert result == data

    def test_dumps_returns_bytes(self) -> None:
        assert isinstance(dumps({"a": 1}), bytes)

    def test_dumps_serializes_decimal_via_default_encoder(self) -> None:
        result = loads(dumps({"price": Decimal("123.45")}))
        assert result == {"price": "123.45"}

    def test_dumps_serializes_datetime_natively(self) -> None:
        dt = datetime(2021, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = loads(dumps({"ts": dt}))
        assert result["ts"] == "2021-01-01T12:00:00+00:00"

    def test_dumps_serializes_numpy_arrays(self) -> None:
        arr = np.array([1, 2, 3])
        result = loads(dumps({"values": arr}))
        assert result == {"values": [1, 2, 3]}

    def test_dumps_serializes_numpy_scalars(self) -> None:
        result = loads(dumps({"count": np.int64(42)}))
        assert result == {"count": 42}

    def test_dumps_with_non_string_keys(self) -> None:
        # OPT_NON_STR_KEYS allows int keys, which orjson otherwise rejects.
        result = loads(dumps({1: "a", 2: "b"}))
        assert result == {"1": "a", "2": "b"}

    def test_dumps_raises_type_error_for_truly_unsupported_types(self) -> None:
        class Unsupported:
            pass

        with pytest.raises(TypeError, match="not JSON serializable"):
            dumps({"obj": Unsupported()})

    def test_indent_option_produces_multiline_output(self) -> None:
        compact = dumps({"a": 1})
        indented = dumps({"a": 1}, indent=True)
        assert b"\n" not in compact
        assert b"\n" in indented

    def test_loads_accepts_str_input(self) -> None:
        assert loads('{"a": 1}') == {"a": 1}

    def test_loads_accepts_bytes_input(self) -> None:
        assert loads(b'{"a": 1}') == {"a": 1}

    def test_loads_accepts_bytearray_input(self) -> None:
        assert loads(bytearray(b'{"a": 1}')) == {"a": 1}


class TestDumpsStr:
    def test_returns_a_utf8_decoded_string(self) -> None:
        result = dumps_str({"a": 1})
        assert isinstance(result, str)
        assert stdlib_json.loads(result) == {"a": 1}

    def test_respects_indent_option(self) -> None:
        result = dumps_str({"a": 1}, indent=True)
        assert "\n" in result


class _SampleModel(BaseModel):
    name: str
    count: int


class TestToDictAndFromDict:
    def test_to_dict_converts_a_pydantic_model(self) -> None:
        model = _SampleModel(name="btc", count=5)
        result = to_dict(model)
        assert result == {"name": "btc", "count": 5}

    def test_to_dict_falls_back_to_dict_for_non_pydantic_objects(self) -> None:
        # dict(model) works for anything implementing the mapping protocol
        # (keys() + __getitem__), which a plain dict already satisfies.
        result = to_dict({"already": "a-dict"})
        assert result == {"already": "a-dict"}

    def test_from_dict_constructs_a_pydantic_model(self) -> None:
        model = from_dict(_SampleModel, {"name": "eth", "count": 10})
        assert isinstance(model, _SampleModel)
        assert model.name == "eth"
        assert model.count == 10

    def test_to_dict_and_from_dict_round_trip(self) -> None:
        original = _SampleModel(name="sol", count=1)
        restored = from_dict(_SampleModel, to_dict(original))
        assert restored == original


class TestDecimalEncoder:
    def test_encodes_decimal_as_string(self) -> None:
        result = stdlib_json.dumps({"price": Decimal("9.99")}, cls=DecimalEncoder)
        assert stdlib_json.loads(result) == {"price": "9.99"}

    def test_encodes_datetime_as_isoformat(self) -> None:
        dt = datetime(2021, 1, 1, tzinfo=UTC)
        result = stdlib_json.dumps({"ts": dt}, cls=DecimalEncoder)
        assert stdlib_json.loads(result) == {"ts": "2021-01-01T00:00:00+00:00"}

    def test_raises_type_error_for_unsupported_types_via_super(self) -> None:
        class Unsupported:
            pass

        with pytest.raises(TypeError):
            stdlib_json.dumps({"obj": Unsupported()}, cls=DecimalEncoder)


class TestJsonUtilsShim:
    """src.utils.json re-exports dumps/dumps_str/loads from core.serialization."""

    def test_dumps_shim_matches_core_serialization_dumps(self) -> None:
        assert json_shim_dumps({"a": 1}) == dumps({"a": 1})

    def test_loads_shim_matches_core_serialization_loads(self) -> None:
        assert json_shim_loads(b'{"a": 1}') == loads(b'{"a": 1}')

    def test_shim_round_trip(self) -> None:
        assert json_shim_loads(json_shim_dumps({"x": Decimal("1.5")})) == {"x": "1.5"}
