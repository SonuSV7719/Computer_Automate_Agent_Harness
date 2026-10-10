"""Document storage for the brain: Firestore in the cloud, memory for tests and local runs.

Both expose the same small interface, so nothing else knows which one is in use.
"""
from __future__ import annotations

import copy
import threading
import uuid
from typing import Optional, Protocol


class Store(Protocol):
    def get(self, collection: str, doc_id: str) -> Optional[dict]: ...

    def set(self, collection: str, doc_id: str, data: dict) -> None: ...

    def update(self, collection: str, doc_id: str, data: dict) -> None: ...

    def add(self, collection: str, data: dict) -> str: ...

    def delete(self, collection: str, doc_id: str) -> None: ...

    def query(self, collection: str, filters: dict, order_by: Optional[str] = None,
              descending: bool = False, limit: int = 50) -> list[dict]:
        """Documents whose fields equal every filter. Each result carries its id as 'id'."""

    def append(self, collection: str, doc_id: str, field: str, item) -> None:
        """Add an item to a list field."""


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    def get(self, collection, doc_id):
        with self._lock:
            doc = self._data.get(collection, {}).get(doc_id)
            return copy.deepcopy(doc) if doc is not None else None

    def set(self, collection, doc_id, data):
        with self._lock:
            self._data.setdefault(collection, {})[doc_id] = copy.deepcopy(data)

    def update(self, collection, doc_id, data):
        with self._lock:
            self._data.setdefault(collection, {}).setdefault(doc_id, {}).update(copy.deepcopy(data))

    def add(self, collection, data):
        doc_id = uuid.uuid4().hex[:16]
        self.set(collection, doc_id, data)
        return doc_id

    def delete(self, collection, doc_id):
        with self._lock:
            self._data.get(collection, {}).pop(doc_id, None)

    def query(self, collection, filters, order_by=None, descending=False, limit=50):
        with self._lock:
            docs = [{**copy.deepcopy(doc), "id": doc_id} for doc_id, doc in self._data.get(collection, {}).items()
                    if all(doc.get(k) == v for k, v in filters.items())]
        if order_by:
            docs.sort(key=lambda d: d.get(order_by) or 0, reverse=descending)
        return docs[:limit]

    def append(self, collection, doc_id, field, item):
        with self._lock:
            doc = self._data.setdefault(collection, {}).setdefault(doc_id, {})
            doc.setdefault(field, []).append(copy.deepcopy(item))


class FirestoreStore:
    """Cloud Firestore in Native mode. Uses the service's own credentials on Cloud Run."""

    def __init__(self, project: Optional[str] = None, database: Optional[str] = None) -> None:
        from google.cloud import firestore

        kwargs = {}
        if project:
            kwargs["project"] = project
        if database:
            kwargs["database"] = database
        self._db = firestore.Client(**kwargs)
        self._firestore = firestore

    def get(self, collection, doc_id):
        snap = self._db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def set(self, collection, doc_id, data):
        self._db.collection(collection).document(doc_id).set(data)

    def update(self, collection, doc_id, data):
        self._db.collection(collection).document(doc_id).set(data, merge=True)

    def add(self, collection, data):
        ref = self._db.collection(collection).document()
        ref.set(data)
        return ref.id

    def delete(self, collection, doc_id):
        self._db.collection(collection).document(doc_id).delete()

    def query(self, collection, filters, order_by=None, descending=False, limit=50):
        from google.cloud.firestore_v1.base_query import FieldFilter

        ref = self._db.collection(collection)
        for field, value in filters.items():
            ref = ref.where(filter=FieldFilter(field, "==", value))
        # Sorting here instead of in the query avoids needing a composite index per filter.
        docs = [{**snap.to_dict(), "id": snap.id} for snap in ref.limit(500).stream()]
        if order_by:
            docs.sort(key=lambda d: d.get(order_by) or 0, reverse=descending)
        return docs[:limit]

    def append(self, collection, doc_id, field, item):
        self._db.collection(collection).document(doc_id).set(
            {field: self._firestore.ArrayUnion([item])}, merge=True)
