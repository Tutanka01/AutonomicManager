# Troubleshooting Guide

This guide covers the most common problems encountered when deploying and operating the MAPE-K Autonomic Manager on Proxmox VE, along with step-by-step diagnostic procedures and fixes.

---

## Table of Contents

1. [Bridge vmbr1 Not Found](#1-bridge-vmbr1-not-found)
2. [Template Download Fails](#2-template-download-fails)
3. [Container Creation Fails](#3-container-creation-fails)
4. [pvesh / pct Permission Errors](#4-pvesh--pct-permission-errors)
5. [Health Checks Always Failing](#5-health-checks-always-failing)
6. [iptables Rules Not Applied](#6-iptables-rules-not-applied)
7. [Port Forwarding Not Reachable From Outside](#7-port-forwarding-not-reachable-from-outside)
8. [CPU Metric Seems Wrong](#8-cpu-metric-seems-wrong)
9. [Knowledge Base Corruption or Desync](#9-knowledge-base-corruption-or-desync)
10. [Scaling Replicas Left Behind After Restart](#10-scaling-replicas-left-behind-after-restart)
11. [Quarantine Loop (Container Keeps Getting Quarantined)](#11-quarantine-loop)
12. [systemd Service Not Starting](#12-systemd-service-not-starting)
13. [Manager Loops Without Taking Any Action](#13-manager-loops-without-taking-any-action)
14. [Connection Count Always 0 or -1](#14-connection-count-always-0-or--1)

---

## 1. Bridge vmbr1 Not Found

### Symptoms
```
[ERROR] [proxmox] pvesh create /nodes/.../lxc ... failed: bridge 'vmbr1' not found
[ERROR] [executor] _execute_create_service: bridge vmbr1 does not exist
```

### Cause
The internal bridge `vmbr1` was not created on the Proxmox host, or it was created but not persisted across reboots.

### Diagnosis
```bash
ip link show vmbr1
# Should show: vmbr1: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
# If absent: no such device
```

### Fix
**Step 1 – Create the bridge permanently** by editing `/etc/network/interfaces`:
```
auto vmbr1
iface vmbr1 inet static
    address 192.168.100.1/24
    bridge-ports none
    bridge-stp off
    bridge-fd 0
```

**Step 2 – Apply without rebooting:**
```bash
ifup vmbr1
ip addr show vmbr1   # Verify 192.168.100.1/24 is assigned
```

**Step 3 – Enable IP forwarding (must survive reboots):**
```bash
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
sysctl -p
```

**Step 4 – Add NAT masquerade rule (if not already present):**
```bash
iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -o vmbr0 -j MASQUERADE
# Persist via iptables-save or your preferred tool
```

---

## 2. Template Download Fails

### Symptoms
```bash
pveam download local turnkey-wordpress-18.0-bullseye-amd64.tar.gz
# Error: can't open file ...  or  400 no storage with content 'vztmpl'
```

### Cause A – Storage `local` does not support VZ templates
```bash
pvesm status
# Check "content" column for 'local' — must include 'vztmpl'
```

**Fix:** Enable template storage in Proxmox GUI: `Datacenter > Storage > local > Edit > Content > check "VZDump backup file" and "CT Template"`, or via CLI:
```bash
pvesm set local --content vztmpl,backup,iso
```

### Cause B – Template name out of date
```bash
pveam update                    # Refresh the template catalogue
pveam available --section system | grep -i wordpress
# Note the exact filename shown
```
Then adjust `templates` section in `knowledge.yaml` to match the exact filename.

### Cause C – No internet connectivity on Proxmox host
```bash
curl -I https://releases.turnkeylinux.org/
# Should return 200 OK
```
If behind a proxy, set proxy in Proxmox (`/etc/apt/apt.conf.d/proxy.conf`) or configure the environment.

---

## 3. Container Creation Fails

### Symptoms
```
[ERROR] [proxmox] create_container: command exited with code 255
pvesh create /nodes/pve/lxc ... 500 storage 'local-lvm' is not available
```

### Cause A – Wrong storage name in knowledge.yaml
```bash
pvesm status | awk '{print $1}'
# Lists all storage IDs
```
Update `global.disk_storage` in `knowledge.yaml` to match (e.g., `local-lvm`, `local-zfs`, `nvme-lvm`).

### Cause B – VMID already taken
```bash
pct list | grep <vmid>
pvesh get /nodes/pve/lxc/<vmid>/status/current
```
If occupied, either free it or raise `global.next_vmid` in `knowledge.yaml` to skip above the conflict.

### Cause C – Template file missing or wrong path
```bash
ls /var/lib/vz/template/cache/
# Should contain turnkey-wordpress-*.tar.gz etc.
```
If missing, re-download:
```bash
pveam download local <template_filename>
```
Also verify `templates.<name>.source` in `knowledge.yaml` contains the exact filename (including extension).

### Cause D – Disk quota exceeded
```bash
df -h /var/lib/vz
pvesm status
```
Free space or reduce container disk sizes in `knowledge.yaml`.

---

## 4. pvesh / pct Permission Errors

### Symptoms
```
[ERROR] [proxmox] _run: permission denied
pvesh: Need to be root to connect to the privileged daemon!
```

### Cause
The manager must run as **root**. It directly calls `pvesh` and `pct`, which require root privileges.

### Fix
```bash
whoami          # Must return 'root'
sudo python3 main.py   # If not already root
# Or better: configure the systemd service with User=root (default)
```

Verify `pvesh` works:
```bash
pvesh get /nodes
# Should return a JSON list, no error
```

---

## 5. Health Checks Always Failing

### Symptoms
```
[WARNING] [monitor] VMID 101 health check failed: Connection refused
[WARNING] [monitor] VMID 101 health check failed: Timeout
```
Even though the container appears to be running.

### Cause A – Service not yet initialized after container start
LXC containers using TurnKey templates run init scripts on first boot (database setup, SSL generation, etc.) which can take **60–180 seconds**. The manager may attempt a health check too early.

**Fix:** Increase `thresholds.self_healing.max_consecutive_failures` to at least `3` or `5`. Also increase `global.monitor_interval` from `30` to `60` temporarily during initial deployment.

**Verification:**
```bash
pct exec 101 -- curl -s http://localhost:80   # Check from inside the container
```

### Cause B – Wrong port in knowledge.yaml
```yaml
templates:
  wordpress:
    health_check:
      type: http
      port: 80    # <-- Verify this matches the actual service port
      path: /
```
Execute a manual check:
```bash
# Get container IP
pct exec 101 -- ip addr show eth0 | grep inet

# Test from Proxmox host
curl -v http://192.168.100.10:80/
```

### Cause C – Firewall inside the container blocking the check
```bash
pct exec 101 -- iptables -L -n     # Check container-internal rules
pct exec 101 -- ss -tlnp           # Verify the service is listening
```

### Cause D – Wrong health check type
If the service expects TCP (not HTTP), update `knowledge.yaml`:
```yaml
health_check:
  type: tcp    # instead of http
  port: 3306
```

---

## 6. iptables Rules Not Applied

### Symptoms
```
[ERROR] [network] add_port_forwarding: iptables: No chain/target/match by that name
[WARNING] [network] rule_exists returned False but iptables -C exits 2
```

### Cause A – iptables-legacy vs iptables-nft conflict (Proxmox 8.x / Debian 12)
Proxmox 8 uses `nftables` by default. The `iptables` command may be a wrapper (`iptables-nft`) that is incompatible with some rule formats.

**Diagnosis:**
```bash
iptables --version
# iptables v1.8.9 (nf_tables)  ← nftables backend
# iptables v1.8.9 (legacy)     ← direct iptables
```

**Fix for nftables backend:**
```bash
update-alternatives --set iptables /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
```
Then restart the manager.

### Cause B – Required kernel modules not loaded
```bash
lsmod | grep -E "iptable_nat|nf_nat|xt_DNAT"
# If missing:
modprobe iptable_nat
modprobe nf_nat
modprobe xt_DNAT
```
Persist with:
```bash
echo -e "iptable_nat\nnf_nat\nxt_DNAT" >> /etc/modules
```

### Cause C – FORWARD chain policy DROP
```bash
iptables -L FORWARD -n | head -3
# Chain FORWARD (policy DROP)  ← Problem
```
**Fix:**
```bash
iptables -P FORWARD ACCEPT
# Or add a permissive rule for vmbr1 traffic:
iptables -A FORWARD -i vmbr1 -j ACCEPT
iptables -A FORWARD -o vmbr1 -j ACCEPT
```

---

## 7. Port Forwarding Not Reachable From Outside

### Symptoms
Port forwarding rules appear in `iptables -t nat -L -n` but external clients cannot connect.

### Diagnosis Checklist

**Step 1 – Verify the DNAT rule:**
```bash
iptables -t nat -L PREROUTING -n --line-numbers
# Look for: DNAT  tcp  --  0.0.0.0/0  0.0.0.0/0  tcp dpt:<port>  to:192.168.100.10:<port>
```

**Step 2 – Verify the FORWARD rule:**
```bash
iptables -L FORWARD -n | grep 192.168.100.10
# Must exist, not just the NAT rule
```

**Step 3 – Check Proxmox host firewall:**
The Proxmox **GUI firewall** (Datacenter > Firewall) may block incoming connections independently of iptables.
- Disable the Proxmox GUI firewall for the node, or
- Add an inbound rule allowing the forwarded port.

**Step 4 – Check external IP:**
```bash
ip addr show vmbr0 | grep inet
# Proxmox host external IP
curl http://<external_ip>:<forwarded_port>
```

**Step 5 – Test from host itself:**
```bash
curl http://127.0.0.1:<forwarded_port>   # Loopback test
```
If loopback fails but DNAT rule exists, add:
```bash
sysctl -w net.ipv4.conf.all.route_localnet=1
```

---

## 8. CPU Metric Seems Wrong

### Symptoms
CPU reads always 0, or reads values > 100%, or triggers HIGH_CPU_SUSTAINED spuriously.

### Explanation
Proxmox VE `pvesh get /nodes/<node>/lxc/<vmid>/status/current` returns a `cpu` field as a **ratio** (0.0 to 1.0 × number of cores). The manager converts it via:

```python
cpu_raw = status.get("cpu", 0)
cores   = max(1, status.get("cpus", 1))
cpu_pct = cpu_raw / cores * 100   # normalize to 0–100%
```

If `cpus` is returned as 0 (bug in Proxmox version), `max(1, ...)` prevents division by zero.

### Diagnosis
```bash
pvesh get /nodes/pve/lxc/101/status/current | python3 -c "
import sys, json
s = json.load(sys.stdin)
print('cpu raw:', s.get('cpu'))
print('cpus:   ', s.get('cpus'))
print('calc %: ', s.get('cpu', 0) / max(1, s.get('cpus', 1)) * 100)
"
```

### Fix
If Proxmox returns incorrect `cpus` value, override the cores count in the container:
```bash
pct config 101 | grep cores
pct set 101 --cores 2
```

---

## 9. Knowledge Base Corruption or Desync

### Symptoms
```
[ERROR] [knowledge] load_kb: YAML parse error on line 47
[ERROR] [knowledge] load_kb: key 'desired_state' not found
```
Or the manager takes unexpected actions because KB doesn't reflect reality.

### Cause
- Manager was killed mid-write (atomic write prevents partial YAML, but a previous non-atomic write could corrupt)
- Manual edit introduced a YAML syntax error
- A programming bug left `allocated` IPs for deleted containers

### Diagnosis
```bash
python3 -c "import yaml; yaml.safe_load(open('knowledge.yaml'))"
# ScannerError shown if syntax is invalid

# Compare running containers vs desired_state:
pct list
# vs. cat knowledge.yaml | grep -A3 "services:"
```

### Fix – Syntax Error
Edit `knowledge.yaml` carefully. Common YAML pitfalls:
- Indentation must be consistent (2 spaces used throughout)
- Strings with `:` inside must be quoted: `"http://example.com:80"`
- Lists use `- ` prefix

### Fix – Desync (IP / VMID leaks)
If `ip_pool.allocated` contains IPs for containers that no longer exist, clean them up:

```yaml
# In knowledge.yaml: remove stale entries from ip_pool.allocated
ip_pool:
  allocated:
    - 192.168.100.10   # Keep only IPs actually in use
```

Or reset the runtime_state completely (safe — it is rebuilt each cycle):
```yaml
runtime_state:
  scaling_replicas: {}
  restart_counters: {}
  last_restart_times: {}
  sustained_counters: {}
  quarantined: {}
  active_port_forwarding: []
```

---

## 10. Scaling Replicas Left Behind After Restart

### Symptoms
After the manager was stopped mid-scale-out, orphan containers remain running (e.g., VMID 150 with IP 192.168.100.50) but are no longer tracked in `knowledge.yaml`.

### Cause
The manager stopped between `executor._execute_scale_out` creating the container and updating `runtime_state.scaling_replicas`.

### Manual Cleanup
```bash
# List all running containers
pct list

# Identify orphans (VMIDs in scaling range 150–199 if default)
pct stop 150
pct destroy 150 --purge

# Remove orphan IP from allocated pool in knowledge.yaml
# Remove orphan iptables rules
iptables -t nat -L PREROUTING -n --line-numbers
iptables -t nat -D PREROUTING <line_number>   # Remove stale DNAT
iptables -D FORWARD <line_number>              # Remove stale FORWARD

# Clean knowledge.yaml runtime_state.scaling_replicas
# Remove the orphan entry if present
```

**Prevention:** The manager is designed to be restarted safely — it rebuilds awareness from Proxmox state each cycle. However, if `ip_pool.allocated` is not cleaned, the IP may be considered "in use" even for a deleted container. Check and clean as shown above.

---

## 11. Quarantine Loop

### Symptoms
The same container is quarantined, then a replacement is deployed, and then the replacement gets quarantined too — cycling indefinitely.

### Cause
`thresholds.self_protection.max_cpu_spike` is set too low relative to normal operating CPU usage. Every container appears to trigger spike detection.

### Diagnosis
```bash
# Watch CPU for 1 minute
watch -n 5 'pvesh get /nodes/pve/lxc/101/status/current | python3 -c "
import sys,json
s=json.load(sys.stdin)
print(round(s[\"cpu\"]/max(1,s[\"cpus\"])*100, 1), \"%\")"'
```

Compare the actual peak CPU with `max_cpu_spike` in `knowledge.yaml`.

### Fix
Raise the threshold to something more realistic:
```yaml
thresholds:
  self_protection:
    max_cpu_spike: 95.0       # Was e.g. 80.0 — too aggressive
    min_spike_duration: 120   # Require sustained spike for 2 minutes, not 30s
```

Also check `quarantine_duration` — if very short, the quarantined container is released quickly and may re-trigger before the root cause is fixed:
```yaml
    quarantine_duration: 600   # 10 minutes instead of 60s
```

---

## 12. systemd Service Not Starting

### Symptoms
```
$ systemctl status autonomic-manager
● autonomic-manager.service - MAPE-K Autonomic Manager
     Loaded: loaded (/etc/systemd/system/autonomic-manager.service)
     Active: failed (Result: exit-code)
```

### Diagnosis
```bash
journalctl -u autonomic-manager -n 50 --no-pager
# Look for: ModuleNotFoundError, FileNotFoundError, PermissionError
```

### Cause A – Wrong WorkingDirectory
```ini
# In .service file:
WorkingDirectory=/opt/autonomic-manager   # Must match actual path
```

**Fix:**
```bash
ls /opt/autonomic-manager/main.py       # Verify path
systemctl edit autonomic-manager        # Correct WorkingDirectory
```

### Cause B – Wrong Python path
```ini
ExecStart=/usr/bin/python3 main.py
```
```bash
which python3          # Verify path
python3 --version      # Must be 3.10+
```

### Cause C – Missing dependencies
```bash
pip3 show pyyaml requests
# If not installed:
pip3 install pyyaml requests
```

### Cause D – knowledge.yaml missing
```bash
ls /opt/autonomic-manager/knowledge.yaml
```
The file must exist before first start (it is not auto-generated).

### Full Reload Procedure
```bash
systemctl daemon-reload
systemctl restart autonomic-manager
systemctl status autonomic-manager
journalctl -fu autonomic-manager   # Follow live logs
```

---

## 13. Manager Loops Without Taking Any Action

### Symptoms
Logs show Monitor and Analyze completing each cycle but no events are emitted and no actions are executed, even though something is clearly wrong (container down, high CPU, etc.).

### Cause A – All containers unknown to the manager
The manager only monitors containers listed in `desired_state.services` and `runtime_state.scaling_replicas`. If a container is running but not in the Knowledge Base, it is invisible.

**Fix:** Add the container to `desired_state.services` in `knowledge.yaml`.

### Cause B – Thresholds too high
Check `thresholds` section. Example:
```yaml
thresholds:
  self_healing:
    max_consecutive_failures: 10   # Requires 10 failed checks before acting
```
Temporarily lower to `2` for testing.

### Cause C – Analyzer sustained_counters not incrementing
If a container is absent from monitoring output (returns no metrics), the counter doesn't increment. Check that `monitor.py` is actually reaching the container:
```bash
pct exec 101 -- echo "container reachable"
pvesh get /nodes/pve/lxc/101/status/current
```

### Cause D – Events emitted but planner skips them
If `handled_vmids` in the planner already processed that VMID in the same cycle (due to a higher-priority event consuming it), lower-priority events are skipped. This is expected behavior — check if a self-protection event is consuming the VMID first.

---

## 14. Connection Count Always 0 or -1

### Symptoms
```
[DEBUG] [monitor] VMID 101 connections: 0
```
Even with active client connections, the scale-out never triggers.

### Cause A – `ss` not installed in container
The monitor uses `pct exec <vmid> -- sh -c "ss -t state established | wc -l"` which requires the `iproute2` package.

**Fix:**
```bash
pct exec 101 -- which ss
# If missing:
pct exec 101 -- apt-get install -y iproute2
```

### Cause B – The `-1` offset
The command counts all lines including the header row:
```
State   Recv-Q  Send-Q  Local Address:Port  Peer Address:Port
ESTAB   0       0       192.168.100.10:80   <client>:12345
```
So the code subtracts 1: `max(0, count - 1)`. A value of `0` means no established connections (normal when idle).

### Cause C – `pct exec` timeout
If the container is under high load, `ss` may take longer than the command timeout. Check:
```bash
time pct exec 101 -- sh -c "ss -t state established | wc -l"
```

---

## Quick Diagnostic Checklist

Run this sequence when the manager is not behaving as expected:

```bash
# 1. Is the manager running?
systemctl status autonomic-manager   # or ps aux | grep main.py

# 2. Is the bridge up?
ip addr show vmbr1

# 3. Are containers running?
pct list

# 4. Is IP forwarding on?
cat /proc/sys/net/ipv4/ip_forward   # Must be 1

# 5. Are iptables working?
iptables -t nat -L PREROUTING -n
iptables -L FORWARD -n

# 6. Is knowledge.yaml valid YAML?
python3 -c "import yaml; print('OK:', yaml.safe_load(open('knowledge.yaml')).keys())"

# 7. Check last 50 log lines
tail -50 /var/log/autonomic-manager.log   # or journalctl -u autonomic-manager -n50

# 8. Manual MAPE-K dry run (single cycle, then exit)
cd /opt/autonomic-manager
timeout 60 python3 main.py   # Ctrl+C after first cycle completes
```

---

*For issues not covered here, consult the [architecture documentation](architecture.md) to understand which module handles a specific phase, then inspect the corresponding source file in `../` for implementation details.*
