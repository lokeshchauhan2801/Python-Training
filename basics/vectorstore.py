"""ChromaDB-backed vector store (ported from the sibling django-unfold project).

The store never constructs a Chroma client at import time. The client is built
lazily on first use via the ``client`` property, reading CHROMA_HOST/CHROMA_PORT/
CHROMA_PATH from settings. This keeps the project import-safe (``check``/
``migrate`` run with no Chroma on disk and no server reachable). Product
telemetry is disabled through ``DisabledProductTelemetry``.

No Django model imports here; this layer stays infrastructure-only.
"""
from __future__ import annotations

from pathlib import Path

from chromadb.errors import InvalidCollectionException, NotFoundError
from chromadb.telemetry.product import ProductTelemetryClient, ProductTelemetryEvent
from overrides import override

# A read against a collection that was never created (no documents have been
# indexed yet) is a NORMAL "zero hits" case, not an error. Chroma signals the
# missing collection with one of these depending on the client version.
_MISSING_COLLECTION_ERRORS = (InvalidCollectionException, NotFoundError, ValueError)


class DisabledProductTelemetry(ProductTelemetryClient):
    """Chroma telemetry client that swallows every event."""

    @override
    def capture(self, event: ProductTelemetryEvent) -> None:
        return None


class ChromaVectorStore:
    """ChromaDB-backed vector store with a lazy client.

    Pass explicit host/port/persistent_path to override, otherwise the values
    are read from Django settings on construction. The underlying chromadb
    client is only created on first access of ``client``.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        persistent_path: str | Path | None = None,
    ):
        from django.conf import settings

        self._host = host if host is not None else getattr(settings, "CHROMA_HOST", "")
        self._port = port or getattr(settings, "CHROMA_PORT", 8000)
        self._persistent_path = Path(
            persistent_path
            or getattr(settings, "CHROMA_PATH", settings.BASE_DIR / "chroma_data")
        )
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import chromadb
            from chromadb.config import Settings

            telemetry_settings = {
                "anonymized_telemetry": False,
                "chroma_product_telemetry_impl": (
                    "basics.vectorstore.DisabledProductTelemetry"
                ),
                "chroma_telemetry_impl": (
                    "basics.vectorstore.DisabledProductTelemetry"
                ),
            }
            if self._host:
                self._client = chromadb.HttpClient(
                    host=self._host,
                    port=self._port,
                    settings=Settings(**telemetry_settings),
                )
            else:
                self._persistent_path.mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(
                    path=str(self._persistent_path),
                    settings=Settings(**telemetry_settings),
                )
        return self._client

    def add_documents(self, documents: list[dict], collection: str) -> list[str]:
        # Callers pass a deterministic per-chunk id (``f"{document_id}:{chunk_index}"``);
        # ``col.add(ids=...)`` upserts by id, so re-adding the same ids overwrites
        # in place (no duplicate vectors).
        col = self.client.get_or_create_collection(
            collection,
            metadata={"hnsw:space": "cosine"},
        )
        ids = [str(doc["id"]) for doc in documents]
        embeddings = [doc["embedding"] for doc in documents]
        metadatas = [doc.get("metadata", {}) for doc in documents]
        texts = [doc.get("text", "") for doc in documents]
        col.add(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=texts)
        return ids

    def similarity_search(
        self,
        query_embedding: list[float],
        collection: str,
        k: int = 5,
        filter: dict | None = None,
    ) -> list[dict]:
        try:
            col = self.client.get_collection(collection)
        except _MISSING_COLLECTION_ERRORS:
            # Collection never created (no documents indexed yet) => no hits.
            # Do NOT auto-create the collection on a read.
            return []
        if col.count() == 0:
            return []
        results = col.query(
            query_embeddings=[query_embedding],
            n_results=min(k, col.count()),
            where=filter,
        )
        out = []
        for i, doc_id in enumerate(results["ids"][0]):
            out.append(
                {
                    "id": doc_id,
                    "text": results["documents"][0][i],
                    "metadata": results["metadatas"][0][i],
                    "distance": results["distances"][0][i],
                }
            )
        return out

    def get_embeddings(self, ids: list[str], collection: str) -> dict[str, list[float]]:
        if not ids:
            return {}
        try:
            col = self.client.get_collection(collection)
        except _MISSING_COLLECTION_ERRORS:
            # Missing collection => no stored embeddings.
            return {}
        result = col.get(ids=ids, include=["embeddings"])
        return {
            vector_id: [float(value) for value in embedding]
            for vector_id, embedding in zip(
                result["ids"],
                result["embeddings"],
                strict=True,
            )
            if embedding is not None
        }

    def delete_collection(self, collection: str) -> None:
        self.client.delete_collection(collection)

    def delete_documents(self, ids: list[str], collection: str) -> None:
        col = self.client.get_or_create_collection(collection)
        col.delete(ids=ids)
