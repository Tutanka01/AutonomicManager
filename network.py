"""
network.py — iptables management for the autonomic MAPE-K manager.

Handles port-forwarding (DNAT + FORWARD ACCEPT), IP blocking (DROP),
and rule idempotency checks.  All iptables operations use subprocess.
"""

import logging
import subprocess
from typing import List, Optional

logger = logging.getLogger(__name__)

_TIMEOUT = 10


def _run_iptables(args: List[str], timeout: int = _TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    """Run an iptables command. Returns CompletedProcess or None on exception."""
    cmd = ["iptables"] + args
    logger.debug("iptables %s", " ".join(args))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result
    except subprocess.TimeoutExpired:
        logger.error("iptables command timed out: %s", " ".join(args))
        return None
    except Exception as exc:
        logger.error("iptables command exception (%s): %s", exc, " ".join(args))
        return None


def rule_exists(table: str, chain: str, rule_spec: List[str]) -> bool:
    """Return True if the given iptables rule already exists (uses -C check)."""
    args = ["-t", table, "-C", chain] + rule_spec
    result = _run_iptables(args)
    if result is None:
        return False
    # returncode == 0 means rule exists; 1 means it does not
    return result.returncode == 0


def add_port_forwarding(
    ext_iface: str,
    host_port: int,
    container_ip: str,
    container_port: int,
    protocol: str = "tcp",
) -> bool:
    """Add a DNAT rule (PREROUTING) and a FORWARD ACCEPT rule for the given mapping.

    Idempotent: rules are only added if they do not already exist.
    """
    # --- DNAT rule in nat/PREROUTING ---
    dnat_spec = [
        "-i", ext_iface,
        "-p", protocol,
        "--dport", str(host_port),
        "-j", "DNAT",
        "--to-destination", f"{container_ip}:{container_port}",
    ]
    if not rule_exists("nat", "PREROUTING", dnat_spec):
        result = _run_iptables(["-t", "nat", "-A", "PREROUTING"] + dnat_spec)
        if result is None or result.returncode != 0:
            logger.error(
                "Failed to add DNAT rule :%d → %s:%d: %s",
                host_port, container_ip, container_port,
                result.stderr.strip() if result else "no output",
            )
            return False
        logger.info("Added DNAT :%d → %s:%d (%s)", host_port, container_ip, container_port, protocol)
    else:
        logger.debug("DNAT rule :%d → %s:%d already exists, skipped", host_port, container_ip, container_port)

    # --- FORWARD ACCEPT rule in filter/FORWARD ---
    fwd_spec = [
        "-d", container_ip,
        "-p", protocol,
        "--dport", str(container_port),
        "-j", "ACCEPT",
    ]
    if not rule_exists("filter", "FORWARD", fwd_spec):
        result = _run_iptables(["-t", "filter", "-A", "FORWARD"] + fwd_spec)
        if result is None or result.returncode != 0:
            logger.error(
                "Failed to add FORWARD ACCEPT rule for %s:%d: %s",
                container_ip, container_port,
                result.stderr.strip() if result else "no output",
            )
            return False
        logger.info("Added FORWARD ACCEPT → %s:%d (%s)", container_ip, container_port, protocol)
    else:
        logger.debug("FORWARD rule for %s:%d already exists, skipped", container_ip, container_port)

    return True


def remove_port_forwarding(
    ext_iface: str,
    host_port: int,
    container_ip: str,
    container_port: int,
    protocol: str = "tcp",
) -> bool:
    """Remove the DNAT and FORWARD rules for the given mapping.

    Idempotent: only attempts deletion if the rule exists.
    """
    dnat_spec = [
        "-i", ext_iface,
        "-p", protocol,
        "--dport", str(host_port),
        "-j", "DNAT",
        "--to-destination", f"{container_ip}:{container_port}",
    ]
    dnat_ok = True
    if rule_exists("nat", "PREROUTING", dnat_spec):
        result = _run_iptables(["-t", "nat", "-D", "PREROUTING"] + dnat_spec)
        if result is None or result.returncode != 0:
            logger.error(
                "Failed to remove DNAT rule :%d → %s:%d: %s",
                host_port, container_ip, container_port,
                result.stderr.strip() if result else "no output",
            )
            dnat_ok = False
        else:
            logger.info("Removed DNAT :%d → %s:%d", host_port, container_ip, container_port)
    else:
        logger.debug("DNAT rule :%d → %s:%d does not exist, nothing to remove", host_port, container_ip, container_port)

    fwd_spec = [
        "-d", container_ip,
        "-p", protocol,
        "--dport", str(container_port),
        "-j", "ACCEPT",
    ]
    fwd_ok = True
    if rule_exists("filter", "FORWARD", fwd_spec):
        result = _run_iptables(["-t", "filter", "-D", "FORWARD"] + fwd_spec)
        if result is None or result.returncode != 0:
            logger.error(
                "Failed to remove FORWARD rule for %s:%d: %s",
                container_ip, container_port,
                result.stderr.strip() if result else "no output",
            )
            fwd_ok = False
        else:
            logger.info("Removed FORWARD → %s:%d", container_ip, container_port)
    else:
        logger.debug("FORWARD rule for %s:%d does not exist, nothing to remove", container_ip, container_port)

    return dnat_ok and fwd_ok


def block_ip(ip: str) -> bool:
    """Block all forwarded traffic to and from *ip* using DROP rules.

    Inserts rules at position 1 so they take priority.
    Idempotent: rules are only added when not already present.
    """
    rules = [
        # Inbound: traffic directed to the quarantined IP
        ["-d", ip, "-j", "DROP"],
        # Outbound: traffic originating from the quarantined IP
        ["-s", ip, "-j", "DROP"],
    ]

    all_ok = True
    for spec in rules:
        if not rule_exists("filter", "FORWARD", spec):
            result = _run_iptables(["-t", "filter", "-I", "FORWARD", "1"] + spec)
            if result is None or result.returncode != 0:
                logger.error(
                    "Failed to add DROP rule for %s: %s", ip,
                    result.stderr.strip() if result else "no output",
                )
                all_ok = False
            else:
                logger.info("Blocked traffic for IP %s (rule: %s)", ip, " ".join(spec))
        else:
            logger.debug("DROP rule for %s (%s) already exists, skipped", ip, " ".join(spec))

    return all_ok


def unblock_ip(ip: str) -> bool:
    """Remove DROP rules for *ip* that were added by block_ip.

    Idempotent: only removes rules that exist.
    """
    rules = [
        ["-d", ip, "-j", "DROP"],
        ["-s", ip, "-j", "DROP"],
    ]

    all_ok = True
    for spec in rules:
        if rule_exists("filter", "FORWARD", spec):
            result = _run_iptables(["-t", "filter", "-D", "FORWARD"] + spec)
            if result is None or result.returncode != 0:
                logger.error(
                    "Failed to remove DROP rule for %s: %s", ip,
                    result.stderr.strip() if result else "no output",
                )
                all_ok = False
            else:
                logger.info("Unblocked traffic for IP %s (rule: %s)", ip, " ".join(spec))
        else:
            logger.debug("DROP rule for %s (%s) does not exist, nothing to remove", ip, " ".join(spec))

    return all_ok
