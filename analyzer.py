"""
analyzer.py — MAPE-K Analyze phase.

Compares the observed state (from monitor) against the desired state and
thresholds in the Knowledge Base to produce a list of typed events.

Event priorities (lower = more urgent):
  0 — self-protection
  1 — self-healing
  2 — self-configuration
  3/4 — self-optimization
"""

import logging
from typing import Any, Dict, List, Optional

from utils import now_ts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------
EV_MISSING_SERVICE = "MISSING_SERVICE"
EV_CONTAINER_DOWN = "CONTAINER_DOWN"
EV_SERVICE_UNREACHABLE = "SERVICE_UNREACHABLE"
EV_HIGH_CPU_SUSTAINED = "HIGH_CPU_SUSTAINED"
EV_LOW_CPU_SUSTAINED = "LOW_CPU_SUSTAINED"
EV_CPU_SPIKE = "CPU_SPIKE"
EV_EXCESSIVE_CONNECTIONS = "EXCESSIVE_CONNECTIONS"
EV_RESTART_LOOP = "RESTART_LOOP"
EV_QUARANTINE_EXPIRED = "QUARANTINE_EXPIRED"


def _make_event(ev_type: str, vmid: int, prop: str, **kwargs: Any) -> Dict[str, Any]:
    """Build a structured event dict."""
    return {"type": ev_type, "vmid": vmid, "property": prop, **kwargs}


def _get_restart_counter(kb: Dict[str, Any], vmid: int) -> int:
    """Return restart count for *vmid*, handling both int and str keys (YAML round-trip)."""
    counters: Dict[Any, Any] = kb["runtime_state"]["restart_counters"]
    return int(counters.get(vmid, counters.get(str(vmid), 0)))


def _get_replicas_for_parent(kb: Dict[str, Any], parent_vmid: int) -> List[Dict[str, Any]]:
    """Return all replica dicts belonging to *parent_vmid*."""
    replicas: Dict[str, Any] = kb["runtime_state"].get("scaling_replicas", {})
    result = []
    for _key, replica in replicas.items():
        if int(replica.get("parent_vmid", -1)) == int(parent_vmid):
            result.append(replica)
    return result


