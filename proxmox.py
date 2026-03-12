"""
proxmox.py — Wrapper for all Proxmox VE interactions via pvesh + pct CLI.

No other module may call subprocess for pvesh/pct commands directly.
Every function logs its command at DEBUG level and its result at INFO/ERROR.
Errors are always caught; functions return False/None instead of raising.
"""

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default subprocess timeout (seconds)
_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Cgroup-based CPU measurement (accurate delta sampling)
# ---------------------------------------------------------------------------
# Stores the last (monotonic_time, cpu_nanoseconds) sample per VMID so that
# successive calls can compute a real delta without any added sleep.
_cpu_samples: Dict[int, Tuple[float, int]] = {}


def _read_cgroup_cpu_ns(vmid: int) -> Optional[int]:
    """Read cumulative CPU usage in nanoseconds for *vmid* from cgroup files.

    Supports cgroup v2 (Proxmox 8 / Bookworm) and cgroup v1 (Proxmox 7).
    Returns nanoseconds of total CPU time consumed, or None if unavailable.
    """
    # cgroup v2 — /sys/fs/cgroup/lxc/<vmid>/cpu.stat → usage_usec
    v2_stat = f"/sys/fs/cgroup/lxc/{vmid}/cpu.stat"
    if os.path.exists(v2_stat):
        try:
            with open(v2_stat) as fh:
                for line in fh:
                    if line.startswith("usage_usec"):
                        return int(line.split()[1]) * 1_000  # µs → ns
        except (OSError, ValueError):
            pass

    # cgroup v1 — /sys/fs/cgroup/cpuacct/lxc/<vmid>/cpuacct.usage (already in ns)
    v1_usage = f"/sys/fs/cgroup/cpuacct/lxc/{vmid}/cpuacct.usage"
    if os.path.exists(v1_usage):
        try:
            with open(v1_usage) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            pass

    return None


def _cgroup_cpu_percent(vmid: int) -> Optional[float]:
    """Return CPU usage % since the last call by diffing cgroup cumulative counters.

    Returns None when cgroup data is unavailable (falls back to pvesh value).
    Returns 0.0 on the very first call for a given VMID (no previous sample).
    """
    now = time.monotonic()
    cpu_ns = _read_cgroup_cpu_ns(vmid)
    if cpu_ns is None:
        return None

    prev = _cpu_samples.get(vmid)
    _cpu_samples[vmid] = (now, cpu_ns)

    if prev is None:
        return 0.0  # first observation — no delta yet

    prev_time, prev_ns = prev
    elapsed_s = now - prev_time
    if elapsed_s < 0.1:
        return 0.0  # samples too close together — unreliable

    cpu_delta_ns = cpu_ns - prev_ns
    if cpu_delta_ns < 0:
        # Counter was reset (container restarted)
        return 0.0

    cpu_percent = (cpu_delta_ns / (elapsed_s * 1e9)) * 100.0
    return round(min(cpu_percent, 100.0), 1)


