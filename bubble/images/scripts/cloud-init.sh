#!/bin/bash
set -euo pipefail

# Ensure curl is available (usually present but not guaranteed)
apt-get update
apt-get install -y curl ca-certificates

# Install Incus via Zabbly repo
mkdir -p /etc/apt/keyrings/
curl -fsSL https://pkgs.zabbly.com/key.asc > /etc/apt/keyrings/zabbly.asc

CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
ARCH=$(dpkg --print-architecture)

cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources <<SOURCES
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: $CODENAME
Components: main
Architectures: $ARCH
Signed-By: /etc/apt/keyrings/zabbly.asc
SOURCES

apt-get update
apt-get install -y incus

# Initialize Incus
incus admin init --auto

# Install idle auto-shutdown
mkdir -p /var/lib/bubble

cat > /etc/bubble-idle.conf <<CONF
IDLE_TIMEOUT={idle_timeout}
CONF

cat > /usr/local/bin/bubble-idle-check <<'IDLESCRIPT'
#!/bin/bash
set -euo pipefail

CONF_FILE="/etc/bubble-idle.conf"
ACTIVITY_FILE="/var/lib/bubble/last-activity"
BOOT_GRACE=900  # 15 minutes after boot

# Read config
IDLE_TIMEOUT=900
if [ -f "$CONF_FILE" ]; then
    source "$CONF_FILE"
fi

NOW=$(date +%s)

# Grace period after boot
BOOT_TIME=$(date -d "$(uptime -s)" +%s 2>/dev/null || echo 0)
if [ $((NOW - BOOT_TIME)) -lt $BOOT_GRACE ]; then
    echo "$NOW" > "$ACTIVITY_FILE"
    exit 0
fi

ACTIVE=false

# Check for established SSH connections from outside
if ss -tnp state established dport = :22 2>/dev/null | grep -q .; then
    ACTIVE=true
fi

# Check normalized CPU load (load1 / nproc > 0.5 means busy)
if [ "$ACTIVE" = false ]; then
    LOAD1=$(awk '{print $1}' /proc/loadavg)
    NPROC=$(nproc)
    if awk "BEGIN{exit !(${LOAD1}/${NPROC} > 0.5)}"; then
        ACTIVE=true
    fi
fi

if [ "$ACTIVE" = true ]; then
    echo "$NOW" > "$ACTIVITY_FILE"
    exit 0
fi

# Check how long we've been idle
if [ -f "$ACTIVITY_FILE" ]; then
    LAST_ACTIVE=$(cat "$ACTIVITY_FILE")
else
    # First check — start the idle timer now
    echo "$NOW" > "$ACTIVITY_FILE"
    exit 0
fi

IDLE_SECONDS=$((NOW - LAST_ACTIVE))
if [ "$IDLE_SECONDS" -ge "$IDLE_TIMEOUT" ]; then
    logger -t bubble-idle "Shutting down after ${IDLE_SECONDS}s idle"
    shutdown -h now
fi
IDLESCRIPT
chmod +x /usr/local/bin/bubble-idle-check

# Systemd timer for idle check
cat > /etc/systemd/system/bubble-idle.service <<'UNIT'
[Unit]
Description=Bubble idle shutdown check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/bubble-idle-check
UNIT

cat > /etc/systemd/system/bubble-idle.timer <<'TIMER'
[Unit]
Description=Bubble idle shutdown check timer

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now bubble-idle.timer

# Initialize activity timestamp
echo "$(date +%s)" > /var/lib/bubble/last-activity

# Mark readiness (after everything succeeds)
touch /var/run/bubble-cloud-ready
