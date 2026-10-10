"""The brain's storage interface, on the memory store and on Firestore.

The Firestore half runs only against the emulator:
    firebase emulators:exec --only firestore --project demo-shadow "pytest tests/test_store.py"
"""
import os
import uuid

import pytest

from brain.app.store import MemoryStore


def firestore_store():
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("Firestore emulator not running")
    from brain.app.store import FirestoreStore
    return FirestoreStore(project=os.environ.get("GCLOUD_PROJECT", "demo-shadow"))


@pytest.fixture(params=["memory", "firestore"])
def store(request):
    return MemoryStore() if request.param == "memory" else firestore_store()


def test_documents_round_trip_and_are_isolated_copies(store):
    key = uuid.uuid4().hex
    assert store.get("things", key) is None
    data = {"name": "a", "nested": {"n": 1}, "items": [1, 2]}
    store.set("things", key, data)
    data["nested"]["n"] = 99  # changing the caller's dict must not change what is stored
    assert store.get("things", key) == {"name": "a", "nested": {"n": 1}, "items": [1, 2]}
    store.update("things", key, {"name": "b", "extra": True})
    assert store.get("things", key) == {"name": "b", "nested": {"n": 1}, "items": [1, 2], "extra": True}
    store.delete("things", key)
    assert store.get("things", key) is None


def test_update_creates_a_missing_document(store):
    key = uuid.uuid4().hex
    store.update("things", key, {"status": "queued"})
    assert store.get("things", key) == {"status": "queued"}


def test_query_filters_orders_and_limits(store):
    group = uuid.uuid4().hex
    ids = [store.add("jobs", {"group": group, "status": status, "created": created})
           for status, created in (("queued", 3), ("done", 1), ("queued", 2), ("queued", 5))]
    assert len(set(ids)) == 4
    queued = store.query("jobs", {"group": group, "status": "queued"}, "created", False, 2)
    assert [doc["created"] for doc in queued] == [2, 3]  # oldest first, limited
    assert all(doc["id"] in ids for doc in queued)
    newest = store.query("jobs", {"group": group}, "created", True, 1)
    assert newest[0]["created"] == 5
    assert store.query("jobs", {"group": group, "status": "missing"}) == []


def test_append_adds_to_a_list_field(store):
    key = uuid.uuid4().hex
    store.set("commands", key, {"events": []})
    store.append("commands", key, "events", {"n": 1})
    store.append("commands", key, "events", {"n": 2})
    assert store.get("commands", key)["events"] == [{"n": 1}, {"n": 2}]
    other = uuid.uuid4().hex
    store.append("commands", other, "events", {"n": 1})  # the document need not exist yet
    assert store.get("commands", other)["events"] == [{"n": 1}]
