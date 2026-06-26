#!/usr/bin/env bash
# ============================================================
#  Manual deploy (same thing the GitHub Action runs).
#  On the EC2 box:  bash deploy/server-deploy.sh
#
#  Rollback to a previous commit:
#     bash deploy/server-deploy.sh <commit-sha>
# ============================================================
set -e

REPO=~/Video-portal
cd "$REPO"

TARGET="${1:-origin/main}"

echo "==> Fetching"
git fetch --all --quiet

echo "==> Checking out $TARGET"
git reset --hard "$TARGET"

if [ ! -f .env ]; then
  echo "ERROR: .env missing — create it first (cp .env.example .env && nano .env)"
  exit 1
fi

echo "==> Rebuilding container"
docker compose up -d --build
docker image prune -f

echo "==> Status"
docker compose ps
echo "==> Tail logs with:  docker compose logs -f"
