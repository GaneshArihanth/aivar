#!/usr/bin/env bash
# End-to-end demo: brings up infrastructure, seeds a fleet, and walks the
# dashboard through every enforcement path.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
API=http://127.0.0.1:8000

step() { printf "\n\033[36m▸ %s\033[0m\n" "$1"; }

step "Starting Redis and PostgreSQL"
make -s infra-up

step "Applying migrations"
make -s migrate >/dev/null && echo "  schema up to date"

step "Seeding four products, twelve agents"
if [[ "$(${PY} -c "
import asyncio,sys
sys.path.insert(0,'.')
from sqlalchemy import select, func
from app.db.session import init_engine, session_scope, dispose_engine
from app.db.models import Agent
async def m():
    init_engine()
    async with session_scope() as s:
        print((await s.execute(select(func.count()).select_from(Agent).where(Agent.deleted_at.is_(None)))).scalar_one())
    await dispose_engine()
asyncio.run(m())
" 2>/dev/null)" == "0" ]]; then
  make -s seed
else
  echo "  fleet already seeded (use 'make db-drop db-create migrate seed' to reset)"
fi

step "Starting the mock provider and the budget proxy"
./scripts/devctl.sh start

step "Opening the dashboard"
echo "  $API/dashboard"
command -v open >/dev/null && open "$API/dashboard" || true
sleep 2

step "Scenario 1 — steady traffic across three agents"
$PY -m loadgen.main --scenario burst --agents 3 --calls 40 --concurrency 25 2>&1 | tail -11

step "Scenario 2 — one session spending past its cap"
$PY -m loadgen.main --scenario session --calls 20 2>&1 | tail -10

step "Scenario 3 — an agent in a recursive loop (runaway detector)"
$PY -m loadgen.main --scenario runaway --calls 30 2>&1 | tail -11

step "Scenario 4 — the full fleet, with one greedy agent"
$PY -m loadgen.main --scenario mixed --calls 10 2>&1 | tail -10

step "Reconciliation — Redis counters vs the PostgreSQL ledger"
$PY -m scripts.reconcile 2>&1 | tail -20

cat <<'EOF'

▸ Now try it yourself in the dashboard:
    · "+ New Agent"      — create one; the key is shown exactly once
    · row "⋯" menu       — edit budgets, rotate the key, pause, delete
    · a red "Unblock"    — appears on any agent the runaway detector paused

  Stop everything with:  ./scripts/devctl.sh stop
EOF
