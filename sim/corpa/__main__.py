"""Run Corp A: `python -m sim.corpa` (HTTP on $PORT, default 8080, plus the fulfillment engine loop)."""
from __future__ import annotations

import logging
import os
import threading
import time

import uvicorn

from sim.common.clock import RealClock
from sim.common.store import CosmosStore, MemoryStore
from sim.common.web import load_secret

from .api import Tokens, create_app
from .engine import CorpA

log = logging.getLogger("sim.corpa")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ
    store = (CosmosStore(env["SIM_COSMOS_URL"], env.get("SIM_COSMOS_DB", "corpa"))
             if env.get("SIM_COSMOS_URL") else MemoryStore())
    engine = CorpA(store, RealClock(), webhook_url=env["CORPA_WEBHOOK_URL"],
                   webhook_secret=load_secret("partner-corpa-webhook-secret"),
                   speed=float(env.get("SIM_SPEED", "1")),
                   shrink_rate=float(env.get("CORPA_SHRINK_RATE", "0.0005")))
    tokens = Tokens(load_secret("mcf-client-id"), load_secret("mcf-client-secret"))
    faults = tuple(float(x) for x in env.get("CORPA_FAULTS", "0.015,0.01,0.003").split(","))

    def loop() -> None:
        while True:
            try:
                engine.tick()
            except Exception:  # noqa: BLE001 - keep the warehouse running; the next tick retries
                log.exception("tick failed")
            time.sleep(float(env.get("CORPA_TICK_S", "20")))

    def start() -> None:
        threading.Thread(target=loop, daemon=True, name="corpa-engine").start()

    uvicorn.run(create_app(engine, tokens, faults=faults, on_startup=[start]), host="0.0.0.0",
                port=int(env.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
