#!/usr/bin/env bash
# Start/stop the local web client (agent/token_server.py: the page in web/ + POST /api/token).
# The agent itself runs on GCP; this only lets a browser on this machine join a call.
#
#   scripts/web-client.sh start [safari|chrome]   start in the background (and open a browser)
#   scripts/web-client.sh stop
#   scripts/web-client.sh status
#   scripts/web-client.sh logs                    follow the server log
#   scripts/web-client.sh open [safari|chrome]    open the page (default: Safari)
#
# PORT (default 8080) picks the port. Docs: docs/local-web-client.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8080}"
URL="http://localhost:${PORT}"
STATE="$ROOT/.cache/web-client"
PID_FILE="$STATE/server.pid"
LOG_FILE="$STATE/server.log"

running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
  else
    rm -f "$PID_FILE" # stale: the process is gone
    return 1
  fi
}

open_browser() {
  case "${1:-safari}" in
    safari) open -a Safari "$URL" ;;
    chrome) open -a "Google Chrome" "$URL" ;;
    *) echo "unknown browser '$1' (use safari or chrome)" >&2; exit 2 ;;
  esac
}

start() {
  if pid="$(running_pid)"; then
    echo "already running (pid $pid) at $URL"
  else
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "port $PORT is taken by another process; try PORT=8081 $0 start" >&2
      exit 1
    fi
    if [[ ! -f "$ROOT/agent/.env.local" ]]; then
      echo "agent/.env.local is missing (copy agent/.env.example and fill in the LiveKit keys)" >&2
      exit 1
    fi
    mkdir -p "$STATE"
    (cd "$ROOT/agent" && PORT="$PORT" PYTHONUNBUFFERED=1 nohup uv run python token_server.py \
      >"$LOG_FILE" 2>&1 & echo $! >"$PID_FILE")
    # The first start can take a while on a slow machine (Python imports).
    for _ in $(seq 1 60); do
      if curl -fsS -o /dev/null "$URL/" 2>/dev/null; then break; fi
      if ! running_pid >/dev/null; then
        echo "server exited; last log lines:" >&2
        tail -20 "$LOG_FILE" >&2
        exit 1
      fi
      sleep 1
    done
    curl -fsS -o /dev/null "$URL/" || { echo "server didn't answer within 60 s; see $LOG_FILE" >&2; exit 1; }
    echo "running (pid $(cat "$PID_FILE")) at $URL"
  fi
  if [[ $# -gt 0 ]]; then open_browser "$1"; fi
}

stop() {
  if pid="$(running_pid)"; then
    # `uv run` starts python as a child; stop both.
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    rm -f "$PID_FILE"
    echo "stopped"
  else
    echo "not running"
  fi
}

status() {
  if pid="$(running_pid)"; then
    echo "running (pid $pid) at $URL"
  else
    echo "not running"
  fi
}

case "${1:-}" in
  start) shift; start "$@" ;;
  stop) stop ;;
  restart) stop; shift; start "$@" ;;
  status) status ;;
  logs) tail -f "$LOG_FILE" ;;
  open) shift; open_browser "${1:-safari}" ;;
  *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
