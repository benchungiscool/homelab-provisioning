#!/usr/bin/env bash
# Creates a Debian 12 cloud-init VM template on Proxmox.
# Run this on the Proxmox host as root.
set -euo pipefail

VMID=9000
VMNAME="debian12-cloudinit"
STORAGE="local-lvm"
BRIDGE="vmbr1"
IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
IMAGE_FILE="/tmp/debian-12-genericcloud-amd64.qcow2"
SNIPPETS_DIR="/var/lib/vz/snippets"

# Enable snippets on local storage if not already enabled
if ! pvesm status -storage local | grep -q snippets 2>/dev/null; then
  echo "Enabling snippets on local storage..."
  pvesm set local --content iso,vztmpl,backup,snippets
fi

mkdir -p "$SNIPPETS_DIR"

# Copy cloud-init user-data (assumes this script lives in scripts/ inside the repo)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/../cloud-init/user-data.yml" "$SNIPPETS_DIR/bootstrap-user-data.yml"
echo "Cloud-init user-data copied to $SNIPPETS_DIR/bootstrap-user-data.yml"

# Download cloud image if not already present
if [ ! -f "$IMAGE_FILE" ]; then
  echo "Downloading Debian 12 cloud image..."
  wget -q --show-progress -O "$IMAGE_FILE" "$IMAGE_URL"
fi

# Remove existing VM with this ID if present
if qm status "$VMID" &>/dev/null; then
  echo "Removing existing VM $VMID..."
  qm destroy "$VMID" --purge
fi

echo "Creating VM $VMID ($VMNAME)..."
qm create "$VMID" \
  --name "$VMNAME" \
  --memory 2048 \
  --cores 2 \
  --net0 "virtio,bridge=${BRIDGE}" \
  --ostype l26 \
  --cpu x86-64-v2-AES \
  --scsihw virtio-scsi-single \
  --agent enabled=1

echo "Importing disk..."
qm importdisk "$VMID" "$IMAGE_FILE" "$STORAGE"
qm set "$VMID" \
  --scsi0 "${STORAGE}:vm-${VMID}-disk-0,iothread=1,discard=on" \
  --ide2 "${STORAGE}:cloudinit" \
  --boot "order=scsi0" \
  --serial0 socket \
  --vga serial0

echo "Configuring cloud-init..."
qm set "$VMID" \
  --cicustom "user=local:snippets/bootstrap-user-data.yml" \
  --citype nocloud \
  --nameserver 192.168.100.2 \
  --searchdomain home

echo "Converting to template..."
qm template "$VMID"

echo "Done. Template $VMID ($VMNAME) is ready."
echo "Clone it with: qm clone $VMID <new-vmid> --name <hostname> --full"
echo "Then set a static IP: qm set <new-vmid> --ipconfig0 ip=192.168.100.X/24,gw=192.168.100.1"
