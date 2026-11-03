"""Google / Microsoft providers against a mock HTTP transport."""

import json
import time

import pytest

from app.http import httpx
from app.providers import GoogleProvider, MicrosoftProvider
from app.providers.base import CursorExpired, Event, OAuthTokens, ProviderError, to_utc_iso, window


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def fresh_tokens():
    return OAuthTokens(access_token="at", refresh_token="rt", expires_at=time.time() + 3600)


# --- time helpers ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-11-02T19:00:00Z", "2026-11-02T19:00:00+00:00"),
        ("2026-11-02T11:00:00-08:00", "2026-11-02T19:00:00+00:00"),
        ("2026-11-02T19:00:00.0000000", "2026-11-02T19:00:00+00:00"),  # Graph, 7 fractional digits, UTC assumed
        ("2026-11-02T19:00:00.1234567Z", "2026-11-02T19:00:00+00:00"),
    ],
)
def test_to_utc_iso(raw, expected):
    assert to_utc_iso(raw) == expected


def test_window_shape():
    s, e = window(30, 365)
    assert s.endswith("+00:00") and e.endswith("+00:00") and s < e


# --- Google ------------------------------------------------------------------------------

GOOGLE_EVENT = {
    "id": "g1",
    "summary": "Standup",
    "description": "daily",
    "location": "Zoom",
    "start": {"dateTime": "2026-11-03T09:00:00-08:00"},
    "end": {"dateTime": "2026-11-03T09:15:00-08:00"},
    "status": "confirmed",
}


def test_google_list_changes_pages_and_sync_token():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if "pageToken" not in req.url.params:
            return httpx.Response(200, json={"items": [GOOGLE_EVENT], "nextPageToken": "p2"})
        return httpx.Response(
            200,
            json={
                "items": [{"id": "g2", "status": "cancelled"}, {"id": "g3", "summary": "Day", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}}],
                "nextSyncToken": "tok",
            },
        )

    p = GoogleProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    events, cursor = p.list_changes("primary", None, "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
    assert cursor == "tok" and len(events) == 3
    assert seen[0]["timeMin"] and seen[0]["singleEvents"] == "true" and seen[0]["showDeleted"] == "true"
    e = events[0]
    assert e.start == "2026-11-03T17:00:00+00:00" and e.title == "Standup" and not e.is_mirror
    assert events[1].deleted
    assert events[2].all_day and events[2].start == "2026-11-05" and events[2].end == "2026-11-06"


def test_google_sync_token_expiry_raises():
    p = GoogleProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(lambda r: httpx.Response(410)))
    with pytest.raises(CursorExpired):
        p.list_changes("primary", "old", "a", "b")


def test_google_create_body_and_marker_detection():
    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"id": "new1"})

    p = GoogleProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    ev = Event(id="", title="T", description="x\n\n[calsync:ref=l:s]", start="2026-11-03T17:00:00+00:00",
               end="2026-11-03T18:00:00+00:00", show_as="free", extra={"calsync_ref": "l:s"})
    assert p.create_event("a b@group.calendar.google.com", ev) == "new1"
    b = captured["body"]
    assert b["transparency"] == "transparent" and b["start"] == {"dateTime": ev.start, "timeZone": "UTC"}
    assert b["extendedProperties"]["private"]["calsync_ref"] == "l:s"
    assert "a%20b%40group.calendar.google.com" in captured["url"]
    # Reading it back flags it as a mirror even without the description marker.
    got = GoogleProvider._to_event({"id": "z", "extendedProperties": {"private": {"calsync_ref": "l:s"}}, "start": {"dateTime": "2026-11-03T17:00:00Z"}, "end": {"dateTime": "2026-11-03T18:00:00Z"}})
    assert got.is_mirror


def test_google_refresh_on_401_then_retry():
    calls = []

    def handler(req):
        calls.append((req.method, str(req.url)))
        if "oauth2.googleapis.com/token" in str(req.url):
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})
        if req.headers["Authorization"] == "Bearer at":
            return httpx.Response(401)
        return httpx.Response(200, json={"email": "a@b.c", "name": "A"})

    stored = {}
    p = GoogleProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), on_tokens=lambda t: stored.update(t), client=mock_client(handler))
    assert p.user_info()["email"] == "a@b.c"
    assert stored["access_token"] == "at2" and stored["refresh_token"] == "rt"


def test_google_watch_and_stop():
    def handler(req):
        if req.url.path.endswith("/watch"):
            body = json.loads(req.content)
            assert body["type"] == "web_hook" and body["token"] == "sec" and body["id"] == "chan"
            return httpx.Response(200, json={"resourceId": "res1", "expiration": str(int((time.time() + 100) * 1000))})
        if req.url.path.endswith("/channels/stop"):
            return httpx.Response(204)
        return httpx.Response(500)

    p = GoogleProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    rid, exp = p.subscribe("primary", "http://cb/webhooks/google", "chan", "sec")
    assert rid == "res1" and exp > time.time()
    with pytest.raises(ProviderError):
        p.renew(rid, "chan")
    p.unsubscribe(rid, "chan")


def test_google_delete_tolerates_missing():
    p = GoogleProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(lambda r: httpx.Response(404)))
    p.delete_event("primary", "gone")  # no raise
    with pytest.raises(ProviderError) as ei:
        p.update_event("primary", "gone", Event(id="", start="2026-11-03T17:00:00+00:00", end="2026-11-03T18:00:00+00:00"))
    assert ei.value.status == 404


