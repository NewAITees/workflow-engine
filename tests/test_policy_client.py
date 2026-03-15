"""Tests for shared/policy_client.py."""

from unittest.mock import MagicMock

import pytest

from shared.policy_client import EmbeddingBackend, OpenAIEmbeddingBackend, PolicyClient
from shared.policy_store import (
    STRENGTH_HIGH,
    STRENGTH_LOW,
    STRENGTH_MEDIUM,
    Policy,
    PolicyStore,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path) -> PolicyStore:
    db = PolicyStore(str(tmp_path / "test_policies.db"))
    yield db
    db.close()


@pytest.fixture
def client(store: PolicyStore) -> PolicyClient:
    return PolicyClient(store)


def _insert_active(
    store: PolicyStore,
    title: str = "Policy",
    strength: str = STRENGTH_MEDIUM,
    tags: list[str] | None = None,
) -> str:
    pid = store.insert_candidate(
        {
            "title": title,
            "why": "Because it matters",
            "rules": ["Do the thing"],
            "strength": strength,
            "trigger_tags": tags or ["bugfix"],
            "trigger_conditions": [],
        }
    )
    store.approve(pid)
    return pid


# ── EmbeddingBackend ABC ──────────────────────────────────────────────────────


class TestEmbeddingBackendABC:
    def test_cannot_instantiate_abstract_class(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingBackend()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_embed(self) -> None:
        class NoEmbed(EmbeddingBackend):
            pass

        with pytest.raises(TypeError):
            NoEmbed()  # type: ignore[abstract]

    def test_valid_concrete_subclass(self) -> None:
        class MockBackend(EmbeddingBackend):
            def embed(self, text: str) -> list[float]:
                return [0.1, 0.2, 0.3]

        backend = MockBackend()
        assert backend.embed("hello") == [0.1, 0.2, 0.3]


# ── OpenAIEmbeddingBackend ────────────────────────────────────────────────────


class TestOpenAIEmbeddingBackend:
    def test_raises_import_error_if_openai_missing(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "openai":
                raise ImportError("No module named 'openai'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        with pytest.raises(ImportError, match="openai package is required"):
            OpenAIEmbeddingBackend()


# ── PolicyClient: tag search (Phase A) ───────────────────────────────────────


class TestPolicyClientTagSearch:
    def test_returns_empty_for_empty_store(self, client: PolicyClient) -> None:
        assert client.get_policies_for_task("some spec") == []

    def test_returns_active_policies(
        self, store: PolicyStore, client: PolicyClient
    ) -> None:
        pid = _insert_active(store)
        results = client.get_policies_for_task("spec text")
        assert any(p.id == pid for p in results)

    def test_draft_not_returned(self, store: PolicyStore, client: PolicyClient) -> None:
        store.insert_candidate(
            {
                "title": "Draft policy",
                "why": "reason",
                "rules": ["rule"],
                "strength": STRENGTH_MEDIUM,
                "trigger_tags": ["bugfix"],
                "trigger_conditions": [],
            }
        )
        assert client.get_policies_for_task("spec") == []

    def test_tag_filter_applied(self, store: PolicyStore, client: PolicyClient) -> None:
        bugfix_id = _insert_active(store, title="Bugfix policy", tags=["bugfix"])
        refactor_id = _insert_active(store, title="Refactor policy", tags=["refactor"])

        results = client.get_policies_for_task("spec", tags=["bugfix"])
        ids = [p.id for p in results]
        assert bugfix_id in ids
        assert refactor_id not in ids

    def test_strength_filter_excludes_low(
        self, store: PolicyStore, client: PolicyClient
    ) -> None:
        low_id = _insert_active(store, title="Low", strength=STRENGTH_LOW)
        high_id = _insert_active(store, title="High", strength=STRENGTH_HIGH)

        results = client.get_policies_for_task("spec")
        ids = [p.id for p in results]
        assert high_id in ids
        assert low_id not in ids

    def test_limit_respected(self, store: PolicyStore, client: PolicyClient) -> None:
        for i in range(10):
            _insert_active(store, title=f"Policy {i}")

        results = client.get_policies_for_task("spec", limit=3)
        assert len(results) == 3

    def test_returns_policy_objects(
        self, store: PolicyStore, client: PolicyClient
    ) -> None:
        _insert_active(store)
        results = client.get_policies_for_task("spec")
        assert all(isinstance(p, Policy) for p in results)


# ── PolicyClient: min_strength configuration ─────────────────────────────────


class TestPolicyClientMinStrength:
    def test_custom_min_strength_low_includes_all(self, store: PolicyStore) -> None:
        client = PolicyClient(store, min_strength=STRENGTH_LOW)
        low_id = _insert_active(store, title="Low", strength=STRENGTH_LOW)
        results = client.get_policies_for_task("spec")
        assert any(p.id == low_id for p in results)

    def test_custom_min_strength_high_excludes_medium(self, store: PolicyStore) -> None:
        client = PolicyClient(store, min_strength=STRENGTH_HIGH)
        medium_id = _insert_active(store, title="Medium", strength=STRENGTH_MEDIUM)
        high_id = _insert_active(store, title="High", strength=STRENGTH_HIGH)

        results = client.get_policies_for_task("spec")
        ids = [p.id for p in results]
        assert high_id in ids
        assert medium_id not in ids


# ── PolicyClient: vector search fallback ─────────────────────────────────────


class TestPolicyClientVectorFallback:
    def test_falls_back_to_tag_search_when_embed_raises(
        self, store: PolicyStore
    ) -> None:
        backend = MagicMock(spec=EmbeddingBackend)
        backend.embed.side_effect = RuntimeError("embed failed")

        # Force _vec_enabled True so vector path is attempted
        store._vec_enabled = True
        client = PolicyClient(store, embedding_backend=backend)

        pid = _insert_active(store)
        results = client.get_policies_for_task("spec", tags=["bugfix"])
        # Fallback to tag search should still find the policy
        assert any(p.id == pid for p in results)

    def test_uses_tag_search_when_no_backend(
        self, store: PolicyStore, client: PolicyClient
    ) -> None:
        # No embedding backend → always tag search regardless of vec_enabled
        store._vec_enabled = True
        pid = _insert_active(store, tags=["bugfix"])
        results = client.get_policies_for_task("spec", tags=["bugfix"])
        assert any(p.id == pid for p in results)

    def test_uses_tag_search_when_vec_disabled(self, store: PolicyStore) -> None:
        backend = MagicMock(spec=EmbeddingBackend)
        store._vec_enabled = False
        client = PolicyClient(store, embedding_backend=backend)

        pid = _insert_active(store, tags=["bugfix"])
        results = client.get_policies_for_task("spec", tags=["bugfix"])
        # embed() should NOT have been called
        backend.embed.assert_not_called()
        assert any(p.id == pid for p in results)
