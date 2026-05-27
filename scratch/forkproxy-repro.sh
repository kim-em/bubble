#!/usr/bin/env bash
# Repro for incus forkproxy leak on container stop/start.
#
# Background: incusd spawns a `forkproxy` helper process per proxy device.
# On a normal container stop, the helper is expected to be killed (see
# `internal/server/device/proxy.go` `Stop()` -> `killProxyProc()`). In
# practice on incus 6.0.6 LTS (NixOS build 2026-03-27), some stop/start
# paths leave the helper running and reparented to PID 1, while the next
# `incus start` spawns a fresh helper. Helpers accumulate linearly with
# stop/start cycles.
#
# This script demonstrates the leak. Run as a user in the `incus-admin`
# group on a Linux host with a default incus install. It uses a unique
# container name including a UUID so its forkproxy processes can be
# filtered out of the global ps output, and prints per-iteration
# snapshots so growth is visible.
#
# Usage: ./forkproxy-repro.sh [ITERATIONS]
#   Default ITERATIONS=5.
#
# Cleanup: always runs (trap on EXIT). Records before- and
# after-cleanup forkproxy state.

set -euo pipefail

ITERATIONS="${1:-5}"
UUID="$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)"
NAME="fproxy-repro-${UUID:0:8}"

# Unique listen addresses so we filter only our processes.
TCP_PORT=17$((RANDOM % 900 + 100))   # 17100-17999
SOCK_PATH="/tmp/${NAME}-gh.sock"

cleanup() {
    echo "=== cleanup ==="
    echo "--- forkproxy processes before cleanup ---"
    ps_match || true
    echo "--- incus delete ${NAME} ---"
    incus delete -f "${NAME}" 2>&1 || true
    sleep 1
    echo "--- forkproxy processes after cleanup ---"
    ps_match || true
}
trap cleanup EXIT

ps_match() {
    # Match incusd forkproxy processes whose argv references our
    # specific listen addresses. Print PID, PPID, elapsed seconds,
    # full argv.
    ps -eo pid,ppid,etimes,args \
      | grep -E "[i]ncusd forkproxy.*(${TCP_PORT}|${NAME}-gh\\.sock)" \
      | sort -k3 -n
}

count_match() {
    ps -eo args \
      | grep -cE "[i]ncusd forkproxy.*(${TCP_PORT}|${NAME}-gh\\.sock)" \
      || echo 0
}

echo "=== repro: ${NAME}  (TCP port ${TCP_PORT}, sock ${SOCK_PATH}) ==="
echo "incus version: $(incus version 2>/dev/null | head -1)"
echo "kernel: $(uname -r)"
echo "iterations: ${ITERATIONS}"

echo "=== launch container ==="
incus launch images:ubuntu/noble "${NAME}" 2>&1 | tail -5
# Wait for container to be ready
for _ in $(seq 1 30); do
    if incus exec "${NAME}" -- true 2>/dev/null; then break; fi
    sleep 1
done

echo "=== install persistent listener (python -m http.server on 9999) ==="
# Use python3 which is in the base image, served from /, port 9999.
incus exec "${NAME}" -- bash -c "
    apt-get update -qq >/dev/null 2>&1 || true
    nohup python3 -m http.server 9999 --bind 127.0.0.1 >/var/log/repro.log 2>&1 &
    disown
    sleep 1
"
# Verify listener is up
if ! incus exec "${NAME}" -- bash -c "exec 3<>/dev/tcp/127.0.0.1/9999 && echo ok" >/dev/null 2>&1; then
    echo "!! listener not reachable inside container; aborting" >&2
    exit 1
fi
echo "listener OK"

echo "=== add proxy devices ==="
incus config device add "${NAME}" repro-auth proxy \
    listen="tcp:127.0.0.1:${TCP_PORT}" \
    connect="tcp:127.0.0.1:9999" \
    bind=container 2>&1 | head -3
incus config device add "${NAME}" repro-gh proxy \
    listen="unix:${SOCK_PATH}" \
    connect="tcp:127.0.0.1:9999" \
    bind=container \
    uid=0 gid=0 mode=0660 2>&1 | head -3

echo "=== baseline (after device add) ==="
echo "forkproxy count: $(count_match)"
ps_match

# We expect 2 helpers at baseline (one per device).

for i in $(seq 1 "${ITERATIONS}"); do
    echo
    echo "=== iteration ${i}: stop + start ==="
    incus stop "${NAME}"
    incus start "${NAME}"
    # Re-spawn listener (gone with the stop).
    incus exec "${NAME}" -- bash -c "
        nohup python3 -m http.server 9999 --bind 127.0.0.1 >/var/log/repro.log 2>&1 &
        disown
        sleep 1
    " 2>/dev/null || true
    sleep 1
    echo "forkproxy count: $(count_match)"
    ps_match
done

echo
echo "=== summary ==="
echo "Expected on a correct incus: 2 helpers (one live per device)."
echo "Observed: $(count_match) helpers."
echo "Argv references container init PIDs that may or may not be alive."
echo "Per-iteration ps output above shows growth pattern."
