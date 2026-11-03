"""Google Calendar provider (REST API v3)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote, urlencode

from .base import Calendar, CalendarProvider, CursorExpired, Event, ProviderError, is_mirror, to_utc_iso

API = "https://www.googleapis.com/calendar/v3"


class GoogleProvider(CalendarProvider):
    name = "google"
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    SCOPES = ["openid", "email", "profile", "https://www.googleapis.com/auth/calendar"]
    WATCH_TTL_SECONDS = 7 * 24 * 3600

    def auth_url(self, state: str) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # always get a refresh token
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(q)}"

    # --- identity ------------------------------------------------------------------
    def user_info(self) -> dict[str, str]:
        r = self.request("GET", "https://www.googleapis.com/oauth2/v3/userinfo")
        self._check(r, "userinfo")
        b = r.json()
        return {"email": b.get("email", ""), "name": b.get("name", "")}

    # --- calendars -----------------------------------------------------------------
    def list_calendars(self) -> list[Calendar]:
        r = self.request("GET", f"{API}/users/me/calendarList", params={"minAccessRole": "writer"})
        self._check(r, "calendarList")
        out = []
        for item in r.json().get("items", []):
            out.append(
                Calendar(
                    id=item["id"],
                    name=item.get("summaryOverride") or item.get("summary", item["id"]),
                    primary=bool(item.get("primary")),
                    writable=item.get("accessRole") in ("writer", "owner"),
                )
            )
        return out

    # --- events --------------------------------------------------------------------
    def _events_url(self, calendar_id: str) -> str:
        return f"{API}/calendars/{quote(calendar_id, safe='')}/events"

    def list_changes(
        self, calendar_id: str, cursor: str | None, window_start: str, window_end: str
    ) -> tuple[list[Event], str]:
        events: list[Event] = []
        params: dict[str, Any] = {"singleEvents": "true", "showDeleted": "true", "maxResults": 250}
        if cursor:
            params["syncToken"] = cursor
        else:
            params["timeMin"] = window_start
            params["timeMax"] = window_end
        page_token = None
        while True:
            p = dict(params)
            if page_token:
                p["pageToken"] = page_token
            r = self.request("GET", self._events_url(calendar_id), params=p)
            if r.status_code == 410:
                raise CursorExpired("google syncToken expired")
            self._check(r, "events.list")
            body = r.json()
            events.extend(self._to_event(e) for e in body.get("items", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                return events, body.get("nextSyncToken", "")

    def get_event(self, calendar_id: str, event_id: str) -> Event | None:
        r = self.request("GET", f"{self._events_url(calendar_id)}/{quote(event_id, safe='')}")
        if r.status_code == 404:
            return None
        self._check(r, "events.get")
        return self._to_event(r.json())

    def create_event(self, calendar_id: str, event: Event) -> str:
        r = self.request("POST", self._events_url(calendar_id), json=self._to_body(event))
        self._check(r, "events.insert")
        return r.json()["id"]

    def update_event(self, calendar_id: str, event_id: str, event: Event) -> None:
        r = self.request(
            "PATCH", f"{self._events_url(calendar_id)}/{quote(event_id, safe='')}", json=self._to_body(event)
        )
        if r.status_code == 404:
            raise ProviderError("google: event vanished", 404)
        self._check(r, "events.patch")

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        r = self.request("DELETE", f"{self._events_url(calendar_id)}/{quote(event_id, safe='')}")
        if r.status_code in (404, 410):
            return  # already gone
        self._check(r, "events.delete")

    # --- push notifications ----------------------------------------------------------
    def subscribe(self, calendar_id: str, callback_url: str, channel_id: str, secret: str) -> tuple[str, float]:
        r = self.request(
            "POST",
            f"{self._events_url(calendar_id)}/watch",
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": callback_url,
                "token": secret,
                "params": {"ttl": str(self.WATCH_TTL_SECONDS)},
            },
        )
        self._check(r, "events.watch")
        body = r.json()
        expires = int(body.get("expiration", 0)) / 1000 or (time.time() + self.WATCH_TTL_SECONDS)
        return body["resourceId"], expires

    def renew(self, provider_sub_id: str, channel_id: str) -> float:
        # Google channels cannot be extended; the caller stops it and re-subscribes.
        raise ProviderError("google channels cannot be renewed; re-subscribe", 400)

    def unsubscribe(self, provider_sub_id: str, channel_id: str) -> None:
        r = self.request("POST", f"{API}/channels/stop", json={"id": channel_id, "resourceId": provider_sub_id})
        if r.status_code in (404, 400):
            return
        self._check(r, "channels.stop")

    # --- mapping -------------------------------------------------------------------
    @staticmethod
    def _to_event(e: dict[str, Any]) -> Event:
        start = e.get("start", {}) or {}
        end = e.get("end", {}) or {}
        all_day = "date" in start
        desc = e.get("description", "") or ""
        return Event(
            id=e["id"],
            title=e.get("summary", "") or "",
            description=desc,
            location=e.get("location", "") or "",
            start=start.get("date") if all_day else to_utc_iso(start.get("dateTime", "")) if start.get("dateTime") else "",
            end=end.get("date") if all_day else to_utc_iso(end.get("dateTime", "")) if end.get("dateTime") else "",
            all_day=all_day,
            show_as="free" if e.get("transparency") == "transparent" else "busy",
            deleted=e.get("status") == "cancelled",
            is_mirror=is_mirror(desc) or bool((e.get("extendedProperties", {}).get("private") or {}).get("calsync_ref")),
        )

    @staticmethod
    def _to_body(ev: Event) -> dict[str, Any]:
        if ev.all_day:
            start: dict[str, Any] = {"date": ev.start}
            end: dict[str, Any] = {"date": ev.end}
        else:
            start = {"dateTime": ev.start, "timeZone": "UTC"}
            end = {"dateTime": ev.end, "timeZone": "UTC"}
        body: dict[str, Any] = {
            "summary": ev.title,
            "description": ev.description,
            "location": ev.location,
            "start": start,
            "end": end,
            "transparency": "transparent" if ev.show_as == "free" else "opaque",
            "reminders": {"useDefault": False},
        }
        ref = ev.extra.get("calsync_ref")
        if ref:
            body["extendedProperties"] = {"private": {"calsync_ref": ref}}
        return body

    @staticmethod
    def _check(r, what: str) -> None:
        if r.status_code >= 400:
            raise ProviderError(f"google {what} failed: HTTP {r.status_code}", r.status_code, r.text[:2000])