def _run(cmd: List[str], timeout: int = _TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    """Execute *cmd* via subprocess. Return CompletedProcess or None on exception."""
    logger.debug("Running: %s", " ".join(str(c) for c in cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds: %s", timeout, " ".join(str(c) for c in cmd))
        return None
    except Exception as exc:
        logger.error("Command failed with exception (%s): %s", exc, " ".join(str(c) for c in cmd))
        return None


def list_containers(node: str) -> List[Dict[str, Any]]:
    """Return list of container dicts (vmid, status, name …) from pvesh."""
    cmd = ["pvesh", "get", f"/nodes/{node}/lxc", "--output-format", "json"]
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error("list_containers failed (node=%s): %s", node,
                     result.stderr if result else "no output")
        return []
    try:
        data = json.loads(result.stdout)
        logger.debug("list_containers: %d container(s) found on node %s", len(data), node)
        return data
    except json.JSONDecodeError as exc:
        logger.error("list_containers: JSON parse error: %s", exc)
        return []


def get_container_status(node: str, vmid: int) -> Optional[Dict[str, Any]]:
    """Return container status dict with cpu_percent, mem_percent, status, uptime."""
    cmd = [
        "pvesh", "get", f"/nodes/{node}/lxc/{vmid}/status/current",
        "--output-format", "json",
    ]
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error("get_container_status failed (vmid=%s): %s", vmid,
                     result.stderr if result else "no output")
        return None
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("get_container_status: JSON parse error for vmid=%s: %s", vmid, exc)
        return None

    # Try cgroup-based CPU measurement first: it performs a delta between two
    # successive readings so it is accurate regardless of pvestatd caching.
    # Falls back to the pvesh 'cpu' field if cgroup files are not accessible.
    cgroup_cpu = _cgroup_cpu_percent(vmid)
    if cgroup_cpu is not None:
        cpu_percent: float = cgroup_cpu
    else:
        # pvesh fallback: cpu is a ratio in [0..1] relative to allocated cores
        cpu_raw: float = float(raw.get("cpu", 0.0))
        cores: int = int(raw.get("cpus", raw.get("maxcpu", 1)) or 1)
        cpu_percent = (cpu_raw / cores) * 100.0

    mem: int = int(raw.get("mem", 0))
    maxmem: int = int(raw.get("maxmem", 1) or 1)
    mem_percent: float = (mem / maxmem) * 100.0 if maxmem > 0 else 0.0

    status = raw.get("status", "unknown")
    uptime = int(raw.get("uptime", 0))

    logger.info(
        "CT %s: status=%s, CPU=%.1f%%, MEM=%.1f%%, uptime=%ds",
        vmid, status, cpu_percent, mem_percent, uptime,
    )
    return {
        "status": status,
        "cpu_percent": cpu_percent,
        "mem_percent": mem_percent,
        "uptime": uptime,
        "raw": raw,
    }


def create_container(
    node: str,
    vmid: int,
    template_conf: Dict[str, Any],
    hostname: str,
    ip: str,
    gateway: str,
    nameserver: str,
) -> bool:
    """Create an LXC container from a TurnKey template and start it.

    template_conf keys expected: file, memory, cores, disk
    """
    template_file: str = template_conf["file"]
    memory: int = int(template_conf["memory"])
    cores: int = int(template_conf["cores"])
    disk: str = template_conf["disk"]

    net_spec = f"name=eth0,bridge=vmbr1,ip={ip}/24,gw={gateway}"

    cmd = [
        "pct", "create", str(vmid), template_file,
        "--hostname", hostname,
        "--memory", str(memory),
        "--cores", str(cores),
        "--rootfs", disk,
        "--net0", net_spec,
        "--nameserver", nameserver,
        "--start", "1",
    ]

    logger.info("Creating container CT %s (hostname=%s, ip=%s, template=%s)", vmid, hostname, ip, template_file)
    result = _run(cmd, timeout=120)
    if result is None or result.returncode != 0:
        logger.error(
            "create_container failed (vmid=%s): %s",
            vmid,
            result.stderr.strip() if result else "no output",
        )
        return False
    logger.info("Container CT %s created and started successfully", vmid)
    return True


def start_container(node: str, vmid: int) -> bool:
    """Start a stopped container. Returns True on success."""
    cmd = ["pct", "start", str(vmid)]
    logger.info("Starting CT %s", vmid)
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error(
            "start_container failed (vmid=%s): %s",
            vmid,
            result.stderr.strip() if result else "no output",
        )
        return False
    logger.info("CT %s started", vmid)
    return True


def stop_container(node: str, vmid: int) -> bool:
    """Stop a running container (force). Returns True on success."""
    cmd = ["pct", "stop", str(vmid), "--force"]
    logger.info("Stopping CT %s (force)", vmid)
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error(
            "stop_container failed (vmid=%s): %s",
            vmid,
            result.stderr.strip() if result else "no output",
        )
        return False
    logger.info("CT %s stopped", vmid)
    return True


def destroy_container(node: str, vmid: int) -> bool:
    """Force-stop and purge-destroy a container. Returns True on success."""
    # Attempt to stop first; ignore errors (container may already be stopped)
    stop_cmd = ["pct", "stop", str(vmid), "--force"]
    logger.info("Stopping CT %s before destroy", vmid)
    _run(stop_cmd)
    time.sleep(2)

    cmd = ["pct", "destroy", str(vmid), "--purge"]
    logger.info("Destroying CT %s (purge)", vmid)
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error(
            "destroy_container failed (vmid=%s): %s",
            vmid,
            result.stderr.strip() if result else "no output",
        )
        return False
    logger.info("CT %s destroyed", vmid)
    return True


def set_container_network(node: str, vmid: int, ip: str, gateway: str) -> bool:
    """Change the network configuration of a container (used for quarantine)."""
    net_spec = f"name=eth0,bridge=vmbr1,ip={ip}/24,gw={gateway}"
    cmd = ["pct", "set", str(vmid), "--net0", net_spec]
    logger.info("Changing CT %s network to ip=%s", vmid, ip)
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error(
            "set_container_network failed (vmid=%s): %s",
            vmid,
            result.stderr.strip() if result else "no output",
        )
        return False
    logger.info("CT %s network reconfigured to %s", vmid, ip)
    return True


def exec_in_container(node: str, vmid: int, command: str) -> Optional[str]:
    """Execute *command* inside container *vmid* and return stdout."""
    cmd = ["pct", "exec", str(vmid), "--"] + command.split()
    logger.debug("exec_in_container CT %s: %s", vmid, command)
    result = _run(cmd)
    if result is None or result.returncode != 0:
        logger.error(
            "exec_in_container failed (vmid=%s, cmd=%s): %s",
            vmid,
            command,
            result.stderr.strip() if result else "no output",
        )
        return None
    return result.stdout


def container_exists(node: str, vmid: int) -> bool:
    """Return True if the given VMID exists on the node.

    Uses ``pct status`` as the primary check: it works locally without
    depending on a successful pvesh/API call and returns exit code 0 when
    the container exists (running *or* stopped) and non-zero when it does not.
    Falls back to list_containers only when pct itself is unavailable.
    """
    result = _run(["pct", "status", str(vmid)])
    if result is not None:
        return result.returncode == 0
    # pct unavailable — fall back to pvesh list
    logger.warning("container_exists: pct status unavailable, falling back to list_containers for CT %s", vmid)
    containers = list_containers(node)
    return any(int(ct.get("vmid", -1)) == int(vmid) for ct in containers)
