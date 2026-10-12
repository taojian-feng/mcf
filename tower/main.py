"""Process entrypoint.

Long-running consumer:  MCF_CLOUD=azure python -m tower.main
Scheduled job:          MCF_CLOUD=azure python -m tower.main job reconciliation_daily
Partner gateway:        MCF_CLOUD=azure python -m tower.edge.gateway
"""
from __future__ import annotations

import logging
import os
import sys

from tower.core.config import PilotConfig
from tower.runtime import build, from_env


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ports = from_env()
    load = getattr(ports.directory, "pilot_config", None)
    system = build(ports, load() if load else PilotConfig())

    from tower.edge.connectors import PartnerConnectors, keep_catalog_fresh, load_catalogs
    load_catalogs(system)  # each client's item master from its catalog feed
    if argv[:1] == ["job"]:
        system.run_job(argv[1])
        return
    run = getattr(ports.bus, "run_forever", None)
    if run is None:
        raise SystemExit("this bus adapter has no consumer loop (use MCF_CLOUD=azure)")
    secrets = getattr(ports, "secrets", None)
    connectors = PartnerConnectors(system, corpb_url=os.environ.get("MCF_CORPB_URL"), env=os.environ.get,
                                   secret=secrets.get_secret if secrets else (lambda name: os.environ[name.upper().replace("-", "_")]))
    try:
        connectors.catch_up_catalogs()
    except Exception:  # noqa: BLE001 - the next catalog feed still loads normally
        logging.getLogger("tower.main").exception("could not catch up missing catalogs")
    try:
        connectors.catch_up_planning_feeds()
    except Exception:  # noqa: BLE001 - the next weekly feed still loads normally
        logging.getLogger("tower.main").exception("could not catch up planning feeds")
    logging.getLogger("tower.main").info("catalogs: %s", {t: len(system.config.catalog(t)) for t in system.config.tenants})
    keep_catalog_fresh(system)
    run()


if __name__ == "__main__":
    main(sys.argv[1:])
