#!/usr/bin/env bash
# Deploys the SID Benchmark Service end-to-end to GKE Autopilot.
#
# Idempotent: re-running on an already-deployed environment will skip
# resource creation and roll out a new image revision.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project ID, e.g. PROJECT_ID=my-proj ./deploy.sh}"
REGION="${REGION:-us-central1}"
CLUSTER="${CLUSTER:-sid-benchmark-autopilot}"
REPO="${REPO:-sid-benchmark}"
IMAGE_NAME="${IMAGE_NAME:-sid-api}"
TAG="${TAG:-v1}"
BUCKET="${BUCKET:-sid-benchmark-results-${PROJECT_ID}}"
DATASET="${DATASET:-sid_benchmark}"
TABLE="${TABLE:-runs}"
MODEL="${MODEL:-gemini-3.1-pro-preview}"
GSA_NAME="${GSA_NAME:-sid-benchmark-sa}"
KSA_NAME="${KSA_NAME:-sid-benchmark-sa}"
NAMESPACE="${NAMESPACE:-default}"

GSA_EMAIL="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:${TAG}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Project ${PROJECT_ID} / region ${REGION}"
gcloud config set project "${PROJECT_ID}" >/dev/null

say "Enabling APIs"
gcloud services enable \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  container.googleapis.com \
  compute.googleapis.com \
  iamcredentials.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  bigquery.googleapis.com

PROJECT_NUM="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
CB_SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"
say "Granting Cloud Build SA (${CB_SA}) the roles it needs"
for role in roles/cloudbuild.builds.builder roles/storage.admin \
            roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${CB_SA}" --role="${role}" --condition=None >/dev/null
done

say "Artifact Registry repo: ${REPO}"
gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "${REPO}" \
       --repository-format=docker --location="${REGION}" \
       --description="SID benchmark images"

say "GCS bucket: gs://${BUCKET}"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" --uniform-bucket-level-access

say "BigQuery dataset: ${DATASET}"
bq --location="${REGION}" --project_id="${PROJECT_ID}" show "${DATASET}" >/dev/null 2>&1 \
  || bq --location="${REGION}" --project_id="${PROJECT_ID}" mk -d "${DATASET}"

say "BigQuery table: ${DATASET}.${TABLE}"
bq --project_id="${PROJECT_ID}" show "${DATASET}.${TABLE}" >/dev/null 2>&1 \
  || bq --project_id="${PROJECT_ID}" mk -t "${DATASET}.${TABLE}" \
       run_id:STRING,timestamp:TIMESTAMP,model:STRING,prompt:STRING,num_variants:INTEGER,mean_drift:FLOAT,max_drift:FLOAT,artifact_uri:STRING

say "Google Service Account: ${GSA_EMAIL}"
gcloud iam service-accounts describe "${GSA_EMAIL}" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "${GSA_NAME}" --display-name="SID Benchmark Workload"

for role in roles/aiplatform.user roles/storage.objectAdmin roles/bigquery.dataEditor roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${GSA_EMAIL}" --role="${role}" --condition=None >/dev/null
done

say "GKE Autopilot cluster: ${CLUSTER}"
gcloud container clusters describe "${CLUSTER}" --region="${REGION}" >/dev/null 2>&1 \
  || gcloud container clusters create-auto "${CLUSTER}" \
       --region="${REGION}" --release-channel=regular

say "Cluster credentials"
gcloud container clusters get-credentials "${CLUSTER}" --region="${REGION}"

say "Workload Identity binding (requires cluster to exist)"
gcloud iam service-accounts add-iam-policy-binding "${GSA_EMAIL}" \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA_NAME}]" >/dev/null

say "Build & push image via Cloud Build: ${IMAGE}"
gcloud builds submit --tag="${IMAGE}" --region="${REGION}" --timeout=1200s .

say "Render manifest"
TMPMANIFEST="$(mktemp)"
sed \
  -e "s|SID_GSA_EMAIL|${GSA_EMAIL}|g" \
  -e "s|SID_IMAGE|${IMAGE}|g" \
  -e "s|SID_PROJECT_ID|${PROJECT_ID}|g" \
  -e "s|SID_REGION|${REGION}|g" \
  -e "s|SID_MODEL|${MODEL}|g" \
  -e "s|SID_BUCKET|${BUCKET}|g" \
  -e "s|SID_DATASET|${DATASET}|g" \
  k8s/deployment.yaml > "${TMPMANIFEST}"

say "Apply"
kubectl apply -f "${TMPMANIFEST}"

say "Wait for rollout"
kubectl rollout status deployment/sid-benchmark --timeout=10m

say "Service endpoint"
echo "Waiting for external IP..."
for _ in $(seq 1 60); do
  IP="$(kubectl get svc sid-benchmark -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  [[ -n "${IP}" ]] && break
  sleep 5
done
if [[ -z "${IP:-}" ]]; then
  echo "External IP not assigned yet; check 'kubectl get svc sid-benchmark'." >&2
  exit 1
fi
echo "Endpoint: http://${IP}"
echo "Health:   curl http://${IP}/health"
echo "Run:      curl -X POST http://${IP}/run -H 'Content-Type: application/json' -d '{\"prompt\":\"What is entropy?\",\"num_variants\":5}'"
