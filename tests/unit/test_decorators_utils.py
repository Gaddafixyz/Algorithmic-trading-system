"""Unit tests for src.utils.decorators."""

from __future__ import annotations

import logging

import pytest

from src.utils.decorators import log_call, singleton, validate_positive


class TestLogCall:
    def test_preserves_the_wrapped_functions_return_value(self) -> None:
        @log_call
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

    def test_preserves_function_metadata_via_functools_wraps(self) -> None:
        @log_call
        def my_function() -> None:
            """Docstring."""

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "Docstring."

    def test_logs_entry_and_exit_at_debug_level(self, caplog: pytest.LogCaptureFixture) -> None:
        @log_call
        def traced() -> str:
            return "ok"

        with caplog.at_level(logging.DEBUG, logger="src.utils.decorators"):
            result = traced()

        assert result == "ok"
        messages = [record.getMessage() for record in caplog.records]
        assert any("traced" in m and "→" in m for m in messages)
        assert any("traced" in m and "←" in m for m in messages)

    def test_propagates_exceptions_from_the_wrapped_function(self) -> None:
        @log_call
        def failing() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing()


class TestSingleton:
    def test_returns_the_same_instance_on_repeated_calls(self) -> None:
        @singleton
        class Config:
            def __init__(self) -> None:
                self.value = object()

        first = Config()
        second = Config()
        assert first is second

    def test_constructor_arguments_only_apply_to_the_first_instantiation(self) -> None:
        @singleton
        class Holder:
            def __init__(self, value: int) -> None:
                self.value = value

        first = Holder(1)
        second = Holder(2)  # ignored: singleton already constructed
        assert first is second
        assert second.value == 1

    def test_distinct_singleton_classes_do_not_share_state(self) -> None:
        @singleton
        class A:
            def __init__(self) -> None:
                self.name = "a"

        @singleton
        class B:
            def __init__(self) -> None:
                self.name = "b"

        assert A().name == "a"
        assert B().name == "b"
        assert A() is not B()


class TestValidatePositive:
    def test_allows_a_positive_value(self) -> None:
        @validate_positive("amount")
        def place_order(*, amount: float) -> float:
            return amount

        assert place_order(amount=10.0) == 10.0

    def test_rejects_a_zero_value(self) -> None:
        @validate_positive("amount")
        def place_order(*, amount: float) -> float:
            return amount

        with pytest.raises(ValueError, match="amount must be positive, got 0"):
            place_order(amount=0)

    def test_rejects_a_negative_value(self) -> None:
        @validate_positive("amount")
        def place_order(*, amount: float) -> float:
            return amount

        with pytest.raises(ValueError, match="amount must be positive, got -5"):
            place_order(amount=-5)

    def test_missing_keyword_argument_is_not_validated(self) -> None:
        # The decorator only validates the named kwarg if present; a
        # positional-only call (or omitted kwarg) passes through, leaving
        # any required-argument enforcement to the wrapped function itself.
        @validate_positive("amount")
        def optional_amount(*, amount: float = 1.0) -> float:
            return amount

        assert optional_amount() == 1.0

    def test_none_value_is_not_treated_as_invalid(self) -> None:
        @validate_positive("amount")
        def nullable(*, amount: float | None) -> float | None:
            return amount

        assert nullable(amount=None) is None
