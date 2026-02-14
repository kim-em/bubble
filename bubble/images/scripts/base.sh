#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install system packages
apt-get update -qq
apt-get install -y -qq \
    git curl build-essential openssh-server \
    ca-certificates netcat-openbsd iptables < /dev/null

# Create user (no sudo, no password)
useradd -m -s /bin/bash user
passwd -l user

# Configure SSH (key-based auth only)
mkdir -p /run/sshd
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config

# Enable SSH to start on boot
systemctl enable ssh 2>/dev/null || true

# Create mount points for shared volumes
mkdir -p /shared/git
chown user:user /shared/git

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "bubble-base setup complete."
