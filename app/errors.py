"""Error log: every sync failure is written to local disk *and* to the store.

Disk gives you a quick `tail -f` while debugging; the store (GCS in
production) is what the web UI reads, since Cloud Run instances are ephemeral.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class ErrorRecord:
    id: str
    ts: float
    user_id: str | None
    link_id: str | None
    stage: str
    message: str
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ErrorLog:
    def __init__(self, store: Store, path: str, keep: int = 500) -> None:
        self.store = store
        self.path = path
        self.keep = keep
        self._mutex = threading.Lock()
        self._seq = itertools.count()

    def record(
        self,
        stage: str,
        message: str,
        *,
        user_id: str | None = None,
        link_id: str | None = None,
        exc: BaseException | None = None,
        context: dict[str, Any] | None = None,
    ) -> ErrorRecord:
        rec = ErrorRecord(
            id=uuid.uuid4().hex,
            ts=time.time(),
            user_id=user_id,
            link_id=link_id,
            stage=stage,
            message=message,
            detail="".join(traceback.format_exception(exc)) if exc else "",
            context=context or {},
        )
        log.error("[%s] %s (link=%s): %s", stage, message, link_id, rec.detail[-500:] if rec.detail else "")
        self._write_disk(rec)
        try:
            # Key sorts chronologically: zero-padded ns timestamp + per-process sequence
            # (two records in the same tick must still keep insertion order).
            key = f"errors/{time.time_ns():020d}-{next(self._seq):06d}-{rec.id}"
            self.store.put(key, rec.to_dict())
        except Exception:  # pragma: no cover - never let error logging raise
            log.exception("failed to persist error record")
        return rec

    def _write_disk(self, rec: ErrorRecord) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._mutex, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec.to_dict()) + "\n")
        except OSError:  # pragma: no cover
            log.exception("failed to write error log to disk")

    def list(self, user_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        keys = self.store.list_keys("errors/")
        keys.reverse()  # newest first
        out: list[dict[str, Any]] = []
        for k in keys:
            rec = self.store.get(k)
            if rec is None:
                continue
            if user_id is not None and rec.get("user_id") != user_id:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def clear(self, user_id: str | None = None) -> int:
        n = 0
        for k in self.store.list_keys("errors/"):
            rec = self.store.get(k)
            if rec and (user_id is None or rec.get("user_id") == user_id):
                self.store.delete(k)
                n += 1
        return n

    def prune(self) -> int:
        """Keep the newest ``keep`` records in the store."""
        keys = self.store.list_keys("errors/")
        extra = keys[: max(0, len(keys) - self.keep)]
        for k in extra:
            self.store.delete(k)
        return len(extra)
