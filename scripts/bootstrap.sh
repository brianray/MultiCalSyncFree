#!/usr/bin/env bash
# One-time GCP setup for MultiCalSyncFree. Idempotent: safe to re-run.
#
#   PROJECT_ID=my-project GITHUB_REPO=me/MultiCalSyncFree ./scripts/bootstrap.sh
#
# What it does:
#   1. enables the APIs (Cloud Run, Cloud Build, Artifact Registry, Secret Manager,
#      Cloud Scheduler, Cloud Storage, IAM Credentials)
#   2. creates the GCS bucket that holds all state
#   3. creates a runtime service account (Cloud Run identity) with access to the
#      bucket and the secrets
#   4. creates a deploy service account + Workload Identity Federation pool so
#      GitHub Actions can deploy with no stored keys
#   5. creates the secrets (random values for session/token/cron; placeholders
#      for the OAuth client secrets that you fill in afterwards)
#   6. deploys the service once so you get a URL for the OAuth redirect URIs
#   7. creates a Cloud Scheduler job that hits /cron/tick every 15 minutes
#   8. prints the GitHub variables/secrets to configure for deploy.yml
#
# Requirements: gcloud (authenticated, billing enabled on the project), openssl.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${GITHUB_REPO:?set GITHUB_REPO (owner/name)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-calsync}"
BUCKET="${BUCKET:-${PROJECT_ID}-calsync}"
RUNTIME_SA="${SERVICE}-runtime"
DEPLOY_SA="${SERVICE}-deploy"
POOL="github"
PROVIDER="github"
CRON_SCHEDULE="${CRON_SCHEDULE:-*/15 * * * *}"

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

say "Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com \
  iamcredentials.googleapis.com iam.googleapis.com --quiet

say "Artifact Registry repo"
gcloud artifacts repositories describe calsync --location "$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create calsync --repository-format docker --location "$REGION" --quiet

say "Bucket gs://$BUCKET (uniform access, versioning on so a bad write is recoverable)"
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access \
    --public-access-prevention
fi
gcloud storage buckets update "gs://$BUCKET" --versioning --quiet

say "Runtime service account"
gcloud iam service-accounts describe "$RUNTIME_SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$RUNTIME_SA" --display-name "MultiCalSyncFree runtime"
RUNTIME_SA_EMAIL="$RUNTIME_SA@$PROJECT_ID.iam.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$RUNTIME_SA_EMAIL" --role roles/storage.objectAdmin --quiet >/dev/null

say "Secrets"
make_secret() { # name value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    echo "  $1 exists (kept)"
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy automatic --quiet
    echo "  $1 created"
  fi
  gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$RUNTIME_SA_EMAIL" \
    --role roles/secretmanager.secretAccessor --quiet >/dev/null
}
make_secret calsync-session-secret "$(openssl rand -base64 48)"
make_secret calsync-cron-secret "$(openssl rand -hex 32)"
make_secret calsync-token-key "$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
make_secret calsync-google-client-secret "REPLACE_ME"
make_secret calsync-ms-client-secret "REPLACE_ME"

say "Deploy service account + Workload Identity Federation for GitHub Actions"
gcloud iam service-accounts describe "$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$DEPLOY_SA" --display-name "MultiCalSyncFree deployer"
DEPLOY_SA_EMAIL="$DEPLOY_SA@$PROJECT_ID.iam.gserviceaccount.com"
for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/storage.admin roles/iam.serviceAccountUser roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$DEPLOY_SA_EMAIL" \
    --role "$role" --quiet >/dev/null
done
# Cloud Build runs as the compute SA by default; it needs to push the image.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role roles/artifactregistry.writer --quiet >/dev/null

gcloud iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global --workload-identity-pool "$POOL" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --location global --workload-identity-pool "$POOL" --display-name "GitHub" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition "assertion.repository == '$GITHUB_REPO'"
WIF_PROVIDER="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA_EMAIL" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$GITHUB_REPO" \
  --quiet >/dev/null

say "First deploy (so the service URL exists for the OAuth redirect URIs)"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --platform managed --allow-unauthenticated \
  --service-account "$RUNTIME_SA_EMAIL" --no-cpu-throttling --min-instances 0 --max-instances 3 \
  --set-env-vars "STORAGE_BACKEND=gcs,GCS_BUCKET=$BUCKET,MS_TENANT=common" \
  --set-secrets "SESSION_SECRET=calsync-session-secret:latest,TOKEN_ENCRYPTION_KEY=calsync-token-key:latest,CRON_SECRET=calsync-cron-secret:latest,GOOGLE_CLIENT_SECRET=calsync-google-client-secret:latest,MS_CLIENT_SECRET=calsync-ms-client-secret:latest" \
  --quiet
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "BASE_URL=$URL" --quiet >/dev/null

say "Cloud Scheduler: /cron/tick every 15 minutes (safety net + webhook renewal)"
CRON_SECRET=$(gcloud secrets versions access latest --secret calsync-cron-secret)
if gcloud scheduler jobs describe "$SERVICE-tick" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SERVICE-tick" --location "$REGION" --schedule "$CRON_SCHEDULE" \
    --uri "$URL/cron/tick" --http-method POST --headers "X-Cron-Secret=$CRON_SECRET" --quiet >/dev/null
else
  gcloud scheduler jobs create http "$SERVICE-tick" --location "$REGION" --schedule "$CRON_SCHEDULE" \
    --uri "$URL/cron/tick" --http-method POST --headers "X-Cron-Secret=$CRON_SECRET" --attempt-deadline 540s --quiet >/dev/null
fi

cat <<EOF

================================================================================
MultiCalSyncFree is deployed at: $URL

NEXT STEPS

1. Create OAuth clients (see README "OAuth apps") with these redirect URIs:
     Google:    $URL/auth/google/callback
     Microsoft: $URL/auth/microsoft/callback

2. Store the client secrets:
     printf '%s' 'GOOGLE_SECRET' | gcloud secrets versions add calsync-google-client-secret --data-file=-
     printf '%s' 'MS_SECRET'     | gcloud secrets versions add calsync-ms-client-secret --data-file=-

3. Configure the GitHub repository ($GITHUB_REPO) for deploy.yml:

   Repository VARIABLES (Settings > Secrets and variables > Actions > Variables)
     GCP_PROJECT_ID     = $PROJECT_ID
     GCP_REGION         = $REGION
     CLOUD_RUN_SERVICE  = $SERVICE
     GCP_RUNTIME_SA     = $RUNTIME_SA_EMAIL
     GCS_BUCKET         = $BUCKET
     BASE_URL           = $URL
     GOOGLE_CLIENT_ID   = <your google client id>
     MS_CLIENT_ID       = <your microsoft application (client) id>
     MS_TENANT          = common        (or your tenant id)
     ALLOWED_EMAILS     = you@example.com   (recommended for a personal instance)

   Repository SECRETS
     GCP_WIF_PROVIDER   = $WIF_PROVIDER
     GCP_DEPLOY_SA      = $DEPLOY_SA_EMAIL

4. Push to main. GitHub Actions runs the tests and deploys.
================================================================================
EOF
