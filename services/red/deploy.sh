#!/usr/bin/env bash
# Deploys the EvolveOps red team (RedLoop) to GCP: mock target (Cloud Run
# service), red-team harness (Cloud Run Job), and the public trigger
# service (Cloud Run service) that starts a Job execution over HTTPS.
#
# Run this from a normal terminal with real internet access and gcloud
# already authenticated (gcloud auth login) against the target project --
# NOT from inside a sandboxed/proxied shell.
#
# Usage:
#   cd services/red && PROJECT_ID=your-project REGION=asia-east1 ./deploy.sh
#
# Optional: set ANTHROPIC_API_KEY in your shell before running to give the
# job a real LLM attacker/judge instead of the offline mock backend.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID, e.g. PROJECT_ID=my-gcp-project}"
REGION="${REGION:-asia-east1}"
TARGET_SERVICE="${TARGET_SERVICE:-evolveops-red-mock-target}"
TRIGGER_SERVICE="${TRIGGER_SERVICE:-evolveops-red-trigger}"
JOB_NAME="${JOB_NAME:-evolveops-red-job}"
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-evolveops-red-results}"
JOB_SA_NAME="evolveops-red-job-sa"
TRIGGER_SA_NAME="evolveops-red-trigger-sa"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "== Project: $PROJECT_ID   Region: $REGION =="
gcloud config set project "$PROJECT_ID" >/dev/null

echo "== Enabling required APIs (idempotent, safe to re-run) =="
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  --project "$PROJECT_ID"

echo "== Results bucket =="
if ! gcloud storage buckets describe "gs://$BUCKET_NAME" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET_NAME" --location="$REGION" --project="$PROJECT_ID"
else
  echo "  gs://$BUCKET_NAME already exists"
fi

echo "== Service accounts =="
for sa in "$JOB_SA_NAME" "$TRIGGER_SA_NAME"; do
  if ! gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$sa" --project="$PROJECT_ID" --display-name="$sa"
  else
    echo "  $sa already exists"
  fi
done
JOB_SA="${JOB_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
TRIGGER_SA="${TRIGGER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "== Secrets =="
create_secret_if_missing() {
  local name="$1" value="$2"
  if ! gcloud secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets create "$name" --project="$PROJECT_ID" --data-file=-
  else
    echo "  secret $name already exists (not overwriting -- delete it first to rotate)"
  fi
}
TARGET_TOKEN_VALUE="${TARGET_TOKEN:-$(openssl rand -hex 24)}"
TRIGGER_KEY_VALUE="${TRIGGER_KEY:-$(openssl rand -hex 24)}"
create_secret_if_missing "evolveops-red-mock-target-token" "$TARGET_TOKEN_VALUE"
create_secret_if_missing "evolveops-red-trigger-key" "$TRIGGER_KEY_VALUE"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  create_secret_if_missing "evolveops-red-anthropic-key" "$ANTHROPIC_API_KEY"
  HAVE_ANTHROPIC_SECRET=1
else
  echo "  ANTHROPIC_API_KEY not set in your shell -- job will run in offline mock mode"
  HAVE_ANTHROPIC_SECRET=0
fi

echo "== Granting IAM =="
gcloud secrets add-iam-policy-binding evolveops-red-mock-target-token --project="$PROJECT_ID" \
  --member="serviceAccount:${JOB_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
gcloud secrets add-iam-policy-binding evolveops-red-mock-target-token --project="$PROJECT_ID" \
  --member="serviceAccount:$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" >/dev/null 2>&1 || true
if [ "$HAVE_ANTHROPIC_SECRET" = "1" ]; then
  gcloud secrets add-iam-policy-binding evolveops-red-anthropic-key --project="$PROJECT_ID" \
    --member="serviceAccount:${JOB_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
fi
gcloud secrets add-iam-policy-binding evolveops-red-trigger-key --project="$PROJECT_ID" \
  --member="serviceAccount:${TRIGGER_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
  --member="serviceAccount:${JOB_SA}" --role="roles/storage.objectAdmin" >/dev/null
# NOTE: the trigger SA's permission to run the job is granted further down,
# scoped to just this one Job resource (roles/run.invoker on the Job, not
# roles/run.developer on the whole project) -- the job has to exist first.

