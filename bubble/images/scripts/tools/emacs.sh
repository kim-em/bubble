#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq emacs-nox < /dev/null

# Clean up
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Emacs setup complete."
