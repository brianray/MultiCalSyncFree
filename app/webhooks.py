"""Push-notification subscriptions and the handlers that react to them.

* Google: ``events.watch`` channels (max 7 days; cannot be renewed, so we stop
  and re-create them before expiry). Notifications arrive as POSTs with
  ``X-Goog-*`` headers and an empty body.
* Microsoft Graph: change-notification subscriptions (max ~3 days; renewable).
  Graph first calls the URL with ``?validationToken=...`` and expects the token
  echoed back as text/plain; real notifications are JSON with ``value[]``.

Both carry a secret we generated so forged notifications are ignored.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from .models import Account, Subscription, new_id
from .providers.base import ProviderError
from .services import AppContext
from .sync import SyncEngine

log = logging.getLogger(__name__)

RENEW_BEFORE_SECONDS = 36 * 3600


class SubscriptionManager:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.repo = ctx.repo

    def ensure(self, account: Account, calendar_id: str) -> Subscription:
        """Make sure a fresh subscription exists for this calendar."""
        existing = self.repo.subscription_for(account.id, calendar_id)
        if existing and existing.expires_at - time.time() > RENEW_BEFORE_SECONDS:
            return existing
        provider = self.ctx.provider_for(account)
        if existing:
            try:
                existing.expires_at = provider.renew(existing.provider_sub_id, existing.id)
                self.repo.save_subscription(existing)
                return existing
            except ProviderError:
                self._drop(existing, provider)
        sub = Subscription(
            id=new_id(),
            account_id=account.id,
            calendar_id=calendar_id,
            provider=account.provider,
            provider_sub_id="",
            expires_at=0,
            secret=secrets.token_urlsafe(24),
        )
        sub.provider_sub_id, sub.expires_at = provider.subscribe(
            calendar_id, f"{self.ctx.settings.webhook_url}/{account.provider}", sub.id, sub.secret
        )
        self.repo.save_subscription(sub)
        return sub

    def _drop(self, sub: Subscription, provider=None) -> None:
        try:
            (provider or self.ctx.provider_for(self.repo.get_account(sub.account_id))).unsubscribe(
                sub.provider_sub_id, sub.id
            )
        except Exception as exc:  # best effort
            log.warning("unsubscribe failed for %s: %s", sub.id, exc)
        self.repo.delete_subscription(sub.id)

    def remove_if_unused(self, account_id: str, calendar_id: str) -> None:
        if self.repo.links_for_source(account_id, calendar_id):
            return
        sub = self.repo.subscription_for(account_id, calendar_id)
        if sub:
            self._drop(sub)

    def renew_all(self) -> dict[str, int]:
        """Called from the cron tick. Renews / re-creates everything expiring soon
        and creates subscriptions for links that somehow lack one."""
        stats = {"renewed": 0, "created": 0, "failed": 0}
        wanted: set[tuple[str, str]] = set()
        for link in self.repo.all_links():
            if link.enabled:
                wanted.add((link.source_account_id, link.source_calendar_id))
        for account_id, calendar_id in wanted:
            account = self.repo.get_account(account_id)
            if not account:
                continue
            before = self.repo.subscription_for(account_id, calendar_id)
            try:
                after = self.ensure(account, calendar_id)
                if before is None:
                    stats["created"] += 1
                elif after.id != before.id or after.expires_at != before.expires_at:
                    stats["renewed"] += 1
            except Exception as exc:
                stats["failed"] += 1
                self.ctx.errors.record(
                    "subscription", f"Could not keep webhook alive for {account.email}: {exc}",
                    user_id=account.user_id, exc=exc, context={"calendar_id": calendar_id},
                )
        # Drop subscriptions nobody uses any more.
        for sub in self.repo.all_subscriptions():
            if (sub.account_id, sub.calendar_id) not in wanted:
                self._drop(sub)
        return stats


class WebhookHandler:
    def __init__(self, ctx: AppContext, engine: SyncEngine | None = None) -> None:
        self.ctx = ctx
        self.repo = ctx.repo
        self.engine = engine or SyncEngine(ctx)

    def google(self, headers: dict[str, str]) -> dict[str, Any]:
        h = {k.lower(): v for k, v in headers.items()}
        channel_id = h.get("x-goog-channel-id", "")
        token = h.get("x-goog-channel-token", "")
        state = h.get("x-goog-resource-state", "")
        sub = self.repo.get_subscription(channel_id)
        if not sub or sub.secret != token:
            return {"accepted": False, "reason": "unknown channel"}
        if state == "sync":  # initial handshake ping
            return {"accepted": True, "synced": 0}
        results = self.engine.sync_for_source(sub.account_id, sub.calendar_id)
        return {"accepted": True, "synced": len(results), "results": [r.to_dict() for r in results]}

    def microsoft(self, body: dict[str, Any]) -> dict[str, Any]:
        synced = []
        seen: set[str] = set()
        for n in body.get("value", []) or []:
            client_state = n.get("clientState", "")
            channel_id, _, secret = client_state.partition(":")
            sub = self.repo.get_subscription(channel_id)
            if not sub or sub.secret != secret:
                continue
            key = f"{sub.account_id}:{sub.calendar_id}"
            if key in seen:
                continue
            seen.add(key)
            synced.extend(r.to_dict() for r in self.engine.sync_for_source(sub.account_id, sub.calendar_id))
        return {"accepted": True, "synced": len(synced), "results": synced}
