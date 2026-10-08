"""Run Corp C: `python -m sim.corpc` (HTTP on $PORT, default 8080, plus the website and store loop)."""
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
from .engine import CorpC

log = logging.getLogger("sim.corpc")


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
    store = (CosmosStore(env["SIM_COSMOS_URL"], env.get("SIM_COSMOS_DB", "corpc"))
             if env.get("SIM_COSMOS_URL") else MemoryStore())
    engine = CorpC(store, RealClock(), gateway_url=env["CORPC_GATEWAY_URL"],
                   api_key=load_secret("partner-corpc-api-key"), upload=blob_uploader(env["CORPC_LANDING_URL"]),
                   web_orders_per_day=float(env.get("CORPC_WEB_ORDERS_PER_DAY", "60")),
                   return_rate=float(env.get("CORPC_RETURN_RATE", "0.06")),
                   speed=float(env.get("SIM_SPEED", "1")))

    def loop() -> None:
        while True:
            try:
                engine.tick()
            except Exception:  # noqa: BLE001 - keep taking orders; the next tick retries
                log.exception("tick failed")
            time.sleep(float(env.get("CORPC_TICK_S", "30")))

    def start() -> None:
        threading.Thread(target=loop, daemon=True, name="corpc-oms").start()

    uvicorn.run(create_app(engine, load_secret("corpc-inbound-api-key"), on_startup=[start]), host="0.0.0.0",
                port=int(env.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
