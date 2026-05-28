#!/usr/bin/env bash
# Reproducer: incusd does not reap orphaned `forkproxy` helpers after an
# ungraceful daemon restart, leaking one per (OOM-kill + container-exit)
# cycle. Confirmed on incus 6.0.6 and 7.0.0.
#
# Mechanism: a proxy device's `forkproxy` helper is a child of incusd.
# If incusd is killed without its cgroup — exactly what the kernel OOM
# killer does, it kills the single incusd process, not the unit's whole
# control group — the forkproxy survives and reparents to PID 1. If the
# container then stops while incusd is down, the restarted incusd has no
# running instance to associate the orphan with and never kills it. The
# orphan lives forever (holding its FDs/RSS). On a busy host that
# OOM-kills incusd repeatedly while containers churn, these accumulate
# (observed: ~270/day, tens of GiB RSS).
#
# Run as root on a host with incus + a default bridge. Self-contained.
set -euo pipefail
ITERS="${1:-4}"
MARK="fpleak-$$"   # unique listen port base picks this up via /proc args

# Count live forkproxy helpers in our dedicated port range. We match on
# the helper's argv and confirm comm==incusd, so this never counts the
# reproducer's own shell (whose argv happens to contain the pattern).
count() {
    local n=0 p
    for p in $(pgrep -f "incusd forkproxy -- .*127.0.0.1:39[0-9][0-9][0-9]" 2>/dev/null); do
        [ "$(cat /proc/$p/comm 2>/dev/null)" = incusd ] && n=$((n+1))
    done
    echo "$n"
}

echo "incus: $(incus version | tr '\n' ' ')"
echo "kernel: $(uname -r)"
echo "start: forkproxy helpers in our port range = $(count)"
echo

for i in $(seq 1 "$ITERS"); do
    n="${MARK}-${i}"; port=$((39000 + i))
    incus launch images:ubuntu/24.04 "$n" >/dev/null 2>&1
    for _ in $(seq 1 30); do incus exec "$n" -- true 2>/dev/null && break; sleep 1; done
    incus config device add "$n" p proxy \
        listen="tcp:127.0.0.1:${port}" connect="tcp:127.0.0.1:${port}" bind=container >/dev/null
    sleep 1
    cpid=$(incus info "$n" | awk '/^PID:/{print $2}')

    # (1) OOM-style: SIGKILL only the main incusd process (not the cgroup,
    #     so the forkproxy child survives and reparents to PID 1).
    kill -9 "$(systemctl show -p MainPID --value incus)" 2>/dev/null || true
    sleep 1
    # (2) container exits while incusd is down (incusd never sees the stop).
    kill -9 "$cpid" 2>/dev/null || true
    # (3) bring incusd back.
    systemctl start incus 2>/dev/null || true
    for _ in $(seq 1 30); do incus info >/dev/null 2>&1 && break; sleep 1; done
    sleep 2
    echo "iter $i: container exited while incusd was down -> leaked forkproxy helpers = $(count)"
done

echo
echo "Leaked orphans (PPID=1, referencing now-dead container init PIDs):"
pgrep -f "bin/incusd forkproxy -- .*127.0.0.1:39[0-9][0-9][0-9]" \
  | while read -r p; do ps -o pid,ppid,stat,etimes,args --no-headers -p "$p"; done
echo
echo "Cleanup: kill leaked orphans (incusd won't) + delete any leftover containers."
pgrep -f "bin/incusd forkproxy -- .*127.0.0.1:39[0-9][0-9][0-9]" | xargs -r kill -9 2>/dev/null || true
incus list -c n --format csv 2>/dev/null | grep "^${MARK}-" | xargs -r -n1 incus delete -f 2>/dev/null || true
