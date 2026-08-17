"""
Retrieval-based correction memory (ADR-0001, Decision 3).

NOT yet wired into the graph — this is the stage 5 piece of the build
plan. Scaffolded now so the interface is settled before the escalation
node needs to call it.

Intended flow once implemented:
1. On human-resolved escalation: embed (question + context) and store
   {question, attempted_sql, error, human_correction} in Qdrant.
2. Before SQL generation: embed the incoming question, retrieve top-k
   similar past corrections above `settings.retrieval_similarity_threshold`,
   and inject them into the sql_generator prompt as few-shot examples.
3. If retrieval returns nothing above threshold, that low-novelty-match
   signal feeds the escalation trigger (ADR-0001, Decision 2, trigger #2).
"""

from typing import Optional

from qdrant_client import QdrantClient

from src.config import settings


class CorrectionStore:
    def __init__(self):
        self.client = QdrantClient(
            host=settings.qdrant_host, port=settings.qdrant_port
        )
        self.collection = settings.correction_collection

    def ensure_collection(self, vector_size: int = 384):
        # TODO: create collection if it doesn't exist (stage 5)
        raise NotImplementedError

    def store_correction(
        self,
        question: str,
        attempted_sql: str,
        error: Optional[str],
        human_correction: str,
    ):
        # TODO: embed question + error context, upsert into Qdrant (stage 5)
        raise NotImplementedError

    def retrieve_similar(self, question: str, top_k: int = 3):
        # TODO: embed question, query Qdrant, return matches + scores (stage 5)
        raise NotImplementedError
