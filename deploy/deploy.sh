#!/usr/bin/env bash
# Deploys Shadow to Google Cloud: the brain and the sandbox on Cloud Run, storage in
# Firestore, the Gemini key in Secret Manager, the web harness on Firebase Hosting.
#
# Before running:
#   1. Install the gcloud and firebase CLIs and sign in (gcloud auth login, firebase login).
#   2. Create a Google Cloud project with billing, and add Firebase to it in the Firebase console.
#   3. In the Firebase console: Authentication > Sign-in method > enable Anonymous.
#      Then Project settings > Your apps > add a Web app and copy its config.
#   4. Get a Gemini API key from Google AI Studio (use a billed key so judging is not rate limited).
#
# Usage:
#   PROJECT=my-project REGION=asia-south1 GEMINI_API_KEY=... bash deploy/deploy.sh
#
# This script has not been run by its author against a live project: read it before using it.
set -euo pipefail
: "${PROJECT:?set PROJECT}" "${GEMINI_API_KEY:?set GEMINI_API_KEY}"
REGION="${REGION:-asia-south1}"
REPO="$REGION-docker.pkg.dev/$PROJECT/shadow"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== Enabling services"
gcloud config set project "$PROJECT" >/dev/null
gcloud services enable run.googleapis.com firestore.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

echo "== Firestore, image repository, secrets"
gcloud firestore databases describe >/dev/null 2>&1 || \
  gcloud firestore databases create --location="$REGION" --type=firestore-native
gcloud artifacts repositories describe shadow --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create shadow --repository-format=docker --location="$REGION"
if gcloud secrets describe gemini-api-key >/dev/null 2>&1; then
  printf '%s' "$GEMINI_API_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
else
  printf '%s' "$GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
fi
# One fixed token lets the sandbox identify itself to the brain.
if ! gcloud secrets describe sandbox-token >/dev/null 2>&1; then
  python -c "import secrets; print(secrets.token_urlsafe(32), end='')" | gcloud secrets create sandbox-token --data-file=-
fi
SANDBOX_TOKEN="$(gcloud secrets versions access latest --secret=sandbox-token)"
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
for secret in gemini-api-key sandbox-token; do
  gcloud secrets add-iam-policy-binding "$secret" --role=roles/secretmanager.secretAccessor \
    --member="serviceAccount:$NUMBER-compute@developer.gserviceaccount.com" >/dev/null
done

echo "== Building images"
gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_REPO="$REPO"

echo "== Sandbox (deployed first so the brain can link to its screen)"
gcloud run deploy shadow-sandbox --image "$REPO/sandbox:latest" --region "$REGION" --allow-unauthenticated \
  --cpu 4 --memory 8Gi --min-instances 1 --max-instances 1 --concurrency 20 --timeout 3600 \
  --no-cpu-throttling --set-secrets SHADOW_DEVICE_TOKEN=sandbox-token:latest
SANDBOX_URL="$(gcloud run services describe shadow-sandbox --region "$REGION" --format='value(status.url)')"

echo "== Brain"
gcloud run deploy shadow-brain --image "$REPO/brain:latest" --region "$REGION" --allow-unauthenticated \
  --cpu 1 --memory 1Gi --min-instances 1 --max-instances 1 --concurrency 80 --timeout 120 \
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest,SHADOW_SANDBOX_TOKEN=sandbox-token:latest \
  --set-env-vars "SHADOW_SANDBOX_VIEW=$SANDBOX_URL/vnc_lite.html?autoconnect=1&view_only=1&resize=scale"
BRAIN_URL="$(gcloud run services describe shadow-brain --region "$REGION" --format='value(status.url)')"

echo "== Pointing the sandbox at the brain"
gcloud run services update shadow-sandbox --region "$REGION" --update-env-vars "SHADOW_BRAIN_URL=$BRAIN_URL"

echo "== Web harness"
if grep -q 'firebase: null' dashboard/public/config.js; then
  echo "Edit dashboard/public/config.js first:"
  echo "  brainUrl: \"$BRAIN_URL\""
  echo "  firebase: { ...the web app config from the Firebase console... }"
  echo "then run: (cd dashboard && firebase deploy --only hosting --project $PROJECT)"
else
  (cd dashboard && firebase deploy --only hosting --project "$PROJECT")
fi

echo
echo "Brain:   $BRAIN_URL/healthz"
echo "Sandbox: $SANDBOX_URL"
echo "Pair a PC with:  shadow pair <code from the web harness> --url $BRAIN_URL"
