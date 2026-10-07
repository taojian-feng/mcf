"""Run Corp B: `python -m sim.corpb` (HTTP on $PORT, default 8080, plus the order and feed loop)."""
from __future__ import annotations

import logging
import os
import threading
import time

import uvicorn

from sim.common.clock import RealClock
from sim.common.store import CosmosStore, MemoryStore
from sim.common.web import load_secret

from .api import create_app
from .engine import CorpB

log = logging.getLogger("sim.corpb")


def blob_uploader(account_url: str):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient, ContentSettings
    container = BlobServiceClient(account_url, credential=DefaultAzureCredential()).get_container_client("landing")

    def upload(path: str, data: bytes) -> None:
        container.upload_blob(path, data, overwrite=True,
                              content_settings=ContentSettings(content_type="application/json"))
    return upload


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    env = os.environ
    store = (CosmosStore(env["SIM_COSMOS_URL"], env.get("SIM_COSMOS_DB", "corpb"))
             if env.get("SIM_COSMOS_URL") else MemoryStore())
    engine = CorpB(store, RealClock(), gateway_url=env["CORPB_GATEWAY_URL"],
                   api_key=load_secret("partner-corpb-api-key"), upload=blob_uploader(env["CORPB_LANDING_URL"]),
                   orders_per_day=float(env.get("CORPB_ORDERS_PER_DAY", "140")),
                   speed=float(env.get("SIM_SPEED", "1")))

    def loop() -> None:
        while True:
            try:
                engine.tick()
            except Exception:  # noqa: BLE001 - keep taking orders; the next tick retries
                log.exception("tick failed")
            time.sleep(float(env.get("CORPB_TICK_S", "30")))

    def start() -> None:
        threading.Thread(target=loop, daemon=True, name="corpb-oms").start()

    uvicorn.run(create_app(engine, load_secret("corpb-inbound-api-key"), on_startup=[start]), host="0.0.0.0",
                port=int(env.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
