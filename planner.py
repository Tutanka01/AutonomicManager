"""
planner.py — MAPE-K Plan phase.

Converts analyzer events into concrete, prioritized actions.
Verifies resource constraints (max replicas, max restarts) before
allocating new VMIDs, IPs, and ports.
"""

import logging
from typing import Any, Dict, List, Optional

from knowledge import (
    allocate_ip,
    allocate_port,
    allocate_vmid,
    get_replicas_for_parent,
    get_template_config,
)

from analyzer import (
    EV_CONTAINER_DOWN,
    EV_CPU_SPIKE,
    EV_EXCESSIVE_CONNECTIONS,
    EV_HIGH_CPU_SUSTAINED,
    EV_LOW_CPU_SUSTAINED,
    EV_MISSING_SERVICE,
    EV_QUARANTINE_EXPIRED,
    EV_RESTART_LOOP,
    EV_SERVICE_UNREACHABLE,
)

logger = logging.getLogger(__name__)

# Action priority constants (lower = executed first)
PRIO_SELF_PROTECTION = 0
PRIO_SELF_HEALING = 1
PRIO_SELF_CONFIGURATION = 2
PRIO_SCALE_OUT = 3
PRIO_SCALE_IN = 4


def plan(events: List[Dict[str, Any]], kb: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert events into sorted, deduplicated actions.

    A VMID that has already been assigned a higher-priority action will not
    receive a second, contradictory action.
    """
    actions: List[Dict[str, Any]] = []
    # Track which VMIDs already have an action so we do not generate duplicates
    handled_vmids: set = set()

    _handlers = {
        EV_MISSING_SERVICE: _plan_missing_service,
        EV_CONTAINER_DOWN: _plan_container_down,
        EV_SERVICE_UNREACHABLE: _plan_service_unreachable,
        EV_HIGH_CPU_SUSTAINED: _plan_high_cpu,
        EV_LOW_CPU_SUSTAINED: _plan_low_cpu,
        EV_CPU_SPIKE: _plan_quarantine,
        EV_EXCESSIVE_CONNECTIONS: _plan_quarantine,
        EV_RESTART_LOOP: _plan_quarantine,
        EV_QUARANTINE_EXPIRED: _plan_quarantine_expired,
    }

    for event in events:
        ev_type = event["type"]
        vmid = int(event["vmid"])

        handler = _handlers.get(ev_type)
        if handler is None:
            logger.warning("No handler for event type '%s', skipping", ev_type)
            continue

        # Do not generate a lower-priority action if we already have one for this vmid
        if vmid in handled_vmids and ev_type not in (
            EV_CPU_SPIKE, EV_EXCESSIVE_CONNECTIONS, EV_RESTART_LOOP
        ):
            logger.debug("VMID %d already handled, skipping event %s", vmid, ev_type)
            continue

        try:
            action = handler(event, kb)
        except Exception as exc:
            logger.error("Planner handler error for event %s vmid=%d: %s", ev_type, vmid, exc)
            continue

        if action is not None:
            actions.append(action)
            handled_vmids.add(vmid)
            logger.info("Planned action %s for CT %d (priority=%d)", action["action"], vmid, action["priority"])

    # Sort by priority (lowest number = executed first)
    actions.sort(key=lambda a: a["priority"])
    logger.info("%d action(s) planned", len(actions))
    return actions


# ---------------------------------------------------------------------------
# Individual event handlers
# ---------------------------------------------------------------------------

def _find_service(kb: Dict[str, Any], vmid: int) -> Optional[Dict[str, Any]]:
    """Return the service dict from desired_state matching *vmid*."""
    for svc in kb.get("desired_state", {}).get("services", []):
        if int(svc["vmid"]) == vmid:
            return svc
    return None


def _find_replica(kb: Dict[str, Any], vmid: int) -> Optional[Dict[str, Any]]:
    """Return the replica dict from scaling_replicas matching *vmid*."""
    replicas: Dict[str, Any] = kb["runtime_state"].get("scaling_replicas", {})
    for _key, replica in replicas.items():
        if int(replica.get("vmid", -1)) == vmid:
            return replica
    return None


def _plan_missing_service(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan a DEPLOY_NEW action for a service that does not exist yet."""
    vmid = int(event["vmid"])
    svc = _find_service(kb, vmid)
    if svc is None:
        logger.error("Plan MISSING_SERVICE: service config not found for vmid=%d", vmid)
        return None

    template_name: str = svc["template"]
    try:
        tmpl = get_template_config(kb, template_name)
    except KeyError as exc:
        logger.error("Plan MISSING_SERVICE: %s", exc)
        return None

    return {
        "action": "DEPLOY_NEW",
        "priority": PRIO_SELF_CONFIGURATION,
        "vmid": vmid,
        "hostname": svc["name"],
        "ip": svc["ip"],
        "template": template_name,
        "template_conf": tmpl,
        "port_forwarding": svc.get("port_forwarding"),
    }


def _plan_container_down(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan RESTART or REDEPLOY depending on restart attempt count."""
    vmid = int(event["vmid"])
    svc = _find_service(kb, vmid)
    if svc is None:
        svc = _find_replica(kb, vmid)
    if svc is None:
        logger.error("Plan CONTAINER_DOWN: service/replica config not found for vmid=%d", vmid)
        return None

    max_attempts: int = int(kb["thresholds"]["self_healing"]["max_restart_attempts"])
    counters: Dict[Any, Any] = kb["runtime_state"]["restart_counters"]
    attempt: int = int(counters.get(vmid, counters.get(str(vmid), 0)))

    if attempt < max_attempts:
        logger.info("Plan RESTART for CT %d (attempt %d/%d)", vmid, attempt + 1, max_attempts)
        return {
            "action": "RESTART",
            "priority": PRIO_SELF_HEALING,
            "vmid": vmid,
            "attempt": attempt + 1,
            "max_attempts": max_attempts,
        }
    else:
        # Too many restarts → redeploy
        logger.info("Plan REDEPLOY for CT %d (restarts=%d, exceeds max=%d)", vmid, attempt, max_attempts)
        template_name = svc.get("template", "")
        try:
            tmpl = get_template_config(kb, template_name)
        except KeyError as exc:
            logger.error("Plan REDEPLOY: %s", exc)
            return None

        return {
            "action": "REDEPLOY",
            "priority": PRIO_SELF_HEALING,
            "vmid": vmid,
            "hostname": svc.get("name", f"ct-{vmid}"),
            "ip": svc.get("ip", ""),
            "template": template_name,
            "template_conf": tmpl,
            "port_forwarding": svc.get("port_forwarding"),
        }


def _plan_service_unreachable(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan a RESTART for a running-but-unresponsive container."""
    vmid = int(event["vmid"])
    return {
        "action": "RESTART",
        "priority": PRIO_SELF_HEALING,
        "vmid": vmid,
        "attempt": None,   # executor will resolve from counter
        "max_attempts": int(kb["thresholds"]["self_healing"]["max_restart_attempts"]),
    }


def _plan_high_cpu(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan SCALE_OUT if replica count < max_replicas."""
    vmid = int(event["vmid"])

    # If vmid is a replica, resolve its parent service
    replica = _find_replica(kb, vmid)
    if replica is not None:
        parent_vmid = int(replica.get("parent_vmid", vmid))
        svc = _find_service(kb, parent_vmid)
    else:
        parent_vmid = vmid
        svc = _find_service(kb, vmid)

    if svc is None:
        logger.error("Plan SCALE_OUT: service config not found for vmid=%d", vmid)
        return None

    max_replicas: int = int(kb["thresholds"]["self_optimization"]["max_replicas"])
    existing_replicas = get_replicas_for_parent(kb, parent_vmid)

    if len(existing_replicas) >= max_replicas:
        logger.info(
            "Plan SCALE_OUT: CT %d already at max replicas (%d/%d), skipping",
            parent_vmid, len(existing_replicas), max_replicas,
        )
        return None

    template_name: str = svc["template"]
    try:
        tmpl = get_template_config(kb, template_name)
    except KeyError as exc:
        logger.error("Plan SCALE_OUT: %s", exc)
        return None

    new_vmid = allocate_vmid(kb)
    new_ip = allocate_ip(kb, "scaling")
    if new_ip is None:
        logger.error("Plan SCALE_OUT: scaling IP pool exhausted for CT %d", parent_vmid)
        # Roll back vmid allocation by decrementing (simple heuristic)
        kb["global"]["next_vmid"] = new_vmid
        return None

    new_host_port = allocate_port(kb)
    hostname = f"{svc['name']}-replica-{len(existing_replicas) + 1}"

    logger.info(
        "Plan SCALE_OUT for CT %d: new replica CT %d at %s, host_port=%d",
        parent_vmid, new_vmid, new_ip, new_host_port,
    )
    return {
        "action": "SCALE_OUT",
        "priority": PRIO_SCALE_OUT,
        "parent_vmid": parent_vmid,
        "new_vmid": new_vmid,
        "hostname": hostname,
        "ip": new_ip,
        "template": template_name,
        "template_conf": tmpl,
        "new_host_port": new_host_port,
        "container_port": svc.get("port_forwarding", {}).get("container_port", 80),
        "protocol": svc.get("port_forwarding", {}).get("protocol", "tcp"),
    }


def _plan_low_cpu(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan SCALE_IN by removing the most recently added replica."""
    vmid = int(event["vmid"])   # always a base service (analyzer guarantees this)
    replicas = get_replicas_for_parent(kb, vmid)

    if not replicas:
        logger.info("Plan SCALE_IN: no replicas to remove for CT %d", vmid)
        return None

    # Choose the most recently added replica (last in list, which grows by append)
    target_replica = replicas[-1]
    replica_vmid = int(target_replica["vmid"])
    replica_ip: str = target_replica["ip"]
    host_port: int = int(target_replica.get("host_port", 0))
    container_port: int = int(target_replica.get("container_port", 80))
    protocol: str = target_replica.get("protocol", "tcp")

    logger.info(
        "Plan SCALE_IN for CT %d: removing replica CT %d at %s",
        vmid, replica_vmid, replica_ip,
    )
    return {
        "action": "SCALE_IN",
        "priority": PRIO_SCALE_IN,
        "parent_vmid": vmid,
        "replica_vmid": replica_vmid,
        "container_ip": replica_ip,
        "host_port": host_port,
        "container_port": container_port,
        "protocol": protocol,
    }


def _plan_quarantine(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan a QUARANTINE action for a misbehaving container."""
    vmid = int(event["vmid"])
    quarantined: Dict[Any, Any] = kb["runtime_state"].get("quarantined", {})

    # Skip if already quarantined
    if str(vmid) in quarantined or vmid in quarantined:
        logger.debug("CT %d is already quarantined, skipping QUARANTINE plan", vmid)
        return None

    # Determine if it's a base service or replica
    svc = _find_service(kb, vmid)
    is_replica = False
    if svc is None:
        svc = _find_replica(kb, vmid)
        is_replica = True

    if svc is None:
        logger.error("Plan QUARANTINE: service/replica config not found for vmid=%d", vmid)
        return None

    original_ip: str = svc.get("ip", "")
    template_name: str = svc.get("template", "")
    try:
        tmpl = get_template_config(kb, template_name)
    except KeyError as exc:
        logger.error("Plan QUARANTINE: %s", exc)
        return None

    quarantine_ip = allocate_ip(kb, "quarantine")
    if quarantine_ip is None:
        logger.error("Plan QUARANTINE: quarantine IP pool exhausted for CT %d", vmid)
        return None

    new_vmid = allocate_vmid(kb)
    hostname = svc.get("name", f"ct-{vmid}")

    logger.warning(
        "Plan QUARANTINE for CT %d: quarantine_ip=%s, replacement CT %d at %s",
        vmid, quarantine_ip, new_vmid, original_ip,
    )
    return {
        "action": "QUARANTINE",
        "priority": PRIO_SELF_PROTECTION,
        "vmid": vmid,
        "original_ip": original_ip,
        "quarantine_ip": quarantine_ip,
        "new_vmid": new_vmid,
        "hostname": hostname,
        "template": template_name,
        "template_conf": tmpl,
        "port_forwarding": svc.get("port_forwarding"),
        "is_replica": is_replica,
    }


def _plan_quarantine_expired(event: Dict[str, Any], kb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Plan cleanup of an expired quarantined container."""
    vmid = int(event["vmid"])
    quarantine_ip: str = event.get("quarantine_ip", "")
    q_key: str = str(event.get("q_key", vmid))

    logger.info("Plan CLEANUP_QUARANTINE for CT %d (quarantine_ip=%s)", vmid, quarantine_ip)
    return {
        "action": "CLEANUP_QUARANTINE",
        "priority": PRIO_SELF_PROTECTION,
        "vmid": vmid,
        "quarantine_ip": quarantine_ip,
        "q_key": q_key,
    }
