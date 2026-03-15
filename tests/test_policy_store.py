"""Tests for shared/policy_store.py."""

import pytest

from shared.policy_store import (
    STATUS_ACTIVE,
    STATUS_DRAFT,
    STRENGTH_HIGH,
    STRENGTH_LOW,
    STRENGTH_MEDIUM,
    Policy,
    PolicyStore,
)


@pytest.fixture
def store(tmp_path) -> PolicyStore:
    db = PolicyStore(str(tmp_path / "test_policies.db"))
    yield db
    db.close()


def _candidate(
    title: str = "Read tests before editing",
    why: str = "Past fixes broke existing tests",
    rules: list[str] | None = None,
    strength: str = STRENGTH_MEDIUM,
    trigger_tags: list[str] | None = None,
) -> dict:
    return {
        "title": title,
        "why": why,
        "rules": rules or ["Read tests before modifying implementation"],
        "strength": strength,
        "trigger_tags": trigger_tags or ["bugfix"],
        "trigger_conditions": [],
    }


# ── insert_candidate ─────────────────────────────────────────────────────────


class TestInsertCandidate:
    def test_returns_policy_id(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        assert policy_id.startswith("policy_")

    def test_saved_as_draft(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        policy = store.get(policy_id)
        assert policy is not None
        assert policy.status == STATUS_DRAFT

    def test_stores_all_fields(self, store: PolicyStore) -> None:
        cand = _candidate(
            title="Check scope first",
            why="Scope crept in past",
            rules=["Rule A", "Rule B"],
            strength=STRENGTH_HIGH,
            trigger_tags=["refactor", "bugfix"],
        )
        policy_id = store.insert_candidate(cand, source_task="42")
        policy = store.get(policy_id)

        assert policy.title == "Check scope first"
        assert policy.why == "Scope crept in past"
        assert policy.rules == ["Rule A", "Rule B"]
        assert policy.strength == STRENGTH_HIGH
        assert policy.trigger_tags == ["refactor", "bugfix"]
        assert policy.source_task == "42"

    def test_unique_ids_per_insert(self, store: PolicyStore) -> None:
        id1 = store.insert_candidate(_candidate())
        id2 = store.insert_candidate(_candidate())
        assert id1 != id2


# ── approve ──────────────────────────────────────────────────────────────────


class TestApprove:
    def test_promotes_draft_to_active(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        result = store.approve(policy_id)

        assert result is True
        assert store.get(policy_id).status == STATUS_ACTIVE

    def test_sets_approved_by(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        store.approve(policy_id, approved_by="alice")

        assert store.get(policy_id).approved_by == "alice"

    def test_returns_false_for_unknown_id(self, store: PolicyStore) -> None:
        assert store.approve("nonexistent_id") is False

    def test_returns_false_if_already_active(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        store.approve(policy_id)

        # Second approve attempt fails (already active, not draft)
        assert store.approve(policy_id) is False


# ── query ─────────────────────────────────────────────────────────────────────


class TestQuery:
    def test_empty_store_returns_empty(self, store: PolicyStore) -> None:
        assert store.query() == []

    def test_draft_not_returned_by_default(self, store: PolicyStore) -> None:
        store.insert_candidate(_candidate())
        assert store.query(status=STATUS_ACTIVE) == []

    def test_returns_active_policies(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        store.approve(policy_id)

        results = store.query(status=STATUS_ACTIVE)
        assert len(results) == 1
        assert results[0].id == policy_id

    def test_returns_draft_policies_when_requested(self, store: PolicyStore) -> None:
        store.insert_candidate(_candidate())
        results = store.query(status=STATUS_DRAFT)
        assert len(results) == 1

    def test_strength_filter_excludes_low(self, store: PolicyStore) -> None:
        low_id = store.insert_candidate(_candidate(strength=STRENGTH_LOW))
        high_id = store.insert_candidate(_candidate(strength=STRENGTH_HIGH))
        store.approve(low_id)
        store.approve(high_id)

        results = store.query(status=STATUS_ACTIVE, strength=STRENGTH_MEDIUM)
        ids = [p.id for p in results]
        assert high_id in ids
        assert low_id not in ids

    def test_tag_filter_returns_matching(self, store: PolicyStore) -> None:
        id1 = store.insert_candidate(_candidate(trigger_tags=["bugfix"]))
        id2 = store.insert_candidate(_candidate(trigger_tags=["refactor"]))
        store.approve(id1)
        store.approve(id2)

        results = store.query(status=STATUS_ACTIVE, tags=["bugfix"])
        ids = [p.id for p in results]
        assert id1 in ids
        assert id2 not in ids

    def test_limit_respected(self, store: PolicyStore) -> None:
        for i in range(5):
            pid = store.insert_candidate(_candidate(title=f"Policy {i}"))
            store.approve(pid)

        results = store.query(status=STATUS_ACTIVE, limit=3)
        assert len(results) == 3


# ── increment counters ────────────────────────────────────────────────────────


class TestCounters:
    def test_fired_count_increments(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        store.increment_fired(policy_id)
        store.increment_fired(policy_id)

        assert store.get(policy_id).fired_count == 2

    def test_accepted_count_increments(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        store.increment_accepted(policy_id)

        assert store.get(policy_id).accepted_count == 1

    def test_counters_independent(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate())
        store.increment_fired(policy_id)
        store.increment_fired(policy_id)
        store.increment_accepted(policy_id)

        policy = store.get(policy_id)
        assert policy.fired_count == 2
        assert policy.accepted_count == 1


# ── get ───────────────────────────────────────────────────────────────────────


class TestGet:
    def test_returns_none_for_missing_id(self, store: PolicyStore) -> None:
        assert store.get("does_not_exist") is None

    def test_returns_policy_for_valid_id(self, store: PolicyStore) -> None:
        policy_id = store.insert_candidate(_candidate(title="My policy"))
        policy = store.get(policy_id)
        assert isinstance(policy, Policy)
        assert policy.title == "My policy"


# ── persistence ───────────────────────────────────────────────────────────────


class TestPersistence:
    def test_data_survives_reconnect(self, tmp_path) -> None:
        db_path = str(tmp_path / "persist.db")

        store1 = PolicyStore(db_path)
        policy_id = store1.insert_candidate(_candidate(title="Persistent policy"))
        store1.close()

        store2 = PolicyStore(db_path)
        policy = store2.get(policy_id)
        store2.close()

        assert policy is not None
        assert policy.title == "Persistent policy"
