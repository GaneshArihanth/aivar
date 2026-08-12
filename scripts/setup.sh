#!/usr/bin/env bash
# Bootstrap the Agent Budget Controller on macOS.
set -euo pipefail

cd "$(dirname "$0")/.."

# Python 3.14 framework build, chosen over whatever `python3` resolves to so the
# project venv is not coupled to an unrelated conda/pyenv environment.
PYTHON="${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3}"
if [[ ! -x "$PYTHON" ]]; then
  echo "==> $PYTHON not found; falling back to python3 ($(python3 --version))"
  PYTHON="$(command -v python3)"
fi

echo "==> Creating venv with $($PYTHON --version)"
[[ -d .venv ]] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements-dev.txt --quiet
echo "==> Dependencies installed"

if command -v brew >/dev/null 2>&1; then
  for formula in redis postgresql@17; do
    if ! brew list --formula 2>/dev/null | grep -qx "$formula"; then
      echo "==> Installing $formula"
      brew install "$formula"
    fi
  done
else
  echo "!! Homebrew not found. Either install Redis/PostgreSQL yourself, or run"
  echo "   the whole system with DEV_MODE=embedded (fakeredis + SQLite)."
fi

[[ -f .env ]] || {
  cp .env.example .env
  PEPPER="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  # macOS/BSD sed needs the empty -i argument.
  sed -i '' "s/^API_KEY_PEPPER=.*/API_KEY_PEPPER=${PEPPER}/" .env
  echo "==> Wrote .env with a generated API_KEY_PEPPER"
}

echo "==> Next: make infra-up && make db-create && make migrate && make seed"
