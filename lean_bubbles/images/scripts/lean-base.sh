#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install system packages (including iptables for network allowlisting)
apt-get update -qq
apt-get install -y -qq \
    git curl build-essential openssh-server \
    ca-certificates netcat-openbsd iptables < /dev/null

# Create lean user (no sudo, no password)
useradd -m -s /bin/bash lean
passwd -l lean

# Configure SSH (key-based auth only)
mkdir -p /run/sshd
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config

# Enable SSH to start on boot
systemctl enable ssh 2>/dev/null || true

# Install elan as lean user
su - lean -c 'curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | bash -s -- -y --default-toolchain none'

# Add elan to PATH for all sessions
echo 'export PATH="$HOME/.elan/bin:$PATH"' >> /home/lean/.bashrc
echo 'export PATH="/home/lean/.elan/bin:$PATH"' >> /etc/profile.d/elan.sh

# Create mount points for shared volumes
mkdir -p /shared/git /shared/lake-cache
chown lean:lean /shared/git /shared/lake-cache

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "lean-base setup complete."
