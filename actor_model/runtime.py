from __future__ import annotations

import logging
import subprocess
import threading
import time

import config

from .actors import AntonActor, AxonActor, PlaceholderActor, ReflectionActor
from .registry import Registry
from .scheduler import Scheduler
from .store import SupabaseActorStore
from .worker import TransitionWorker

log = logging.getLogger(__name__)


def _reflection_model(prompt: str, timeout: int) -> str:
    """Independent tool-less model turn; never occupies the conversational PTY."""
    result = subprocess.run([config.CLAUDE_PATH, "--print", "--tools", "", prompt],
        capture_output=True, text=True, timeout=min(timeout, config.ACTOR_MAX_WALL_SECONDS),
        cwd=config.PROJECT_DIR, check=False)
    if result.returncode:
        raise RuntimeError(f"reflection model failed: {result.stderr[-500:]}")
    return result.stdout.strip()


def default_registry() -> Registry:
    registry = Registry()
    for actor in (AxonActor(), AntonActor(), ReflectionActor(_reflection_model),
                  PlaceholderActor("anplos-improvement"),
                  PlaceholderActor("axon-improvement"), PlaceholderActor("commitments")):
        registry.register(actor)
    return registry


class ActorRuntime:
    def __init__(self, store=None, owner: str | None = None) -> None:
        self.store = store or SupabaseActorStore()
        self.scheduler = Scheduler()
        self.worker = TransitionWorker(self.store, default_registry(), owner or config.ACTOR_WORKER_ID)
        self._running = threading.Event(); self._running.set()

    def run_once(self) -> bool:
        actors = self.store.list_actors()
        ranked = self.scheduler.rank(actors)
        selected = ranked[0] if ranked else None
        self.scheduler.account(actors, selected.actor_id if selected else None)
        for actor in actors:
            self.store.save_schedule(actor)
        return self.worker.activate(selected) if selected else False

    def run(self) -> None:
        last_heartbeat = time.monotonic()
        passes = 0
        while self._running.is_set():
            activated = False
            try:
                activated = self.run_once()
            except Exception: log.exception("actor runtime pass failed")
            passes += 1
            now = time.monotonic()
            if now - last_heartbeat >= 60:
                log.info("heartbeat worker_id=%s passes=%d activated_last_pass=%s",
                         self.worker.owner, passes, activated)
                last_heartbeat = now
            time.sleep(config.ACTOR_POLL_INTERVAL)

    def stop(self) -> None: self._running.clear()
