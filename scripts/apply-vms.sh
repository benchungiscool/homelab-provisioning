#!/usr/bin/env bash
# Syncs config/vms.yml and reconcile-vms.py to the Proxmox host and runs reconciliation.
# Run this locally from anywhere in the repo.
set -euo pipefail

PROXMOX_HOST="root@192.168.100.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_TMP="/tmp/homelab-reconcile"

ssh "$PROXMOX_HOST" "mkdir -p $REMOTE_TMP"
scp "$SCRIPT_DIR/reconcile-vms.py" "$SCRIPT_DIR/../config/vms.yml" "$PROXMOX_HOST:$REMOTE_TMP/"
ssh "$PROXMOX_HOST" "python3 $REMOTE_TMP/reconcile-vms.py $REMOTE_TMP/vms.yml"
