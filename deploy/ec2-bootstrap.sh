#!/usr/bin/env bash
# ============================================================
#  One-time EC2 setup. Run on the instance:
#     curl -fsSL https://raw.githubusercontent.com/namanjain221995/Video-portal/main/deploy/ec2-bootstrap.sh | bash
#  ...or scp this file up and: bash ec2-bootstrap.sh
# ============================================================
set -e

echo "==> Installing Docker + git"
sudo apt-get update -y
sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

REPO=~/Video-portal
if [ ! -d "$REPO/.git" ]; then
  echo "==> Cloning repo"
  git clone https://github.com/namanjain221995/Video-portal.git "$REPO"
fi
cd "$REPO"

if [ ! -f .env ]; then
  echo "==> Seeding .env from .env.example (EDIT IT before going live)"
  cp .env.example .env
  chmod 600 .env
fi

echo ""
echo "============================================================"
echo " Next steps:"
echo "   1. nano $REPO/.env"
echo "        - SECRET_KEY   -> python3 -c 'import secrets;print(secrets.token_hex(32))'"
echo "        - ADMIN_USERS  -> your admin logins"
echo "        - DEMO_MODE=false"
echo "        - AWS_REGION=us-east-1"
echo "   2. Make sure the IAM role is attached + IMDS hop-limit=2 (see DEPLOY.md)."
echo "   3. Re-login (for the docker group):   exit, then SSH back in"
echo "   4. Launch:   cd $REPO && docker compose up -d --build"
echo "   5. Open the app port in the security group (8000, or 80 if you remap)."
echo "============================================================"
