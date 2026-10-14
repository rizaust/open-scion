"""Repository operation facade shared by HTTP handlers."""

from ..graph import CommitGraph
from ..store import Store
from .inspection import InspectionOperations
from .integration import IntegrationOperations


class Operations(InspectionOperations, IntegrationOperations):
    def __init__(self, store: Store):
        self.store = store
        self.graph = CommitGraph(store)
