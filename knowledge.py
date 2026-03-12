"""
knowledge.py — Knowledge Base (KB) loading, saving, and helper functions.

Manages the YAML-backed state that persists across MAPE-K cycles.
Write operations are atomic: data is written to a temporary file then
renamed over the live file to prevent corruption.
"""

import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_kb(path: str) -> Dict[str, Any]:
    """Load the knowledge base YAML file and return as a dict.

    Ensures all expected runtime sub-keys exist with sensible defaults.
    """
    logger.debug("Loading knowledge base from %s", path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            kb: Dict[str, Any] = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        logger.error("Knowledge base file not found: %s", path)
        raise
    except yaml.YAMLError as exc:
        logger.error("YAML parse error in knowledge base: %s", exc)
        raise

    # Ensure runtime sub-keys are present and are the expected types
    rt = kb.setdefault("runtime_state", {})
    rt.setdefault("scaling_replicas", {})
    rt.setdefault("restart_counters", {})
    rt.setdefault("quarantined", {})
    rt.setdefault("sustained_counters", {})
    rt.setdefault("last_restart_times", {})

    # Ensure optional top-level lists/dicts
    kb.setdefault("active_port_forwarding", [])
    ip_pool = kb.setdefault("ip_pool", {})
    ip_pool.setdefault("allocated", {})

    # Pre-seed the allocated pool with static IPs declared in desired_state so
    # that the scaling/quarantine allocators never hand out a service IP.
    allocated: Dict[str, Any] = ip_pool["allocated"]
    for svc in kb.get("desired_state", {}).get("services", []):
        static_ip: Optional[str] = svc.get("ip")
        if static_ip and static_ip not in allocated:
            allocated[static_ip] = {
                "vmid": svc.get("vmid"),
                "service": svc.get("name", ""),
                "static": True,
            }
            logger.debug("Pre-seeded static IP %s for service '%s'", static_ip, svc.get("name", ""))

    logger.debug("Knowledge base loaded successfully")
    return kb


def save_kb(kb: Dict[str, Any], path: str) -> None:
    """Atomically save the knowledge base to *path*.

    Writes to a temporary file in the same directory first, then renames
    it over the target file so the operation is crash-safe.
    """
    dir_name = os.path.dirname(os.path.abspath(path))
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.dump(kb, fh, default_flow_style=False, allow_unicode=True)
            os.replace(tmp_path, path)
            logger.debug("Knowledge base saved to %s", path)
        except Exception:
            # Clean up temp file if rename failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.error("Failed to save knowledge base: %s", exc)


# ---------------------------------------------------------------------------
# IP Pool helpers
# ---------------------------------------------------------------------------

def _pool_range(kb: Dict[str, Any], pool: str) -> Dict[str, int]:
    """Return the {start, end} dict for the given pool name."""
    return kb["ip_pool"][f"{pool}_range"]


def allocate_ip(kb: Dict[str, Any], pool: str = "scaling") -> Optional[str]:
    """Return the next available IP in *pool* range and mark it allocated.

    *pool* is one of 'service', 'scaling', 'quarantine'.
    Returns None if the pool is exhausted.
    """
    pool_range = _pool_range(kb, pool)
    start: int = pool_range["start"]
    end: int = pool_range["end"]
    allocated: Dict[str, Any] = kb["ip_pool"]["allocated"]

    for last_octet in range(start, end + 1):
        ip = f"192.168.100.{last_octet}"
        if ip not in allocated:
            allocated[ip] = {"pool": pool}
            logger.debug("Allocated IP %s from pool '%s'", ip, pool)
            return ip

    logger.warning("IP pool '%s' exhausted (range .%d-.%d)", pool, start, end)
    return None


def release_ip(kb: Dict[str, Any], ip: str) -> None:
    """Remove *ip* from the allocated pool, making it available again."""
    allocated: Dict[str, Any] = kb["ip_pool"]["allocated"]
    if ip in allocated:
        del allocated[ip]
        logger.debug("Released IP %s back to pool", ip)
    else:
        logger.debug("release_ip: %s was not in allocated pool, nothing to release", ip)


def allocate_vmid(kb: Dict[str, Any]) -> int:
    """Return the next available VMID and increment the counter in the KB."""
    vmid: int = int(kb["global"]["next_vmid"])
    kb["global"]["next_vmid"] = vmid + 1
    logger.debug("Allocated VMID %d (next will be %d)", vmid, vmid + 1)
    return vmid


def allocate_port(kb: Dict[str, Any]) -> int:
    """Return the next available host port and increment the counter in the KB."""
    port: int = int(kb["port_forwarding_next_port"])
    kb["port_forwarding_next_port"] = port + 1
    logger.debug("Allocated host port %d", port)
    return port


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def find_service_by_vmid(kb: Dict[str, Any], vmid: int) -> Optional[Dict[str, Any]]:
    """Search desired_state.services and runtime_state.scaling_replicas for *vmid*.

    Returns the service/replica dict or None if not found.
    """
    vmid_int = int(vmid)

    # Search in desired services
    for svc in kb.get("desired_state", {}).get("services", []):
        if int(svc.get("vmid", -1)) == vmid_int:
            return svc

    # Search in scaling replicas
    replicas: Dict[str, Any] = kb["runtime_state"]["scaling_replicas"]
    for key, replica in replicas.items():
        if int(replica.get("vmid", -1)) == vmid_int:
            return replica

    return None


def get_template_config(kb: Dict[str, Any], template_name: str) -> Dict[str, Any]:
    """Return the template configuration dict for *template_name*.

    Raises KeyError if the template is not defined.
    """
    templates: Dict[str, Any] = kb.get("templates", {})
    if template_name not in templates:
        raise KeyError(f"Template '{template_name}' not found in knowledge base")
    return templates[template_name]


def get_replicas_for_parent(kb: Dict[str, Any], parent_vmid: int) -> List[Dict[str, Any]]:
    """Return all scaling-replica dicts whose parent_vmid matches *parent_vmid*."""
    replicas: Dict[str, Any] = kb["runtime_state"]["scaling_replicas"]
    result = []
    for key, replica in replicas.items():
        if int(replica.get("parent_vmid", -1)) == int(parent_vmid):
            result.append(replica)
    return result
