"""
Status dashboard: HTTP polling endpoints + a real-time WebSocket feed.

HTTP (unchanged, thread-based, works with any client):
  GET /health    -> component health
  GET /status    -> live trading status
  GET /metrics   -> key performance metrics

WebSocket (new): connect to ws://<host>:<ws_port>/ws and receive a JSON
snapshot combining health+status+metrics every ``broadcast_interval``
seconds, plus an immediate snapshot on connect. This replaces dashboard
polling for anything that wants sub-second-feeling updates (a live
terminal UI, a browser dashboard) without hammering the HTTP endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

logger = logging.getLogger(__name__)

try:
    import websockets
except ImportError:  # pragma: no cover - optional at import time
    websockets = None  # type: ignore[assignment]


class DashboardHandler(BaseHTTPRequestHandler):
    """Request handler for the status dashboard."""

    data_provider: Any = None  # Injected after class definition

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # Suppress default access logging

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, self.data_provider.health() if self.data_provider else {})
        elif self.path == "/status":
            self._respond(200, self.data_provider.status() if self.data_provider else {})
        elif self.path == "/metrics":
            self._respond(200, self.data_provider.metrics() if self.data_provider else {})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Dashboard:
    """Runs the status dashboard: HTTP on a background thread, WS on the event loop."""

    def __init__(self, port: int = 8080, ws_port: int = 8081) -> None:
        self._port = port
        self._ws_port = ws_port
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None
        self._health_fn: Any = None
        self._status_fn: Any = None
        self._metrics_fn: Any = None

        self._ws_server: Any = None
        self._ws_clients: set[Any] = set()
        self._ws_broadcast_task: asyncio.Task | None = None

    def set_providers(
        self,
        health_fn: Any,
        status_fn: Any,
        metrics_fn: Any,
    ) -> None:
        self._health_fn = health_fn
        self._status_fn = status_fn
        self._metrics_fn = metrics_fn

    # ─── HTTP (polling) ───────────────────────────────────────────────────────

    def start(self) -> None:
        provider = self

        # NOTE: `Handler` subclasses BaseHTTPRequestHandler, whose __init__
        # requires (request, client_address, server) and is invoked by
        # HTTPServer per-connection — it must never be instantiated
        # directly. A prior version did `Handler.data_provider = Handler()`,
        # which raises TypeError as soon as a request comes in. Use a
        # plain, non-handler class to supply data instead.
        class _DataProvider:
            def health(self) -> dict[str, Any]:
                return provider._health_fn() if provider._health_fn else {"status": "unknown"}

            def status(self) -> dict[str, Any]:
                return provider._status_fn() if provider._status_fn else {}

            def metrics(self) -> dict[str, Any]:
                return provider._metrics_fn() if provider._metrics_fn else {}

        class Handler(DashboardHandler):
            pass

        Handler.data_provider = _DataProvider()
        self._server = HTTPServer(("0.0.0.0", self._port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True, name="dashboard")
        self._thread.start()
        logger.info("Dashboard HTTP started on port %d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            # HTTPServer.shutdown() only stops the serve_forever() loop;
            # it does not release the listening socket. Without
            # server_close() the socket keeps accepting TCP connections
            # at the OS level (nothing ever reads/responds to them since
            # the loop that would call do_GET has already exited), so a
            # client sees a hang until its own timeout rather than an
            # immediate connection-refused — and, on a long-running
            # process that stops/starts the dashboard repeatedly (e.g.
            # around a reload), each cycle leaks one listening file
            # descriptor.
            self._server.server_close()
        if self._ws_broadcast_task is not None:
            self._ws_broadcast_task.cancel()

    # ─── WebSocket (push) ─────────────────────────────────────────────────────

    def _snapshot(self) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "health": self._health_fn() if self._health_fn else {"status": "unknown"},
            "status": self._status_fn() if self._status_fn else {},
            "metrics": self._metrics_fn() if self._metrics_fn else {},
        }

    async def _handle_ws_client(self, connection: Any) -> None:
        self._ws_clients.add(connection)
        client_addr = getattr(connection, "remote_address", "unknown")
        logger.info(
            "Dashboard WS client connected: %s (total=%d)", client_addr, len(self._ws_clients)
        )
        try:
            # Immediate snapshot on connect so the UI doesn't wait for the
            # next broadcast tick.
            await connection.send(json.dumps(self._snapshot(), default=str))
            async for _ in connection:
                pass  # This feed is push-only; ignore any inbound messages.
        except Exception as exc:
            logger.debug("Dashboard WS client %s disconnected: %s", client_addr, exc)
        finally:
            self._ws_clients.discard(connection)
            logger.info(
                "Dashboard WS client disconnected: %s (total=%d)",
                client_addr,
                len(self._ws_clients),
            )

    async def _broadcast_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            if not self._ws_clients:
                continue
            payload = json.dumps(self._snapshot(), default=str)
            stale: set[Any] = set()
            for client in list(self._ws_clients):
                try:
                    await client.send(payload)
                except Exception:
                    stale.add(client)
            self._ws_clients -= stale

    async def start_ws(self, broadcast_interval: float = 2.0) -> None:
        """
        Start the WebSocket push server on the current asyncio event loop.

        Must be awaited from within a running event loop (e.g. scheduled
        alongside ``heartbeat.start()`` / ``watchdog.start()`` in
        ``bootstrap/startup.py``). No-op with a clear log message if the
        optional ``websockets`` dependency isn't installed.
        """
        if websockets is None:
            logger.warning("Dashboard WS server not started: 'websockets' package not installed.")
            return

        self._ws_server = await websockets.serve(self._handle_ws_client, "0.0.0.0", self._ws_port)
        self._ws_broadcast_task = asyncio.create_task(
            self._broadcast_loop(broadcast_interval), name="dashboard-ws-broadcast"
        )
        logger.info(
            "Dashboard WS started on port %d (broadcast every %.1fs)",
            self._ws_port,
            broadcast_interval,
        )

    async def stop_ws(self) -> None:
        if self._ws_broadcast_task is not None:
            self._ws_broadcast_task.cancel()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
