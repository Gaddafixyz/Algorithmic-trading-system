"""Unit tests for src.monitoring.metrics.

Covers the module-level Prometheus metric singletons (correct type,
name, and label configuration) and start_metrics_server().
"""

from __future__ import annotations

from unittest.mock import patch

from prometheus_client import Counter, Gauge, Histogram, Info

from src.monitoring import metrics


class TestMetricDefinitions:
    """Each metric must be the expected Prometheus type with the
    documented name and label set, since dashboards/alerts key off
    these exact names.
    """

    def test_signals_generated_is_a_counter_with_expected_labels(self) -> None:
        assert isinstance(metrics.signals_generated, Counter)
        assert set(metrics.signals_generated._labelnames) == {
            "strategy", "symbol", "direction",
        }

    def test_orders_submitted_is_a_counter_with_expected_labels(self) -> None:
        assert isinstance(metrics.orders_submitted, Counter)
        assert set(metrics.orders_submitted._labelnames) == {
            "symbol", "side", "order_type",
        }

    def test_orders_filled_is_a_counter(self) -> None:
        assert isinstance(metrics.orders_filled, Counter)
        assert set(metrics.orders_filled._labelnames) == {"symbol", "side"}

    def test_trades_closed_is_a_counter(self) -> None:
        assert isinstance(metrics.trades_closed, Counter)
        assert set(metrics.trades_closed._labelnames) == {
            "symbol", "strategy", "outcome",
        }

    def test_open_positions_count_is_a_gauge_with_no_labels(self) -> None:
        assert isinstance(metrics.open_positions_count, Gauge)
        assert metrics.open_positions_count._labelnames == ()

    def test_position_pnl_is_a_gauge_labeled_by_symbol(self) -> None:
        assert isinstance(metrics.position_pnl, Gauge)
        assert metrics.position_pnl._labelnames == ("symbol",)

    def test_account_equity_daily_pnl_and_drawdown_are_unlabeled_gauges(self) -> None:
        for gauge in (
            metrics.account_equity,
            metrics.account_daily_pnl,
            metrics.account_drawdown_pct,
        ):
            assert isinstance(gauge, Gauge)
            assert gauge._labelnames == ()

    def test_circuit_breaker_state_is_a_gauge(self) -> None:
        assert isinstance(metrics.circuit_breaker_state, Gauge)

    def test_risk_validations_is_a_counter_labeled_by_result(self) -> None:
        assert isinstance(metrics.risk_validations, Counter)
        assert metrics.risk_validations._labelnames == ("result",)

    def test_ws_metrics_are_counters(self) -> None:
        assert isinstance(metrics.ws_messages_received, Counter)
        assert metrics.ws_messages_received._labelnames == ("stream",)
        assert isinstance(metrics.ws_reconnects, Counter)
        assert metrics.ws_reconnects._labelnames == ()

    def test_api_requests_is_a_counter_with_expected_labels(self) -> None:
        assert isinstance(metrics.api_requests, Counter)
        assert set(metrics.api_requests._labelnames) == {
            "method", "endpoint", "status",
        }

    def test_api_latency_is_a_histogram_with_expected_buckets(self) -> None:
        assert isinstance(metrics.api_latency, Histogram)
        assert metrics.api_latency._labelnames == ("endpoint",)
        # +Inf bucket is appended automatically by prometheus_client.
        assert metrics.api_latency._upper_bounds[:-1] == (
            0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
        )

    def test_candle_processing_latency_is_a_histogram(self) -> None:
        assert isinstance(metrics.candle_processing_latency, Histogram)
        assert set(metrics.candle_processing_latency._labelnames) == {
            "symbol", "timeframe",
        }

    def test_system_info_is_an_info_metric(self) -> None:
        assert isinstance(metrics.system_info, Info)


class TestMetricUsage:
    """Metrics must actually be incrementable/observable the way the
    rest of the codebase uses them (label(...).inc() / .set() / .observe()).
    """

    def test_counter_increments_per_label_combination(self) -> None:
        before = metrics.signals_generated.labels(
            strategy="smc", symbol="BTCUSDT", direction="long"
        )._value.get()
        metrics.signals_generated.labels(
            strategy="smc", symbol="BTCUSDT", direction="long"
        ).inc()
        after = metrics.signals_generated.labels(
            strategy="smc", symbol="BTCUSDT", direction="long"
        )._value.get()
        assert after == before + 1

    def test_gauge_can_be_set(self) -> None:
        metrics.account_equity.set(12345.67)
        assert metrics.account_equity._value.get() == 12345.67

    def test_histogram_can_observe(self) -> None:
        metrics.api_latency.labels(endpoint="/order").observe(0.03)
        # No exception, and the observation lands in a real bucket sample.
        samples = list(metrics.api_latency.collect()[0].samples)
        assert any(s.name.endswith("_count") for s in samples)


class TestStartMetricsServer:
    def test_calls_start_http_server_with_given_port(self) -> None:
        with patch("src.monitoring.metrics.start_http_server") as mock_start:
            metrics.start_metrics_server(port=9999)
        mock_start.assert_called_once_with(9999)

    def test_defaults_to_port_9090(self) -> None:
        with patch("src.monitoring.metrics.start_http_server") as mock_start:
            metrics.start_metrics_server()
        mock_start.assert_called_once_with(9090)

    def test_logs_after_starting_server(self, caplog) -> None:  # type: ignore[no-untyped-def]
        import logging

        with patch("src.monitoring.metrics.start_http_server"):
            with caplog.at_level(logging.INFO, logger="src.monitoring.metrics"):
                metrics.start_metrics_server(port=9091)
        assert any("9091" in rec.message or "9091" in str(rec.args) for rec in caplog.records)
