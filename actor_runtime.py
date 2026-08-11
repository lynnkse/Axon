#!/usr/bin/env python3
import logging

from actor_model.runtime import ActorRuntime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [actor_runtime] %(levelname)s %(message)s")

if __name__ == "__main__":
    runtime = ActorRuntime()
    logging.getLogger(__name__).info(
        "actor runtime starting, worker_id=%s", runtime.worker.owner)
    runtime.run()
