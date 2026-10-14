"""Generation-pruned graph queries over immutable changeset metadata."""

from __future__ import annotations

from collections import OrderedDict
import heapq
import threading


class CommitGraph:
    """Share a bounded metadata cache while keeping query traversal state local."""

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

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor, descendant = self.store.resolve(ancestor), self.store.resolve(descendant)
        if ancestor == descendant:
            return True
        threshold = self._node(ancestor)[0]
        pending, visited = [descendant], set()
        while pending:
            oid = pending.pop()
            if oid == ancestor:
                return True
            if oid in visited:
                continue
            visited.add(oid)
            generation, parents = self._node(oid)
            if generation > threshold:
                pending.extend(parents)
        return False

    def merge_base(self, left: str, right: str) -> str | None:
        """Find the highest-generation common ancestor, breaking ties by ID."""
        left, right = self.store.resolve(left), self.store.resolve(right)
        if left == right:
            return left
        colors, propagated, queue = {}, {}, []
        for oid, color in ((left, 1), (right, 2)):
            colors[oid] = colors.get(oid, 0) | color
            heapq.heappush(queue, (-self._node(oid)[0], oid))
        best = None
        while queue:
            negative_generation, oid = heapq.heappop(queue)
            generation = -negative_generation
            if best is not None and generation < best[0]:
                break
            color = colors[oid]
            if propagated.get(oid) == color:
                continue
            propagated[oid] = color
            if color == 3:
                best = max(best, (generation, oid)) if best else (generation, oid)
                continue
            for parent in self._node(oid)[1]:
                combined = colors.get(parent, 0) | color
                if combined != colors.get(parent, 0):
                    colors[parent] = combined
                    heapq.heappush(queue, (-self._node(parent)[0], parent))
        return best[1] if best else None

    def cache_info(self) -> dict:
        with self._lock:
            return {"entries": len(self._cache), "capacity": self.cache_size}
