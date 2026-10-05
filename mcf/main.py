"""Process entrypoint.

Long-running consumer:  MCF_CLOUD=azure python -m mcf.main
Scheduled job:          MCF_CLOUD=azure python -m mcf.main job reconciliation_daily
"""
from __future__ import annotations

import sys

from mcf.core.config import PilotConfig
from mcf.runtime import build, from_env


def main(argv: list[str]) -> None:
    ports = from_env()
    load = getattr(ports.directory, "pilot_config", None)
    system = build(ports, load() if load else PilotConfig())
    if argv[:1] == ["job"]:
        system.run_job(argv[1])
        return
    run = getattr(ports.bus, "run_forever", None)
    if run is None:
        raise SystemExit("this bus adapter has no consumer loop (use MCF_CLOUD=azure)")
    run()


if __name__ == "__main__":
    main(sys.argv[1:])
