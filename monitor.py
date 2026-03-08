"""
monitor.py — MAPE-K Monitor phase.

Polls every known container (desired services + scaling replicas), collects
CPU/RAM/status metrics, performs health checks (HTTP or TCP), and counts
established network connections inside each container.

Returns an observed-state dict keyed by integer VMID.
"""

import logging
import socket
from typing import Any, Dict, Optional

import requests

import proxmox
from knowledge import get_replicas_for_parent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health check helpers
# ---------------------------------------------------------------------------

def _http_health_check(ip: str, hc: Dict[str, Any], timeout: int) -> bool:
    """Return True if an HTTP health check against *ip* succeeds."""
    port = int(hc.get("port", 80))
    path = hc.get("path", "/")
    expected_status = int(hc.get("expected_status", 200))
    url = f"http://{ip}:{port}{path}"
    try:
        resp = requests.get(url, timeout=timeout)
        return resp.status_code == expected_status
    except Exception as exc:
        logger.debug("HTTP health check failed for %s: %s", url, exc)
        return False


def _tcp_health_check(ip: str, hc: Dict[str, Any], timeout: int) -> bool:
    """Return True if a TCP connection to *ip*:port succeeds."""
    port = int(hc.get("port", 80))
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception as exc:
        logger.debug("TCP health check failed for %s:%d: %s", ip, port, exc)
        return False


def _run_health_check(ip: str, hc: Optional[Dict[str, Any]], timeout: int) -> bool:
    """Dispatch to the correct health check implementation.

    Returns True (alive) when no health-check config is available.
    """
    if not hc:
        return True
    check_type = hc.get("type", "tcp").lower()
    if check_type == "http":
        return _http_health_check(ip, hc, timeout)
    return _tcp_health_check(ip, hc, timeout)


def _get_connection_count(node: str, vmid: int) -> int:
    """Return the number of established TCP connections inside a container.

    Uses: pct exec {vmid} -- ss -t state established | wc -l
    The first line of 'ss' output is a header, so we subtract 1.
    """
    try:
        # Build the pipeline as a shell command executed by pct exec
        output = proxmox.exec_in_container(
            node, vmid, "sh -c ss -t state established | wc -l"
        )
        if output is None:
            return 0
        count = int(output.strip())
        # Subtract 1 for the header line produced by ss
        return max(0, count - 1)
    except (ValueError, TypeError) as exc:
        logger.debug("Could not parse connection count for CT %s: %s", vmid, exc)
        return 0


# ---------------------------------------------------------------------------
# Main monitor function
# ---------------------------------------------------------------------------

def monitor(kb: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Collect runtime state for all known containers.

    Returns a dict keyed by VMID:
    {
        vmid: {
            "exists": bool,
            "status": str,           # "running" | "stopped" | "missing" | "unknown"
            "cpu_percent": float,
            "mem_percent": float,
            "service_alive": bool,
            "connections": int,
            "is_replica": bool,
            "parent_vmid": int | None,
        }
    }
    """
    node: str = kb["global"]["node_name"]
    hc_timeout: int = int(kb["thresholds"]["self_healing"]["health_check_timeout"])

    observed: Dict[int, Dict[str, Any]] = {}

    # Build list of (vmid, ip, template_name, is_replica, parent_vmid)
    targets = []

    for svc in kb.get("desired_state", {}).get("services", []):
        vmid = int(svc["vmid"])
        ip = svc["ip"]
        template_name = svc.get("template", "")
        targets.append({
            "vmid": vmid,
            "ip": ip,
            "template_name": template_name,
            "is_replica": False,
            "parent_vmid": None,
        })

    replicas: Dict[str, Any] = kb["runtime_state"].get("scaling_replicas", {})
    for key, replica in replicas.items():
        vmid = int(replica["vmid"])
        ip = replica["ip"]
        template_name = replica.get("template", "")
        parent_vmid = int(replica.get("parent_vmid", -1))
        targets.append({
            "vmid": vmid,
            "ip": ip,
            "template_name": template_name,
            "is_replica": True,
            "parent_vmid": parent_vmid,
        })

    logger.info("%d container(s) to monitor", len(targets))

    for target in targets:
        vmid: int = target["vmid"]
        ip: str = target["ip"]
        template_name: str = target["template_name"]
        is_replica: bool = target["is_replica"]
        parent_vmid: Optional[int] = target["parent_vmid"]

        # Default result (container not found)
        entry: Dict[str, Any] = {
            "exists": False,
            "status": "missing",
            "cpu_percent": 0.0,
            "mem_percent": 0.0,
            "service_alive": False,
            "connections": 0,
            "is_replica": is_replica,
            "parent_vmid": parent_vmid,
        }

        # Check existence
        try:
            exists = proxmox.container_exists(node, vmid)
        except Exception as exc:
            logger.error("container_exists error for CT %s: %s", vmid, exc)
            exists = False

        if not exists:
            logger.warning("CT %s does not exist on node %s", vmid, node)
            observed[vmid] = entry
            continue

        entry["exists"] = True

        # Fetch status metrics
        try:
            status_data = proxmox.get_container_status(node, vmid)
        except Exception as exc:
            logger.error("get_container_status error for CT %s: %s", vmid, exc)
            status_data = None

        if status_data is None:
            entry["status"] = "unknown"
            observed[vmid] = entry
            continue

        entry["status"] = status_data.get("status", "unknown")
        entry["cpu_percent"] = status_data.get("cpu_percent", 0.0)
        entry["mem_percent"] = status_data.get("mem_percent", 0.0)

        if entry["status"] != "running":
            logger.warning(
                "CT %s: status=%s, CPU=0%%, MEM=0%%",
                vmid, entry["status"],
            )
            observed[vmid] = entry
            continue

        # Resolve health check config from template
        hc_config: Optional[Dict[str, Any]] = None
        try:
            templates = kb.get("templates", {})
            if template_name and template_name in templates:
                hc_config = templates[template_name].get("health_check")
        except Exception as exc:
            logger.debug("Could not resolve health check config for CT %s: %s", vmid, exc)

        # Run health check
        try:
            alive = _run_health_check(ip, hc_config, hc_timeout)
        except Exception as exc:
            logger.error("Health check exception for CT %s: %s", vmid, exc)
            alive = False

        entry["service_alive"] = alive

        # Count connections
        try:
            connections = _get_connection_count(node, vmid)
        except Exception as exc:
            logger.debug("Connection count error for CT %s: %s", vmid, exc)
            connections = 0

        entry["connections"] = connections

        logger.info(
            "CT %s: %s, CPU=%.1f%%, MEM=%.1f%%, service=%s, connections=%d%s",
            vmid,
            entry["status"],
            entry["cpu_percent"],
            entry["mem_percent"],
            "UP" if alive else "DOWN",
            connections,
            " [replica]" if is_replica else "",
        )

        observed[vmid] = entry

    return observed
