#!/usr/bin/env bash
#
# Bare-metal install script for trading-system.
# Tested against Ubuntu/Debian. Run as a user with sudo access:
#
#   ./install.sh
#
set -euo pipefail

APP_USER="${APP_USER:-trading}"
APP_DIR="${APP_DIR:-/opt/trading-system}"
DB_NAME="${DB_NAME:-trading}"
DB_USER="${DB_USER:-trader}"
DB_PASSWORD="${DB_PASSWORD:-secret}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

echo "==> Installing system packages"
sudo apt update
sudo apt install -y "$PYTHON_BIN" "${PYTHON_BIN}-venv" postgresql postgresql-contrib \
    redis-server build-essential git

echo "==> Creating service user '${APP_USER}'"
if ! id "$APP_USER" &>/dev/null; then
    sudo useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Setting up PostgreSQL role and database"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"

echo "==> Enabling Redis"
sudo systemctl enable --now redis-server

echo "==> Deploying application to ${APP_DIR}"
if [ ! -d "$APP_DIR" ]; then
    sudo mkdir -p "$APP_DIR"
fi
# Copy the current repo checkout (run this script from inside the repo root)
sudo rsync -a --exclude='.git' --exclude='venv' ./ "${APP_DIR}/"
sudo mkdir -p "${APP_DIR}"/{data,logs,backups,models}
sudo chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"

echo "==> Creating virtualenv and installing dependencies"
sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "${APP_DIR}/venv"
sudo -u "$APP_USER" "${APP_DIR}/venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> Configuring environment"
if [ ! -f "${APP_DIR}/.env" ]; then
    sudo -u "$APP_USER" cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    sudo -u "$APP_USER" sed -i \
        "s#^DATABASE_URL=.*#DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}#" \
        "${APP_DIR}/.env"
    echo "    Created ${APP_DIR}/.env — edit it to add API keys before starting."
fi

echo "==> Running database migrations"
sudo -u "$APP_USER" env -C "$APP_DIR" "${APP_DIR}/venv/bin/python" scripts/migrate_db.py

echo "==> Installing systemd unit"
sudo cp "${APP_DIR}/deployments/systemd/trading-system.service" /etc/systemd/system/
sudo systemctl daemon-reload

cat <<EOF

Install complete.

Next steps:
  1. Edit ${APP_DIR}/.env — set BINANCE_API_KEY/SECRET and TRADING_ENV
     (paper | testnet | production).
  2. Start the service:
       sudo systemctl enable --now trading-system
  3. Check status / logs:
       sudo systemctl status trading-system
       journalctl -u trading-system -f
EOF
