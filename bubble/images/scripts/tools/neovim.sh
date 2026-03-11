#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq neovim < /dev/null

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Neovim setup complete."
