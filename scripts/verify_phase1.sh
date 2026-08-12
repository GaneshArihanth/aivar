#!/usr/bin/env bash
# Phase 1 acceptance: agent creation, key security, and CRUD behaviour.
# Asserts against a running proxy on :8000.
set -uo pipefail
cd "$(dirname "$0")/.."

API=http://127.0.0.1:8000
PSQL=/opt/homebrew/opt/postgresql@17/bin/psql
PY=.venv/bin/python
pass=0; fail=0

check() { # description, condition
  if [[ "$2" == "true" ]]; then echo "  ✓ $1"; pass=$((pass+1));
  else echo "  ✗ $1"; fail=$((fail+1)); fi
}

jqp() { $PY -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }

echo "── Creating an agent via the API the modal will call"
TEAM_ID=$(curl -s $API/admin/teams | jqp "d[0]['id']")
RESP=$(curl -s -w '\n%{http_code}' -X POST $API/admin/agents \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"phase1-probe\",\"team_id\":$TEAM_ID,\"monthly_budget_usd\":25.50,
       \"session_budget_usd\":1.25,\"preferred_model\":\"gpt-4o\",\"allow_substitution\":true}")
CODE=$(tail -1 <<<"$RESP"); BODY=$(sed '$d' <<<"$RESP")
check "POST /admin/agents returns 201 (got $CODE)" "$([[ $CODE == 201 ]] && echo true || echo false)"

RAW_KEY=$(jqp "d['api_key']" <<<"$BODY")
AGENT_ID=$(jqp "d['agent']['id']" <<<"$BODY")
check "response carries a raw key with the sk-agent- prefix" \
  "$([[ $RAW_KEY == sk-agent-* ]] && echo true || echo false)"
check "budgets round-trip (monthly \$25.50 / session \$1.25)" \
  "$([[ $(jqp "d['agent']['monthly_budget_usd']" <<<"$BODY") == 25.5 && \
        $(jqp "d['agent']['session_budget_usd']" <<<"$BODY") == 1.25 ]] && echo true || echo false)"
check "fallback chain derived: $(jqp "d['agent']['fallback_chain']" <<<"$BODY")" \
  "$([[ $(jqp "'|'.join(d['agent']['fallback_chain'])" <<<"$BODY") == "gpt-4o|gpt-4o-mini|gpt-4.1-nano" ]] && echo true || echo false)"

echo
echo "── Key security"
IN_DB=$($PSQL -d budget_controller -tAc \
  "SELECT count(*) FROM agents WHERE key_hash = '$RAW_KEY' OR key_prefix = '$RAW_KEY';")
check "raw key is NOT stored in PostgreSQL" \
  "$([[ $IN_DB == 0 ]] && echo true || echo false)"

HASH_LEN=$($PSQL -d budget_controller -tAc \
  "SELECT length(key_hash) FROM agents WHERE id = $AGENT_ID;")
check "stored value is a 64-char SHA-256 hex digest (got $HASH_LEN)" \
  "$([[ $HASH_LEN == 64 ]] && echo true || echo false)"

GET_BODY=$(curl -s $API/admin/agents/$AGENT_ID)
check "GET /admin/agents/{id} never returns the key" \
  "$(grep -q "$RAW_KEY" <<<"$GET_BODY" && echo false || echo true)"
check "GET exposes only the non-secret prefix ($(jqp "d['key_prefix']" <<<"$GET_BODY"))" \
  "$([[ $(jqp "d['key_prefix']" <<<"$GET_BODY") == sk-agent-* ]] && echo true || echo false)"

# grep -c both prints 0 and exits 1 when there are no matches, so test the
# exit status directly rather than the count.
if grep -q "$RAW_KEY" .data/logs/proxy.log 2>/dev/null; then LOG_HIT=true; else LOG_HIT=false; fi
check "raw key never appears in the server log" \
  "$([[ $LOG_HIT == false ]] && echo true || echo false)"

echo
echo "── Validation and conflicts"
DUP=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/admin/agents \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"phase1-probe\",\"team_id\":$TEAM_ID,\"monthly_budget_usd\":10,
       \"session_budget_usd\":1,\"preferred_model\":\"gpt-4o\"}")
