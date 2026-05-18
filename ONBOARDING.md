# SID (Semantic Intent Drift) Benchmark Service — Handoff

A FastAPI app wrapping a 6-agent LangGraph pipeline that measures how a Vertex Gemini model's outputs drift semantically across paraphrased prompts. Deployed live on GKE Autopilot.

## Live state

| | |
|---|---|
| GCP project | `<YOUR_GCP_PROJECT_ID>` |
| Region | `us-central1` |
| Endpoint | `http://<LOAD_BALANCER_IP>` |
| Image (running) | `us-central1-docker.pkg.dev/<YOUR_GCP_PROJECT_ID>/sid-benchmark/sid-api:v3` |
| Active model | `gemini-2.5-pro` (env var override) |

> The examples below assume `ENDPOINT=http://<LOAD_BALANCER_IP>` is exported in your shell.

### Quick health check

```bash
curl "$ENDPOINT/health"
curl "$ENDPOINT/readyz"
```

### Submit a run (async)

```bash
curl -X POST "$ENDPOINT/run" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"What is entropy?","num_variants":3}'
# → 202 Accepted, returns run_id + poll_url
```

Poll `GET /results/{run_id}` — 202 while `pending`/`running`, 200 when `succeeded`/`failed`. List recent runs at `GET /results`.

## Architecture

**Pipeline (LangGraph):** `anchor → paraphraser → regenerator → embedder → drift_scorer → reporter`

- `anchor`: baseline answer
- `paraphraser`: N semantic rewrites of the prompt
- `regenerator`: model answers each rewrite
- `embedder`: gemini-embedding-001 vectors
- `drift_scorer`: 1 − cosine similarity (anchor vs each variant)
- `reporter`: writes full run JSON to GCS + summary row to BigQuery

**Execution model:** `POST /run` returns 202 immediately and queues the pipeline in a FastAPI BackgroundTask. State machine (`pending → running → succeeded|failed`) is persisted to GCS so any replica can answer `/results/{id}`.

## Resources

- **Artifact Registry**: `sid-benchmark` (us-central1-docker.pkg.dev/.../sid-benchmark)
- **GCS bucket**: `gs://sid-benchmark-results-<YOUR_GCP_PROJECT_ID>/runs/<run_id>.json`
- **BigQuery**: `<YOUR_GCP_PROJECT_ID>.sid_benchmark.runs` (run_id, timestamp, model, prompt, num_variants, mean_drift, max_drift, artifact_uri)
- **Google SA**: `sid-benchmark-sa@<YOUR_GCP_PROJECT_ID>.iam.gserviceaccount.com` — has `aiplatform.user`, `storage.objectAdmin`, `bigquery.dataEditor`, `bigquery.jobUser`
- **GKE Autopilot**: cluster `sid-benchmark-autopilot` (regional, us-central1)
- **K8s namespace**: `default`
- **K8s SA**: `sid-benchmark-sa` (Workload-Identity-bound to the Google SA)
- **Workloads**: Deployment `sid-benchmark` (2 replicas, HPA 2–10), LoadBalancer Service, PodDisruptionBudget (`minAvailable: 1`), HorizontalPodAutoscaler (CPU 70% / mem 75%)

## File layout

- `main.py` — FastAPI app + LangGraph pipeline + GCS/BQ sinks
- `Dockerfile` — Python 3.12-slim, uvicorn 2 workers, non-root, healthcheck
- `requirements.txt` — pinned (fastapi, langgraph, vertexai SDK, tenacity, python-json-logger)
- `k8s/deployment.yaml` — templated manifest (5 docs: SA / Deployment / Service / PDB / HPA). Use `sed` to substitute `SID_*` tokens — see `deploy.sh`
- `deploy.sh` — idempotent end-to-end: APIs → AR repo → GCS → BQ → GSA → WI → cluster → Cloud Build → kubectl apply
- `.dockerignore`

## Common operations

```bash
# Swap to a different Vertex model (no rebuild — env var change)
kubectl set env deployment/sid-benchmark VERTEX_MODEL=gemini-2.5-flash
kubectl rollout status deployment/sid-benchmark

# Tail structured JSON logs
kubectl logs -l app=sid-benchmark -f --tail=50

# Force a rollout
kubectl rollout restart deployment/sid-benchmark

# Inspect a stored run artifact
gcloud storage cat "gs://sid-benchmark-results-${PROJECT_ID}/runs/<run_id>.json"

# Query recent runs
bq query --use_legacy_sql=false "SELECT * FROM \`${PROJECT_ID}.sid_benchmark.runs\` ORDER BY timestamp DESC LIMIT 20"

# Rebuild the image (uses Cloud Build, not local Docker)
gcloud builds submit --tag="us-central1-docker.pkg.dev/${PROJECT_ID}/sid-benchmark/sid-api:v4" --region=us-central1 .
kubectl set image "deployment/sid-benchmark" "api=us-central1-docker.pkg.dev/${PROJECT_ID}/sid-benchmark/sid-api:v4"
```

## Caveats / gotchas

- **Gemini 3.x is allowlist-gated** for this project — `gemini-3.1-pro-preview`, `gemini-3-pro-preview`, etc. all 404. Service currently runs on `gemini-2.5-pro`. Once allowlist access lands, swap via `kubectl set env deployment/sid-benchmark VERTEX_MODEL=gemini-3.1-pro-preview` (no rebuild).
- **`request_options=` is not supported** on `GenerativeModel.generate_content` at SDK pin `google-cloud-aiplatform==1.71.1`. v2 image had this and crashed; v3 dropped it. Don't add it back without bumping the SDK.
- **Cloud Build, not local Docker.** The local Docker daemon wasn't running during initial deploy, so `deploy.sh` uses `gcloud builds submit`. The default Cloud Build SA (`<project-number>-compute@developer.gserviceaccount.com`) needs `cloudbuild.builds.builder`, `storage.admin`, `artifactregistry.writer`, `logging.logWriter` — `deploy.sh` grants these. New project? Grant them before the first build.
- **ADC quota project**. If your active gcloud user differs from the ADC principal, and the ADC principal lacks `serviceusage.services.use` on the project, commands like `gcloud ai model-garden models list` fail. Doesn't affect deploys; use REST with `gcloud auth print-access-token` instead.
- **Topology spread is soft** (`whenUnsatisfiable: ScheduleAnyway`). With 2 replicas on Autopilot's bin-packing, both pods may currently sit in the same zone (`us-central1-b`). HPA scale-up will spread them.
- **Synchronous /run is gone.** The old GET /run returned the full result. Now `POST /run` is 202 + run_id, and you must poll `/results/{run_id}`. Pipelines run ~100–150s for 3 variants.

## Repro from scratch in a new project

```bash
PROJECT_ID=<your-project> ./deploy.sh
```

`deploy.sh` is idempotent — re-running on an already-deployed environment skips existing resources and rolls out a new image. Override any of `PROJECT_ID`, `REGION`, `CLUSTER`, `REPO`, `IMAGE_NAME`, `TAG`, `BUCKET`, `DATASET`, `MODEL` via env vars.
