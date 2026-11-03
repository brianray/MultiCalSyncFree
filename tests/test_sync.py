"""Sync engine behaviour: create / update / delete, loop prevention, errors, reconcile."""

from app.providers import make_marker, parse_marker, strip_marker
from app.providers.base import Event
from app.sync import BUSY_TITLE, SyncEngine, build_mirror

from .conftest import make_link, timed


def mirrors(provider, cal="primary"):
    return [e for e in provider.events.get(cal, {}).values() if e.is_mirror]


# --- markers ------------------------------------------------------------------------

def test_marker_roundtrip():
    m = make_marker("link1", "evt_ABC-123")
    assert parse_marker(f"Lunch with Sam\n\n{m}") == ("link1", "evt_ABC-123")
    assert parse_marker(m) == ("link1", "evt_ABC-123")
    assert parse_marker("plain text") is None
    assert strip_marker(f"Lunch with Sam\n\n{m}") == "Lunch with Sam"
    assert strip_marker(None) == ""


def test_build_mirror_full_and_busy(link):
    src = Event(id="s1", title="Dentist", description="bring card", location="Main St", **timed())
    full = build_mirror(link, src)
    assert full.title == "Dentist" and full.location == "Main St"
    assert full.description.startswith("bring card") and parse_marker(full.description) == (link.id, "s1")
    link.mode = "busy"
    busy = build_mirror(link, src)
    assert busy.title == BUSY_TITLE and busy.location == "" and parse_marker(busy.description) == (link.id, "s1")
    assert busy.start == src.start and busy.end == src.end


def test_build_mirror_strips_existing_marker(link):
    src = Event(id="s1", title="x", description="notes\n\n[calsync:ref=old:zzz]", **timed())
    out = build_mirror(link, src)
    assert out.description.count("[calsync:ref=") == 1
    assert parse_marker(out.description) == (link.id, "s1")


# --- one-way ------------------------------------------------------------------------

def test_initial_full_sync_creates_mirrors(ctx, providers, link):
    g, m = providers["google"], providers["microsoft"]
    g.add("primary", title="A", **timed())
    g.add("primary", title="B", start="2026-11-05", end="2026-11-06", all_day=True)
    res = SyncEngine(ctx).sync_link(link)
    assert res.status == "ok" and res.full and res.created == 2
    ms = mirrors(m)
    assert {e.title for e in ms} == {"A", "B"}
    assert any(e.all_day for e in ms)
    state = ctx.repo.get_state(link.id)
    assert len(state.mapping) == 2 and state.cursor


def test_incremental_update_and_delete(ctx, providers, link):
    g, m = providers["google"], providers["microsoft"]
    a = g.add("primary", title="A", **timed())
    engine = SyncEngine(ctx)
    engine.sync_link(link)
    g.change("primary", a.id, title="A renamed")
    res = engine.sync_link(link)
    assert not res.full and res.updated == 1 and res.created == 0
    assert mirrors(m)[0].title == "A renamed"
    g.remove("primary", a.id)
    res = engine.sync_link(link)
    assert res.deleted == 1 and mirrors(m) == []
    assert ctx.repo.get_state(link.id).mapping == {}


def test_unchanged_event_is_skipped_on_full_sync(ctx, providers, link):
    g, m = providers["google"], providers["microsoft"]
    g.add("primary", title="A", **timed())
    engine = SyncEngine(ctx)
    engine.sync_link(link)
    res = engine.sync_link(link, full=True)
    assert res.created == 0 and res.updated == 0 and res.skipped == 1
    assert len(mirrors(m)) == 1


def test_cursor_expiry_falls_back_to_full(ctx, providers, link):
    g = providers["google"]
    g.add("primary", title="A", **timed())
    engine = SyncEngine(ctx)
    engine.sync_link(link)
    g.expire_cursor = True
    g.add("primary", title="B", **timed())
    res = engine.sync_link(link)
    assert res.full and res.created == 1 and res.status == "ok"


def test_mirror_deleted_by_hand_is_recreated(ctx, providers, link):
    g, m = providers["google"], providers["microsoft"]
    a = g.add("primary", title="A", **timed())
    engine = SyncEngine(ctx)
    engine.sync_link(link)
    tgt_id = ctx.repo.get_state(link.id).mapping[a.id]
    del m.events["primary"][tgt_id]  # user deleted it in Outlook
    g.change("primary", a.id, title="A2")
    res = engine.sync_link(link)
    assert res.created == 1 and mirrors(m)[0].title == "A2"


