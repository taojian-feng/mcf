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

    from tower.edge.connectors import CATALOG_DOC, PartnerConnectors, apply_catalog_doc, keep_catalog_fresh
    apply_catalog_doc(system.config, ports.store.get(*CATALOG_DOC))  # the pilot item master from Corp B's feed
    if argv[:1] == ["job"]:
        system.run_job(argv[1])
        return
    run = getattr(ports.bus, "run_forever", None)
    if run is None:
        raise SystemExit("this bus adapter has no consumer loop (use MCF_CLOUD=azure)")
    secrets = getattr(ports, "secrets", None)
    PartnerConnectors(system, corpb_url=os.environ.get("MCF_CORPB_URL"),
                      secret=secrets.get_secret if secrets else (lambda name: os.environ[name.upper().replace("-", "_")]))
    keep_catalog_fresh(system)
    run()


if __name__ == "__main__":
    main(sys.argv[1:])
