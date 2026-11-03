"""HTTP layer: OAuth login, JSON API for the single-page UI, webhooks and cron."""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .models import Account, Link, User, new_id
from .providers.base import OAuthTokens, ProviderError
from .services import AppContext, get_ctx
from .sync import SyncEngine
from .webhooks import SubscriptionManager, WebhookHandler

log = logging.getLogger("calsync")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
SESSION_COOKIE = "cs_session"
STATE_COOKIE = "cs_oauth"  # suffixed with the provider name so flows cannot clobber each other
STATE_MAX_AGE = 15 * 60
SESSION_MAX_AGE = 30 * 24 * 3600


# --- helpers ---------------------------------------------------------------------

def _serializer(ctx: AppContext) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(ctx.settings.session_secret, salt="calsync-session")


def current_user(request: Request) -> User | None:
    ctx = get_ctx()
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer(ctx).loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return ctx.repo.get_user(data.get("uid", ""))


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "not logged in")
    return user


def _set_session(resp: Response, ctx: AppContext, user: User) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        _serializer(ctx).dumps({"uid": user.id}),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=ctx.settings.base_url.startswith("https"),
    )


def _secure(ctx: AppContext) -> bool:
    return ctx.settings.base_url.startswith("https")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _account_view(a: Account) -> dict[str, Any]:
    return {"id": a.id, "provider": a.provider, "email": a.email, "name": a.name}


def _link_view(link: Link, repo) -> dict[str, Any]:
    d = link.to_dict()
    state = repo.get_state(link.id)
    d["mirrored"] = len(state.mapping)
    d["has_cursor"] = bool(state.cursor)
    return d


# --- pages ---------------------------------------------------------------------------

async def index(request: Request) -> Response:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True, "version": __version__, "storage": get_ctx().settings.storage_backend})


# --- auth ------------------------------------------------------------------------------

