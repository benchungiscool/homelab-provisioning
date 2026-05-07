#!/usr/bin/env bash
# Clones the Debian 12 cloud-init template and assigns the next available
# static IP in the 192.168.100.0/24 subnet.
# Run this on the Proxmox host as root.
#
# Usage: clone-vm.sh <hostname> [new-vmid]
# Example: clone-vm.sh myserver
#          clone-vm.sh myserver 115
set -euo pipefail

TEMPLATE_VMID=9000
STORAGE="local-lvm"
SUBNET_PREFIX="192.168.100"
GATEWAY="${SUBNET_PREFIX}.1"
NAMESERVER="${SUBNET_PREFIX}.2"
SEARCHDOMAIN="home"
# IPs to never assign: gateway, nameserver, and any reserved addresses
RESERVED=(1 2)

usage() {
  echo "Usage: $0 <hostname> [new-vmid]"
  echo "  hostname  — name for the new VM"
  echo "  new-vmid  — optional; auto-selected if omitted"
  exit 1
}

[[ $# -lt 1 ]] && usage

HOSTNAME="$1"

# Auto-select next free VMID (100-899) if not provided
if [[ $# -ge 2 ]]; then
  NEW_VMID="$2"
else
  used_vmids=()
  while IFS= read -r f; do
    used_vmids+=("$(basename "$f" .conf)")
  done < <(find /etc/pve/nodes -name '*.conf' 2>/dev/null)
  NEW_VMID=""
  for i in $(seq 100 899); do
    if ! printf '%s\n' "${used_vmids[@]}" | grep -qx "$i"; then
      NEW_VMID="$i"
      break
    fi
  done
  if [[ -z "$NEW_VMID" ]]; then
    echo "ERROR: No free VMIDs available in range 100-899" >&2
    exit 1
  fi
fi

# Collect all IPs already assigned via Proxmox ipconfig0
used_ips=()
while IFS= read -r line; do
  ip=$(echo "$line" | grep -oP "${SUBNET_PREFIX//./\\.}\.\K[0-9]+(?=/)" || true)
  [[ -n "$ip" ]] && used_ips+=("$ip")
done < <(grep -r 'ipconfig0' /etc/pve/nodes/*/qemu-server/*.conf 2>/dev/null | grep -oP 'ip=[^,]+' || true)

# Add reserved IPs
used_ips+=("${RESERVED[@]}")

# Find the next free IP above .6 (below .50 to stay out of DHCP range .50-.150)
next_ip=""
for i in $(seq 3 49); do
  if ! printf '%s\n' "${used_ips[@]}" | grep -qx "$i"; then
    next_ip="${SUBNET_PREFIX}.${i}"
    break
  fi
done

# Fall back to above the DHCP range if low range is full
if [[ -z "$next_ip" ]]; then
  for i in $(seq 151 254); do
    if ! printf '%s\n' "${used_ips[@]}" | grep -qx "$i"; then
      next_ip="${SUBNET_PREFIX}.${i}"
      break
    fi
  done
fi

if [[ -z "$next_ip" ]]; then
  echo "ERROR: No free IPs available in ${SUBNET_PREFIX}.0/24" >&2
  exit 1
fi

echo "Cloning template $TEMPLATE_VMID -> VM $NEW_VMID ($HOSTNAME)"
echo "Assigning IP: $next_ip/24 (gw: $GATEWAY)"

qm clone "$TEMPLATE_VMID" "$NEW_VMID" --name "$HOSTNAME" --full --storage "$STORAGE"

qm set "$NEW_VMID" \
  --ipconfig0 "ip=${next_ip}/24,gw=${GATEWAY}" \
  --nameserver "$NAMESERVER" \
  --searchdomain "$SEARCHDOMAIN"

echo ""
echo "Done. Start the VM with: qm start $NEW_VMID"
echo "Then run Ansible: ansible-playbook -i inventory/hosts.yml bootstrap_vm.yml -e ansible_host=${next_ip}"
