"""Provider-neutral event model, marker helpers and the provider interface.

Both providers normalise to the same ``Event`` shape:

* timed events: ``start``/``end`` are ISO-8601 strings in UTC (``...+00:00``)
* all-day events: ``start``/``end`` are ``YYYY-MM-DD`` with an exclusive end date

Mirrored events carry a marker in their description so that

1. the reverse link (B -> A) recognises them and does not mirror them back, and
2. the event-id mapping can be rebuilt from the target calendar if lost.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..http import httpx

MARKER_PREFIX = "[calsync:ref="
_MARKER_RE = re.compile(r"\n*\[calsync:ref=([A-Za-z0-9_\-]+):([^\]\s]+)\]\s*$")


def make_marker(link_id: str, source_event_id: str) -> str:
    return f"{MARKER_PREFIX}{link_id}:{source_event_id}]"


def parse_marker(description: str | None) -> tuple[str, str] | None:
    """Return (link_id, source_event_id) if the text ends with our marker."""
    if not description:
        return None
    m = _MARKER_RE.search(description)
    if not m:
        return None
    return m.group(1), m.group(2)


def strip_marker(description: str | None) -> str:
    if not description:
        return ""
    return _MARKER_RE.sub("", description).rstrip()


def is_mirror(description: str | None) -> bool:
    return bool(description) and MARKER_PREFIX in description  # type: ignore[arg-type]


class CursorExpired(Exception):
    """The incremental cursor is no longer valid; caller must do a full resync."""


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Calendar:
    id: str
    name: str
    primary: bool = False
    writable: bool = True


@dataclass
class Event:
    id: str
    title: str = ""
    description: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    all_day: bool = False
    show_as: str = "busy"  # "busy" | "free"
    deleted: bool = False
    is_mirror: bool = False  # True when the description carries a calsync marker
    extra: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = json.dumps(
            [self.title, self.description, self.location, self.start, self.end, self.all_day, self.show_as],
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- time helpers ----------------------------------------------------------------

def to_utc_iso(value: str) -> str:
    """Normalise any ISO-8601 datetime (with or without offset) to UTC ``...+00:00``."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Graph returns 7 fractional digits; Python handles at most 6.
    if "." in v:
        head, _, tail = v.partition(".")
        frac = ""
        offset = ""
        for i, ch in enumerate(tail):
            if ch in "+-":
                frac, offset = tail[:i], tail[i:]
                break
        else:
            frac = tail
        v = f"{head}.{frac[:6]}{offset}" if frac else f"{head}{offset}"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def window(past_days: int, future_days: int, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=past_days)).replace(microsecond=0)
    end = (now + timedelta(days=future_days)).replace(microsecond=0)
    return start.isoformat(), end.isoformat()


# --- OAuth token handling ----------------------------------------------------------

class OAuthTokens(dict):
    """{access_token, refresh_token, expires_at}"""

    @property
    def expired(self) -> bool:
        return time.time() > float(self.get("expires_at", 0)) - 60


class CalendarProvider(ABC):
    """Interface each provider implements. All methods are synchronous."""

    name: str = ""
    AUTH_URL = ""
    TOKEN_URL = ""
    SCOPES: list[str] = []

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        tokens: OAuthTokens | None = None,
        on_tokens: Callable[[OAuthTokens], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.tokens = tokens or OAuthTokens()
        self._on_tokens = on_tokens
        self.http = client or httpx.Client(timeout=30.0)

    # --- OAuth ---------------------------------------------------------------------
    def auth_url(self, state: str) -> str:  # pragma: no cover - trivial, provider specific
        raise NotImplementedError

    def exchange_code(self, code: str) -> OAuthTokens:
        r = self.http.post(
            self.TOKEN_URL,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if r.status_code >= 400:
            raise ProviderError(f"{self.name}: code exchange failed", r.status_code, r.text)
        return self._store_tokens(r.json())

    def refresh(self) -> OAuthTokens:
        rt = self.tokens.get("refresh_token")
        if not rt:
            raise ProviderError(f"{self.name}: no refresh token; user must reconnect", 401)
        r = self.http.post(
            self.TOKEN_URL,
            data={
                "refresh_token": rt,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code >= 400:
            raise ProviderError(f"{self.name}: token refresh failed", r.status_code, r.text)
        return self._store_tokens(r.json())

    def _store_tokens(self, body: dict[str, Any]) -> OAuthTokens:
        tokens = OAuthTokens(self.tokens)
        tokens["access_token"] = body["access_token"]
        if body.get("refresh_token"):
            tokens["refresh_token"] = body["refresh_token"]
        tokens["expires_at"] = time.time() + int(body.get("expires_in", 3600))
        self.tokens = tokens
        if self._on_tokens:
            self._on_tokens(tokens)
        return tokens

    def _auth_headers(self) -> dict[str, str]:
        if self.tokens.expired:
            self.refresh()
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    def request(self, method: str, url: str, *, retry_auth: bool = True, **kw: Any) -> httpx.Response:
        headers = {**self._auth_headers(), **kw.pop("headers", {})}
        r = self.http.request(method, url, headers=headers, **kw)
        if r.status_code == 401 and retry_auth:
            self.refresh()
            return self.request(method, url, retry_auth=False, headers=kw.pop("headers", {}), **kw)
        if r.status_code == 429 or r.status_code >= 500:
            # One polite retry on throttling / transient server errors.
            wait = float(r.headers.get("Retry-After", "2"))
            time.sleep(min(wait, 10))
            r = self.http.request(method, url, headers=headers, **kw)
        return r

    # --- Calendar API --------------------------------------------------------------
    @abstractmethod
    def user_info(self) -> dict[str, str]:
        """{email, name}"""

    @abstractmethod
    def list_calendars(self) -> list[Calendar]: ...

    @abstractmethod
    def list_changes(
        self, calendar_id: str, cursor: str | None, window_start: str, window_end: str
    ) -> tuple[list[Event], str]:
        """Return (events, new_cursor). Raise CursorExpired for a stale cursor."""

    @abstractmethod
    def get_event(self, calendar_id: str, event_id: str) -> Event | None: ...

    @abstractmethod
    def create_event(self, calendar_id: str, event: Event) -> str: ...

    @abstractmethod
    def update_event(self, calendar_id: str, event_id: str, event: Event) -> None: ...

    @abstractmethod
    def delete_event(self, calendar_id: str, event_id: str) -> None: ...

    @abstractmethod
    def subscribe(self, calendar_id: str, callback_url: str, channel_id: str, secret: str) -> tuple[str, float]:
        """Create a push subscription. Return (provider_subscription_id, expires_at_epoch)."""

    @abstractmethod
    def renew(self, provider_sub_id: str, channel_id: str) -> float:
        """Extend a subscription; return new expires_at. Providers that cannot renew
        may raise ProviderError so the caller re-subscribes."""

    @abstractmethod
    def unsubscribe(self, provider_sub_id: str, channel_id: str) -> None: ...
