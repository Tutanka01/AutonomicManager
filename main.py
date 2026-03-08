"""
main.py — Entry point for the autonomic MAPE-K manager.

Bootstraps logging, loads the Knowledge Base, then runs the
Monitor → Analyze → Plan → Execute loop indefinitely.

Run as root on the Proxmox VE host:
    python3 main.py
"""

import logging
import time

from analyzer import analyze
from executor import execute
from knowledge import load_kb, save_kb
from monitor import monitor
from planner import plan
from utils import setup_logging

logger = logging.getLogger(__name__)

_KB_PATH = "knowledge.yaml"


def main() -> None:
    setup_logging("logs")
    logger.info("Autonomic MAPE-K Manager starting")

    kb = load_kb(_KB_PATH)
    logger.info(
        "Knowledge base loaded — node=%s, interval=%ds",
        kb["global"]["node_name"],
        kb["global"]["check_interval"],
    )

    cycle = 0
    while True:
        cycle += 1
        logger.info("=== Cycle MAPE-K #%d ===", cycle)

        # ------------------------------------------------------------------
        # 1. MONITOR — collect state for all known containers
        # ------------------------------------------------------------------
        try:
            observed = monitor(kb)
        except Exception as exc:
            logger.error("MONITOR phase crashed: %s", exc)
            observed = {}

        # ------------------------------------------------------------------
        # 2. ANALYZE — detect deviations from desired state
        # ------------------------------------------------------------------
        try:
            events = analyze(observed, kb)
        except Exception as exc:
            logger.error("ANALYZE phase crashed: %s", exc)
            events = []

        # ------------------------------------------------------------------
        # 3. PLAN — convert events into prioritized actions
        # ------------------------------------------------------------------
        try:
            actions = plan(events, kb)
        except Exception as exc:
            logger.error("PLAN phase crashed: %s", exc)
            actions = []

        # ------------------------------------------------------------------
        # 4. EXECUTE — apply actions, update KB in-place
        # ------------------------------------------------------------------
        if actions:
            try:
                execute(actions, kb)
            except Exception as exc:
                logger.error("EXECUTE phase crashed: %s", exc)
        else:
            logger.info("No actions to execute")

        # ------------------------------------------------------------------
        # 5. KNOWLEDGE — persist updated state
        # ------------------------------------------------------------------
        try:
            save_kb(kb, _KB_PATH)
            logger.info("Knowledge base saved")
        except Exception as exc:
            logger.error("Could not save knowledge base: %s", exc)

        # ------------------------------------------------------------------
        # 6. Sleep until next cycle
        # ------------------------------------------------------------------
        interval: int = int(kb["global"].get("check_interval", 15))
        logger.info("Next cycle in %ds", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
