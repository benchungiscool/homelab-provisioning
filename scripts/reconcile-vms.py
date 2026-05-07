#!/usr/bin/env python3
"""
Reconcile Proxmox VMs against a declarative YAML config.
Run on the Proxmox host as root (via apply-vms.sh).

Usage: python3 reconcile-vms.py <path-to-vms.yml>
"""

import subprocess
import sys
import os
import re

try:
    import yaml
except ImportError:
    subprocess.run(["apt-get", "install", "-y", "-q", "python3-yaml"], check=True)
    import yaml

DRY_RUN = False

MANAGED_TAG = "homelab-managed"
TEMPLATE_VMID = 9000
VMID_MIN, VMID_MAX = 100, 899
SUBNET = "192.168.100"
GATEWAY = "192.168.100.1"
NAMESERVER = "192.168.100.2"
SEARCHDOMAIN = "home"
IP_RANGES = list(range(3, 50)) + list(range(151, 255))


def run(cmd, check=True, mutating=False):
    if DRY_RUN and mutating:
        print(f"  [dry-run] {cmd}")
        return ""
    result = subprocess.run(cmd, shell=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode != 0:
        print(f"Error running: {cmd}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def get_all_vmids(running_only=False):
    """Returns dict of vmid (int) -> name (str) for all VMs."""
    out = run("qm list")
    vms = {}
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split()
        if parts:
            status = parts[2] if len(parts) > 2 else "unknown"
            if running_only and status != "running":
                continue
            vms[int(parts[0])] = parts[1]
    return vms


def get_all_conf_vmids():
    """Returns set of all VMIDs that exist as .conf files across all nodes (including stale)."""
    out = run("find /etc/pve/nodes -name '*.conf' 2>/dev/null || true", check=False)
    vmids = set()
    for path in out.splitlines():
        basename = os.path.basename(path).replace(".conf", "")
        if basename.isdigit():
            vmids.add(int(basename))
    return vmids


def get_vm_config(vmid):
    """Parse qm config output into a dict."""
    out = run(f"qm config {vmid}", check=False)
    cfg = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            cfg[key.strip()] = val.strip()
    return cfg


def get_managed_vms(running_only=False):
    """Returns dict of vmid (int) -> (name, config) for VMs tagged as managed."""
    managed = {}
    for vmid, name in get_all_vmids(running_only=running_only).items():
        cfg = get_vm_config(vmid)
        if MANAGED_TAG in cfg.get("tags", ""):
            managed[vmid] = (name, cfg)
    return managed


def next_free_vmid():
    existing = get_all_conf_vmids()
    for vmid in range(VMID_MIN, VMID_MAX + 1):
        if vmid not in existing:
            return vmid
    raise RuntimeError("No free VMIDs in range 100-899")


def next_free_ip():
    used = set()
    out = run(
        "grep -rh 'ipconfig0' /etc/pve/nodes/*/qemu-server/*.conf 2>/dev/null || true",
        check=False,
    )
    for line in out.splitlines():
        m = re.search(rf"ip={re.escape(SUBNET)}\.(\d+)", line)
        if m:
            used.add(int(m.group(1)))
    for i in IP_RANGES:
        if i not in used:
            return f"{SUBNET}.{i}"
    raise RuntimeError("No free IPs available")


def create_vm(name, spec):
    vmid = next_free_vmid()
    ip = next_free_ip()
    cores = spec.get("cores", 2)
    memory = spec.get("memory", 2048)
    disk = spec.get("disk")

    print(f"  {'[dry-run] ' if DRY_RUN else ''}Creating VM {vmid} at {ip}...")
    run(f"qm clone {TEMPLATE_VMID} {vmid} --name {name} --full", mutating=True)
    run(
        f"qm set {vmid}"
        f" --cores {cores} --memory {memory}"
        f" --ipconfig0 ip={ip}/24,gw={GATEWAY}"
        f" --nameserver {NAMESERVER} --searchdomain {SEARCHDOMAIN}"
        f" --tags {MANAGED_TAG}",
        mutating=True,
    )
    if disk:
        run(f"qm resize {vmid} scsi0 {disk}", mutating=True)
    run(f"qm start {vmid}", mutating=True)
    if not DRY_RUN:
        print(f"  Started (add 'vmid: {vmid}' to vms.yml to enable renames)")


def update_vm(name, vmid, current_cfg, spec):
    updates = []
    qm_args = []

    current_name = current_cfg.get("name", "")
    if current_name != name:
        updates.append(f"name {current_name!r} -> {name!r}")
        qm_args.append(f"--name {name}")

    desired_cores = str(spec.get("cores", 2))
    if current_cfg.get("cores") != desired_cores:
        updates.append(f"cores {current_cfg.get('cores')} -> {desired_cores}")
        qm_args.append(f"--cores {desired_cores}")

    desired_mem = str(spec.get("memory", 2048))
    if current_cfg.get("memory") != desired_mem:
        updates.append(f"memory {current_cfg.get('memory')} -> {desired_mem}")
        qm_args.append(f"--memory {desired_mem}")

    if qm_args:
        run(f"qm set {vmid} {' '.join(qm_args)}", mutating=True)
        if any("cores" in u or "memory" in u for u in updates):
            updates.append("(takes effect on next reboot)")

    desired_disk = spec.get("disk")
    if desired_disk:
        scsi0 = current_cfg.get("scsi0", "")
        m = re.search(r"size=(\d+)G", scsi0)
        if m:
            current_size = int(m.group(1))
            desired_size = int(str(desired_disk).rstrip("G"))
            if desired_size > current_size:
                updates.append(f"disk {current_size}G -> {desired_size}G")
                run(f"qm resize {vmid} scsi0 {desired_disk}", mutating=True)
            elif desired_size < current_size:
                print(f"  Warning: cannot shrink disk ({current_size}G -> {desired_size}G), skipping")

    if updates:
        print(f"  Updated: {', '.join(updates)}")
    else:
        print(f"  Up to date")


def destroy_vm(vmid, name):
    print(f"  {'[dry-run] ' if DRY_RUN else ''}Destroying VM {vmid} ({name})...")
    status = run(f"qm status {vmid}", check=False)
    if "running" in status:
        run(f"qm stop {vmid}", mutating=True)
        run(f"qm wait {vmid}", mutating=True)
    run(f"qm destroy {vmid} --purge", mutating=True)


def list_untracked(config_path):
    import json
    tracked_names = set()
    tracked_vmids = set()
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            config = yaml.safe_load(f)
        for spec in (config.get("vms") or []):
            tracked_names.add(spec["name"])
            if "vmid" in spec:
                tracked_vmids.add(int(spec["vmid"]))

    result = []
    for vmid, name in get_all_vmids().items():
        if vmid == TEMPLATE_VMID:
            continue
        if name in tracked_names or vmid in tracked_vmids:
            continue
        cfg = get_vm_config(vmid)
        ipconfig = cfg.get("ipconfig0", "")
        m = re.search(r"ip=([\d.]+)/", ipconfig)
        scsi0 = cfg.get("scsi0", "")
        disk_m = re.search(r"size=(\d+G)", scsi0)
        result.append({
            "name": name,
            "vmid": vmid,
            "ip": m.group(1) if m else None,
            "cores": int(cfg.get("cores", 2)),
            "memory": int(cfg.get("memory", 2048)),
            "disk": disk_m.group(1) if disk_m else None,
        })
    print(json.dumps(result))


def list_managed(config_path):
    import json
    role_map = {}
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            config = yaml.safe_load(f)
        for spec in (config.get("vms") or []):
            role_map[spec["name"]] = spec.get("role")
    managed = get_managed_vms(running_only=True)
    result = []
    for vmid, (name, cfg) in managed.items():
        ipconfig = cfg.get("ipconfig0", "")
        m = re.search(r"ip=([\d.]+)/", ipconfig)
        result.append({"name": name, "vmid": vmid, "ip": m.group(1) if m else None, "role": role_map.get(name)})
    print(json.dumps(result))


def main():
    global DRY_RUN
    DRY_RUN = "--dry-run" in sys.argv

    config_path = next((a for a in sys.argv[1:] if not a.startswith("--")), os.path.join(
        os.path.dirname(__file__), "../vms.yml"
    ))

    if "--list-untracked" in sys.argv:
        list_untracked(config_path)
        return

    if "--list-managed" in sys.argv:
        list_managed(config_path)
        return

    if DRY_RUN:
        print("--- DRY RUN: no changes will be made ---\n")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    managed = get_managed_vms()  # vmid (int) -> (name, cfg)
    # Track which managed VMIDs are accounted for by the config
    accounted_vmids = set()

    for spec in (config.get("vms") or []):
        name = spec["name"]
        print(f"\n[{name}]")

        # Resolve which managed VM this entry refers to
        if "vmid" in spec:
            pinned_vmid = int(spec["vmid"])
            match = (pinned_vmid, managed[pinned_vmid][1]) if pinned_vmid in managed else None
            accounted_vmids.add(pinned_vmid)
        else:
            # Fall back to name matching
            match_entry = next(((vid, cfg) for vid, (n, cfg) in managed.items() if n == name), None)
            if match_entry:
                match = match_entry
                accounted_vmids.add(match_entry[0])
            else:
                match = None

        if spec.get("force_destroy", False):
            if match:
                vmid, _ = match
                destroy_vm(vmid, name)
            else:
                print(f"  Not found, nothing to destroy")
            continue

        if match:
            vmid, current_cfg = match
            update_vm(name, vmid, current_cfg, spec)
        else:
            create_vm(name, spec)

    for vmid, (name, _) in managed.items():
        if vmid not in accounted_vmids:
            print(f"\nWarning: VM {vmid} ({name}) is managed but not in vms.yml — add it back with force_destroy: true to remove it")


if __name__ == "__main__":
    main()
