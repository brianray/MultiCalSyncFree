"""HTTP API: auth flow (mocked providers), link CRUD, errors, webhooks and cron."""

import time

import pytest
from starlette.testclient import TestClient

from app.main import SESSION_COOKIE, _serializer, app
from app.providers.base import OAuthTokens

from .conftest import timed


@pytest.fixture
def client(ctx):
    return TestClient(app)


@pytest.fixture
def authed(client, ctx, user):
    client.cookies.set(SESSION_COOKIE, _serializer(ctx).dumps({"uid": user.id}))
    return client


def test_health_and_index(client):
    assert client.get("/healthz").json()["ok"] is True
    assert "MultiCalSyncFree" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_me_logged_out_reports_provider_config(client):
    body = client.get("/api/me").json()
    assert body["user"] is None and set(body["providers"]) == {"google", "microsoft"}


def test_api_requires_login(client):
    assert client.get("/api/errors").status_code == 401
    assert client.post("/api/links", json={}).status_code == 401


def test_auth_start_without_client_config(client):
    assert client.get("/auth/google/start", follow_redirects=False).status_code == 500
    assert client.get("/auth/nope/start").status_code == 404


def test_oauth_callback_creates_user_and_attaches_second_account(client, ctx, providers, monkeypatch):
    ctx.settings.google_client_id = "gid"
    ctx.settings.ms_client_id = "mid"
    tokens = OAuthTokens(access_token="a", refresh_token="r", expires_in=100)

    class FakeOAuth:
        def __init__(self, name, email):
            self.name, self.email, self.client_id = name, email, "x"

        def auth_url(self, state):
            return f"https://idp/{self.name}?state={state}"

        def exchange_code(self, code):
            assert code == "ok"
            return tokens

        def user_info(self):
            return {"email": self.email, "name": self.name.title()}

    fakes = {"google": FakeOAuth("google", "Brian@Gmail.com"), "microsoft": FakeOAuth("microsoft", "brian@work.com")}
    monkeypatch.setattr(ctx, "new_provider", lambda name, tokens=None, on_tokens=None: fakes[name])

    r = client.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 302
    state = r.headers["location"].split("state=")[1]
    r = client.get(f"/auth/google/callback?code=ok&state={state}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    me = client.get("/api/me").json()
    assert me["user"]["email"] == "brian@gmail.com"
    assert [a["provider"] for a in me["accounts"]] == ["google"]

    # Second provider attaches to the same user (not a new identity).
    r = client.get("/auth/microsoft/start", follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    client.get(f"/auth/microsoft/callback?code=ok&state={state}", follow_redirects=False)
    me = client.get("/api/me").json()
    assert sorted(a["provider"] for a in me["accounts"]) == ["google", "microsoft"]
    acct = ctx.repo.get_account(me["accounts"][0]["id"])
    assert acct.tokens_enc and "refresh_token" not in acct.tokens_enc  # encrypted at rest

    # Bad state is rejected; provider error is logged.
    assert client.get("/auth/google/callback?code=ok&state=wrong").status_code == 400
    assert client.get("/auth/google/callback?error=access_denied").status_code == 400


def test_allow_list_blocks_first_login(client, ctx, monkeypatch):
    ctx.settings.google_client_id = "gid"
    ctx.settings.allowed_emails = ("someone@else.com",)

    class FakeOAuth:
        client_id = "x"
        def auth_url(self, state): return f"https://idp?state={state}"
        def exchange_code(self, code): return OAuthTokens(access_token="a", expires_in=10)
        def user_info(self): return {"email": "intruder@evil.com", "name": ""}

    monkeypatch.setattr(ctx, "new_provider", lambda *a, **k: FakeOAuth())
    r = client.get("/auth/google/start", follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    assert client.get(f"/auth/google/callback?code=ok&state={state}", follow_redirects=False).status_code == 403


def test_list_calendars(authed, accounts):
    r = authed.get(f"/api/accounts/{accounts['google'].id}/calendars")
    assert r.status_code == 200 and r.json()["calendars"][0]["primary"] is True
    assert authed.get("/api/accounts/nope/calendars").status_code == 404


def test_create_two_way_link_runs_initial_sync(authed, ctx, providers, accounts):
    providers["google"].add("primary", title="G", **timed())
    providers["microsoft"].add("primary", title="M", **timed())
    body = {
        "source_account_id": accounts["google"].id, "source_calendar_id": "primary",
        "target_account_id": accounts["microsoft"].id, "target_calendar_id": "primary",
        "mode": "full", "two_way": True, "label": "Test",
    }
    r = authed.post("/api/links", json=body)
    assert r.status_code == 201 and len(r.json()["links"]) == 2
    # Background initial sync ran (TestClient runs background tasks inline).
    assert len(providers["google"].events["primary"]) == 2 and len(providers["microsoft"].events["primary"]) == 2
    # Webhooks were set up for both sources.
    assert ctx.repo.subscription_for(accounts["google"].id, "primary")
    assert ctx.repo.subscription_for(accounts["microsoft"].id, "primary")
    # Duplicate request is a no-op.
    assert authed.post("/api/links", json=body).json()["links"] == []
    me = authed.get("/api/me").json()
    assert len(me["links"]) == 2 and me["links"][0]["mirrored"] == 1 and me["links"][0]["last_status"] == "ok"


def test_create_link_validation(authed, accounts):
    assert authed.post("/api/links", json={"source_account_id": "x"}).status_code == 400
    same = {"source_account_id": accounts["google"].id, "source_calendar_id": "p",
            "target_account_id": accounts["google"].id, "target_calendar_id": "p"}
    assert authed.post("/api/links", json=same).status_code == 400
    assert authed.post("/api/links", json={**same, "target_calendar_id": "q", "mode": "weird"}).status_code == 400
    assert authed.post("/api/links", json={**same, "target_account_id": "ghost"}).status_code == 404


def test_link_lifecycle(authed, ctx, providers, accounts, link):
    g, m = providers["google"], providers["microsoft"]
    a = g.add("primary", title="A", **timed())
    r = authed.post(f"/api/links/{link.id}/sync")
    assert r.json()["result"]["created"] == 1
    r = authed.patch(f"/api/links/{link.id}", json={"enabled": False, "label": "Paused one"})
    assert r.json()["link"]["enabled"] is False and r.json()["link"]["label"] == "Paused one"
    r = authed.patch(f"/api/links/{link.id}", json={"mode": "busy"})
    assert r.json()["link"]["mode"] == "busy" and r.json()["link"]["has_cursor"] is False
    r = authed.post(f"/api/links/{link.id}/sync")
    assert r.json()["result"]["updated"] == 1
    assert [e for e in m.events["primary"].values() if e.is_mirror][0].title == "Busy"
    r = authed.post(f"/api/links/{link.id}/reconcile")
    assert r.json()["result"]["status"] == "ok"
    r = authed.delete(f"/api/links/{link.id}?remove_mirrors=1")
    assert r.json()["removed_mirrors"] == 1 and m.events["primary"] == {}
    assert authed.get("/api/me").json()["links"] == []
    assert authed.post(f"/api/links/{link.id}/sync").status_code == 404


def test_other_users_links_are_invisible(authed, ctx, link):
    from app.models import User, new_id
    other = User(id=new_id(), email="other@example.com")
    ctx.repo.save_user(other)
    authed.cookies.set(SESSION_COOKIE, _serializer(ctx).dumps({"uid": other.id}))
    assert authed.post(f"/api/links/{link.id}/sync").status_code == 404
    assert authed.get("/api/me").json()["links"] == []


def test_disconnect_account_removes_links(authed, ctx, accounts, link):
    r = authed.delete(f"/api/accounts/{accounts['google'].id}")
    assert r.json()["ok"] is True
    me = authed.get("/api/me").json()
    assert me["links"] == [] and [a["provider"] for a in me["accounts"]] == ["microsoft"]


def test_errors_endpoint(authed, ctx, user):
    ctx.errors.record("sync", "mine", user_id=user.id)
    ctx.errors.record("sync", "not mine", user_id="someone-else")
    errs = authed.get("/api/errors").json()["errors"]
    assert [e["message"] for e in errs] == ["mine"]
    assert authed.delete("/api/errors").json()["cleared"] == 1
    assert authed.get("/api/errors").json()["errors"] == []


def test_webhook_endpoints(client, ctx, providers, accounts, link):
    from app.webhooks import SubscriptionManager
    providers["google"].add("primary", title="A", **timed())
    sub = SubscriptionManager(ctx).ensure(accounts["google"], "primary")
    r = client.post("/webhooks/google", headers={"X-Goog-Channel-ID": sub.id, "X-Goog-Channel-Token": sub.secret, "X-Goog-Resource-State": "exists"})
    assert r.status_code == 204
    assert len([e for e in providers["microsoft"].events["primary"].values() if e.is_mirror]) == 1
    assert client.get("/webhooks/microsoft?validationToken=tok123").text == "tok123"
    assert client.post("/webhooks/microsoft", json={"value": [{"clientState": "x:y"}]}).status_code == 202
    assert client.post("/webhooks/microsoft", content=b"not json").status_code == 202


def test_cron_tick(client, ctx, providers, accounts, link):
    providers["google"].add("primary", title="A", **timed())
    assert client.post("/cron/tick").status_code == 403
    r = client.post("/cron/tick", headers={"Authorization": f"Bearer {ctx.settings.cron_secret}"})
    body = r.json()
    assert body["links"][0]["created"] == 1 and body["subscriptions"]["created"] == 1
    assert ctx.repo.get_link(link.id).last_sync > time.time() - 5
