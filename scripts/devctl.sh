#!/usr/bin/env bash
# Start/stop the proxy and mock provider with pidfiles, so a stale process
# never quietly holds a port while you wonder why your new route 404s.
set -uo pipefail
cd "$(dirname "$0")/.."

RUN_DIR=.data/run
LOG_DIR=.data/logs
mkdir -p "$RUN_DIR" "$LOG_DIR"

start_one() {  # name, module, port
  local name="$1" module="$2" port="$3"
  local pidfile="$RUN_DIR/$name.pid"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return 0
  fi
  if lsof -ti:"$port" >/dev/null 2>&1; then
    echo "!! port $port is held by pid $(lsof -ti:"$port" | tr '\n' ' ')— run '$0 stop' first"
    return 1
  fi

  .venv/bin/uvicorn "$module" --port "$port" --log-level warning \
    > "$LOG_DIR/$name.log" 2>&1 &
  echo $! > "$pidfile"
  echo "$name started on :$port (pid $(cat "$pidfile"))"
}

stop_one() {
  local name="$1" port="$2"
  local pidfile="$RUN_DIR/$name.pid"
  if [[ -f "$pidfile" ]]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
  fi
  # Belt and braces: whatever still holds the port is ours to clear.
  local held
  held=$(lsof -ti:"$port" 2>/dev/null || true)
  [[ -n "$held" ]] && kill $held 2>/dev/null || true
  echo "$name stopped"
}

wait_healthy() {
  for _ in {1..40}; do
    curl -sf "http://127.0.0.1:8000/health" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  echo "!! proxy did not become healthy; see $LOG_DIR/proxy.log"
  tail -20 "$LOG_DIR/proxy.log"
  return 1
}

case "${1:-}" in
  start)
    start_one mock mock_llm.main:app 9000
    start_one proxy app.main:app 8000
    wait_healthy && echo "ready: http://127.0.0.1:8000/dashboard"
    ;;
  stop)
    stop_one proxy 8000
    stop_one mock 9000
    ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  status)
    for p in 8000 9000; do
      pid=$(lsof -ti:$p 2>/dev/null || echo "-")
      echo "port $p: $pid"
    done
    ;;
  logs) tail -n "${2:-40}" "$LOG_DIR"/*.log ;;
  *) echo "usage: $0 {start|stop|restart|status|logs [n]}"; exit 1 ;;
esac
