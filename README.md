# MultiCalSyncFree
<img width="2400" height="1260" alt="MultiCalSyncFree-linkedin" src="https://github.com/user-attachments/assets/dc65bd48-7d21-40b7-b098-d585166dcf0d" />


Two-way calendar mirroring between **Google Calendar** and **Microsoft 365 / Outlook**, running on Google Cloud with no servers to manage and no database to pay for.

<img width="760" height="820" alt="Screenshot 2026-11-02 at 10 00 46 PM" src="https://github.com/user-attachments/assets/1b98433a-40b2-4a32-9f06-154d3679eb58" />


Sign in with either Google or Microsoft (that account becomes your primary login), attach as many accounts of either kind as you like, pick a calendar on each side, and MultiCalSyncFree keeps them in step. Changes arrive within seconds via push notifications; a scheduled tick every 15 minutes catches anything a webhook missed. Every failure is written to disk and to Cloud Storage and shown in the app.

```
 Google Calendar  ──watch──▶ ┌──────────────┐ ◀──change notifications── Microsoft Graph
                             │  Cloud Run   │
   events.list/insert ◀───── │ (MultiCalSyncFree) │ ─────▶ calendarView/delta, events
                             └──────┬───────┘
                                    │ JSON objects: users, accounts (encrypted tokens),
                                    │ links, cursors, event-id maps, subscriptions, errors
                             ┌──────▼───────┐        ┌──────────────────┐
                             │ Cloud Storage│        │ Cloud Scheduler  │──▶ /cron/tick every 15 min
                             └──────────────┘        └──────────────────┘
```



## Features

