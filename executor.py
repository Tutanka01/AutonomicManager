"""
executor.py — MAPE-K Execute phase.

Applies each planned action in priority order by calling proxmox.py and
network.py.  Keeps the Knowledge Base consistent after every operation.

Never raises exceptions — all errors are caught, logged, and execution
continues with the next action.
"""

import logging
import socket
import time
from typing import Any, Dict, List, Optional

import requests

import network
import proxmox
from knowledge import release_ip
from utils import now_ts

logger = logging.getLogger(__name__)

# Seconds to wait after container creation / restart before checking state
_BOOT_WAIT = 10


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def execute(actions: List[Dict[str, Any]], kb: Dict[str, Any]) -> None:
    """Execute each action in the provided list (already sorted by priority)."""
    _dispatch = {
        "DEPLOY_NEW": _execute_deploy_new,
        "RESTART": _execute_restart,
        "REDEPLOY": _execute_redeploy,
        "SCALE_OUT": _execute_scale_out,
        "SCALE_IN": _execute_scale_in,
        "QUARANTINE": _execute_quarantine,
        "CLEANUP_QUARANTINE": _execute_cleanup_quarantine,
    }

    for action in actions:
        action_name: str = action["action"]
        vmid: int = int(action.get("vmid", action.get("new_vmid", 0)))
        handler = _dispatch.get(action_name)
        if handler is None:
            logger.error("No executor handler for action '%s', skipping", action_name)
            continue
        logger.info("Executing %s for CT %s", action_name, vmid)
        try:
            handler(action, kb)
        except Exception as exc:
            logger.error("Executor error for action %s (CT %s): %s", action_name, vmid, exc)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _execute_deploy_new(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Configuration: create and start a new container."""
    node: str = kb["global"]["node_name"]
    gateway: str = kb["global"]["gateway"]
    nameserver: str = kb["global"]["nameserver"]
    ext_iface: str = kb["global"]["bridge_external"]

    vmid: int = int(action["vmid"])
    hostname: str = action["hostname"]
    ip: str = action["ip"]
    tmpl: Dict[str, Any] = action["template_conf"]
    pf: Optional[Dict[str, Any]] = action.get("port_forwarding")

    logger.info("DEPLOY_NEW: creating CT %d (%s) at %s", vmid, hostname, ip)

    success = proxmox.create_container(node, vmid, tmpl, hostname, ip, gateway, nameserver)
    if not success:
        logger.error("DEPLOY_NEW: container CT %d creation failed", vmid)
        return

    logger.info("DEPLOY_NEW: waiting %ds for CT %d to boot", _BOOT_WAIT, vmid)
    time.sleep(_BOOT_WAIT)

    # Configure port forwarding if requested
    if pf:
        host_port: int = int(pf["host_port"])
        container_port: int = int(pf["container_port"])
        protocol: str = pf.get("protocol", "tcp")
        pf_ok = network.add_port_forwarding(ext_iface, host_port, ip, container_port, protocol)
        if pf_ok:
            _register_port_forwarding(kb, host_port, ip, container_port, protocol, vmid)
            logger.info("DEPLOY_NEW: port forwarding :%d → %s:%d configured", host_port, ip, container_port)
        else:
            logger.error("DEPLOY_NEW: port forwarding setup failed for CT %d", vmid)

    # Mark IP as allocated
    kb["ip_pool"]["allocated"][ip] = {"vmid": vmid, "service": hostname}
    logger.info("DEPLOY_NEW: CT %d deployed successfully", vmid)


def _execute_restart(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Healing: start a stopped/unreachable container, respecting cooldown."""
    node: str = kb["global"]["node_name"]
    vmid: int = int(action["vmid"])
    max_attempts: int = int(action.get("max_attempts", 3))

    rt = kb["runtime_state"]
    counters: Dict[Any, Any] = rt.setdefault("restart_counters", {})
    last_times: Dict[Any, Any] = rt.setdefault("last_restart_times", {})

    cooldown: int = int(kb["thresholds"]["self_healing"]["restart_cooldown"])
    last_restart = float(last_times.get(str(vmid), last_times.get(vmid, 0.0)))

    if now_ts() - last_restart < cooldown:
        remaining = cooldown - (now_ts() - last_restart)
        logger.info("RESTART: CT %d in cooldown, %.0fs remaining — skipping", vmid, remaining)
        return

    current_attempt: int = int(counters.get(vmid, counters.get(str(vmid), 0)))
    logger.info("RESTART: starting CT %d (attempt %d/%d)", vmid, current_attempt + 1, max_attempts)

    success = proxmox.start_container(node, vmid)
    last_times[str(vmid)] = now_ts()
    counters[str(vmid)] = current_attempt + 1

    if not success:
        logger.error("RESTART: pct start failed for CT %d", vmid)
        return

    logger.info("RESTART: waiting %ds for CT %d to boot", _BOOT_WAIT, vmid)
    time.sleep(_BOOT_WAIT)

    # Verify the container is actually running now
    status_data = proxmox.get_container_status(node, vmid)
    if status_data and status_data.get("status") == "running":
        # Attempt a quick health check to decide whether to reset counter
        svc = _find_service_config(kb, vmid)
        if svc and _quick_health_check(svc, kb):
            counters[str(vmid)] = 0
            logger.info("RESTART: CT %d recovered and service responding — restart counter reset", vmid)
        else:
            logger.warning("RESTART: CT %d is running but service not yet healthy (attempt %d)", vmid, current_attempt + 1)
    else:
        logger.warning("RESTART: CT %d did not come up after start (attempt %d)", vmid, current_attempt + 1)


def _execute_redeploy(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Healing (advanced): destroy and recreate a container with the same identity."""
    node: str = kb["global"]["node_name"]
    gateway: str = kb["global"]["gateway"]
    nameserver: str = kb["global"]["nameserver"]
    ext_iface: str = kb["global"]["bridge_external"]

    vmid: int = int(action["vmid"])
    hostname: str = action["hostname"]
    ip: str = action["ip"]
    tmpl: Dict[str, Any] = action["template_conf"]
    pf: Optional[Dict[str, Any]] = action.get("port_forwarding")

    logger.info("REDEPLOY: destroying and recreating CT %d (%s) at %s", vmid, hostname, ip)

    # Remove port forwarding before destroying
    if pf:
        host_port = int(pf["host_port"])
        container_port = int(pf["container_port"])
        protocol = pf.get("protocol", "tcp")
        network.remove_port_forwarding(ext_iface, host_port, ip, container_port, protocol)
        _unregister_port_forwarding(kb, host_port, ip, container_port)

    ok_destroy = proxmox.destroy_container(node, vmid)
    if not ok_destroy:
        logger.error("REDEPLOY: could not destroy CT %d", vmid)
        return

    time.sleep(3)

    ok_create = proxmox.create_container(node, vmid, tmpl, hostname, ip, gateway, nameserver)
    if not ok_create:
        logger.error("REDEPLOY: could not recreate CT %d", vmid)
        return

    logger.info("REDEPLOY: waiting %ds for CT %d to boot", _BOOT_WAIT, vmid)
    time.sleep(_BOOT_WAIT)

    if pf:
        host_port = int(pf["host_port"])
        container_port = int(pf["container_port"])
        protocol = pf.get("protocol", "tcp")
        pf_ok = network.add_port_forwarding(ext_iface, host_port, ip, container_port, protocol)
        if pf_ok:
            _register_port_forwarding(kb, host_port, ip, container_port, protocol, vmid)

    # Reset restart counter
    rt = kb["runtime_state"]
    rt["restart_counters"][str(vmid)] = 0
    rt.setdefault("last_restart_times", {})[str(vmid)] = 0.0

    # Update IP pool
    kb["ip_pool"]["allocated"][ip] = {"vmid": vmid, "service": hostname}
    logger.info("REDEPLOY: CT %d redeployed successfully", vmid)


def _execute_scale_out(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Optimization: create a new replica container."""
    node: str = kb["global"]["node_name"]
    gateway: str = kb["global"]["gateway"]
    nameserver: str = kb["global"]["nameserver"]
    ext_iface: str = kb["global"]["bridge_external"]

    parent_vmid: int = int(action["parent_vmid"])
    new_vmid: int = int(action["new_vmid"])
    hostname: str = action["hostname"]
    ip: str = action["ip"]
    tmpl: Dict[str, Any] = action["template_conf"]
    new_host_port: int = int(action["new_host_port"])
    container_port: int = int(action["container_port"])
    protocol: str = action.get("protocol", "tcp")

    logger.info(
        "SCALE_OUT: deploying replica CT %d for service CT %d at %s",
        new_vmid, parent_vmid, ip,
    )

    ok = proxmox.create_container(node, new_vmid, tmpl, hostname, ip, gateway, nameserver)
    if not ok:
        logger.error("SCALE_OUT: container creation failed for CT %d — releasing resources", new_vmid)
        release_ip(kb, ip)
        return

    logger.info("SCALE_OUT: waiting %ds for CT %d to boot", _BOOT_WAIT, new_vmid)
    time.sleep(_BOOT_WAIT)

    pf_ok = network.add_port_forwarding(ext_iface, new_host_port, ip, container_port, protocol)
    if pf_ok:
        _register_port_forwarding(kb, new_host_port, ip, container_port, protocol, new_vmid)

    # Register replica in runtime_state
    replica_key = str(new_vmid)
    kb["runtime_state"]["scaling_replicas"][replica_key] = {
        "vmid": new_vmid,
        "ip": ip,
        "hostname": hostname,
        "parent_vmid": parent_vmid,
        "template": action["template"],
        "host_port": new_host_port,
        "container_port": container_port,
        "protocol": protocol,
    }

    # Update IP pool
    kb["ip_pool"]["allocated"][ip] = {"vmid": new_vmid, "service": hostname, "pool": "scaling"}

    logger.info(
        "SCALE_OUT: replica CT %d deployed — port :%d → %s:%d",
        new_vmid, new_host_port, ip, container_port,
    )


def _execute_scale_in(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Optimization: remove the most recently added replica."""
    node: str = kb["global"]["node_name"]
    ext_iface: str = kb["global"]["bridge_external"]

    parent_vmid: int = int(action["parent_vmid"])
    replica_vmid: int = int(action["replica_vmid"])
    container_ip: str = action["container_ip"]
    host_port: int = int(action["host_port"])
    container_port: int = int(action["container_port"])
    protocol: str = action.get("protocol", "tcp")

    logger.info("SCALE_IN: removing replica CT %d for service CT %d", replica_vmid, parent_vmid)

    # Remove port forwarding
    network.remove_port_forwarding(ext_iface, host_port, container_ip, container_port, protocol)
    _unregister_port_forwarding(kb, host_port, container_ip, container_port)

    # Destroy replica container
    ok = proxmox.destroy_container(node, replica_vmid)
    if not ok:
        logger.error("SCALE_IN: could not destroy replica CT %d", replica_vmid)

    # Remove from scaling_replicas registry
    replicas: Dict[str, Any] = kb["runtime_state"]["scaling_replicas"]
    if str(replica_vmid) in replicas:
        del replicas[str(replica_vmid)]

    # Release IP
    release_ip(kb, container_ip)

    logger.info("SCALE_IN: replica CT %d removed", replica_vmid)


def _execute_quarantine(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Protection: isolate compromised container and deploy a clean replacement."""
    node: str = kb["global"]["node_name"]
    gateway: str = kb["global"]["gateway"]
    nameserver: str = kb["global"]["nameserver"]
    ext_iface: str = kb["global"]["bridge_external"]

    vmid: int = int(action["vmid"])
    original_ip: str = action["original_ip"]
    quarantine_ip: str = action["quarantine_ip"]
    new_vmid: int = int(action["new_vmid"])
    hostname: str = action["hostname"]
    tmpl: Dict[str, Any] = action["template_conf"]
    pf: Optional[Dict[str, Any]] = action.get("port_forwarding")
    is_replica: bool = bool(action.get("is_replica", False))

    logger.warning("QUARANTINE: isolating CT %d → %s (replacement CT %d)", vmid, quarantine_ip, new_vmid)

    # 1. Remove port forwarding for compromised container
    if pf:
        host_port = int(pf["host_port"])
        container_port = int(pf["container_port"])
        protocol = pf.get("protocol", "tcp")
        network.remove_port_forwarding(ext_iface, host_port, original_ip, container_port, protocol)
        _unregister_port_forwarding(kb, host_port, original_ip, container_port)
        logger.info("QUARANTINE: removed port forwarding for CT %d", vmid)

    # 2. Move container to quarantine IP
    net_ok = proxmox.set_container_network(node, vmid, quarantine_ip, gateway)
    if not net_ok:
        logger.error(
            "QUARANTINE: failed to reconfigure network for CT %d — aborting quarantine",
            vmid,
        )
        # Release the quarantine IP we pre-allocated since we can't use it
        release_ip(kb, quarantine_ip)
        return

    # 3. Block all traffic to/from quarantine IP
    network.block_ip(quarantine_ip)
    logger.info("QUARANTINE: CT %d moved to %s and traffic blocked", vmid, quarantine_ip)

    # 4. Record quarantine state
    kb["runtime_state"]["quarantined"][str(vmid)] = {
        "vmid": vmid,
        "quarantine_ip": quarantine_ip,
        "original_ip": original_ip,
        "since": now_ts(),
    }
    kb["ip_pool"]["allocated"][quarantine_ip] = {"vmid": vmid, "service": f"quarantine-{vmid}", "pool": "quarantine"}

    # 5. Deploy clean replacement at the original IP
    logger.info("QUARANTINE: deploying replacement CT %d at %s", new_vmid, original_ip)
    ok = proxmox.create_container(node, new_vmid, tmpl, hostname, original_ip, gateway, nameserver)
    if not ok:
        logger.error("QUARANTINE: replacement container CT %d creation failed", new_vmid)
        return

    logger.info("QUARANTINE: waiting %ds for replacement CT %d to boot", _BOOT_WAIT, new_vmid)
    time.sleep(_BOOT_WAIT)

    # 6. Restore port forwarding for the replacement
    if pf:
        host_port = int(pf["host_port"])
        container_port = int(pf["container_port"])
        protocol = pf.get("protocol", "tcp")
        pf_ok = network.add_port_forwarding(ext_iface, host_port, original_ip, container_port, protocol)
        if pf_ok:
            _register_port_forwarding(kb, host_port, original_ip, container_port, protocol, new_vmid)
        logger.info(
            "QUARANTINE: port forwarding :%d → %s:%d restored for CT %d",
            host_port, original_ip, container_port, new_vmid,
        )

    # 7. Update desired_state to point to new VMID (for base services)
    if not is_replica:
        for svc in kb.get("desired_state", {}).get("services", []):
            if int(svc.get("vmid", -1)) == vmid:
                svc["vmid"] = new_vmid
                logger.info("QUARANTINE: desired_state updated — CT %d now references new CT %d", vmid, new_vmid)
                break
    else:
        # Update replica registry
        replicas: Dict[str, Any] = kb["runtime_state"]["scaling_replicas"]
        old_key = str(vmid)
        if old_key in replicas:
            replica_data = replicas[old_key]
            replica_data["vmid"] = new_vmid
            replica_data["ip"] = original_ip
            replicas[str(new_vmid)] = replica_data
            del replicas[old_key]

    # 8. Update IP pool for original IP
    kb["ip_pool"]["allocated"][original_ip] = {"vmid": new_vmid, "service": hostname}

    logger.info(
        "QUARANTINE: complete — CT %d isolated at %s, replacement CT %d active at %s",
        vmid, quarantine_ip, new_vmid, original_ip,
    )


def _execute_cleanup_quarantine(action: Dict[str, Any], kb: Dict[str, Any]) -> None:
    """Self-Protection: destroy an expired quarantined container and clean up state."""
    node: str = kb["global"]["node_name"]
    vmid: int = int(action["vmid"])
    quarantine_ip: str = action.get("quarantine_ip", "")
    q_key: str = str(action.get("q_key", vmid))

    logger.info("CLEANUP_QUARANTINE: destroying quarantined CT %d at %s", vmid, quarantine_ip)

    # Unblock traffic (in case rules are still present)
    if quarantine_ip:
        network.unblock_ip(quarantine_ip)

    ok = proxmox.destroy_container(node, vmid)
    if ok:
        logger.info("CLEANUP_QUARANTINE: CT %d destroyed", vmid)
    else:
        logger.error("CLEANUP_QUARANTINE: could not destroy CT %d (may already be gone)", vmid)

    # Release quarantine IP
    if quarantine_ip:
        release_ip(kb, quarantine_ip)

    # Remove from quarantined registry
    quarantined: Dict[Any, Any] = kb["runtime_state"].get("quarantined", {})
    quarantined.pop(q_key, None)
    quarantined.pop(vmid, None)

    logger.info("CLEANUP_QUARANTINE: state cleaned for CT %d", vmid)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _register_port_forwarding(
    kb: Dict[str, Any],
    host_port: int,
    container_ip: str,
    container_port: int,
    protocol: str,
    vmid: int,
) -> None:
    """Add a port-forwarding record to the KB active list (no duplicates)."""
    active: List[Dict[str, Any]] = kb.setdefault("active_port_forwarding", [])
    # Check for duplicates before appending
    for entry in active:
        if (
            int(entry.get("host_port", -1)) == host_port
            and entry.get("container_ip") == container_ip
            and int(entry.get("container_port", -1)) == container_port
        ):
            return  # already registered
    active.append({
        "host_port": host_port,
        "container_ip": container_ip,
        "container_port": container_port,
        "protocol": protocol,
        "vmid": vmid,
    })


def _unregister_port_forwarding(
    kb: Dict[str, Any],
    host_port: int,
    container_ip: str,
    container_port: int,
) -> None:
    """Remove matching port-forwarding records from the KB active list."""
    active: List[Dict[str, Any]] = kb.get("active_port_forwarding", [])
    kb["active_port_forwarding"] = [
        e for e in active
        if not (
            int(e.get("host_port", -1)) == host_port
            and e.get("container_ip") == container_ip
            and int(e.get("container_port", -1)) == container_port
        )
    ]


def _find_service_config(kb: Dict[str, Any], vmid: int) -> Optional[Dict[str, Any]]:
    """Return the service or replica config for *vmid*, or None."""
    for svc in kb.get("desired_state", {}).get("services", []):
        if int(svc.get("vmid", -1)) == vmid:
            return svc
    replicas: Dict[str, Any] = kb["runtime_state"].get("scaling_replicas", {})
    for _k, r in replicas.items():
        if int(r.get("vmid", -1)) == vmid:
            return r
    return None


def _quick_health_check(svc: Dict[str, Any], kb: Dict[str, Any]) -> bool:
    """Perform a lightweight health check after a restart.

    Uses the template's health_check configuration if available.
    Returns True on success or when no health check is configured.
    """
    ip: str = svc.get("ip", "")
    template_name: str = svc.get("template", "")
    timeout: int = int(kb["thresholds"]["self_healing"]["health_check_timeout"])

    hc: Optional[Dict[str, Any]] = None
    templates = kb.get("templates", {})
    if template_name and template_name in templates:
        hc = templates[template_name].get("health_check")

    if not hc:
        return True

    check_type = hc.get("type", "tcp").lower()
    port = int(hc.get("port", 80))

    try:
        if check_type == "http":
            path = hc.get("path", "/")
            expected = int(hc.get("expected_status", 200))
            resp = requests.get(f"http://{ip}:{port}{path}", timeout=timeout)
            return resp.status_code == expected
        else:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
    except Exception as exc:
        logger.debug("Quick health check failed for %s:%d: %s", ip, port, exc)
        return False
