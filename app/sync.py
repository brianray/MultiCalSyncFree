"""The mirror engine.

A ``Link`` is one-way: events in the source calendar are mirrored into the
target calendar. Two links in opposite directions give a two-way sync. Loops
are prevented because every mirrored event carries a marker in its description
and the engine never mirrors an event that already carries one.

Each run:

1. takes an advisory lock on the link (so a webhook and the cron tick cannot
   run the same link concurrently),
2. asks the source provider for changes since the stored cursor (or a full
   window listing on the first run / after the cursor expires),
3. creates, updates or deletes the matching target events,
4. stores the new cursor and the source->target event-id mapping.

Failures on individual events are logged to the error log and do not stop the
run; the failing event will be retried on the next full sync or reconcile.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .models import Link, LinkState
from .providers import CursorExpired, Event, make_marker, parse_marker, strip_marker
from .providers.base import ProviderError, window
from .services import AppContext

log = logging.getLogger(__name__)

BUSY_TITLE = "Busy"


@dataclass
class SyncResult:
    link_id: str
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    full: bool = False
    locked: bool = False
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.locked:
            return "locked"
        if self.failed and (self.created or self.updated or self.deleted):
            return "partial"
        return "error" if self.failed else "ok"

    def to_dict(self) -> dict:
        return {
            "link_id": self.link_id,
            "status": self.status,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
            "full": self.full,
            "seconds": round(self.seconds, 2),
        }


def build_mirror(link: Link, src: Event) -> Event:
    """Turn a source event into the event we write to the target calendar."""
    marker = make_marker(link.id, src.id)
    if link.mode == "busy":
        return Event(
            id="",
            title=BUSY_TITLE,
            description=marker,
            location="",
            start=src.start,
            end=src.end,
            all_day=src.all_day,
            show_as=src.show_as,
            extra={"calsync_ref": f"{link.id}:{src.id}"},
        )
    body = strip_marker(src.description)
    return Event(
        id="",
        title=src.title or "(no title)",
        description=f"{body}\n\n{marker}" if body else marker,
        location=src.location,
        start=src.start,
        end=src.end,
        all_day=src.all_day,
        show_as=src.show_as,
        extra={"calsync_ref": f"{link.id}:{src.id}"},
    )


class SyncEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.repo = ctx.repo
        self.errors = ctx.errors
        self.settings = ctx.settings

    # ------------------------------------------------------------------------------
    def sync_link(self, link: Link, full: bool = False) -> SyncResult:
        res = SyncResult(link_id=link.id)
        t0 = time.time()
        lock_key = f"link-{link.id}"
        if not self.ctx.store.try_lock(lock_key, ttl_seconds=600):
            res.locked = True
            return res
        try:
            self._run(link, full, res)
        except Exception as exc:  # whole-run failure (auth, network, ...)
            res.failed += 1
            res.errors.append(str(exc))
            self.errors.record(
                "sync", f"Sync failed: {exc}", user_id=link.user_id, link_id=link.id, exc=exc,
                context={"label": link.label},
            )
        finally:
            self.ctx.store.unlock(lock_key)
        res.seconds = time.time() - t0
        link.last_sync = time.time()
        link.last_status = res.status
        self.repo.save_link(link)
        return res

    def _run(self, link: Link, full: bool, res: SyncResult) -> None:
        src_acct = self.repo.get_account(link.source_account_id)
        tgt_acct = self.repo.get_account(link.target_account_id)
        if not src_acct or not tgt_acct:
            raise ProviderError("link refers to a missing account; reconnect it")
        source = self.ctx.provider_for(src_acct)
        target = self.ctx.provider_for(tgt_acct)
        state = self.repo.get_state(link.id)

        w_start, w_end = window(self.settings.window_past_days, self.settings.window_future_days)
        cursor = None if full else state.cursor
        try:
            events, new_cursor = source.list_changes(link.source_calendar_id, cursor, w_start, w_end)
        except CursorExpired:
            cursor = None
            events, new_cursor = source.list_changes(link.source_calendar_id, None, w_start, w_end)
        res.full = cursor is None

        for ev in events:
            try:
                self._apply(link, state, target, ev, res)
            except Exception as exc:
                res.failed += 1
                res.errors.append(f"{ev.id}: {exc}")
                self.errors.record(
                    "event",
                    f"Could not mirror event {ev.id}: {exc}",
                    user_id=link.user_id,
                    link_id=link.id,
                    exc=exc,
                    context={"event_id": ev.id, "title": ev.title, "start": ev.start, "deleted": ev.deleted},
                )

        state.cursor = new_cursor or state.cursor
        self.repo.save_state(state)

    def _apply(self, link: Link, state: LinkState, target, ev: Event, res: SyncResult) -> None:
        mapping, fps = state.mapping, state.fps
        if ev.is_mirror:
            # Written by us (possibly by the reverse link). Never mirror a mirror.
            res.skipped += 1
            return
        if ev.deleted or not ev.start:
            tgt_id = mapping.pop(ev.id, None)
            fps.pop(ev.id, None)
            if tgt_id:
                target.delete_event(link.target_calendar_id, tgt_id)
                res.deleted += 1
            else:
                res.skipped += 1
            return
        mirror = build_mirror(link, ev)
        fp = mirror.fingerprint()
        tgt_id = mapping.get(ev.id)
        if tgt_id:
            if fps.get(ev.id) == fp:
                res.skipped += 1
                return
            try:
                target.update_event(link.target_calendar_id, tgt_id, mirror)
                res.updated += 1
            except ProviderError as exc:
                if exc.status != 404:
                    raise
                # Mirror was deleted on the target by hand; recreate it.
                mapping[ev.id] = target.create_event(link.target_calendar_id, mirror)
                res.created += 1
        else:
            mapping[ev.id] = target.create_event(link.target_calendar_id, mirror)
            res.created += 1
        fps[ev.id] = fp

    # ------------------------------------------------------------------------------
    def reconcile(self, link: Link) -> SyncResult:
        """Rebuild the id mapping from markers on the target and remove orphans.

        Use after the mapping is lost, after a long outage, or when a user has
        been deleting mirrors by hand. Lists both calendars for the whole window.
        """
        res = SyncResult(link_id=link.id, full=True)
        t0 = time.time()
        src_acct = self.repo.get_account(link.source_account_id)
        tgt_acct = self.repo.get_account(link.target_account_id)
        if not src_acct or not tgt_acct:
            raise ProviderError("link refers to a missing account; reconnect it")
        source = self.ctx.provider_for(src_acct)
        target = self.ctx.provider_for(tgt_acct)
        w_start, w_end = window(self.settings.window_past_days, self.settings.window_future_days)

        tgt_events, _ = target.list_changes(link.target_calendar_id, None, w_start, w_end)
        src_events, cursor = source.list_changes(link.source_calendar_id, None, w_start, w_end)
        live = {e.id for e in src_events if not e.deleted and not e.is_mirror and e.start}

        mapping: dict[str, str] = {}
        for te in tgt_events:
            if te.deleted:
                continue
            ref = parse_marker(te.description)
            if not ref or ref[0] != link.id:
                continue
            src_id = ref[1]
            if src_id in live and src_id not in mapping:
                mapping[src_id] = te.id
            else:
                try:
                    target.delete_event(link.target_calendar_id, te.id)
                    res.deleted += 1
                except Exception as exc:
                    res.failed += 1
                    self.errors.record("reconcile", f"Could not delete orphan {te.id}: {exc}", user_id=link.user_id,
                                       link_id=link.id, exc=exc)

        # Persist the rebuilt mapping with no cursor and no fingerprints, then a
        # full pass creates whatever is missing and refreshes everything mapped.
        self.repo.save_state(LinkState(link_id=link.id, cursor=None, mapping=mapping, fps={}))
        inner = self.sync_link(link, full=True)
        res.created, res.updated, res.failed = inner.created, inner.updated, res.failed + inner.failed
        res.skipped = inner.skipped
        res.seconds = time.time() - t0
        return res

    def sync_all(self, user_id: str | None = None) -> list[SyncResult]:
        out = []
        for link in self.repo.all_links():
            if not link.enabled or (user_id and link.user_id != user_id):
                continue
            out.append(self.sync_link(link))
        return out

    def sync_for_source(self, account_id: str, calendar_id: str) -> list[SyncResult]:
        return [self.sync_link(lk) for lk in self.repo.links_for_source(account_id, calendar_id)]
