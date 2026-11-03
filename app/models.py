"""Persisted records and their store accessors.

Layout in the store (keys, each a JSON object):

    users/<user_id>                  User: identity + account ids
    accounts/<account_id>            Account: provider, email, encrypted tokens
    links/<link_id>                  Link: source calendar -> target calendar
    linkstate/<link_id>              Cursor (sync token / delta link) + event id mapping
    subs/<subscription_id>           Webhook subscription (Google channel / Graph subscription)
    errors/<ts>-<id>                 ErrorRecord (see errors.py)
    locks/<name>                     Advisory locks (GCS backend only)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .storage import Store


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class User:
    id: str
    email: str
    name: str = ""
    account_ids: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "User":
        return cls(**d)


@dataclass
class Account:
    id: str
    user_id: str
    provider: str  # "google" | "microsoft"
    email: str
    name: str = ""
    tokens_enc: str = ""  # encrypted JSON: {access_token, refresh_token, expires_at}
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Account":
        return cls(**d)


@dataclass
class Link:
    """A one-way mirror. Two links (A->B and B->A) make a two-way sync."""

    id: str
    user_id: str
    source_account_id: str
    source_calendar_id: str
    target_account_id: str
    target_calendar_id: str
    mode: str = "full"  # "full" copies details; "busy" copies only time as "Busy"
    enabled: bool = True
    label: str = ""
    created: float = field(default_factory=time.time)
    last_sync: float | None = None
    last_status: str = "never"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Link":
        return cls(**d)


@dataclass
class LinkState:
    link_id: str
    cursor: str | None = None  # Google syncToken or Graph deltaLink
    mapping: dict[str, str] = field(default_factory=dict)  # source event id -> target event id
    fps: dict[str, str] = field(default_factory=dict)  # source event id -> fingerprint of last mirror written
    updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LinkState":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Subscription:
    id: str  # our id; also Google channel id / Graph clientState
    account_id: str
    calendar_id: str
    provider: str
    provider_sub_id: str  # Graph subscription id, or Google resourceId
    expires_at: float
    secret: str
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Subscription":
        return cls(**d)


class Repo:
    """Typed accessors over the key/value store."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # users
    def get_user(self, user_id: str) -> User | None:
        d = self.store.get(f"users/{user_id}")
        return User.from_dict(d) if d else None

    def find_user_by_email(self, email: str) -> User | None:
        email = email.lower()
        for d in self.store.get_many("users/"):
            if d.get("email", "").lower() == email:
                return User.from_dict(d)
        return None

    def save_user(self, user: User) -> None:
        self.store.put(f"users/{user.id}", user.to_dict())

    # accounts
    def get_account(self, account_id: str) -> Account | None:
        d = self.store.get(f"accounts/{account_id}")
        return Account.from_dict(d) if d else None

    def save_account(self, acct: Account) -> None:
        self.store.put(f"accounts/{acct.id}", acct.to_dict())

    def delete_account(self, account_id: str) -> None:
        self.store.delete(f"accounts/{account_id}")

    def accounts_for_user(self, user: User) -> list[Account]:
        out = []
        for aid in user.account_ids:
            a = self.get_account(aid)
            if a:
                out.append(a)
        return out

    def find_account(self, user: User, provider: str, email: str) -> Account | None:
        for a in self.accounts_for_user(user):
            if a.provider == provider and a.email.lower() == email.lower():
                return a
        return None

    # links
    def get_link(self, link_id: str) -> Link | None:
        d = self.store.get(f"links/{link_id}")
        return Link.from_dict(d) if d else None

    def save_link(self, link: Link) -> None:
        self.store.put(f"links/{link.id}", link.to_dict())

    def delete_link(self, link_id: str) -> None:
        self.store.delete(f"links/{link_id}")
        self.store.delete(f"linkstate/{link_id}")

    def all_links(self) -> list[Link]:
        return [Link.from_dict(d) for d in self.store.get_many("links/")]

    def links_for_user(self, user_id: str) -> list[Link]:
        return [lk for lk in self.all_links() if lk.user_id == user_id]

    def links_for_source(self, account_id: str, calendar_id: str) -> list[Link]:
        return [
            lk
            for lk in self.all_links()
            if lk.source_account_id == account_id and lk.source_calendar_id == calendar_id and lk.enabled
        ]

    # link state
    def get_state(self, link_id: str) -> LinkState:
        d = self.store.get(f"linkstate/{link_id}")
        return LinkState.from_dict(d) if d else LinkState(link_id=link_id)

    def save_state(self, state: LinkState) -> None:
        state.updated = time.time()
        self.store.put(f"linkstate/{state.link_id}", state.to_dict())

    # subscriptions
    def get_subscription(self, sub_id: str) -> Subscription | None:
        d = self.store.get(f"subs/{sub_id}")
        return Subscription.from_dict(d) if d else None

    def save_subscription(self, sub: Subscription) -> None:
        self.store.put(f"subs/{sub.id}", sub.to_dict())

    def delete_subscription(self, sub_id: str) -> None:
        self.store.delete(f"subs/{sub_id}")

    def all_subscriptions(self) -> list[Subscription]:
        return [Subscription.from_dict(d) for d in self.store.get_many("subs/")]

    def subscription_for(self, account_id: str, calendar_id: str) -> Subscription | None:
        for s in self.all_subscriptions():
            if s.account_id == account_id and s.calendar_id == calendar_id:
                return s
        return None
