#!/usr/bin/env bash
# NetraAttend - set up / update the cloud server (Ubuntu on Oracle Cloud Always Free).
# Run on the server inside the unzipped NetraAttend folder:   bash deploy/cloud_install.sh
# First run creates .env from .env.example - fill it in (nano .env) and run again.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
fi

echo "==> Opening ports 80 and 443 (Oracle Ubuntu images block them in iptables)"
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || \
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
done
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent >/dev/null 2>&1 || true
sudo netfilter-persistent save >/dev/null 2>&1 || true

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env - fill it in now:   nano .env"
  echo "(DOMAIN, NETRA_MONGO_URI, NETRA_SECRET_KEY, NETRA_ADMIN_PASSWORD), then run this script again."
  exit 0
fi
if grep -q "change-me" .env; then
  echo "Fill in .env first (it still contains 'change-me'):   nano .env"
  exit 1
fi

echo "==> Building and starting NetraAttend"
sudo docker compose up -d --build
sudo docker compose ps
domain=$(grep '^DOMAIN=' .env | cut -d= -f2)
echo
echo "Done. In about a minute open:  https://${domain}"
echo "API docs:                      https://${domain}/api/docs"
echo "Logs:                          sudo docker compose logs -f app"
