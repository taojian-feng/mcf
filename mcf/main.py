"""Process entrypoint for a deployed agent: MCF_CLOUD=azure MCF_AGENT=orchestration python -m mcf.main"""
from __future__ import annotations

from mcf.core.config import PilotConfig
from mcf.runtime import build, from_env


def main() -> None:
    ports = from_env()
    build(ports, PilotConfig())  # pilot config will load from App Configuration in the next iteration
    run = getattr(ports.bus, "run_forever", None)
    if run is None:
        raise SystemExit("this bus adapter has no consumer loop (use MCF_CLOUD=azure)")
    run()


if __name__ == "__main__":
    main()
