"""Shared fixtures: an in-memory app context and a fake calendar provider."""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("STORAGE_BACKEND", "memory")
os.environ.setdefault("ERROR_LOG_PATH", os.path.join(os.path.dirname(__file__), ".errors.log"))

from app.config import Settings, reset_settings  # noqa: E402
from app.models import Account, Link, User, new_id  # noqa: E402
from app.providers.base import Calendar, CalendarProvider, CursorExpired, Event, ProviderError  # noqa: E402
from app.services import AppContext, set_ctx  # noqa: E402
from app.storage import MemoryStore  # noqa: E402


class FakeProvider(CalendarProvider):
    """In-memory calendar that behaves like a real provider for the engine.

    * ``changes`` records every change in order; ``list_changes`` with a cursor
      returns only the entries since that cursor (cursor = index into the log).
    * Full listings return every live event (and nothing deleted), like a
      window listing would.
    """

    name = "fake"

    def __init__(self, label: str = "fake") -> None:
        super().__init__("id", "secret", "http://cb", client=None)
        self.label = label
        self.events: dict[str, dict[str, Event]] = {}  # calendar -> id -> event
        self.log: dict[str, list[Event]] = {}
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()  # method names to fail
        self.expire_cursor = False
        self._counter = 0

    # test helpers --------------------------------------------------------------
    def add(self, cal: str, event_id: str | None = None, **kw) -> Event:
        self._counter += 1
        eid = event_id or f"{self.label}-{self._counter}"
        ev = Event(id=eid, **kw)
        self.events.setdefault(cal, {})[eid] = ev
        self.log.setdefault(cal, []).append(ev)
        return ev

    def change(self, cal: str, event_id: str, **kw) -> Event:
        ev = self.events[cal][event_id]
        for k, v in kw.items():
            setattr(ev, k, v)
        self.log.setdefault(cal, []).append(ev)
        return ev

    def remove(self, cal: str, event_id: str) -> None:
        ev = self.events[cal].pop(event_id)
        self.log.setdefault(cal, []).append(Event(id=ev.id, deleted=True))

    # provider interface -------------------------------------------------------------
    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on:
            raise ProviderError(f"{self.label}: simulated {method} failure", 500)

    def user_info(self):
        return {"email": f"{self.label}@example.com", "name": self.label}

    def list_calendars(self):
        return [Calendar(id="primary", name="Primary", primary=True), Calendar(id="other", name="Other")]

    def list_changes(self, calendar_id, cursor, window_start, window_end):
        self.calls.append(("list_changes", calendar_id, cursor))
        self._maybe_fail("list_changes")
        log = self.log.setdefault(calendar_id, [])
        if cursor:
            if self.expire_cursor:
                self.expire_cursor = False
                raise CursorExpired("stale")
            idx = int(cursor.split(":")[1])
            return [Event(**e.to_dict()) for e in log[idx:]], f"{calendar_id}:{len(log)}"
        live = [Event(**e.to_dict()) for e in self.events.get(calendar_id, {}).values()]
        return live, f"{calendar_id}:{len(log)}"

    def get_event(self, calendar_id, event_id):
        ev = self.events.get(calendar_id, {}).get(event_id)
        return Event(**ev.to_dict()) if ev else None

    def create_event(self, calendar_id, event):
        self.calls.append(("create", calendar_id, event.title))
        self._maybe_fail("create_event")
        self._counter += 1
        eid = f"{self.label}-m{self._counter}"
        ev = Event(**{**event.to_dict(), "id": eid})
        ev.is_mirror = "[calsync:ref=" in ev.description
        self.events.setdefault(calendar_id, {})[eid] = ev
        self.log.setdefault(calendar_id, []).append(ev)
        return eid

    def update_event(self, calendar_id, event_id, event):
        self.calls.append(("update", calendar_id, event_id))
        self._maybe_fail("update_event")
        if event_id not in self.events.get(calendar_id, {}):
            raise ProviderError("vanished", 404)
        ev = Event(**{**event.to_dict(), "id": event_id})
        ev.is_mirror = "[calsync:ref=" in ev.description
        self.events[calendar_id][event_id] = ev
        self.log.setdefault(calendar_id, []).append(ev)

    def delete_event(self, calendar_id, event_id):
        self.calls.append(("delete", calendar_id, event_id))
        self._maybe_fail("delete_event")
        ev = self.events.get(calendar_id, {}).pop(event_id, None)
        if ev:
            self.log.setdefault(calendar_id, []).append(Event(id=event_id, deleted=True))

    def subscribe(self, calendar_id, callback_url, channel_id, secret):
        self.calls.append(("subscribe", calendar_id, channel_id))
        self._maybe_fail("subscribe")
        return f"res-{channel_id}", time.time() + 7 * 86400

    def renew(self, provider_sub_id, channel_id):
        self.calls.append(("renew", provider_sub_id))
        self._maybe_fail("renew")
        return time.time() + 3 * 86400

    def unsubscribe(self, provider_sub_id, channel_id):
        self.calls.append(("unsubscribe", provider_sub_id))


@pytest.fixture
def providers() -> dict[str, FakeProvider]:
    return {"google": FakeProvider("g"), "microsoft": FakeProvider("m")}


@pytest.fixture
def ctx(providers, tmp_path) -> AppContext:
    reset_settings()
    settings = Settings(storage_backend="memory", error_log_path=str(tmp_path / "errors.log"), base_url="http://testserver")
    context = AppContext(settings=settings, store=MemoryStore(), provider_factory=lambda acct: providers[acct.provider])
    set_ctx(context)
    yield context
    set_ctx(None)
    reset_settings()


@pytest.fixture
def user(ctx) -> User:
    u = User(id=new_id(), email="me@example.com", name="Me")
    for prov in ("google", "microsoft"):
        a = Account(id=new_id(), user_id=u.id, provider=prov, email=f"{prov}@example.com", tokens_enc="")
        ctx.repo.save_account(a)
        u.account_ids.append(a.id)
    ctx.repo.save_user(u)
    return u


@pytest.fixture
def accounts(ctx, user):
    return {a.provider: a for a in ctx.repo.accounts_for_user(user)}


def make_link(ctx, accounts, src="google", tgt="microsoft", mode="full", **kw) -> Link:
    link = Link(
        id=new_id(), user_id=accounts[src].user_id,
        source_account_id=accounts[src].id, source_calendar_id="primary",
        target_account_id=accounts[tgt].id, target_calendar_id="primary", mode=mode, **kw,
    )
    ctx.repo.save_link(link)
    return link


@pytest.fixture
def link(ctx, accounts) -> Link:
    return make_link(ctx, accounts)


@pytest.fixture
def two_way(ctx, accounts) -> tuple[Link, Link]:
    return make_link(ctx, accounts, "google", "microsoft"), make_link(ctx, accounts, "microsoft", "google")


def timed(start="2026-11-03T17:00:00+00:00", end="2026-11-03T18:00:00+00:00", **kw) -> dict:
    return {"start": start, "end": end, "all_day": False, **kw}