def test_busy_mode_hides_details(ctx, providers, accounts):
    link = make_link(ctx, accounts, mode="busy")
    g, m = providers["google"], providers["microsoft"]
    g.add("primary", title="Secret client", description="NDA", location="HQ", **timed())
    SyncEngine(ctx).sync_link(link)
    e = mirrors(m)[0]
    assert e.title == BUSY_TITLE and "NDA" not in e.description and e.location == ""


# --- two-way / loops ------------------------------------------------------------------

def test_two_way_does_not_loop(ctx, providers, two_way):
    g2m, m2g = two_way
    g, m = providers["google"], providers["microsoft"]
    g.add("primary", title="From Google", **timed())
    m.add("primary", title="From Outlook", **timed(start="2026-11-04T09:00:00+00:00", end="2026-11-04T10:00:00+00:00"))
    engine = SyncEngine(ctx)
    for _ in range(4):  # several rounds in both directions must converge
        engine.sync_link(g2m)
        engine.sync_link(m2g)
    assert len(g.events["primary"]) == 2 and len(m.events["primary"]) == 2
    assert {e.title for e in g.events["primary"].values()} == {"From Google", "From Outlook"}
    assert {e.title for e in m.events["primary"].values()} == {"From Google", "From Outlook"}
    # Exactly one mirror on each side, and it came from the other side.
    assert len(mirrors(g)) == 1 and mirrors(g)[0].title == "From Outlook"
    assert len(mirrors(m)) == 1 and mirrors(m)[0].title == "From Google"


def test_two_way_delete_propagates_once(ctx, providers, two_way):
    g2m, m2g = two_way
    g, m = providers["google"], providers["microsoft"]
    a = g.add("primary", title="A", **timed())
    engine = SyncEngine(ctx)
    engine.sync_link(g2m); engine.sync_link(m2g)
    g.remove("primary", a.id)
    engine.sync_link(g2m); engine.sync_link(m2g)
    assert g.events["primary"] == {} and m.events["primary"] == {}


# --- errors ---------------------------------------------------------------------------

def test_event_failure_is_logged_and_run_continues(ctx, providers, link):
    g, m = providers["google"], providers["microsoft"]
    g.add("primary", title="A", **timed())
    g.add("primary", title="B", **timed())
    calls = {"n": 0}
    real = m.create_event

    def flaky(cal, ev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(cal, ev)

    m.create_event = flaky
    res = SyncEngine(ctx).sync_link(link)
    assert res.status == "partial" and res.failed == 1 and res.created == 1
    errs = ctx.errors.list()
    assert len(errs) == 1 and errs[0]["stage"] == "event" and errs[0]["link_id"] == link.id
    assert "boom" in errs[0]["detail"]
    # Disk log written too
    assert "boom" in open(ctx.settings.error_log_path).read()


def test_whole_run_failure_sets_error_status(ctx, providers, link):
    providers["google"].fail_on.add("list_changes")
    res = SyncEngine(ctx).sync_link(link)
    assert res.status == "error"
    assert ctx.repo.get_link(link.id).last_status == "error"
    assert ctx.errors.list(user_id=link.user_id)[0]["stage"] == "sync"


def test_lock_prevents_concurrent_runs(ctx, providers, link):
    ctx.store.try_lock(f"link-{link.id}")
    res = SyncEngine(ctx).sync_link(link)
    assert res.locked and res.status == "locked"


# --- reconcile ------------------------------------------------------------------------

def test_reconcile_rebuilds_mapping_and_removes_orphans(ctx, providers, link):
    g, m = providers["google"], providers["microsoft"]
    a = g.add("primary", title="A", **timed())
    b = g.add("primary", title="B", **timed())
    engine = SyncEngine(ctx)
    engine.sync_link(link)
    # Simulate lost state and a source event that vanished without us noticing.
    ctx.store.delete(f"linkstate/{link.id}")
    del g.events["primary"][b.id]
    res = engine.reconcile(link)
    assert res.deleted == 1  # orphan mirror of B removed
    assert res.created == 0  # A's mirror was found via its marker, not duplicated
    state = ctx.repo.get_state(link.id)
    assert list(state.mapping) == [a.id] and len(mirrors(m)) == 1


def test_sync_all_and_sync_for_source(ctx, providers, accounts):
    l1 = make_link(ctx, accounts)
    l2 = make_link(ctx, accounts, enabled=False)
    engine = SyncEngine(ctx)
    assert {r.link_id for r in engine.sync_all()} == {l1.id}
    assert {r.link_id for r in engine.sync_for_source(accounts["google"].id, "primary")} == {l1.id}
    assert engine.sync_for_source(accounts["google"].id, "nope") == []
    assert l2.id not in {r.link_id for r in engine.sync_all()}
