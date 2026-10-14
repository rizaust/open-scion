"""Generation-pruned ancestry and merge-base queries."""

from __future__ import annotations

import heapq

from .cache import MetadataCache


class CommitGraph(MetadataCache):
    """Keep traversal state local while sharing immutable metadata."""

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
