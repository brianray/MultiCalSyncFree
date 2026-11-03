"""Subscription management and webhook dispatch."""

import time

from app.webhooks import RENEW_BEFORE_SECONDS, SubscriptionManager, WebhookHandler

from .conftest import make_link, timed


def test_ensure_creates_then_reuses_then_renews(ctx, providers, accounts):
    g = providers["google"]
    mgr = SubscriptionManager(ctx)
    sub = mgr.ensure(accounts["google"], "primary")
    assert sub.provider_sub_id == f"res-{sub.id}" and sub.secret
    assert mgr.ensure(accounts["google"], "primary").id == sub.id  # fresh: reused
    assert sum(c[0] == "subscribe" for c in g.calls) == 1
    # Expiring soon: fake provider can renew.
    sub.expires_at = time.time() + RENEW_BEFORE_SECONDS - 10
    ctx.repo.save_subscription(sub)
    renewed = mgr.ensure(accounts["google"], "primary")
    assert renewed.id == sub.id and renewed.expires_at > sub.expires_at
    # Provider that cannot renew (like Google): dropped and re-created.
    g.fail_on.add("renew")
    renewed.expires_at = time.time() + 10
    ctx.repo.save_subscription(renewed)
    again = mgr.ensure(accounts["google"], "primary")
    assert again.id != sub.id and ("unsubscribe", f"res-{sub.id}") in g.calls


def test_renew_all_creates_missing_and_drops_unused(ctx, providers, accounts):
    link = make_link(ctx, accounts)
    mgr = SubscriptionManager(ctx)
    stats = mgr.renew_all()
    assert stats == {"renewed": 0, "created": 1, "failed": 0}
    sub = ctx.repo.subscription_for(accounts["google"].id, "primary")
    assert sub is not None
    ctx.repo.delete_link(link.id)
    mgr.renew_all()
    assert ctx.repo.subscription_for(accounts["google"].id, "primary") is None


def test_renew_all_logs_failures(ctx, providers, accounts):
    make_link(ctx, accounts)
    providers["google"].fail_on.add("subscribe")
    stats = SubscriptionManager(ctx).renew_all()
    assert stats["failed"] == 1
    assert ctx.errors.list()[0]["stage"] == "subscription"


def test_google_webhook_dispatch(ctx, providers, accounts):
    link = make_link(ctx, accounts)
    providers["google"].add("primary", title="A", **timed())
    sub = SubscriptionManager(ctx).ensure(accounts["google"], "primary")
    h = WebhookHandler(ctx)
    assert h.google({"X-Goog-Channel-ID": "nope", "X-Goog-Channel-Token": "x"})["accepted"] is False
    assert h.google({"X-Goog-Channel-ID": sub.id, "X-Goog-Channel-Token": "wrong"})["accepted"] is False
    assert h.google({"X-Goog-Channel-ID": sub.id, "X-Goog-Channel-Token": sub.secret, "X-Goog-Resource-State": "sync"})["synced"] == 0
    out = h.google({"X-Goog-Channel-ID": sub.id, "X-Goog-Channel-Token": sub.secret, "X-Goog-Resource-State": "exists"})
    assert out["synced"] == 1 and out["results"][0]["created"] == 1 and out["results"][0]["link_id"] == link.id


def test_microsoft_webhook_dispatch_dedupes(ctx, providers, accounts):
    make_link(ctx, accounts, src="microsoft", tgt="google")
    providers["microsoft"].add("primary", title="M", **timed())
    sub = SubscriptionManager(ctx).ensure(accounts["microsoft"], "primary")
    h = WebhookHandler(ctx)
    body = {"value": [
        {"clientState": f"{sub.id}:{sub.secret}", "changeType": "created"},
        {"clientState": f"{sub.id}:{sub.secret}", "changeType": "updated"},  # same calendar: one sync
        {"clientState": "bogus:bogus"},
    ]}
    out = h.microsoft(body)
    assert out["synced"] == 1 and out["results"][0]["created"] == 1
    assert len([e for e in providers["google"].events["primary"].values() if e.is_mirror]) == 1