async def auth_start(request: Request) -> Response:
    ctx = get_ctx()
    provider_name = request.path_params["provider"]
    if provider_name not in ("google", "microsoft"):
        raise HTTPException(404)
    provider = ctx.new_provider(provider_name)
    if not provider.client_id:
        raise HTTPException(500, f"{provider_name} OAuth client is not configured on this server")
    nonce = secrets.token_urlsafe(16)
    # Browsers prefetch links on hover, so /start can run more than once per click.
    # Keep the last few nonces so whichever one the IdP echoes back still validates.
    nonces = _pending_nonces(request, ctx, provider_name)[-4:] + [nonce]
    resp = RedirectResponse(provider.auth_url(nonce), status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.set_cookie(
        f"{STATE_COOKIE}_{provider_name}", _serializer(ctx).dumps({"n": nonces}),
        max_age=STATE_MAX_AGE, httponly=True, samesite="lax", secure=_secure(ctx), path="/auth/",
    )
    return resp


def _pending_nonces(request: Request, ctx: AppContext, provider_name: str) -> list[str]:
    raw = request.cookies.get(f"{STATE_COOKIE}_{provider_name}", "")
    if not raw:
        return []
    try:
        data = _serializer(ctx).loads(raw, max_age=STATE_MAX_AGE)
    except BadSignature:
        return []
    n = data.get("n", [])
    return list(n) if isinstance(n, list) else [n]


def _login_error(message: str, provider_name: str, status: int = 400) -> Response:
    return PlainTextResponse(
        f"{message}\n\nStart again from the app and complete the sign-in in a single tab:\n"
        f"/auth/{provider_name}/start",
        status_code=status,
    )


async def auth_callback(request: Request) -> Response:
    ctx = get_ctx()
    provider_name = request.path_params["provider"]
    if provider_name not in ("google", "microsoft"):
        raise HTTPException(404)
    if "error" in request.query_params:
        return _login_error(
            f"Login failed: {request.query_params.get('error_description') or request.query_params['error']}", provider_name
        )
    raw = request.cookies.get(f"{STATE_COOKIE}_{provider_name}", "")
    if not raw:
        return _login_error("Login state cookie missing (sign-in was started in another browser or the cookie was "
                            "blocked).", provider_name)
    try:
        _serializer(ctx).loads(raw, max_age=STATE_MAX_AGE)
    except BadSignature:
        return _login_error("Login state expired (sign-in took longer than 15 minutes).", provider_name)
    if request.query_params.get("state") not in _pending_nonces(request, ctx, provider_name):
        return _login_error("Login state mismatch (this sign-in does not match one started in this browser).",
                            provider_name)
    code = request.query_params.get("code", "")
    provider = ctx.new_provider(provider_name)
    try:
        tokens = provider.exchange_code(code)
        info = provider.user_info()
    except ProviderError as exc:
        ctx.errors.record("auth", f"{provider_name} login failed: {exc}", exc=exc)
        raise HTTPException(502, f"{provider_name} login failed: {exc}")

    email = (info.get("email") or "").lower()
    if not email:
        raise HTTPException(502, "provider returned no email address")
    allowed = ctx.settings.allowed_emails
    user = current_user(request)
    if user is None:
        # First login: this provider is the "master" identity.
        if allowed and email not in allowed:
            raise HTTPException(403, f"{email} is not on this server's allow-list")
        user = ctx.repo.find_user_by_email(email) or User(id=new_id(), email=email, name=info.get("name", ""))
    account = ctx.repo.find_account(user, provider_name, email)
    if account is None:
        account = Account(id=new_id(), user_id=user.id, provider=provider_name, email=email, name=info.get("name", ""))
        user.account_ids.append(account.id)
    account.name = info.get("name", account.name)
    ctx.store_tokens(account, OAuthTokens(tokens))
    ctx.repo.save_user(user)
    resp = RedirectResponse("/", status_code=302)
    _set_session(resp, ctx, user)
    resp.delete_cookie(f"{STATE_COOKIE}_{provider_name}", path="/auth/")
    return resp


async def logout(request: Request) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --- API -------------------------------------------------------------------------------

async def api_me(request: Request) -> Response:
    ctx = get_ctx()
    user = current_user(request)
    if not user:
        return JSONResponse({"user": None, "providers": _provider_status(ctx)})
    accounts = ctx.repo.accounts_for_user(user)
    links = [_link_view(lk, ctx.repo) for lk in ctx.repo.links_for_user(user.id)]
    return JSONResponse(
        {
            "user": {"id": user.id, "email": user.email, "name": user.name},
            "accounts": [_account_view(a) for a in accounts],
            "links": links,
            "providers": _provider_status(ctx),
            "window": {"past_days": ctx.settings.window_past_days, "future_days": ctx.settings.window_future_days},
        }
    )


def _provider_status(ctx: AppContext) -> dict[str, bool]:
    return {"google": bool(ctx.settings.google_client_id), "microsoft": bool(ctx.settings.ms_client_id)}


async def api_calendars(request: Request) -> Response:
    ctx = get_ctx()
    user = require_user(request)
    account = ctx.repo.get_account(request.path_params["account_id"])
    if not account or account.user_id != user.id:
        raise HTTPException(404)
    try:
        cals = ctx.provider_for(account).list_calendars()
    except ProviderError as exc:
        ctx.errors.record("calendars", f"Could not list calendars for {account.email}: {exc}", user_id=user.id, exc=exc)
        raise HTTPException(502, str(exc))
    return JSONResponse({"calendars": [c.__dict__ for c in cals]})


async def api_delete_account(request: Request) -> Response:
    ctx = get_ctx()
    user = require_user(request)
    account = ctx.repo.get_account(request.path_params["account_id"])
    if not account or account.user_id != user.id:
        raise HTTPException(404)
    subs = SubscriptionManager(ctx)
    for link in ctx.repo.links_for_user(user.id):
        if account.id in (link.source_account_id, link.target_account_id):
            ctx.repo.delete_link(link.id)
            subs.remove_if_unused(link.source_account_id, link.source_calendar_id)
    ctx.repo.delete_account(account.id)
    user.account_ids = [a for a in user.account_ids if a != account.id]
    ctx.repo.save_user(user)
    return JSONResponse({"ok": True})


async def api_create_link(request: Request) -> Response:
    ctx = get_ctx()
    user = require_user(request)
    body = await _json_body(request)
    required = ("source_account_id", "source_calendar_id", "target_account_id", "target_calendar_id")
    if any(not body.get(k) for k in required):
        raise HTTPException(400, f"required: {', '.join(required)}")
    src = ctx.repo.get_account(body["source_account_id"])
    tgt = ctx.repo.get_account(body["target_account_id"])
    if not src or not tgt or src.user_id != user.id or tgt.user_id != user.id:
        raise HTTPException(404, "account not found")
    if src.id == tgt.id and body["source_calendar_id"] == body["target_calendar_id"]:
        raise HTTPException(400, "source and target are the same calendar")
    mode = body.get("mode", "full")
    if mode not in ("full", "busy"):
        raise HTTPException(400, "mode must be 'full' or 'busy'")
    label = (body.get("label") or "").strip()
    pairs = [(src, body["source_calendar_id"], tgt, body["target_calendar_id"])]
    if body.get("two_way"):
        pairs.append((tgt, body["target_calendar_id"], src, body["source_calendar_id"]))

    created: list[Link] = []
    for s_acct, s_cal, t_acct, t_cal in pairs:
        dup = [
            lk for lk in ctx.repo.links_for_user(user.id)
            if (lk.source_account_id, lk.source_calendar_id, lk.target_account_id, lk.target_calendar_id)
            == (s_acct.id, s_cal, t_acct.id, t_cal)
        ]
        if dup:
            continue
        link = Link(
            id=new_id(), user_id=user.id, source_account_id=s_acct.id, source_calendar_id=s_cal,
            target_account_id=t_acct.id, target_calendar_id=t_cal, mode=mode,
            label=label or f"{s_acct.email} -> {t_acct.email}",
        )
        ctx.repo.save_link(link)
        created.append(link)

    def after_response() -> None:
        # Webhook subscriptions are created here, after the response has gone
        # out, so this instance is free to answer the provider's validation
        # ping (Graph allows 10 seconds). Failures are logged; the cron tick retries.
        subs = SubscriptionManager(ctx)
        for lk in created:
            src_acct = ctx.repo.get_account(lk.source_account_id)
            try:
                subs.ensure(src_acct, lk.source_calendar_id)
            except Exception as exc:
                ctx.errors.record("subscription", f"Webhook setup failed for {src_acct.email}: {exc}",
                                  user_id=user.id, link_id=lk.id, exc=exc)
        engine = SyncEngine(ctx)
        for lk in created:
            engine.sync_link(lk, full=True)

    return JSONResponse(
        {"links": [_link_view(lk, ctx.repo) for lk in created]},
        status_code=201,
        background=BackgroundTask(after_response) if created else None,
    )


def _owned_link(request: Request) -> tuple[AppContext, User, Link]:
    ctx = get_ctx()
    user = require_user(request)
    link = ctx.repo.get_link(request.path_params["link_id"])
    if not link or link.user_id != user.id:
        raise HTTPException(404)
    return ctx, user, link


async def api_sync_link(request: Request) -> Response:
    ctx, _, link = _owned_link(request)
    full = request.query_params.get("full") in ("1", "true")
    res = SyncEngine(ctx).sync_link(link, full=full)
    return JSONResponse({"result": res.to_dict(), "link": _link_view(ctx.repo.get_link(link.id), ctx.repo)})


async def api_reconcile_link(request: Request) -> Response:
    ctx, _, link = _owned_link(request)
    res = SyncEngine(ctx).reconcile(link)
    return JSONResponse({"result": res.to_dict(), "link": _link_view(ctx.repo.get_link(link.id), ctx.repo)})


async def api_update_link(request: Request) -> Response:
    ctx, _, link = _owned_link(request)
    body = await _json_body(request)
    if "enabled" in body:
        link.enabled = bool(body["enabled"])
    if body.get("mode") in ("full", "busy") and body["mode"] != link.mode:
        link.mode = body["mode"]
        # Force every mirror to be rewritten on the next (full) sync.
        state = ctx.repo.get_state(link.id)
        state.cursor, state.fps = None, {}
        ctx.repo.save_state(state)
    if "label" in body:
        link.label = str(body["label"]).strip()
    ctx.repo.save_link(link)
    return JSONResponse({"link": _link_view(link, ctx.repo)})


async def api_delete_link(request: Request) -> Response:
    ctx, _, link = _owned_link(request)
    remove_mirrors = request.query_params.get("remove_mirrors") in ("1", "true")
    removed = 0
    if remove_mirrors:
        tgt = ctx.repo.get_account(link.target_account_id)
        state = ctx.repo.get_state(link.id)
        if tgt:
            provider = ctx.provider_for(tgt)
            for tgt_id in state.mapping.values():
                try:
                    provider.delete_event(link.target_calendar_id, tgt_id)
                    removed += 1
                except Exception as exc:
                    ctx.errors.record("cleanup", f"Could not remove mirror {tgt_id}: {exc}", user_id=link.user_id,
                                      link_id=link.id, exc=exc)
    ctx.repo.delete_link(link.id)
    SubscriptionManager(ctx).remove_if_unused(link.source_account_id, link.source_calendar_id)
    return JSONResponse({"ok": True, "removed_mirrors": removed})


async def api_errors(request: Request) -> Response:
    ctx = get_ctx()
    user = require_user(request)
    limit = min(int(request.query_params.get("limit", "100")), 500)
    return JSONResponse({"errors": ctx.errors.list(user_id=user.id, limit=limit)})


async def api_clear_errors(request: Request) -> Response:
    ctx = get_ctx()
    user = require_user(request)
    return JSONResponse({"cleared": ctx.errors.clear(user_id=user.id)})


# --- webhooks ----------------------------------------------------------------------------

async def webhook_google(request: Request) -> Response:
    ctx = get_ctx()
    handler = WebhookHandler(ctx)
    headers = dict(request.headers)
    # Google only needs a 2xx quickly; do the work after responding.
    return Response(status_code=204, background=BackgroundTask(handler.google, headers))


async def webhook_microsoft(request: Request) -> Response:
    ctx = get_ctx()
    token = request.query_params.get("validationToken")
    if token is not None:
        return PlainTextResponse(token)  # Graph subscription validation handshake
    body = await _json_body(request)
    handler = WebhookHandler(ctx)
    return Response(status_code=202, background=BackgroundTask(handler.microsoft, body))


# --- cron ----------------------------------------------------------------------------------

async def cron_tick(request: Request) -> Response:
    ctx = get_ctx()
    supplied = request.headers.get("x-cron-secret") or request.headers.get("authorization", "").removeprefix("Bearer ")
    if not secrets.compare_digest(supplied, ctx.settings.cron_secret):
        raise HTTPException(403)
    t0 = time.time()
    results = SyncEngine(ctx).sync_all()
    subs = SubscriptionManager(ctx).renew_all()
    pruned = ctx.errors.prune()
    return JSONResponse(
        {
            "links": [r.to_dict() for r in results],
            "subscriptions": subs,
            "errors_pruned": pruned,
            "seconds": round(time.time() - t0, 2),
        }
    )


# --- app -----------------------------------------------------------------------------------

class CanonicalHostMiddleware(BaseHTTPMiddleware):
    """Redirect browser traffic to BASE_URL's host.

    Cloud Run serves every service on more than one hostname. Cookies are per
    host, so a login started on one hostname and finished on another looks like
    a missing cookie. Webhooks and cron are exempt (they are host-agnostic and
    must never be redirected).
    """

    EXEMPT_PREFIXES = ("/webhooks/", "/cron/", "/api/health")

    async def dispatch(self, request: Request, call_next):
        base = get_ctx().settings.base_url
        host = request.headers.get("host", "")
        canonical = base.split("://", 1)[-1].rstrip("/")
        if (
            canonical
            and host
            and host != canonical
            and not canonical.startswith(("localhost", "127.0.0.1"))
            and request.method == "GET"
            and not request.url.path.startswith(self.EXEMPT_PREFIXES)
        ):
            target = f"{base.rstrip('/')}{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(target, status_code=308)
        return await call_next(request)


async def http_exception(request: Request, exc: HTTPException) -> Response:
    if request.url.path.startswith("/api/") or request.url.path.startswith("/cron"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code)


routes = [
    Route("/", index),
    Route("/api/health", healthz),
    Route("/auth/{provider}/start", auth_start),
    Route("/auth/{provider}/callback", auth_callback),
    Route("/auth/logout", logout, methods=["POST"]),
    Route("/api/me", api_me),
    Route("/api/accounts/{account_id}/calendars", api_calendars),
    Route("/api/accounts/{account_id}", api_delete_account, methods=["DELETE"]),
    Route("/api/links", api_create_link, methods=["POST"]),
    Route("/api/links/{link_id}/sync", api_sync_link, methods=["POST"]),
    Route("/api/links/{link_id}/reconcile", api_reconcile_link, methods=["POST"]),
    Route("/api/links/{link_id}", api_update_link, methods=["PATCH"]),
    Route("/api/links/{link_id}", api_delete_link, methods=["DELETE"]),
    Route("/api/errors", api_errors),
    Route("/api/errors", api_clear_errors, methods=["DELETE"]),
    Route("/webhooks/google", webhook_google, methods=["POST"]),
    Route("/webhooks/microsoft", webhook_microsoft, methods=["GET", "POST"]),
    Route("/cron/tick", cron_tick, methods=["POST", "GET"]),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(
    routes=routes,
    exception_handlers={HTTPException: http_exception},
    middleware=[Middleware(CanonicalHostMiddleware)],
)
