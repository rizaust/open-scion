"""Bounded shared cache for immutable graph metadata."""

from __future__ import annotations

from collections import OrderedDict
import threading


class MetadataCache:
    """Cache parents and generations with LRU eviction."""

    def __init__(self, store, cache_size: int = 4096):
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 1:
            raise ValueError("Graph cache size must be a positive integer")
        self.store = store
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def _node(self, oid: str) -> tuple[int, tuple[str, ...]]:
        with self._lock:
            found = self._cache.get(oid)
            if found is not None:
                self._cache.move_to_end(oid)
                return found
        record = self.store.get_changeset(oid)
        found = (record["generation"], tuple(record["parents"]))
        with self._lock:
            self._cache[oid] = found
            self._cache.move_to_end(oid)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return found

    def cache_info(self) -> dict:
        with self._lock:
            return {"entries": len(self._cache), "capacity": self.cache_size}
