"""Storage backends, token encryption and the error log."""

import json
import time

import pytest

from app.crypto import TokenCipher
from app.errors import ErrorLog
from app.http import httpx
from app.storage import GCSStore, MemoryStore


def test_memory_store_roundtrip_and_isolation():
    s = MemoryStore()
    s.put("a/1", {"x": [1, 2]})
    got = s.get("a/1")
    got["x"].append(3)  # must not leak into the store
    assert s.get("a/1") == {"x": [1, 2]}
    s.put("a/2", {"y": 1})
    s.put("b/1", {"z": 1})
    assert s.list_keys("a/") == ["a/1", "a/2"]
    assert len(s.get_many("a/")) == 2
    s.delete("a/1")
    assert s.get("a/1") is None


def test_memory_lock_ttl():
    s = MemoryStore()
    assert s.try_lock("k", ttl_seconds=1)
    assert not s.try_lock("k", ttl_seconds=1)
    s.unlock("k")
    assert s.try_lock("k", ttl_seconds=0)
    time.sleep(0.01)
    assert s.try_lock("k")  # expired lock is reclaimable


class FakeGCS:
    """Just enough of the GCS JSON API for the client to be exercised."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.requests = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if "metadata.google.internal" in url:
            return httpx.Response(200, json={"access_token": "meta-token", "expires_in": 3600})
        assert req.headers.get("Authorization") == "Bearer meta-token"
        if "/upload/storage/" in url:
            name = req.url.params["name"]
            igm = req.url.params.get("ifGenerationMatch")
            if igm == "0" and name in self.objects:
                return httpx.Response(412)
            gen = self.objects.get(name, (b"", 0))[1] + 1
            self.objects[name] = (req.content, gen)
            return httpx.Response(200, json={"name": name, "generation": gen})
        if req.method == "GET" and url.split("?")[0].endswith("/o"):
            prefix = req.url.params.get("prefix", "")
            items = [{"name": n} for n in sorted(self.objects) if n.startswith(prefix)]
            return httpx.Response(200, json={"items": items})
        name = httpx.URL(url).path.split("/o/")[-1]
        from urllib.parse import unquote
        name = unquote(name)
        if req.method == "GET":
            if name not in self.objects:
                return httpx.Response(404)
            return httpx.Response(200, content=self.objects[name][0])
        if req.method == "DELETE":
            if name not in self.objects:
                return httpx.Response(404)
            del self.objects[name]
            return httpx.Response(204)
        return httpx.Response(500)


@pytest.fixture
def gcs():
    fake = FakeGCS()
    store = GCSStore("bucket", "pfx", client=httpx.Client(transport=httpx.MockTransport(fake)))
    return store, fake


def test_gcs_store_crud_and_listing(gcs, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    store, fake = gcs
    assert store.get("users/u1") is None
    store.put("users/u1", {"email": "a@b"})
    store.put("users/u2", {"email": "c@d"})
    store.put("links/l1", {"id": "l1"})
    assert "pfx/users/u1.json" in fake.objects
    assert store.get("users/u1") == {"email": "a@b"}
    assert store.list_keys("users/") == ["users/u1", "users/u2"]
    store.delete("users/u1")
    store.delete("users/u1")  # idempotent
    assert store.list_keys("users/") == ["users/u2"]
    # Token fetched once and reused.
    assert sum("metadata.google.internal" in str(r.url) for r in fake.requests) == 1


def test_gcs_store_lock_is_create_if_absent(gcs, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    store, fake = gcs
    assert store.try_lock("link-1")
    assert not store.try_lock("link-1")
    store.unlock("link-1")
    assert store.try_lock("link-1")
    # Stale lock gets reclaimed.
    fake.objects["pfx/locks/link-2.json"] = (json.dumps({"acquired": 0, "expires": 1}).encode(), 1)
    assert store.try_lock("link-2")


def test_gcs_requires_bucket():
    with pytest.raises(ValueError):
        GCSStore("")


def test_token_cipher_roundtrip_and_key_change():
    key = TokenCipher.generate_key()
    c = TokenCipher(key)
    ct = c.encrypt('{"refresh_token":"abc"}')
    assert ct != '{"refresh_token":"abc"}' and c.decrypt(ct) == '{"refresh_token":"abc"}'
    other = TokenCipher(TokenCipher.generate_key())
    with pytest.raises(ValueError):
        other.decrypt(ct)
    # Fallback derivation is deterministic for the same secret.
    assert TokenCipher(None, "s").decrypt(TokenCipher(None, "s").encrypt("x")) == "x"


def test_error_log_disk_and_store(tmp_path):
    store = MemoryStore()
    log = ErrorLog(store, str(tmp_path / "e.log"), keep=2)
    try:
        raise ValueError("bad thing")
    except ValueError as exc:
        log.record("sync", "failed", user_id="u1", link_id="l1", exc=exc, context={"k": 1})
    log.record("auth", "other user", user_id="u2")
    log.record("auth", "newest", user_id="u1")
    lines = (tmp_path / "e.log").read_text().strip().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["message"] == "failed"
    mine = log.list(user_id="u1")
    assert [e["message"] for e in mine] == ["newest", "failed"]  # newest first
    assert "bad thing" in mine[1]["detail"] and mine[1]["context"] == {"k": 1}
    assert log.prune() == 1 and len(log.list()) == 2
    assert log.clear(user_id="u1") >= 1
    assert all(e["user_id"] != "u1" for e in log.list())
