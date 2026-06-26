#!/usr/bin/env bash
# Quick local start. Usage: ./run_local.sh
set -e

if [ ! -d "venv" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

echo "==> Installing dependencies"
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo "==> No .env found — copying .env.example (DEMO_MODE=true)"
  cp .env.example .env
fi

echo "==> Starting on http://localhost:${PORT:-8000}"
python app.py
