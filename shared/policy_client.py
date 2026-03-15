"""Policy Client: retrieves relevant policies for a task spec.

Provides a layered search strategy:
  Phase A (current): tag-based filtering via PolicyStore.query()
  Phase B (future):  vector search via sqlite-vec + OpenAI embeddings
  Phase C (future):  local embedding model drop-in

The EmbeddingBackend abstraction allows backend swaps without changing
PolicyClient's interface.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from shared.policy_store import STATUS_ACTIVE, STRENGTH_MEDIUM, Policy, PolicyStore

logger = logging.getLogger(__name__)


# ── Embedding backend abstraction ─────────────────────────────────────────────


class EmbeddingBackend(ABC):
    """Abstract embedding backend — swap implementations without changing client."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return a float vector for *text*."""


class OpenAIEmbeddingBackend(EmbeddingBackend):
    """
    OpenAI text-embedding-3-small backend (Phase B).

    Requires: pip install openai
    Usage:
        backend = OpenAIEmbeddingBackend(api_key="sk-...")
        client  = PolicyClient(store, embedding_backend=backend)
    """

    MODEL = "text-embedding-3-small"

    def __init__(self, api_key: str | None = None):
        try:
            import openai  # type: ignore[import-untyped, unused-ignore]

            self._client = openai.OpenAI(api_key=api_key)
        except ImportError as e:
            raise ImportError(
                "openai package is required for OpenAIEmbeddingBackend. "
                "Install with: pip install openai"
            ) from e

    def embed(self, text: str) -> list[float]:
        response = self._client.embeddings.create(input=text, model=self.MODEL)
        return list(response.data[0].embedding)


# ── PolicyClient ──────────────────────────────────────────────────────────────


def _policy_embed_text(policy: Policy) -> str:
    """Build the text to embed for a policy — title + why + rules."""
    rules_text = " ".join(policy.rules)
    return f"{policy.title}. {policy.why}. {rules_text}"


class PolicyClient:
    """
    High-level interface for fetching policies relevant to a task spec.

    Search strategy (falls back gracefully):
    1. Tag filter  — always available, zero extra dependencies
    2. Vector search — used when sqlite-vec is loaded AND an EmbeddingBackend
                       is provided; returns top-N by cosine similarity

    Args:
        store:             PolicyStore instance (caller owns lifecycle).
        embedding_backend: Optional EmbeddingBackend for vector search.
                           If None, tag-based search only.
        min_strength:      Minimum policy strength to include
                           (default: "medium").
    """

    def __init__(
        self,
        store: PolicyStore,
        embedding_backend: EmbeddingBackend | None = None,
        min_strength: str = STRENGTH_MEDIUM,
    ) -> None:
        self._store = store
        self._embedding_backend = embedding_backend
        self._min_strength = min_strength

    # ── Public API ────────────────────────────────────────────────────────────

    def get_policies_for_task(
        self,
        task_spec: str,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[Policy]:
        """
        Return up to *limit* active policies relevant to *task_spec*.

        Search order:
        1. If an EmbeddingBackend is set and the store has vec support:
           vector search (top-20) → strength filter → trim to limit
        2. Otherwise: tag-based filter → trim to limit

        Args:
            task_spec: The task/spec text to search against.
            tags:      Optional list of trigger tags to narrow results.
            limit:     Maximum policies to return (default 5).

        Returns:
            List of Policy objects ordered by relevance.
        """
        if self._embedding_backend is not None and self._store._vec_enabled:
            return self._vector_search(task_spec, tags=tags, limit=limit)
        return self._tag_search(tags=tags, limit=limit)

    # ── Search strategies ─────────────────────────────────────────────────────

    def _tag_search(
        self,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[Policy]:
        """Phase A: pure tag + strength filter via PolicyStore.query()."""
        results = self._store.query(
            status=STATUS_ACTIVE,
            strength=self._min_strength,
            tags=tags,
            limit=limit,
        )
        logger.debug(f"tag_search returned {len(results)} policies (tags={tags})")
        return results

    def _vector_search(
        self,
        task_spec: str,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[Policy]:
        """
        Phase B: embed task_spec → sqlite-vec cosine search → strength filter.

        Falls back to tag search if embedding fails.
        """
        try:
            task_vec = self._embedding_backend.embed(task_spec)  # type: ignore[union-attr]
        except Exception as e:
            logger.warning(f"Embedding failed, falling back to tag search: {e}")
            return self._tag_search(tags=tags, limit=limit)

        try:
            import json

            vec_blob = json.dumps(task_vec)
            rows = self._store._conn.execute(
                """
                SELECT p.* FROM policies p
                JOIN policies_vec v ON p.id = v.id
                WHERE p.status = ?
                ORDER BY vec_distance_cosine(v.embedding, ?)
                LIMIT 20
                """,
                (STATUS_ACTIVE, vec_blob),
            ).fetchall()
        except Exception as e:
            logger.warning(f"Vector search failed, falling back to tag search: {e}")
            return self._tag_search(tags=tags, limit=limit)

        policies = [self._store._row_to_policy(r) for r in rows]

        # Apply strength filter
        from shared.policy_store import _STRENGTH_ORDER

        min_order = _STRENGTH_ORDER.get(self._min_strength, 0)
        policies = [
            p for p in policies if _STRENGTH_ORDER.get(p.strength, 0) >= min_order
        ]

        # Apply tag filter
        if tags:
            tag_set = set(tags)
            policies = [p for p in policies if tag_set & set(p.trigger_tags)]

        result = policies[:limit]
        logger.debug(f"vector_search returned {len(result)} policies")
        return result
