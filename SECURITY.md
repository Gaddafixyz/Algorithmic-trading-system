# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately rather than opening a public issue. Email the maintainers with:

1. A description of the vulnerability and its potential impact.
2. Steps to reproduce.
3. Any suggested remediation.

You should expect an initial response within 5 business days.

## Security Practices in This Codebase

- **No secrets in code or version control.** All credentials (Binance API
  keys, database passwords, notification webhooks) are loaded exclusively
  from environment variables (`.env`, never committed — see `.gitignore`).
  `src/core/config.py` uses Pydantic `SecretStr` for all credential fields
  so they are never accidentally logged or serialized in plaintext.
- **HMAC request signing.** All authenticated Binance API requests are
  signed with HMAC-SHA256 per the exchange's specification
  (`src/exchange/binance/auth.py`). The API secret is never transmitted in
  request bodies or logged.
- **Least privilege.** The Docker image runs as a non-root user (`trader`,
  UID 1001). Database credentials should be scoped to only the permissions
  the application needs (read/write on its own schema).
- **Dependency scanning.** `make security-check` runs `bandit` (static
  analysis for common Python security issues) and `safety` (known CVE
  vulnerability scanning of dependencies) — run this before any release.
- **Input validation.** All external input (exchange API responses, user
  configuration) is validated through Pydantic models with explicit
  constraints (`src/core/validation.py`, `src/core/models.py`) before use
  in financial calculations or order placement.
- **No code execution from untrusted input.** The system never evaluates
  or executes external input as code.

## Known Security Considerations for Operators

- **API key permissions**: when generating Binance API keys, enable only
  "Enable Futures" and **disable withdrawal permissions**. The trading
  system never needs withdrawal access, and disabling it limits blast
  radius if credentials are ever compromised.
- **Network egress**: the Docker container's network access should be
  restricted to only the Binance API endpoints, your database, and your
  notification webhooks in production deployments — see
  `deployments/kubernetes/` for a NetworkPolicy starting point.
- **IP whitelisting**: Binance API keys support IP whitelisting; enable
  this for production deployments and restrict to your server's static
  IP.
- **Testnet first**: always validate strategy and infrastructure changes
  on Binance testnet (`TRADING_ENV=testnet`) before switching to
  `production`.