# --- Microsoft ----------------------------------------------------------------------------

GRAPH_EVENT = {
    "id": "m1",
    "subject": "Review",
    "body": {"contentType": "text", "content": "notes\r\n"},
    "bodyPreview": "notes",
    "location": {"displayName": "Room 4"},
    "start": {"dateTime": "2026-11-03T17:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-11-03T18:00:00.0000000", "timeZone": "UTC"},
    "isAllDay": False,
    "showAs": "busy",
}


def test_microsoft_delta_follows_next_and_delta_links():
    urls = []

    def handler(req):
        urls.append(str(req.url))
        assert 'outlook.timezone="UTC"' in req.headers["Prefer"]
        if "calendarView/delta" in str(req.url) and "skiptoken" not in str(req.url):
            return httpx.Response(200, json={"value": [GRAPH_EVENT], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$skiptoken=abc"})
        return httpx.Response(200, json={"value": [{"id": "m2", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=xyz"})

    p = MicrosoftProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    events, cursor = p.list_changes("cal1", None, "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
    assert "deltatoken=xyz" in cursor and len(events) == 2
    assert "startDateTime=" in urls[0]
    e = events[0]
    assert e.title == "Review" and e.description == "notes" and e.location == "Room 4"
    assert e.start == "2026-11-03T17:00:00+00:00" and not e.all_day
    assert events[1].deleted

    # Incremental call uses the deltaLink verbatim.
    p.list_changes("cal1", cursor, "a", "b")
    assert urls[-1].startswith("https://graph.microsoft.com/v1.0/me/calendarView/delta?")


def test_microsoft_all_day_and_create_body():
    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "new"})

    p = MicrosoftProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    ev = Event(id="", title="Holiday", description="[calsync:ref=l:s]", start="2026-11-05", end="2026-11-06", all_day=True, show_as="free")
    p.create_event("cal1", ev)
    b = captured["body"]
    assert b["isAllDay"] is True and b["start"] == {"dateTime": "2026-11-05T00:00:00", "timeZone": "UTC"}
    assert b["showAs"] == "free" and b["body"]["contentType"] == "text" and b["isReminderOn"] is False
    got = MicrosoftProvider._to_event({**GRAPH_EVENT, "isAllDay": True, "start": {"dateTime": "2026-11-05T00:00:00.0000000"}, "end": {"dateTime": "2026-11-06T00:00:00.0000000"}})
    assert got.all_day and got.start == "2026-11-05" and got.end == "2026-11-06"


def test_microsoft_subscription_lifecycle():
    def handler(req):
        if req.method == "POST" and req.url.path == "/v1.0/subscriptions":
            body = json.loads(req.content)
            assert body["clientState"] == "chan:sec" and body["changeType"] == "created,updated,deleted"
            return httpx.Response(201, json={"id": "sub1", "expirationDateTime": "2026-11-05T00:00:00.0000000Z"})
        if req.method == "PATCH":
            return httpx.Response(200, json={"id": "sub1", "expirationDateTime": "2026-11-08T00:00:00.0000000Z"})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    p = MicrosoftProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    sid, exp = p.subscribe("cal1", "http://cb/webhooks/microsoft", "chan", "sec")
    assert sid == "sub1"
    assert p.renew(sid, "chan") > exp
    p.unsubscribe(sid, "chan")


def test_microsoft_subscribe_falls_back_to_me_events_for_default_calendar():
    seen = []

    def handler(req):
        if req.method == "GET" and req.url.path == "/v1.0/me/calendar":
            return httpx.Response(200, json={"id": "default-cal"})
        body = json.loads(req.content)
        seen.append(body["resource"])
        if body["resource"].startswith("me/calendars/"):
            return httpx.Response(400, json={"error": {"code": "InvalidRequest", "message": "Resource not supported"}})
        return httpx.Response(201, json={"id": "sub2", "expirationDateTime": "2026-11-05T00:00:00.0000000Z"})

    p = MicrosoftProvider("cid", "sec", "http://cb", tokens=fresh_tokens(), client=mock_client(handler))
    sid, _ = p.subscribe("default-cal", "http://cb/webhooks/microsoft", "chan", "sec")
    assert sid == "sub2" and seen == ["me/calendars/default-cal/events", "me/events"]
    # A non-default calendar surfaces Graph's message instead of retrying.
    with pytest.raises(ProviderError) as ei:
        p.subscribe("other-cal", "http://cb/webhooks/microsoft", "chan", "sec")
    assert "Resource not supported" in str(ei.value)


def test_microsoft_auth_url_uses_tenant():
    p = MicrosoftProvider("cid", "sec", "http://cb", tenant="contoso.com")
    url = p.auth_url("st")
    assert url.startswith("https://login.microsoftonline.com/contoso.com/oauth2/v2.0/authorize?")
    assert "Calendars.ReadWrite" in url and "state=st" in url


def test_refresh_without_refresh_token_errors():
    p = GoogleProvider("cid", "sec", "http://cb", tokens=OAuthTokens(access_token="x", expires_at=0), client=mock_client(lambda r: httpx.Response(500)))
    with pytest.raises(ProviderError):
        p.user_info()
