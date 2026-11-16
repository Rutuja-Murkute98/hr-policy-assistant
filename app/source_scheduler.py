"""Sync each organisation's sources when they're due, from a thread in the API. See docs/DECISIONS.md, D59.

It looks every SOURCE_SYNC_SECONDS. A sync is claimed in the database first, so two server processes never run the same
one, and one cut off by a restart is picked up again later. The free host sleeps when idle, and then nothing syncs.
"""
import logging
import threading

from app.sources import SourceStore, sync_source

logger = logging.getLogger("hr_helpdesk")


class SourceScheduler:
    def __init__(self, registry, interval_seconds: float):
        self.registry = registry
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        """Sync every source that's due, organisation by organisation. Returns how many ran."""
        ran = 0
        for tenant_id in self.registry.organisations.active_ids():
            try:
                store = SourceStore(self.registry.engine, tenant_id)
                due = store.due()
                if not due:
                    continue  # most organisations have no sources, and their workflow needn't be built for this
                workflow = self.registry.for_tenant(tenant_id)
                for source in due:
                    if store.claim(source["id"]):
                        sync_source(store, workflow.knowledge, source, llm=workflow.llm)
                        ran += 1
            except Exception:
                # One organisation's problem mustn't stop the others' syncs
                logger.exception("Couldn't sync the sources of %s", tenant_id)
        return ran

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="source-sync")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
