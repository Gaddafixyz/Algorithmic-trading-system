"""Tests for src.monitoring.dashboard.Dashboard.

HTTP tests spin up the real ``Dashboard`` on an OS-assigned port
(``port=0``) and hit it with genuine ``urllib.request`` calls rather
than mocking ``http.server`` -- this is a direct regression test for
the bug documented in ``Dashboard.start()``: a prior version did
``Handler.data_provider = Handler()``, instantiating
``BaseHTTPRequestHandler`` directly (it requires
``(request, client_address, server)`` args supplied per-connection by
``HTTPServer``), which raised ``TypeError`` as soon as any request
came in. A live request round-trip is the only way to catch that
class of bug; a unit test against ``DashboardHandler`` in isolation
would not exercise ``HTTPServer``'s instantiation of the handler.

WebSocket tests use fake, duck-typed connection objects (``send``,
``__aiter__``/``__anext__``, ``remote_address``) rather than the real
``websockets`` library, since ``_handle_ws_client``/``_broadcast_loop``
only depend on that interface and the library may not be installed in
every environment. A separate test covers the graceful no-op path
when ``websockets`` genuinely isn't importable.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

from src.monitoring.dashboard import Dashboard


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def running_dashboard():
    d = Dashboard(port=0, ws_port=0)
    d.start()
    # HTTPServer binds synchronously in start(), but give the serve_forever
    # thread a moment to be scheduled before the first request.
    time.sleep(0.05)
    yield d
    d.stop()


class TestHttpEndpointsWithProviders:
    def test_health_endpoint_returns_provider_data(self, running_dashboard: Dashboard) -> None:
        running_dashboard.set_providers(
            lambda: {"status": "healthy"}, lambda: {}, lambda: {}
        )
        status, body = _get(running_dashboard._server.server_port, "/health")  # noqa: SLF001
        assert status == 200
        assert body == {"status": "healthy"}

    def test_status_endpoint_returns_provider_data(self, running_dashboard: Dashboard) -> None:
        running_dashboard.set_providers(
            lambda: {}, lambda: {"running": True, "positions": 2}, lambda: {}
        )
        status, body = _get(running_dashboard._server.server_port, "/status")  # noqa: SLF001
        assert status == 200
        assert body == {"running": True, "positions": 2}

    def test_metrics_endpoint_returns_provider_data(self, running_dashboard: Dashboard) -> None:
        running_dashboard.set_providers(
            lambda: {}, lambda: {}, lambda: {"win_rate": 0.55}
        )
        status, body = _get(running_dashboard._server.server_port, "/metrics")  # noqa: SLF001
        assert status == 200
        assert body == {"win_rate": 0.55}

    def test_unknown_path_returns_404(self, running_dashboard: Dashboard) -> None:
        status, body = _get(running_dashboard._server.server_port, "/nope")  # noqa: SLF001
        assert status == 404
        assert body == {"error": "not found"}


class TestHttpEndpointsWithoutProviders:
    """Regression: Handler must not crash when no providers were set."""

    def test_health_defaults_to_unknown_status(self, running_dashboard: Dashboard) -> None:
        status, body = _get(running_dashboard._server.server_port, "/health")  # noqa: SLF001
        assert status == 200
        assert body == {"status": "unknown"}

    def test_status_and_metrics_default_to_empty(self, running_dashboard: Dashboard) -> None:
        port = running_dashboard._server.server_port  # noqa: SLF001
        _, status_body = _get(port, "/status")
        _, metrics_body = _get(port, "/metrics")
        assert status_body == {}
        assert metrics_body == {}


class TestStopReleasesSocket:
    """
    Regression coverage: stop() previously only called shutdown() (which
    just stops the serve_forever loop) without server_close() (which
    releases the listening socket) — a client would see a hang until its
    own timeout instead of an immediate refusal, and each stop() leaked
    one listening file descriptor.
    """

    def test_stop_shuts_down_the_http_server(self, running_dashboard: Dashboard) -> None:
        port = running_dashboard._server.server_port  # noqa: SLF001
        _get(port, "/health")  # server is up

        running_dashboard.stop()

        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)


class TestHandlerInstantiationRegression:
    """Regression coverage for the `Handler.data_provider = Handler()` bug:
    a real request must succeed, not raise TypeError inside the server
    thread (which would surface to the client as a broken connection,
    not a clean HTTP error)."""

    def test_multiple_sequential_requests_all_succeed(
        self, running_dashboard: Dashboard
    ) -> None:
        port = running_dashboard._server.server_port  # noqa: SLF001
        for _ in range(5):
            status, _ = _get(port, "/health")
            assert status == 200

    def test_two_dashboards_do_not_share_handler_state(self) -> None:
        """Each Dashboard.start() builds its own Handler subclass, so two
        independently-started dashboards must not clobber each other's
        data_provider."""
        d1 = Dashboard(port=0, ws_port=0)
        d1.set_providers(lambda: {"who": "d1"}, lambda: {}, lambda: {})
        d1.start()
        d2 = Dashboard(port=0, ws_port=0)
        d2.set_providers(lambda: {"who": "d2"}, lambda: {}, lambda: {})
        d2.start()
        time.sleep(0.05)
        try:
            _, body1 = _get(d1._server.server_port, "/health")  # noqa: SLF001
            _, body2 = _get(d2._server.server_port, "/health")  # noqa: SLF001
            assert body1 == {"who": "d1"}
            assert body2 == {"who": "d2"}
        finally:
            d1.stop()
            d2.stop()


class TestSnapshot:
    def test_snapshot_combines_all_three_providers(self) -> None:
        d = Dashboard()
        d.set_providers(
            lambda: {"status": "ok"}, lambda: {"running": True}, lambda: {"pnl": 5.0}
        )
        snap = d._snapshot()  # noqa: SLF001
        assert snap["type"] == "snapshot"
        assert snap["health"] == {"status": "ok"}
        assert snap["status"] == {"running": True}
        assert snap["metrics"] == {"pnl": 5.0}
        assert "timestamp" in snap

    def test_snapshot_defaults_without_providers(self) -> None:
        d = Dashboard()
        snap = d._snapshot()  # noqa: SLF001
        assert snap["health"] == {"status": "unknown"}
        assert snap["status"] == {}
        assert snap["metrics"] == {}


class _FakeWSConnection:
    """Duck-types the subset of a websockets connection that the
    dashboard actually uses: async `send`, async iteration that blocks
    until `close()` is called (simulating an open connection), and
    `remote_address` for logging."""

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.remote_address = ("127.0.0.1", 9999)
        self._fail_after = fail_after
        self._n = 0
        self._closed = asyncio.Event()

    async def send(self, payload: str) -> None:
        self._n += 1
        if self._fail_after is not None and self._n > self._fail_after:
            raise ConnectionError("client gone")
        self.sent.append(payload)

    def close(self) -> None:
        self._closed.set()

    def __aiter__(self) -> "_FakeWSConnection":
        return self

    async def __anext__(self) -> None:
        await self._closed.wait()
        raise StopAsyncIteration


class TestWebSocketClientHandling:
    async def test_client_receives_immediate_snapshot_on_connect(self) -> None:
        d = Dashboard()
        d.set_providers(lambda: {"status": "ok"}, lambda: {}, lambda: {})
        conn = _FakeWSConnection()
        task = asyncio.create_task(d._handle_ws_client(conn))  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert len(conn.sent) == 1
        payload = json.loads(conn.sent[0])
        assert payload["type"] == "snapshot"
        assert payload["health"] == {"status": "ok"}
        conn.close()
        await task

    async def test_client_tracked_while_connected_and_removed_after_disconnect(self) -> None:
        d = Dashboard()
        conn = _FakeWSConnection()
        task = asyncio.create_task(d._handle_ws_client(conn))  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert conn in d._ws_clients  # noqa: SLF001

        conn.close()
        await task
        assert conn not in d._ws_clients  # noqa: SLF001


class TestBroadcastLoop:
    async def test_broadcasts_to_all_connected_clients(self) -> None:
        d = Dashboard()
        d.set_providers(lambda: {"status": "ok"}, lambda: {}, lambda: {})
        c1, c2 = _FakeWSConnection(), _FakeWSConnection()
        d._ws_clients = {c1, c2}  # noqa: SLF001
        task = asyncio.create_task(d._broadcast_loop(0.01))  # noqa: SLF001
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(c1.sent) >= 1
        assert len(c2.sent) >= 1

    async def test_no_broadcast_when_no_clients_connected(self) -> None:
        """No error, no wasted snapshot work, when nobody is listening."""
        d = Dashboard()
        d._ws_clients = set()  # noqa: SLF001
        task = asyncio.create_task(d._broadcast_loop(0.01))  # noqa: SLF001
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Nothing to assert on directly beyond "did not raise" -- the
        # `if not self._ws_clients: continue` branch is what's covered.

    async def test_stale_client_pruned_after_send_failure(self) -> None:
        """Regression: a client whose send() raises must be dropped from
        `_ws_clients` so future broadcasts don't keep retrying it."""
        good = _FakeWSConnection()
        bad = _FakeWSConnection(fail_after=0)
        d = Dashboard()
        d.set_providers(lambda: {"status": "ok"}, lambda: {}, lambda: {})
        d._ws_clients = {good, bad}  # noqa: SLF001
        task = asyncio.create_task(d._broadcast_loop(0.01))  # noqa: SLF001
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bad not in d._ws_clients  # noqa: SLF001
        assert good in d._ws_clients  # noqa: SLF001
        assert len(good.sent) >= 1


class TestWebsocketsNotInstalled:
    async def test_start_ws_no_ops_and_warns_when_dependency_missing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        import src.monitoring.dashboard as dashboard_module

        monkeypatch.setattr(dashboard_module, "websockets", None)
        d = Dashboard()
        with caplog.at_level(logging.WARNING, logger="src.monitoring.dashboard"):
            await d.start_ws()
        assert d._ws_server is None  # noqa: SLF001
        assert any("not installed" in rec.message for rec in caplog.records)

    async def test_stop_ws_is_safe_when_never_started(self) -> None:
        d = Dashboard()
        await d.stop_ws()  # must not raise


class TestRealWebsocketsServer:
    """Only runs when the optional `websockets` dependency is actually
    installed -- exercises `start_ws`/`stop_ws` against the real
    library rather than the fake duck-typed connection used above."""

    async def test_start_and_stop_ws_lifecycle(self) -> None:
        pytest.importorskip("websockets")
        d = Dashboard(port=0, ws_port=0)
        await d.start_ws(broadcast_interval=100.0)
        assert d._ws_server is not None  # noqa: SLF001
        await d.stop_ws()
