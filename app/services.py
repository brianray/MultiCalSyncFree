"""Application context: wires settings, storage, crypto, error log and providers."""

from __future__ import annotations

import json
import logging
from typing import Callable

from .config import Settings, get_settings
from .crypto import TokenCipher
from .errors import ErrorLog
from .models import Account, Repo
from .providers import PROVIDERS, CalendarProvider
from .providers.base import OAuthTokens
from .storage import Store, build_store

log = logging.getLogger(__name__)


class AppContext:
    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        provider_factory: Callable[[Account], CalendarProvider] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or build_store(self.settings)
        self.repo = Repo(self.store)
        self.errors = ErrorLog(self.store, self.settings.error_log_path)
        self.cipher = TokenCipher(self.settings.token_encryption_key, self.settings.session_secret)
        self._provider_factory = provider_factory

    # --- providers -------------------------------------------------------------
    def new_provider(self, provider_name: str, tokens: OAuthTokens | None = None, on_tokens=None) -> CalendarProvider:
        s = self.settings
        if provider_name == "google":
            return PROVIDERS["google"](
                s.google_client_id, s.google_client_secret, s.google_redirect_uri, tokens=tokens, on_tokens=on_tokens
            )
        if provider_name == "microsoft":
            return PROVIDERS["microsoft"](
                s.ms_client_id, s.ms_client_secret, s.ms_redirect_uri, tokens=tokens, on_tokens=on_tokens,
                tenant=s.ms_tenant,
            )
        raise ValueError(f"unknown provider {provider_name}")

    def provider_for(self, account: Account) -> CalendarProvider:
        """Provider bound to an account's stored tokens; refreshed tokens are persisted."""
        if self._provider_factory:
            return self._provider_factory(account)
        tokens = OAuthTokens(json.loads(self.cipher.decrypt(account.tokens_enc))) if account.tokens_enc else OAuthTokens()

        def persist(t: OAuthTokens) -> None:
            account.tokens_enc = self.cipher.encrypt(json.dumps(dict(t)))
            self.repo.save_account(account)

        return self.new_provider(account.provider, tokens, on_tokens=persist)

    def store_tokens(self, account: Account, tokens: OAuthTokens) -> None:
        account.tokens_enc = self.cipher.encrypt(json.dumps(dict(tokens)))
        self.repo.save_account(account)


_ctx: AppContext | None = None


def get_ctx() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = AppContext()
    return _ctx


def set_ctx(ctx: AppContext | None) -> None:
    """Testing hook."""
    global _ctx
    _ctx = ctx
