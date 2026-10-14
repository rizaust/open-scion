"""Graph correctness against an independent full-closure reference."""

import concurrent.futures
import random
import unittest

from open_scion.graph import CommitGraph


class GraphStore:
    def __init__(self):
        self.records = {}
        self.refs = {}
        self.reads = 0

    def add(self, parents=()):
        oid = f"{len(self.records) + 1:064x}"
        self.records[oid] = {"id": oid, "parents": list(parents),
                             "generation": max((self.records[p]["generation"] for p in parents), default=-1) + 1}
        return oid

    def resolve(self, revision):
        return self.refs.get(revision, revision)

    def get_changeset(self, revision):
        self.reads += 1
        return self.records[self.resolve(revision)]

    def closure(self, revision):
        stack, seen = [revision], set()
        while stack:
            oid = stack.pop()
            if oid not in seen:
                seen.add(oid)
                stack.extend(self.records[oid]["parents"])
        return seen


class GraphTests(unittest.TestCase):
    def test_random_merge_graph_matches_reference(self):
        randomizer = random.Random(713)
        store = GraphStore()
        ids = [store.add(), store.add()]
        for _ in range(300):
            ids.append(store.add(randomizer.sample(ids, randomizer.choice((1, 1, 2)))))
        graph = CommitGraph(store, cache_size=19)
        for _ in range(400):
            left, right = randomizer.sample(ids, 2)
            a, b = store.closure(left), store.closure(right)
            common = a & b
            expected = max(common, key=lambda oid: (store.records[oid]["generation"], oid)) if common else None
            self.assertEqual(graph.merge_base(left, right), expected)
            self.assertEqual(graph.is_ancestor(left, right), left in b)
        self.assertLessEqual(graph.cache_info()["entries"], 19)

    def test_recent_fork_does_not_read_old_history(self):
        store = GraphStore()
        base = store.add()
        for _ in range(10000):
            base = store.add([base])
        left, right = store.add([base]), store.add([base])
        graph = CommitGraph(store)
        self.assertEqual(graph.merge_base(left, right), base)
        self.assertLessEqual(store.reads, 4)
        self.assertTrue(graph.is_ancestor(base, right))
        self.assertFalse(graph.is_ancestor(left, right))
        self.assertLessEqual(store.reads, 4)

    def test_mutable_refs_are_resolved_on_each_query(self):
        store = GraphStore()
        base, unrelated = store.add(), store.add()
        child = store.add([base])
        graph = CommitGraph(store)
        store.refs["main"] = child
        self.assertEqual(graph.merge_base(base, "main"), base)
        store.refs["main"] = unrelated
        self.assertIsNone(graph.merge_base(base, "main"))

    def test_criss_cross_tie_and_parallel_cache(self):
        store = GraphStore()
        base = store.add()
        a, b = store.add([base]), store.add([base])
        left, right = store.add([a, b]), store.add([b, a])
        graph = CommitGraph(store, cache_size=3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: graph.merge_base(left, right), range(80)))
        self.assertEqual(results, [max(a, b)] * 80)
        self.assertLessEqual(graph.cache_info()["entries"], 3)


if __name__ == "__main__":
    unittest.main()