def analyze(observed_state: Dict[int, Dict[str, Any]], kb: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Analyze the observed state and return a list of events sorted by priority."""
    events: List[Dict[str, Any]] = []

    opt = kb["thresholds"]["self_optimization"]
    prot = kb["thresholds"]["self_protection"]
    healing = kb["thresholds"]["self_healing"]

    cpu_scale_out: float = float(opt["cpu_scale_out"])
    cpu_scale_in: float = float(opt["cpu_scale_in"])
    sustained_cycles: int = int(opt["sustained_cycles"])
    max_restart_failures: int = int(prot["max_restart_failures"])
    max_cpu_spike: float = float(prot["max_cpu_spike"])
    max_connections: int = int(prot["max_connections"])
    quarantine_duration: float = float(prot["quarantine_duration"])

    rt = kb["runtime_state"]
    sustained_counters: Dict[Any, Any] = rt.setdefault("sustained_counters", {})
    quarantined: Dict[Any, Any] = rt.setdefault("quarantined", {})

    # Set of vmids already scheduled for quarantine/high-priority action
    # so we do not generate lower-priority duplicate actions for the same vmid
    protected_vmids: set = set()

    # -----------------------------------------------------------------------
    # 1: Check quarantine expiry
    # -----------------------------------------------------------------------
    for q_key in list(quarantined.keys()):
        q_info = quarantined[q_key]
        quarantine_start: float = float(q_info.get("since", 0.0))
        q_vmid = int(q_info.get("vmid", q_key))
        if now_ts() - quarantine_start >= quarantine_duration:
            logger.info(
                "Quarantine expired for CT %s (ip=%s), emitting QUARANTINE_EXPIRED",
                q_vmid, q_info.get("quarantine_ip"),
            )
            events.append(
                _make_event(
                    EV_QUARANTINE_EXPIRED, q_vmid, "self-protection",
                    quarantine_ip=q_info.get("quarantine_ip"),
                    q_key=q_key,
                )
            )

    # -----------------------------------------------------------------------
    # 2: Iterate over desired services (base services only for config/healing checks)
    # -----------------------------------------------------------------------
    desired_services = kb.get("desired_state", {}).get("services", [])

    # Keep a dict of vmid → service for fast lookup
    desired_by_vmid: Dict[int, Dict[str, Any]] = {
        int(s["vmid"]): s for s in desired_services
    }

    for svc in desired_services:
        vmid = int(svc["vmid"])

        # Skip containers currently in quarantine (monitored separately)
        if str(vmid) in quarantined or vmid in quarantined:
            continue

        obs = observed_state.get(vmid)

        # --- Self-Configuration: container is missing entirely ---
        if obs is None or not obs["exists"]:
            logger.warning("Event: %s (vmid=%d, property=self-configuration)", EV_MISSING_SERVICE, vmid)
            events.append(_make_event(EV_MISSING_SERVICE, vmid, "self-configuration"))
            protected_vmids.add(vmid)
            continue

        cpu: float = obs["cpu_percent"]
        connections: int = obs["connections"]
        status: str = obs["status"]
        alive: bool = obs["service_alive"]

        # --- Self-Protection: CPU spike ---
        if cpu > max_cpu_spike:
            logger.critical(
                "Event: %s (vmid=%d, cpu=%.1f%%, property=self-protection)",
                EV_CPU_SPIKE, vmid, cpu,
            )
            events.append(_make_event(EV_CPU_SPIKE, vmid, "self-protection", cpu_percent=cpu))
            protected_vmids.add(vmid)

        # --- Self-Protection: excessive connections ---
        if connections > max_connections:
            logger.critical(
                "Event: %s (vmid=%d, connections=%d, property=self-protection)",
                EV_EXCESSIVE_CONNECTIONS, vmid, connections,
            )
            events.append(
                _make_event(EV_EXCESSIVE_CONNECTIONS, vmid, "self-protection", connections=connections)
            )
            protected_vmids.add(vmid)

        # --- Self-Protection: restart loop ---
        restart_count = _get_restart_counter(kb, vmid)
        if restart_count >= max_restart_failures:
            logger.critical(
                "Event: %s (vmid=%d, restarts=%d, property=self-protection)",
                EV_RESTART_LOOP, vmid, restart_count,
            )
            events.append(
                _make_event(EV_RESTART_LOOP, vmid, "self-protection", restart_count=restart_count)
            )
            protected_vmids.add(vmid)

        # Skip healing/optimization checks if already flagged for protection
        if vmid in protected_vmids:
            _reset_sustained(sustained_counters, vmid)
            continue

        # --- Self-Healing: container stopped ---
        if status == "stopped" and svc.get("must_be_running", False):
            logger.warning(
                "Event: %s (vmid=%d, property=self-healing)", EV_CONTAINER_DOWN, vmid
            )
            events.append(_make_event(EV_CONTAINER_DOWN, vmid, "self-healing"))
            continue

        # --- Self-Healing: service unreachable while running ---
        if status == "running" and not alive:
            logger.warning(
                "Event: %s (vmid=%d, property=self-healing)", EV_SERVICE_UNREACHABLE, vmid
            )
            events.append(_make_event(EV_SERVICE_UNREACHABLE, vmid, "self-healing"))
            continue

        # --- Self-Optimization: sustained CPU thresholds ---
        # Only emit for base services; scale-in requires replicas to exist
        sc = sustained_counters.setdefault(str(vmid), {"high_cpu_cycles": 0, "low_cpu_cycles": 0})

        if cpu > cpu_scale_out:
            sc["high_cpu_cycles"] += 1
            sc["low_cpu_cycles"] = 0
            if sc["high_cpu_cycles"] >= sustained_cycles:
                logger.warning(
                    "Event: %s (vmid=%d, cpu=%.1f%%, cycles=%d, property=self-optimization)",
                    EV_HIGH_CPU_SUSTAINED, vmid, cpu, sc["high_cpu_cycles"],
                )
                events.append(
                    _make_event(EV_HIGH_CPU_SUSTAINED, vmid, "self-optimization", cpu_percent=cpu)
                )
                sc["high_cpu_cycles"] = 0  # reset after emission
        elif cpu < cpu_scale_in:
            sc["low_cpu_cycles"] += 1
            sc["high_cpu_cycles"] = 0
            replicas = _get_replicas_for_parent(kb, vmid)
            if sc["low_cpu_cycles"] >= sustained_cycles and len(replicas) > 0:
                logger.info(
                    "Event: %s (vmid=%d, cpu=%.1f%%, cycles=%d, replicas=%d, property=self-optimization)",
                    EV_LOW_CPU_SUSTAINED, vmid, cpu, sc["low_cpu_cycles"], len(replicas),
                )
                events.append(
                    _make_event(
                        EV_LOW_CPU_SUSTAINED, vmid, "self-optimization",
                        cpu_percent=cpu, replica_count=len(replicas),
                    )
                )
                sc["low_cpu_cycles"] = 0  # reset after emission
        else:
            # CPU in normal range — reset both counters
            sc["high_cpu_cycles"] = 0
            sc["low_cpu_cycles"] = 0

    # -----------------------------------------------------------------------
    # 3: Check replicas for self-protection only (not healing/config)
    # -----------------------------------------------------------------------
    replicas_map: Dict[str, Any] = kb["runtime_state"].get("scaling_replicas", {})
    for _key, replica in replicas_map.items():
        r_vmid = int(replica["vmid"])
        if r_vmid in protected_vmids:
            continue
        if str(r_vmid) in quarantined or r_vmid in quarantined:
            continue

        obs = observed_state.get(r_vmid)
        if obs is None or not obs["exists"] or obs["status"] != "running":
            continue

        cpu = obs["cpu_percent"]
        connections = obs["connections"]
        restart_count = _get_restart_counter(kb, r_vmid)

        if cpu > max_cpu_spike:
            logger.critical(
                "Event: %s (vmid=%d [replica], cpu=%.1f%%, property=self-protection)",
                EV_CPU_SPIKE, r_vmid, cpu,
            )
            events.append(_make_event(EV_CPU_SPIKE, r_vmid, "self-protection", cpu_percent=cpu))
            protected_vmids.add(r_vmid)

        if connections > max_connections and r_vmid not in protected_vmids:
            logger.critical(
                "Event: %s (vmid=%d [replica], connections=%d, property=self-protection)",
                EV_EXCESSIVE_CONNECTIONS, r_vmid, connections,
            )
            events.append(
                _make_event(EV_EXCESSIVE_CONNECTIONS, r_vmid, "self-protection", connections=connections)
            )
            protected_vmids.add(r_vmid)

        if restart_count >= max_restart_failures and r_vmid not in protected_vmids:
            logger.critical(
                "Event: %s (vmid=%d [replica], restarts=%d, property=self-protection)",
                EV_RESTART_LOOP, r_vmid, restart_count,
            )
            events.append(
                _make_event(EV_RESTART_LOOP, r_vmid, "self-protection", restart_count=restart_count)
            )
            protected_vmids.add(r_vmid)

    # -----------------------------------------------------------------------
    # 4: Summary log
    # -----------------------------------------------------------------------
    if events:
        logger.info("%d event(s) detected this cycle", len(events))
    else:
        logger.info("No events detected — system nominal")

    # Sort by autonomic property priority: protection(0) < healing(1) < config(2) < optim(3+)
    _PRIORITY = {
        "self-protection": 0,
        "self-healing": 1,
        "self-configuration": 2,
        "self-optimization": 3,
    }
    events.sort(key=lambda e: _PRIORITY.get(e.get("property", "self-optimization"), 99))

    return events


def _reset_sustained(sustained_counters: Dict[Any, Any], vmid: int) -> None:
    """Reset sustained CPU counters for *vmid* (string key as stored in YAML)."""
    sc = sustained_counters.get(str(vmid))
    if sc:
        sc["high_cpu_cycles"] = 0
        sc["low_cpu_cycles"] = 0