echo "== Vendoring shared redloop package into each build context =="
for d in deploy/mock_target deploy/job; do
  rm -rf "$d/vendor"
  mkdir -p "$d/vendor"
  cp -r redloop "$d/vendor/redloop"
done
rm -rf deploy/job/seeds && cp -r seeds deploy/job/seeds
# deploy/service (single-service variant, see RUNNING_A_CAMPAIGN.md)
rm -rf deploy/service/redloop && cp -r redloop deploy/service/redloop
cp seeds/seeds.json deploy/service/seeds.json

echo "== Deploying mock target service (public, app-level auth) =="
gcloud run deploy "$TARGET_SERVICE" \
  --source=deploy/mock_target \
  --region="$REGION" \
  --allow-unauthenticated \
  --set-secrets=TARGET_TOKEN=evolveops-red-mock-target-token:latest \
  --project="$PROJECT_ID"

TARGET_URL="$(gcloud run services describe "$TARGET_SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"
echo "  target URL: $TARGET_URL"

echo "== Deploying red-team Cloud Run Job =="
JOB_ENV_VARS="TARGET_URL=${TARGET_URL}/chat,RESULTS_BUCKET=${BUCKET_NAME}"
JOB_SECRETS="TARGET_TOKEN=evolveops-red-mock-target-token:latest"
if [ "$HAVE_ANTHROPIC_SECRET" = "1" ]; then
  JOB_SECRETS="${JOB_SECRETS},ANTHROPIC_API_KEY=evolveops-red-anthropic-key:latest"
fi
if gcloud run jobs describe "$JOB_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud run jobs update "$JOB_NAME" \
    --source=deploy/job \
    --region="$REGION" \
    --service-account="$JOB_SA" \
    --set-env-vars="$JOB_ENV_VARS" \
    --set-secrets="$JOB_SECRETS" \
    --task-timeout=900 \
    --max-retries=0 \
    --project="$PROJECT_ID"
else
  gcloud run jobs deploy "$JOB_NAME" \
    --source=deploy/job \
    --region="$REGION" \
    --service-account="$JOB_SA" \
    --set-env-vars="$JOB_ENV_VARS" \
    --set-secrets="$JOB_SECRETS" \
    --task-timeout=900 \
    --max-retries=0 \
    --project="$PROJECT_ID"
fi

echo "== Scoping trigger SA's run permission to just this Job =="
gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
  --region="$REGION" --project="$PROJECT_ID" \
  --member="serviceAccount:${TRIGGER_SA}" --role="roles/run.invoker" >/dev/null

echo "== Deploying trigger service (public, shared-secret gated) =="
gcloud run deploy "$TRIGGER_SERVICE" \
  --source=deploy/trigger_service \
  --region="$REGION" \
  --allow-unauthenticated \
  --service-account="$TRIGGER_SA" \
  --set-env-vars="PROJECT_ID=${PROJECT_ID},REGION=${REGION},JOB_NAME=${JOB_NAME}" \
  --set-secrets=TRIGGER_KEY=evolveops-red-trigger-key:latest \
  --project="$PROJECT_ID"

TRIGGER_URL="$(gcloud run services describe "$TRIGGER_SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"

echo ""
echo "================================================================"
echo "Deployed."
echo "  Target:     $TARGET_URL"
echo "  Trigger:    $TRIGGER_URL"
echo "  Results:    gs://$BUCKET_NAME/runs/<run_id>/"
echo ""
echo "Trigger key (save this, needed to call /trigger):"
echo "  $TRIGGER_KEY_VALUE"
echo ""
echo "Test it:"
echo "  curl -s -X POST \"$TRIGGER_URL/trigger\" \\"
echo "    -H \"X-Trigger-Key: $TRIGGER_KEY_VALUE\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -d '{\"run_id\": \"smoke-test\", \"mode\": \"baseline\", \"max_turns\": 3}'"
echo ""
echo "  # then check status (execution name comes back from the call above):"
echo "  curl -s \"$TRIGGER_URL/status/<execution-id-from-above>\" -H \"X-Trigger-Key: $TRIGGER_KEY_VALUE\""
echo ""
echo "  # once SUCCEEDED, fetch results:"
echo "  gcloud storage cp gs://$BUCKET_NAME/runs/smoke-test/report.html ./report.html"
echo "================================================================"
