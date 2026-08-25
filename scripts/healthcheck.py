"""
Health check script.

Used by Docker HEALTHCHECK and manual diagnostics.
Exits 0 if healthy, 1 if unhealthy.

Usage:
    python scripts/healthcheck.py
    python scripts/healthcheck.py --url http://localhost:8080/health
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def check_dashboard(url: str, timeout: float = 5.0) -> bool:
    """Check the dashboard health endpoint."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            status = body.get("status", "unknown")
            return status in ("healthy", "degraded")
    except urllib.error.URLError:
        return False
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading system health check")
    parser.add_argument(
        "--url",
        default="http://localhost:8080/health",
        help="Health endpoint URL",
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    healthy = check_dashboard(args.url, args.timeout)
    if healthy:
        print("HEALTHY")
        sys.exit(0)
    else:
        print("UNHEALTHY")
        sys.exit(1)


if __name__ == "__main__":
    main()