- **Two-way or one-way.** A sync is a pair of one-way *links*; you choose the direction(s) per calendar pair.
- **Any number of accounts and pairs.** Google ↔ Microsoft, Google ↔ Google, Microsoft ↔ Microsoft, work ↔ personal.
- **Full details or busy-only.** Mirror titles, notes and locations, or just block the time as "Busy" (good for keeping client details out of a second employer's tenant).
- **Loop-safe.** Every mirrored event carries a marker; a mirror is never mirrored again, so two-way sync converges instead of ping-ponging.
- **Self-healing.** Event-id maps can be rebuilt from the markers on the target calendar ("Reconcile"), orphaned mirrors are removed, mirrors deleted by hand are recreated, and expired cursors fall back to a full window listing.
- **Push + poll.** Google `events.watch` channels and Graph change-notification subscriptions, renewed automatically; a 15-minute scheduled tick as the safety net.
- **Errors you can see.** Written to a local log file and to Cloud Storage; listed per user in the UI with stack traces and context.
- **Cheap.** Cloud Run scales to zero; state lives in a handful of tiny JSON objects in a GCS bucket. A personal instance costs cents per month.
- **Secure by default.** OAuth refresh tokens are encrypted at rest (Fernet; key in Secret Manager), the bucket is private, cron and webhooks carry secrets, and an optional email allow-list locks a personal instance down.

## Quick start (about 20 minutes)

### 1. Deploy to GCP

You need `gcloud` authenticated against a project with billing enabled, and a GitHub repo containing this code.

```bash
git clone https://github.com/<you>/MultiCalSyncFree && cd MultiCalSyncFree
PROJECT_ID=my-gcp-project GITHUB_REPO=<you>/MultiCalSyncFree ./scripts/bootstrap.sh
```

The script enables the APIs, creates the bucket, service accounts, secrets, Workload Identity Federation for GitHub Actions, deploys once, and sets up the Cloud Scheduler job. It finishes by printing your service URL, the OAuth redirect URIs, and the exact GitHub variables and secrets to configure. It is idempotent; re-run it any time.

### 2. Create the OAuth apps

**Google** ([console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials))

1. Enable the **Google Calendar API** in the project.
2. Configure the OAuth consent screen (External is fine; add yourself as a test user while in "Testing", or publish it).
3. Create an **OAuth client ID**, type *Web application*, with the authorized redirect URI `https://<service-url>/auth/google/callback`.
4. Scopes used: `openid email profile https://www.googleapis.com/auth/calendar`.

**Microsoft** ([entra.microsoft.com](https://entra.microsoft.com) > App registrations)

1. New registration. Supported account types: *Accounts in any organizational directory and personal Microsoft accounts* (or just your tenant, then set `MS_TENANT` to your tenant id).
2. Redirect URI (Web): `https://<service-url>/auth/microsoft/callback`.
3. Certificates & secrets > new client secret.
4. API permissions (delegated): `User.Read`, `Calendars.ReadWrite`, `offline_access`, `openid`, `email`, `profile`. Grant admin consent if your tenant requires it.

Store the two client secrets in Secret Manager (the bootstrap output shows the commands), and put the client ids in the GitHub variables.

### 3. Push to `main`

`.github/workflows/deploy.yml` runs the tests, builds the image with Cloud Build, and deploys to Cloud Run with the env vars and secrets wired in. Every subsequent push to `main` redeploys.

### 4. Use it

Open the service URL, sign in with Google or Microsoft, attach the other side, create a sync. The first full sync runs in the background; after that, changes flow on push notifications.

## How it works

### Links, mirrors and markers

A **link** is `source calendar → target calendar` plus a mode (`full` or `busy`). Creating a sync with "two-way" checked makes two links. For each source event the engine writes a **mirror** event on the target whose description ends with

```
[calsync:ref=<link id>:<source event id>]
```

(Google mirrors also get an `extendedProperties.private.calsync_ref`.) Anything carrying a marker is skipped as a source, which is what makes two-way sync terminate.

### Sync run

Each run (triggered by a webhook, the cron tick, or a button) takes a per-link lock, pulls changes since the stored cursor (Google `syncToken` / Graph `deltaLink`), and applies them: create, update when the mirror's fingerprint changed, delete when the source is cancelled or removed. Cursor and the `source id → target id` map are saved to GCS. The first run, a cursor expiry, or a mode change does a full listing over the window (default 30 days back, 365 ahead).

Recurring events are handled by expanding instances (`singleEvents=true` / `calendarView`), so each occurrence is its own mirror. That sidesteps translating recurrence rules between two systems that disagree about them.

### Webhooks

- Google: `events.watch` channel per source calendar (7-day max, re-created before expiry).
- Microsoft: Graph subscription on `/me/calendars/{id}/events` (≈3-day max, renewed daily). The validation handshake and `clientState` check are handled in `app/webhooks.py`.

Notifications are acknowledged immediately and processed in a background task; Cloud Run is deployed with `--no-cpu-throttling` so that work completes.

### Cron tick

`POST /cron/tick` (secret in `X-Cron-Secret`) syncs every enabled link, renews or re-creates subscriptions expiring within 36 hours, creates subscriptions for links missing one, drops unused ones, and prunes the error log.

### Storage layout (GCS, `gs://<bucket>/calsync/...`)

| Key | Contents |
|---|---|
| `users/<id>.json` | email, name, account ids |
| `accounts/<id>.json` | provider, email, **encrypted** OAuth tokens |
| `links/<id>.json` | source/target, mode, status |
| `linkstate/<id>.json` | cursor, event-id map, fingerprints |
| `subs/<id>.json` | webhook subscription + secret |
| `errors/<ts>-<id>.json` | error records (newest first when listed in reverse) |
| `locks/<name>.json` | advisory locks (create-if-absent) |

### Error handling

Per-event failures are logged and the run continues; the failing event is retried on the next full sync or reconcile. Whole-run failures mark the link `error`. Everything goes to `ERROR_LOG_PATH` on disk and `errors/` in the bucket, and the UI lists the current user's errors with stack traces.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `BASE_URL` | Public URL of the service (used for OAuth redirects and webhook URLs) | `http://localhost:8080` |
| `STORAGE_BACKEND` | `gcs` or `memory` | `memory` |
| `GCS_BUCKET`, `GCS_PREFIX` | State bucket and key prefix | –, `calsync` |
| `SESSION_SECRET` | Signs the login cookie | dev value |
| `TOKEN_ENCRYPTION_KEY` | Fernet key for tokens at rest | derived from session secret (dev only) |
| `CRON_SECRET` | Shared secret for `/cron/tick` | dev value |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth client | – |
| `MS_CLIENT_ID` / `MS_CLIENT_SECRET` / `MS_TENANT` | Microsoft app registration | –, –, `common` |
| `WINDOW_PAST_DAYS` / `WINDOW_FUTURE_DAYS` | Mirroring window | `30` / `365` |
| `ALLOWED_EMAILS` | Comma-separated allow-list for first login | empty (anyone) |
| `ERROR_LOG_PATH` | Local error log file | `/tmp/calsync-errors.log` |

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in OAuth client ids/secrets
python -m app.local_server      # http://localhost:8080
pytest                          # 59 tests, no network needed
```

Webhooks need a public URL; for local testing use a tunnel (e.g. `cloudflared tunnel --url http://localhost:8080`) and set `BASE_URL` to it, or rely on "Sync now" and the cron tick. To use the GCS backend locally: `STORAGE_BACKEND=gcs GCS_BUCKET=... GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)`.

## Project layout

```
app/
  main.py            Starlette app: auth, JSON API, webhooks, cron
  sync.py            the mirror engine
  webhooks.py        subscription lifecycle + notification handlers
  providers/         google.py, microsoft.py, base.py (Event model, OAuth, markers)
  storage.py         MemoryStore + GCSStore (REST, no SDK)
  models.py          records and typed store accessors
  errors.py          disk + bucket error log
  crypto.py          Fernet token encryption
  config.py          env-driven settings
  static/            the UI (vanilla HTML/CSS/JS, no build step)
tests/               pytest suite with a fake provider and a fake GCS
scripts/bootstrap.sh one-time GCP setup
.github/workflows/   test.yml (lint, tests, Docker smoke) and deploy.yml (Cloud Run via WIF)
```

## Operations notes

- **Multi-user.** Anyone who can complete OAuth can use an instance unless `ALLOWED_EMAILS` is set. For a personal deployment, set it. Users only ever see their own accounts, links and errors.
- **Rotating `TOKEN_ENCRYPTION_KEY`** invalidates stored refresh tokens; users reconnect their accounts. Rotating `SESSION_SECRET` just logs everyone out.
- **Deleting a sync** optionally removes the mirrors it created. Disconnecting an account removes its syncs (mirrors are left in place).
- **Limits.** Mirrors are plain events: attendees, conferencing links and reminders are intentionally not copied. Deletions that happen while a cursor is expired are reconciled by the next "Reconcile" (which lists the full window on both sides). Each Graph calendar subscription needs its own renewal; the cron tick handles it.
- **Cost.** Cloud Run (scale-to-zero, ~a few hundred ms per webhook), GCS (kilobytes), Scheduler (free tier), Secret Manager (cents). Cloud Build runs on each deploy.

## Contributing

Issues and PRs welcome. Run `ruff check app tests && pytest` before opening one. Ideas on the list: iCloud (CalDAV) provider, attendee/conference-link passthrough as an option, per-link filters (ignore all-day events, ignore "free" events), and a Terraform module as an alternative to the bootstrap script.

## License

MIT. See `LICENSE`.
