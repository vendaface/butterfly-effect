#!/usr/bin/env bash
# Balance Forecast server management
# Usage: ./server.sh [start|stop|restart|status|logs]

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV=".venv-balance-forecast"
PID_FILE="$SCRIPT_DIR/.server.pid"
LOG_FILE="$SCRIPT_DIR/.server.log"
PORT=5002

_activate() {
  source "$VENV/bin/activate"
}

_is_running() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    rm -f "$PID_FILE"
  fi
  return 1
}

cmd_status() {
  if _is_running; then
    local pid
    pid=$(cat "$PID_FILE")
    echo "✓ Server is running (PID $pid) at http://localhost:$PORT"
  else
    echo "✗ Server is not running"
  fi
}

cmd_start() {
  if _is_running; then
    local pid
    pid=$(cat "$PID_FILE")
    echo "Server already running (PID $pid). Use './server.sh restart' to restart."
    return 0
  fi

  _activate
  pip install -q -r requirements.txt

  echo "Starting Balance Forecast..."
  nohup python server.py > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1

  if _is_running; then
    local pid
    pid=$(cat "$PID_FILE")
    echo "✓ Server started (PID $pid) at http://localhost:$PORT"
    open "http://localhost:$PORT" 2>/dev/null || true
  else
    echo "✗ Server failed to start. Check logs:"
    tail -20 "$LOG_FILE"
    exit 1
  fi
}

cmd_stop() {
  # Stop tracked PID if we have one
  if _is_running; then
    local pid
    pid=$(cat "$PID_FILE")
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✓ Server stopped (PID $pid)"
  fi
  # Also kill any orphaned process on the port (e.g. started via ./run.sh)
  local orphan
  orphan=$(lsof -ti ":$PORT" 2>/dev/null || true)
  if [ -n "$orphan" ]; then
    kill -9 $orphan 2>/dev/null || true
    echo "✓ Killed orphaned process on port $PORT (PID $orphan)"
  fi
  if [ -z "$(lsof -ti ":$PORT" 2>/dev/null)" ] && ! _is_running; then
    : # already printed above
  fi
}

cmd_restart() {
  cmd_stop 2>/dev/null || true
  sleep 1
  cmd_start
}

cmd_logs() {
  if [ ! -f "$LOG_FILE" ]; then
    echo "No log file found ($LOG_FILE). Has the server been started?"
    exit 1
  fi
  tail -f "$LOG_FILE"
}

CMD="${1:-status}"
case "$CMD" in
  start)   cmd_start   ;;
  stop)    cmd_stop    ;;
  restart) cmd_restart ;;
  status)  cmd_status  ;;
  logs)    cmd_logs    ;;
  *)
    echo "Usage: ./server.sh [start|stop|restart|status|logs]"
    exit 1
    ;;
esac
