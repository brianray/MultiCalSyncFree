"""Key/value JSON storage.

Two backends share one interface:

* ``MemoryStore`` -- dict-backed, used by tests and local development.
* ``GCSStore``    -- Google Cloud Storage via its JSON REST API. Every record is
  a small JSON object; the bucket is the system of record for users, links,
  sync cursors, event mappings, subscriptions and the error log.

GCS is used directly through its REST API with a short-lived access token from
the Cloud Run metadata server, so there is no heavy SDK dependency and the
client can be unit-tested with a mock transport.
"""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Iterable
from urllib.parse import quote

from .http import httpx

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


class LockHeld(Exception):
    """Raised when a sync lock is already held by another worker."""


class Store(ABC):
    @abstractmethod
    def get(self, key: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def put(self, key: str, value: dict[str, Any]) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]: ...

    @abstractmethod
    def try_lock(self, key: str, ttl_seconds: int = 300) -> bool:
        """Atomically create a lock object. Returns False if it exists and is fresh."""

    @abstractmethod
    def unlock(self, key: str) -> None: ...

    # Convenience helpers -------------------------------------------------
    def get_many(self, prefix: str) -> list[dict[str, Any]]:
        out = []
        for k in self.list_keys(prefix):
            v = self.get(k)
            if v is not None:
                out.append(v)
        return out


class MemoryStore(Store):
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, float] = {}
        self._mutex = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._mutex:
            v = self._data.get(key)
            return json.loads(json.dumps(v)) if v is not None else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._mutex:
            self._data[key] = json.loads(json.dumps(value))

    def delete(self, key: str) -> None:
        with self._mutex:
            self._data.pop(key, None)

    def list_keys(self, prefix: str) -> list[str]:
        with self._mutex:
            return sorted(k for k in self._data if k.startswith(prefix))

    def try_lock(self, key: str, ttl_seconds: int = 300) -> bool:
        with self._mutex:
            now = time.time()
            expires = self._locks.get(key)
            if expires and expires > now:
                return False
            self._locks[key] = now + ttl_seconds
            return True

    def unlock(self, key: str) -> None:
        with self._mutex:
            self._locks.pop(key, None)


class GCSStore(Store):
    """Minimal GCS JSON-API client. Objects live under ``<prefix>/<key>.json``."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "calsync",
        client: httpx.Client | None = None,
        token_provider=None,
    ) -> None:
        if not bucket:
            raise ValueError("GCS_BUCKET is required when STORAGE_BACKEND=gcs")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client or httpx.Client(timeout=20.0)
        self._token_provider = token_provider or self._metadata_token
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # --- auth -------------------------------------------------------------
    def _metadata_token(self) -> tuple[str, int]:
        # Local dev escape hatch: `gcloud auth print-access-token`
        env_token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        if env_token:
            return env_token, 3000
        r = self._client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        r.raise_for_status()
        body = r.json()
        return body["access_token"], int(body.get("expires_in", 3600))

    def _headers(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_expiry - 60:
            self._token, ttl = self._token_provider()
            self._token_expiry = time.time() + ttl
        return {"Authorization": f"Bearer {self._token}"}

    # --- paths -------------------------------------------------------------
    def _object_name(self, key: str) -> str:
        return f"{self.prefix}/{key}.json"

    def _object_url(self, key: str) -> str:
        return f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(self._object_name(key), safe='')}"

    def _upload_url(self) -> str:
        return f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o"

    # --- interface ---------------------------------------------------------
    def get(self, key: str) -> dict[str, Any] | None:
        r = self._client.get(self._object_url(key), params={"alt": "media"}, headers=self._headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def put(self, key: str, value: dict[str, Any], if_generation_match: int | None = None) -> None:
        params: dict[str, Any] = {"uploadType": "media", "name": self._object_name(key)}
        if if_generation_match is not None:
            params["ifGenerationMatch"] = if_generation_match
        r = self._client.post(
            self._upload_url(),
            params=params,
            content=json.dumps(value).encode(),
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        if r.status_code == 412 and if_generation_match is not None:
            raise LockHeld(key)
        r.raise_for_status()

    def delete(self, key: str) -> None:
        r = self._client.delete(self._object_url(key), headers=self._headers())
        if r.status_code not in (204, 404):
            r.raise_for_status()

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        page_token = None
        full_prefix = f"{self.prefix}/{prefix}"
        while True:
            params = {"prefix": full_prefix, "fields": "items(name),nextPageToken", "maxResults": 1000}
            if page_token:
                params["pageToken"] = page_token
            r = self._client.get(
                f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o",
                params=params,
                headers=self._headers(),
            )
            r.raise_for_status()
            body = r.json()
            for item in body.get("items", []):
                name: str = item["name"]
                if name.endswith(".json"):
                    keys.append(name[len(self.prefix) + 1 : -len(".json")])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return sorted(keys)

    def try_lock(self, key: str, ttl_seconds: int = 300) -> bool:
        """Lock = create-if-absent (ifGenerationMatch=0). Stale locks are reclaimed."""
        lock_key = f"locks/{key}"
        now = time.time()
        try:
            self.put(lock_key, {"acquired": now, "expires": now + ttl_seconds}, if_generation_match=0)
            return True
        except LockHeld:
            existing = self.get(lock_key)
            if existing and existing.get("expires", 0) < now:
                # Stale: delete and retry once.
                self.delete(lock_key)
                try:
                    self.put(lock_key, {"acquired": now, "expires": now + ttl_seconds}, if_generation_match=0)
                    return True
                except LockHeld:
                    return False
            return False

    def unlock(self, key: str) -> None:
        self.delete(f"locks/{key}")


def build_store(settings) -> Store:
    if settings.storage_backend == "gcs":
        return GCSStore(settings.gcs_bucket, settings.gcs_prefix)
    return MemoryStore()


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
