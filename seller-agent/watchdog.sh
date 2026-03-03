#!/usr/bin/env bash
# ACP Seller Agent — Watchdog
# Restarts seller on crash, monitors health, backoff on rapid failures.
# Usage: ./watchdog.sh | ./watchdog.sh --daemon (tmux)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELLER="$DIR/seller.py"
LOG_DIR="$DIR/logs"
PID_FILE="$DIR/.seller.pid"
HEALTH_FILE="$DIR/.seller.health"

# Tuning
MAX_RAPID=5                 # crashes in RAPID_WIN → backoff
RAPID_WIN=120               # seconds
DELAY=3                     # normal restart delay
BACKOFF=30                  # delay after rapid crashes
HEALTH_CHECK_INTERVAL=30    # seconds between health checks
HEALTH_STALE_SEC=600        # kill if no health update for 10 min
HEALTH_DC_LIMIT=8           # kill if socket disconnects exceed this
GRACE_PERIOD=60             # skip health checks for first N seconds after start

mkdir -p "$LOG_DIR"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S')] [watchdog] $*" | tee -a "$LOG_DIR/watchdog.log"; }

# --- Parse health file with pure bash (no python3 spawn) ---
# Extracts a numeric JSON field: get_health_field "ts" → 1771453583
get_health_field() {
    local field="$1"
    if [[ -f "$HEALTH_FILE" ]]; then
        # grep the key, extract the number after the colon
        grep -oP "\"${field}\"\\s*:\\s*\\K[0-9]+" "$HEALTH_FILE" 2>/dev/null || echo 0
    else
        echo 0
    fi
}

# Tmux daemon mode
if [[ "${1:-}" == "--daemon" ]]; then
    S="seller-agent"
    if tmux has-session -t "$S" 2>/dev/null; then
        log "Session '$S' exists — attaching"
        exec tmux attach -t "$S"
    fi
    log "Starting tmux session '$S'"
    exec tmux new-session -d -s "$S" "$DIR/watchdog.sh" \; attach -t "$S"
fi

cleanup() {
    log "Stopping"
    [[ -f "$PID_FILE" ]] && {
        local p; p="$(cat "$PID_FILE")"
        kill -0 "$p" 2>/dev/null && kill "$p" 2>/dev/null; sleep 1
        kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
        rm -f "$PID_FILE"
    }; true
    rm -f "$HEALTH_FILE"
    log "Stopped"
}
trap cleanup SIGINT SIGTERM EXIT

rotate() {
    local f="$LOG_DIR/seller.log"
    [[ -f "$f" ]] && [[ "$(stat -c%s "$f" 2>/dev/null || echo 0)" -gt 10485760 ]] && {
        mv "$f" "$f.$(date -u '+%Y%m%d_%H%M%S').bak"; log "Rotated seller.log"
    }; true
}

log "=== Watchdog started ==="
log "Seller: $SELLER"
crashes=(); run=0

while true; do
    rotate
    run=$((run+1))

    # CRITICAL: wipe stale health file BEFORE starting seller.
    # This prevents the kill-loop where watchdog reads old data from a
    # previous process and immediately kills the new one.
    rm -f "$HEALTH_FILE"

    log "Starting seller (run #$run)"

    python3 -u "$SELLER" >> "$LOG_DIR/seller.log" 2>&1 &
    PID=$!
    echo "$PID" > "$PID_FILE"
    start_ts=$(date +%s)
    log "Seller PID=$PID"

    while kill -0 "$PID" 2>/dev/null; do
        sleep "$HEALTH_CHECK_INTERVAL"

        now=$(date +%s)
        uptime=$(( now - start_ts ))

        # Grace period: let seller boot up before checking health.
        # During this window we only check if the process is alive (the
        # kill -0 in the while condition handles that).
        if (( uptime < GRACE_PERIOD )); then
            continue
        fi

        # No health file yet after grace period = seller isn't writing health
        if [[ ! -f "$HEALTH_FILE" ]]; then
            log "DEGRADED: no health file after ${uptime}s — force-killing PID=$PID"
            kill -9 "$PID" 2>/dev/null || true
            break
        fi

        # PID mismatch = health file from a different process (shouldn't
        # happen after the rm -f above, but guard against it anyway)
        h_pid=$(get_health_field "pid")
        if (( h_pid > 0 && h_pid != PID )); then
            log "WARN: health file PID=$h_pid != seller PID=$PID — ignoring stale file"
            rm -f "$HEALTH_FILE"
            continue
        fi

        # Stale timestamp = seller main loop is stuck
        h_ts=$(get_health_field "ts")
        age=$(( now - h_ts ))
        if (( h_ts > 0 && age > HEALTH_STALE_SEC )); then
            log "DEGRADED: health stale (${age}s, limit=${HEALTH_STALE_SEC}s) — force-killing PID=$PID"
            kill -9 "$PID" 2>/dev/null || true
            break
        fi

        # Socket flapping = too many disconnects
        h_dc=$(get_health_field "recent_disconnects")
        if (( h_dc >= HEALTH_DC_LIMIT )); then
            log "DEGRADED: socket flapping (${h_dc} disconnects, limit=${HEALTH_DC_LIMIT}) — force-killing PID=$PID"
            kill -9 "$PID" 2>/dev/null || true
            break
        fi
    done

    wait "$PID" || true
    exit_code=$?
    rm -f "$PID_FILE" "$HEALTH_FILE"
    log "Seller exited (code=$exit_code, run #$run)"

    # Track crash timestamps for rapid-crash detection
    now=$(date +%s)
    crashes+=("$now")
    fresh=()
    for c in "${crashes[@]}"; do
        (( now - c < RAPID_WIN )) && fresh+=("$c")
    done
    crashes=("${fresh[@]}")

    if (( ${#crashes[@]} >= MAX_RAPID )); then
        log "WARNING: ${#crashes[@]} crashes in ${RAPID_WIN}s — backing off ${BACKOFF}s"
        sleep "$BACKOFF"
        crashes=()
    else
        log "Restarting in ${DELAY}s..."
        sleep "$DELAY"
    fi
done
