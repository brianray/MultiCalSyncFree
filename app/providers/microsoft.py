"""Microsoft 365 / Outlook provider (Microsoft Graph v1.0)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode

from .base import Calendar, CalendarProvider, CursorExpired, Event, ProviderError, is_mirror, to_utc_iso

GRAPH = "https://graph.microsoft.com/v1.0"
UTC_PREFER = 'outlook.timezone="UTC", outlook.body-content-type="text"'


class MicrosoftProvider(CalendarProvider):
    name = "microsoft"
    SCOPES = ["openid", "email", "profile", "offline_access", "User.Read", "Calendars.ReadWrite"]
    # Graph caps Outlook subscriptions at 4230 minutes (~3 days). Renew daily.
    SUB_MINUTES = 4200

    def __init__(self, *args: Any, tenant: str = "common", **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.tenant = tenant
        self.AUTH_URL = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
        self.TOKEN_URL = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    def auth_url(self, state: str) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(self.SCOPES),
            "prompt": "select_account",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(q)}"

    def exchange_code(self, code: str):  # Graph wants scope on the token call too
        r = self.http.post(
            self.TOKEN_URL,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
                "scope": " ".join(self.SCOPES),
            },
        )
        if r.status_code >= 400:
            raise ProviderError("microsoft: code exchange failed", r.status_code, r.text)
        return self._store_tokens(r.json())

    # --- identity ------------------------------------------------------------------
    def user_info(self) -> dict[str, str]:
        r = self.request("GET", f"{GRAPH}/me", params={"$select": "mail,userPrincipalName,displayName"})
        self._check(r, "me")
        b = r.json()
        return {"email": b.get("mail") or b.get("userPrincipalName", ""), "name": b.get("displayName", "")}

    # --- calendars -----------------------------------------------------------------
    def list_calendars(self) -> list[Calendar]:
        r = self.request("GET", f"{GRAPH}/me/calendars", params={"$select": "id,name,isDefaultCalendar,canEdit"})
        self._check(r, "calendars")
        return [
            Calendar(
                id=c["id"],
                name=c.get("name", "Calendar"),
                primary=bool(c.get("isDefaultCalendar")),
                writable=c.get("canEdit", True),
            )
            for c in r.json().get("value", [])
        ]

    # --- events --------------------------------------------------------------------
    def list_changes(
        self, calendar_id: str, cursor: str | None, window_start: str, window_end: str
    ) -> tuple[list[Event], str]:
        events: list[Event] = []
        if cursor:
            url: str | None = cursor
            params: dict[str, Any] | None = None
        else:
            url = f"{GRAPH}/me/calendars/{quote(calendar_id, safe='')}/calendarView/delta"
            params = {"startDateTime": window_start, "endDateTime": window_end}
        while url:
            r = self.request("GET", url, params=params, headers={"Prefer": UTC_PREFER})
            params = None
            if r.status_code == 410:
                raise CursorExpired("graph deltaLink expired")
            self._check(r, "calendarView/delta")
            body = r.json()
            for item in body.get("value", []):
                events.append(self._to_event(item))
            url = body.get("@odata.nextLink")
            if not url:
                return events, body.get("@odata.deltaLink", "")
        return events, ""

    def get_event(self, calendar_id: str, event_id: str) -> Event | None:
        r = self.request("GET", f"{GRAPH}/me/events/{quote(event_id, safe='')}", headers={"Prefer": UTC_PREFER})
        if r.status_code == 404:
            return None
        self._check(r, "events.get")
        return self._to_event(r.json())

    def create_event(self, calendar_id: str, event: Event) -> str:
        r = self.request(
            "POST",
            f"{GRAPH}/me/calendars/{quote(calendar_id, safe='')}/events",
            json=self._to_body(event),
            headers={"Prefer": UTC_PREFER},
        )
        self._check(r, "events.create")
        return r.json()["id"]

    def update_event(self, calendar_id: str, event_id: str, event: Event) -> None:
        r = self.request(
            "PATCH",
            f"{GRAPH}/me/events/{quote(event_id, safe='')}",
            json=self._to_body(event),
            headers={"Prefer": UTC_PREFER},
        )
        if r.status_code == 404:
            raise ProviderError("microsoft: event vanished", 404)
        self._check(r, "events.update")

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        r = self.request("DELETE", f"{GRAPH}/me/events/{quote(event_id, safe='')}")
        if r.status_code == 404:
            return
        self._check(r, "events.delete")

    # --- change notifications --------------------------------------------------------
    def _expiry(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=self.SUB_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    def subscribe(self, calendar_id: str, callback_url: str, channel_id: str, secret: str) -> tuple[str, float]:
        payload = {
            "changeType": "created,updated,deleted",
            "notificationUrl": callback_url,
            "resource": f"me/calendars/{calendar_id}/events",
            "expirationDateTime": self._expiry(),
            "clientState": f"{channel_id}:{secret}",
        }
        r = self.request("POST", f"{GRAPH}/subscriptions", json=payload)
        if r.status_code == 400 and "validation request timed out" in r.text.lower():
            # Graph must get our echo within 10s; a cold Cloud Run instance can
            # miss that. The first attempt warmed it up, so try once more.
            time.sleep(2)
            r = self.request("POST", f"{GRAPH}/subscriptions", json=payload)
        if r.status_code == 400 and self._is_default_calendar(calendar_id):
            # Some tenants reject the per-calendar resource for the default
            # calendar; `me/events` is equivalent for it.
            payload["resource"] = "me/events"
            r = self.request("POST", f"{GRAPH}/subscriptions", json=payload)
        self._check(r, "subscriptions.create")
        body = r.json()
        return body["id"], _parse_graph_time(body["expirationDateTime"])

    def _is_default_calendar(self, calendar_id: str) -> bool:
        try:
            r = self.request("GET", f"{GRAPH}/me/calendar", params={"$select": "id"})
            return r.status_code == 200 and r.json().get("id") == calendar_id
        except Exception:  # pragma: no cover - best effort
            return False

    def renew(self, provider_sub_id: str, channel_id: str) -> float:
        r = self.request(
            "PATCH", f"{GRAPH}/subscriptions/{provider_sub_id}", json={"expirationDateTime": self._expiry()}
        )
        if r.status_code == 404:
            raise ProviderError("microsoft: subscription not found", 404)
        self._check(r, "subscriptions.renew")
        return _parse_graph_time(r.json()["expirationDateTime"])

    def unsubscribe(self, provider_sub_id: str, channel_id: str) -> None:
        r = self.request("DELETE", f"{GRAPH}/subscriptions/{provider_sub_id}")
        if r.status_code == 404:
            return
        self._check(r, "subscriptions.delete")

    # --- mapping -------------------------------------------------------------------
    @staticmethod
    def _to_event(e: dict[str, Any]) -> Event:
        if "@removed" in e:
            return Event(id=e["id"], deleted=True)
        all_day = bool(e.get("isAllDay"))
        start = (e.get("start") or {}).get("dateTime", "")
        end = (e.get("end") or {}).get("dateTime", "")
        body = e.get("body") or {}
        desc = body.get("content", "") if body.get("contentType", "text") == "text" else e.get("bodyPreview", "")
        desc = (desc or "").replace("\r\n", "\n").strip()
        loc = (e.get("location") or {}).get("displayName", "") or ""
        return Event(
            id=e["id"],
            title=e.get("subject", "") or "",
            description=desc,
            location=loc,
            start=start[:10] if all_day else to_utc_iso(start) if start else "",
            end=end[:10] if all_day else to_utc_iso(end) if end else "",
            all_day=all_day,
            show_as="free" if e.get("showAs") == "free" else "busy",
            deleted=e.get("isCancelled", False),
            is_mirror=is_mirror(desc),
        )

    @staticmethod
    def _to_body(ev: Event) -> dict[str, Any]:
        if ev.all_day:
            start = {"dateTime": f"{ev.start}T00:00:00", "timeZone": "UTC"}
            end = {"dateTime": f"{ev.end}T00:00:00", "timeZone": "UTC"}
        else:
            start = {"dateTime": _graph_dt(ev.start), "timeZone": "UTC"}
            end = {"dateTime": _graph_dt(ev.end), "timeZone": "UTC"}
        return {
            "subject": ev.title,
            "body": {"contentType": "text", "content": ev.description},
            "location": {"displayName": ev.location},
            "start": start,
            "end": end,
            "isAllDay": ev.all_day,
            "showAs": "free" if ev.show_as == "free" else "busy",
            "isReminderOn": False,
        }

    @staticmethod
    def _check(r, what: str) -> None:
        if r.status_code >= 400:
            raise ProviderError(
                f"microsoft {what} failed: HTTP {r.status_code}: {_graph_error(r)}", r.status_code, r.text[:2000]
            )


def _graph_error(r) -> str:
    """Pull Graph's human-readable error out of the response, if there is one."""
    try:
        err = r.json().get("error", {})
        return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300] or r.text[:300]
    except Exception:
        return r.text[:300]


def _graph_dt(utc_iso: str) -> str:
    """'2026-11-02T19:00:00+00:00' -> '2026-11-02T19:00:00'"""
    return utc_iso.replace("+00:00", "").replace("Z", "")[:19]


def _parse_graph_time(s: str) -> float:
    return datetime.fromisoformat(to_utc_iso(s)).timestamp()