check "duplicate name on the same team → 409 (got $DUP)" \
  "$([[ $DUP == 409 ]] && echo true || echo false)"

BADMODEL=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/admin/agents \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"phase1-badmodel\",\"team_id\":$TEAM_ID,\"monthly_budget_usd\":10,
       \"session_budget_usd\":1,\"preferred_model\":\"gpt-9-imaginary\"}")
check "unknown model → 422 (got $BADMODEL)" \
  "$([[ $BADMODEL == 422 ]] && echo true || echo false)"

SESSBIG=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/admin/agents \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"phase1-sessbig\",\"team_id\":$TEAM_ID,\"monthly_budget_usd\":5,
       \"session_budget_usd\":50,\"preferred_model\":\"gpt-4o\"}")
check "session budget above monthly → 422 (got $SESSBIG)" \
  "$([[ $SESSBIG == 422 ]] && echo true || echo false)"

BADTEAM=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/admin/agents \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"phase1-badteam\",\"team_id\":99999,\"monthly_budget_usd\":10,
       \"session_budget_usd\":1,\"preferred_model\":\"gpt-4o\"}")
check "unknown team → 404 (got $BADTEAM)" \
  "$([[ $BADTEAM == 404 ]] && echo true || echo false)"

echo
echo "── Key rotation and revocation"
ROT=$(curl -s -X POST $API/admin/agents/$AGENT_ID/rotate-key)
NEW_KEY=$(jqp "d['api_key']" <<<"$ROT")
check "rotate-key issues a different key" \
  "$([[ -n $NEW_KEY && $NEW_KEY != "$RAW_KEY" ]] && echo true || echo false)"
OLD_STILL_THERE=$($PSQL -d budget_controller -tAc \
  "SELECT count(*) FROM agents WHERE id=$AGENT_ID AND key_prefix='${RAW_KEY:0:15}';")
check "old key's hash is replaced, not kept alongside" \
  "$([[ $OLD_STILL_THERE == 0 ]] && echo true || echo false)"

echo
echo "── Update and soft delete"
UPD=$(curl -s -X PATCH $API/admin/agents/$AGENT_ID \
  -H 'Content-Type: application/json' -d '{"monthly_budget_usd": 99.99, "status": "paused"}')
check "PATCH updates budget and status" \
  "$([[ $(jqp "d['monthly_budget_usd']" <<<"$UPD") == 99.99 && \
        $(jqp "d['status']" <<<"$UPD") == paused ]] && echo true || echo false)"

DEL=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE $API/admin/agents/$AGENT_ID)
check "DELETE returns 204 (got $DEL)" "$([[ $DEL == 204 ]] && echo true || echo false)"
LEDGER_SAFE=$($PSQL -d budget_controller -tAc \
  "SELECT count(*) FROM agents WHERE id=$AGENT_ID AND deleted_at IS NOT NULL;")
check "delete is soft — the row survives for audit" \
  "$([[ $LEDGER_SAFE == 1 ]] && echo true || echo false)"
GONE=$(curl -s -o /dev/null -w '%{http_code}' $API/admin/agents/$AGENT_ID)
check "deleted agent is no longer readable → 404 (got $GONE)" \
  "$([[ $GONE == 404 ]] && echo true || echo false)"

REUSE=$(curl -s -o /dev/null -w '%{http_code}' -X POST $API/admin/agents \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"phase1-probe\",\"team_id\":$TEAM_ID,\"monthly_budget_usd\":10,
       \"session_budget_usd\":1,\"preferred_model\":\"gpt-4o\"}")
check "deleted agent's name is free for reuse → 201 (got $REUSE)" \
  "$([[ $REUSE == 201 ]] && echo true || echo false)"

# Clean up the probe agents so the seeded fleet stays tidy.
for id in $(curl -s $API/admin/agents | $PY -c \
  "import json,sys; print(' '.join(str(a['id']) for a in json.load(sys.stdin) if a['name'].startswith('phase1-')))"); do
  curl -s -o /dev/null -X DELETE $API/admin/agents/$id
done

echo
echo "── $pass passed, $fail failed"
exit $(( fail > 0 ))
