"""
utils.py — Utility functions for the autonomic MAPE-K manager.

Provides logging setup, timestamp helpers, and IP range utilities
shared across all modules.
"""

import logging
import os
import socket
from datetime import datetime
from typing import Optional


def setup_logging(log_dir: str = "logs") -> None:
    """Configure global logging with file and console handlers."""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "autonomic.log")

    root_logger = logging.getLogger()
    # Avoid duplicate handlers on repeated calls (e.g. during tests)
    if root_logger.handlers:
        return

    root_logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(module)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — DEBUG and above
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root_logger.addHandler(fh)

    # Console handler — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root_logger.addHandler(ch)


def now_ts() -> float:
    """Return current Unix timestamp as float."""
    return datetime.utcnow().timestamp()


def now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def ip_in_range(ip: str, start: int, end: int, prefix: str = "192.168.100") -> bool:
    """Return True if *ip* is in the range prefix.start – prefix.end (inclusive)."""
    try:
        parts = ip.split(".")
        if ".".join(parts[:3]) != prefix:
            return False
        last = int(parts[3])
        return start <= last <= end
    except (ValueError, IndexError):
        return False


def build_ip(last_octet: int, prefix: str = "192.168.100") -> str:
    """Build an IP address string from prefix and last octet."""
    return f"{prefix}.{last_octet}"


def tcp_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """Return True if a TCP connection to *host*:*port* succeeds within *timeout* seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False
