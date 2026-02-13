#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Install system packages
apt-get update -qq
apt-get install -y -qq \
    git curl build-essential openssh-server \
    ca-certificates sudo netcat-openbsd < /dev/null

# Create lean user
useradd -m -s /bin/bash lean
echo "lean ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/lean

# Configure SSH
mkdir -p /run/sshd
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config
# Set a known password for initial SSH access (user should add keys)
echo "lean:lean" | chpasswd

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
